import os
import sys
import json
import unicodedata
import libsql
import requests
import traceback
from math import prod
from datetime import datetime, timedelta
from dotenv import load_dotenv

from night_window import FINLAND_TZ, get_now_utc, get_next_night_window, today_fi, parse_utc
from market_model import scoreline_probs, prob_spread_cover, prob_total_over
from win_model import (
    train_calibration_model,
    get_current_team_ratings,
    get_current_nhl_season,
    predict_win_probability,
    calculate_edge,
)
from rosters import team_players, norm_player
from check_results import check_and_update_results, get_results_summary

load_dotenv()

DRY_RUN = os.getenv("DRY_RUN") == "1"

MAX_LEGS = 20
MAX_ODDS = 250_000
TARGET_MIN_ODDS = 10_000
MIN_EDGE = 0.05

# Testo-valikko: korkein kerroin joka on silti "järkevä"
TESTO_MIN_PROB = 0.20       # mallin mukaan vähintään 20 % toteutumistodennäköisyys
TESTO_MIN_EV = 0.90         # todennäköisyys * kerroin vähintään 0.90
TESTO_USE_TOTALS = False    # yli/alle ei ole backtestattu -> pois testo-valikosta

# Pelaajaveto samalle lapulle samasta ottelusta. Aseta 1, jos sivusto ei salli
# useampaa valintaa samasta ottelusta.
MAX_PROPS_PER_GAME = 2

# Porrastus: kokeillaan järjestyksessä, ensimmäinen joka yltää tason
# minimikertoimeen valitaan. option: "base" (h2h) tai "testo" (esim. tasoitus).
STAGES = [
    {"name": "pelkkä h2h",          "option": "base",  "props": 0},
    {"name": "h2h + prop",          "option": "base",  "props": 1},
    {"name": "tasoitus + prop",     "option": "testo", "props": 1},
    {"name": "tasoitus + 2 propia", "option": "testo", "props": 2},
]
FALLBACK_STAGE = 1  # jos mikään porras ei yllä tavoitteeseen: h2h + yksi prop

COLD_STREAK_LOOKBACK_DAYS = 5
COLD_STREAK_MIN_SAMPLES = 3
COLD_STREAK_THRESHOLD = 0.30

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

SLIP_TIERS = {
    "chilli": {"emoji": "🧊", "min_odds": 50, "max_odds": 1_000, "min_legs": 2, "max_legs": 6},
    "perus":  {"emoji": "⚖️", "min_odds": 10_000, "max_odds": 100_000, "min_legs": 6, "max_legs": 15},
    "testo":  {"emoji": "🔥", "min_odds": 75_000, "max_odds": 250_000, "min_legs": 12, "max_legs": 20},
}


# ---------- tietokanta ja tila ----------

def get_connection():
    return libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)


def init_state_table():
    conn = get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    return conn


def get_state(conn, key):
    row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_state(conn, key, value):
    conn.execute("""
        INSERT INTO bot_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (key, value))
    conn.commit()


def already_sent_today(conn):
    return get_state(conn, "last_sent_date") == today_fi().isoformat()


def mark_sent_today(conn):
    set_state(conn, "last_sent_date", today_fi().isoformat())


def init_sent_picks_table():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_date TEXT NOT NULL,
            tier TEXT NOT NULL,
            game_id TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            commence_time TEXT NOT NULL,
            pick_description TEXT NOT NULL,
            price REAL NOT NULL,
            checked INTEGER DEFAULT 0,
            hit INTEGER DEFAULT NULL,
            selections_json TEXT
        )
    """)
    conn.commit()
    try:
        c = get_connection()
        c.execute("ALTER TABLE sent_picks ADD COLUMN selections_json TEXT")
        c.commit()
    except Exception:
        pass  # sarake on jo olemassa


def save_sent_picks(conn, tier_key, result, sent_date):
    if result is None:
        return
    for leg in result["legs"]:
        home, away = leg["match"].split(" vs ")
        selections = [
            {k: s.get(k) for k in ("type", "team", "point", "side", "player")}
            for s in leg["selections"]
        ]
        conn.execute("""
            INSERT INTO sent_picks
            (sent_date, tier, game_id, home_team, away_team, commence_time,
             pick_description, price, selections_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (sent_date, tier_key, leg["game_id"], home, away, leg["commence"],
              leg["pick"], leg["price"], json.dumps(selections, ensure_ascii=False)))
    conn.commit()


def get_cold_streak_warnings(conn):
    cutoff = (today_fi() - timedelta(days=COLD_STREAK_LOOKBACK_DAYS)).isoformat()
    cursor = conn.execute("""
        SELECT tier, SUM(hit), COUNT(*)
        FROM sent_picks
        WHERE checked = 1 AND sent_date >= ?
        GROUP BY tier
    """, (cutoff,))
    warnings = []
    for tier, hits, total in cursor.fetchall():
        hits = hits or 0
        if total >= COLD_STREAK_MIN_SAMPLES and hits / total < COLD_STREAK_THRESHOLD:
            warnings.append(
                f"{tier.upper()} on osunut vain {hits / total * 100:.0f}% "
                f"viimeisen {COLD_STREAK_LOOKBACK_DAYS} päivän aikana ({hits}/{total})"
            )
    return warnings


# ---------- data ----------

def load_games(conn):
    """Lukee tuoreimmat kertoimet markkinoittain (jokaiselle ottelulle ja
    markkinalle oma viimeisin snapshot) ja kokoaa ne otteluiksi."""
    cursor = conn.execute("""
        WITH latest AS (
            SELECT game_id, COALESCE(market_key, 'h2h') AS mk, MAX(fetched_at) AS max_fetched
            FROM odds_snapshots
            GROUP BY game_id, COALESCE(market_key, 'h2h')
        )
        SELECT o.game_id, o.home_team, o.away_team, o.commence_time,
               COALESCE(o.market_key, 'h2h') AS mk, o.outcome_name, o.point, AVG(o.price)
        FROM odds_snapshots o
        JOIN latest l
          ON o.game_id = l.game_id
         AND COALESCE(o.market_key, 'h2h') = l.mk
         AND o.fetched_at = l.max_fetched
        GROUP BY o.game_id, COALESCE(o.market_key, 'h2h'), o.outcome_name, o.point
    """)
    games = {}
    for game_id, home, away, commence, mk, outcome, point, price in cursor.fetchall():
        g = games.setdefault(game_id, {
            "game_id": game_id, "home": home, "away": away, "commence": commence,
            "h2h": {}, "spreads": [], "alt_spreads": [], "totals": [], "props": [],
        })
        if mk == "h2h":
            g["h2h"][outcome] = price
            g["commence"] = commence
        elif mk == "spreads":
            g["spreads"].append((outcome, point, price))
        elif mk == "alternate_spreads":
            g["alt_spreads"].append((outcome, point, price))
        elif mk == "totals":
            g["totals"].append((outcome, point, price))
        elif mk == "player_goal_scorer_anytime":
            g["props"].append((outcome, price))
    return games


def games_in_window(all_games, now_utc):
    window_start, window_end = get_next_night_window(now_utc)
    selected = []
    for g in all_games.values():
        commence = parse_utc(g["commence"])
        if window_start <= commence <= window_end and commence > now_utc:
            selected.append(g)
    return selected, window_start, window_end


def norm_name(name):
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    return n.replace(".", "").strip().lower()


def align_ratings_to_odds_names(ratings, odds_team_names):
    """Lisää ratings-sanakirjaan Odds API:n käyttämät nimet (esim. 'St Louis
    Blues' vs NHL:n 'St. Louis Blues'), jotta mallin haku ei jää nimeneroon."""
    by_norm = {norm_name(k): v for k, v in ratings.items()}
    aligned = dict(ratings)
    for name in odds_team_names:
        if name not in aligned and norm_name(name) in by_norm:
            aligned[name] = by_norm[norm_name(name)]
    return aligned


# ---------- valinnat ----------

def build_options(game, ratings, league_avg, calibration_model, season):
    """Palauttaa ottelun 'base'-valinnan (h2h) ja 'testo'-valinnan (korkein
    järkevä kerroin, esim. tasoitus)."""
    home, away = game["home"], game["away"]
    home_price = game["h2h"].get(home)
    away_price = game["h2h"].get(away)
    if home_price is None or away_price is None:
        return None

    if home not in ratings or away not in ratings:
        team, price = min([(home, home_price), (away, away_price)], key=lambda x: x[1])
        opt = {"type": "h2h", "team": team, "description": f"{team} voittaa",
               "price": price, "prob": None}
        return {"base": opt, "testo": opt}

    game_date = (parse_utc(game["commence"]) - timedelta(hours=8)).date()
    calibrated_prob, raw_prob, lam_home, lam_away = predict_win_probability(
        home, away, ratings, league_avg, calibration_model,
        game_date=game_date, season=season,
    )

    options = [
        {"type": "h2h", "team": home, "description": f"{home} voittaa",
         "price": home_price, "prob": calibrated_prob},
        {"type": "h2h", "team": away, "description": f"{away} voittaa",
         "price": away_price, "prob": 1 - calibrated_prob},
    ]

    probs = scoreline_probs(lam_home, lam_away)

    for team, point, price in game["spreads"] + game["alt_spreads"]:
        if team == home:
            is_home = True
        elif team == away:
            is_home = False
        else:
            continue
        options.append({
            "type": "spread", "team": team, "point": point,
            "description": f"{team} tasoitus {point:+.1f}",
            "price": price, "prob": prob_spread_cover(probs, is_home, point),
        })

    for side_name, point, price in game["totals"]:
        over_p = prob_total_over(probs, point)
        if side_name == "Over":
            options.append({"type": "total", "side": "over", "point": point,
                            "description": f"Yli {point:g} maalia",
                            "price": price, "prob": over_p})
        elif side_name == "Under":
            options.append({"type": "total", "side": "under", "point": point,
                            "description": f"Alle {point:g} maalia",
                            "price": price, "prob": 1 - over_p})

    # BASE: h2h, malli ohittaa kirjan suosikin vain jos edge >= MIN_EDGE
    home_edge = calculate_edge(calibrated_prob, home_price)
    away_edge = calculate_edge(1 - calibrated_prob, away_price)
    if max(home_edge, away_edge) >= MIN_EDGE:
        base = options[0] if home_edge >= away_edge else options[1]
    else:
        base = options[0] if home_price <= away_price else options[1]

    # TESTO: korkein kerroin, jonka mallin todennäköisyys ja odotusarvo ovat järkevät
    candidates = [
        o for o in options
        if o["prob"] >= TESTO_MIN_PROB
        and o["prob"] * o["price"] >= TESTO_MIN_EV
        and (o["type"] != "total" or TESTO_USE_TOTALS)
    ]
    testo = max(candidates, key=lambda o: o["price"]) if candidates else base

    return {"base": base, "testo": testo}


def pick_props(game, k, team):
    """Valitsee k lyhythintaisinta maalintekijää VAIN annetun joukkueen
    pelaajista (NHL:n rosterin mukaan). Jos rosteria ei saada tai joukkue
    puuttuu, propseja ei lisätä - väärän joukkueen pelaajaa ei koskaan valita."""
    k = min(k, MAX_PROPS_PER_GAME)
    if k <= 0 or not team:
        return []
    roster = team_players(team)
    if not roster:
        return []
    ranked = sorted(
        (p for p in game["props"] if norm_player(p[0]) in roster),
        key=lambda x: x[1],
    )
    return [
        {"type": "prop", "player": name, "description": f"{name} tekee maalin",
         "price": price, "prob": None}
        for name, price in ranked[:k]
    ]


def make_leg(game, selections):
    price = prod(s["price"] for s in selections)
    return {
        "game_id": game["game_id"],
        "match": f"{game['home']} vs {game['away']}",
        "commence": game["commence"],
        "selections": selections,
        "pick": " + ".join(s["description"] for s in selections),
        "price": price,
        "implied_prob": 1 / price,
    }


def build_slip_tiered(legs, min_odds, max_odds, min_legs=1, max_legs=20):
    sorted_legs = sorted(legs, key=lambda x: -x["implied_prob"])
    selected = []
    total_odds = 1.0

    for leg in sorted_legs:
        if len(selected) >= max_legs:
            break
        candidate_odds = total_odds * leg["price"]
        if candidate_odds > max_odds:
            continue
        selected.append(leg)
        total_odds = candidate_odds
        if total_odds >= min_odds and len(selected) >= min_legs:
            break

    if not selected:
        return None

    return {
        "legs": selected,
        "total_odds": total_odds,
        "total_prob": prod(leg["implied_prob"] for leg in selected),
        "reached_target": total_odds >= min_odds,
    }


def build_staged_slip(games_opts, cfg):
    """Käy STAGES läpi ja palauttaa ensimmäisen kokoonpanon, joka yltää
    tason minimikertoimeen. Jos mikään ei yllä, palautetaan FALLBACK_STAGE."""
    attempts = []
    for stage in STAGES:
        legs = []
        for game, opts in games_opts:
            main = opts[stage["option"]]
            selections = [main] + pick_props(game, stage["props"], main.get("team"))
            legs.append(make_leg(game, selections))
        result = build_slip_tiered(
            legs, cfg["min_odds"], cfg["max_odds"], cfg["min_legs"], cfg["max_legs"]
        )
        attempts.append((stage, result))
        if result and result["reached_target"]:
            result["stage"] = stage["name"]
            return result

    stage, result = attempts[FALLBACK_STAGE]
    if result:
        result["stage"] = stage["name"]
    return result


# ---------- viestin muotoilu ----------

def escape_markdown(text):
    special_chars = r"_*[]()~`>#+-=|{}.!"
    for char in special_chars:
        text = text.replace(char, f"\\{char}")
    return text


def format_date_range(legs):
    dates = sorted({parse_utc(leg["commence"]).astimezone(FINLAND_TZ).date() for leg in legs})
    if not dates:
        return ""
    first, last = dates[0], dates[-1]
    if first == last:
        return first.strftime("%d.%m.%Y")
    return f"{first.strftime('%d.%m')}-{last.strftime('%d.%m.%Y')}"


def format_slip_markdown(tier_key, tier_config, result):
    emoji = tier_config["emoji"]
    name = tier_key.upper()

    if result is None:
        return f"{emoji} *{name}*\n_Ei löytynyt sopivaa lappua tälle riskitasolle\\._"

    status_emoji = "✅" if result["reached_target"] else "⚠️"
    total_odds_str = escape_markdown(f"{result['total_odds']:.1f}")
    lines = [
        f"{emoji} *{name}* {status_emoji}",
        f"*{len(result['legs'])} kohdetta* — yhteiskerroin __*{total_odds_str}*__",
    ]
    if result.get("stage"):
        lines.append("_" + escape_markdown(f"Kokoonpano: {result['stage']}") + "_")
    lines.append("")

    for i, leg in enumerate(result["legs"], start=1):
        match = escape_markdown(leg["match"])
        pick = escape_markdown(leg["pick"])
        price = escape_markdown(f"{leg['price']:.2f}")
        lines.append(f"*{i}\\.* {match}")
        lines.append(f"    → {pick} @ __{price}__")
        lines.append("")
    return "\n".join(lines)


def format_results_report(results_summary):
    if not results_summary:
        return None
    lines = ["📊 *Edellisyön tulokset*", ""]
    for tier, hits, total in results_summary:
        lines.append(escape_markdown(f"{tier.upper()}: {hits or 0}/{total} osui"))
    lines.append("")
    return "\n".join(lines)


def format_cold_streak_warning(conn):
    warnings = get_cold_streak_warnings(conn)
    if not warnings:
        return None
    lines = ["🥶 *Huomio*", ""]
    for w in warnings:
        lines.append(escape_markdown(w))
    lines.append("")
    return "\n".join(lines)


# ---------- Telegram ----------

def send_telegram_message(text):
    """Telegramin raja on 4096 merkkiä, joten pitkä viesti jaetaan
    tyhjien rivien kohdalta useampaan osaan."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    chunks, current = [], ""
    for block in text.split("\n\n"):
        if current and len(current) + len(block) + 2 > 3500:
            chunks.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)

    for chunk in chunks:
        response = requests.post(url, data={
            "chat_id": chat_id, "text": chunk, "parse_mode": "MarkdownV2",
        })
        response.raise_for_status()


def send_error_alert(error_text):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        requests.post(url, data={
            "chat_id": chat_id,
            "text": f"⚠️ Botti epäonnistui tänään:\n\n{error_text[:500]}",
        })
    except Exception:
        pass


# ---------- pääohjelma ----------

if __name__ == "__main__":
    try:
        if os.getenv("FAKE_NOW") and not DRY_RUN:
            sys.exit("FAKE_NOW vaatii DRY_RUN=1 (muuten viesti ja tila menisivät väärään aikaan).")

        conn = init_state_table()
        init_sent_picks_table()

        if not DRY_RUN and already_sent_today(conn):
            print("Viesti jo lähetetty tänään - ohitetaan tämä ajo.")
            sys.exit(0)

        print("Tarkistetaan edellisyön tulokset...")
        check_and_update_results()
        results_text = format_results_report(get_results_summary())
        cold_streak_text = format_cold_streak_warning(conn)

        season = get_current_nhl_season()
        print(f"Käytetään kautta: {season}")

        now_utc = get_now_utc()
        games, window_start, window_end = games_in_window(load_games(conn), now_utc)
        print(f"Löytyi {len(games)} ottelua tälle yölle.")

        games_opts, safe_legs = [], []
        if games:
            print("Koulutetaan kalibrointimalli...")
            calibration_model = train_calibration_model()
            ratings, league_avg = get_current_team_ratings(season)
            ratings = align_ratings_to_odds_names(
                ratings, {t for g in games for t in (g["home"], g["away"])}
            )

            for game in games:
                try:
                    opts = build_options(game, ratings, league_avg, calibration_model, season)
                except Exception as e:
                    print(f"Ottelu ohitettu virheen takia ({game['home']} vs {game['away']}): {e}")
                    continue
                if opts is None:
                    continue
                games_opts.append((game, opts))

                if DRY_RUN and game["props"]:
                    home_r = team_players(game["home"]) or set()
                    away_r = team_players(game["away"]) or set()
                    names = [norm_player(p[0]) for p in game["props"]]
                    h = sum(1 for n in names if n in home_r)
                    a = sum(1 for n in names if n in away_r)
                    print(f"  Propsit {game['home']} vs {game['away']}: "
                          f"koti {h}, vieras {a}, tunnistamatta {len(names) - h - a} / {len(names)}")

                # Chilli: aina kirjan suosikki (matalin h2h-kerroin), ei propseja
                home_price = game["h2h"][game["home"]]
                away_price = game["h2h"][game["away"]]
                fav = game["home"] if home_price <= away_price else game["away"]
                safe = {"type": "h2h", "team": fav, "description": f"{fav} voittaa",
                        "price": min(home_price, away_price), "prob": None}
                safe_legs.append(make_leg(game, [safe]))

        message_parts = []
        if results_text:
            message_parts.append(results_text)
        if cold_streak_text:
            message_parts.append(cold_streak_text)

        tier_results = {}
        if not safe_legs:
            message_parts.append("🏒 *Ei NHL\\-otteluita tälle yölle\\.*")
        else:
            date_range = escape_markdown(format_date_range(safe_legs))
            message_parts.append(f"🏒 *NHL LAPUT — {date_range}* 🏒")
            message_parts.append("")

            for tier_key, cfg in SLIP_TIERS.items():
                if tier_key == "chilli":
                    result = build_slip_tiered(
                        safe_legs, cfg["min_odds"], cfg["max_odds"],
                        cfg["min_legs"], cfg["max_legs"],
                    )
                else:
                    result = build_staged_slip(games_opts, cfg)
                tier_results[tier_key] = result
                message_parts.append(format_slip_markdown(tier_key, cfg, result))
                message_parts.append("")

                print(f"--- {tier_key.upper()} ---")
                if result:
                    print(f"{len(result['legs'])} kohdetta, kerroin {result['total_odds']:.1f}"
                          f" | kokoonpano: {result.get('stage', 'suosikit')}")
                else:
                    print("Ei löytynyt sopivaa lappua.")

            note = "Kertoimet ovat US-kirjojen keskiarvoista laskettuja tuloja - tarkista lopullinen kerroin sivustolta."
            message_parts.append("_" + escape_markdown(note) + "_")
            message_parts.append("")

        credits = get_state(conn, "credits_remaining")
        if credits:
            message_parts.append("_" + escape_markdown(f"Odds API -krediittejä jäljellä: {credits}") + "_")

        message = "\n".join(message_parts)

        if DRY_RUN:
            print("\n=== DRY RUN: viesti ===\n")
            print(message.replace("\\", ""))

            print("\n--- Mallin arviot (perus ja testo) ---")
            for tier in ("perus", "testo"):
                res = tier_results.get(tier)
                for leg in (res["legs"] if res else []):
                    for s in leg["selections"]:
                        p = s.get("prob")
                        if p is not None:
                            print(f"[{tier}] {leg['match']}: {s['description']} @ {s['price']:.2f}"
                                  f" | malli {p*100:.0f}% | EV {p*s['price']:.2f}")

            if os.getenv("TEST_SEND") == "1":
                send_telegram_message(message)
                print("\nTestiviesti lähetetty ryhmään (lähetystila ei muuttunut).")
        else:
            send_telegram_message(message)
            mark_sent_today(conn)
            try:
                today = today_fi().isoformat()
                for tier_key, result in tier_results.items():
                    save_sent_picks(conn, tier_key, result, today)
            except Exception as e:
                print(f"Lappujen tallennus epäonnistui: {e}")

    except Exception as e:
        print(traceback.format_exc())
        send_error_alert(f"{type(e).__name__}: {str(e)}")