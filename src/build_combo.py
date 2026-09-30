import os
import sys
import json
import math
import libsql
import requests
import traceback
from math import prod
from datetime import timedelta
from dotenv import load_dotenv

from night_window import FINLAND_TZ, get_now_utc, get_next_night_window, today_fi, parse_utc
from check_results import check_and_update_results, get_results_summary

load_dotenv()

DRY_RUN = os.getenv("DRY_RUN") == "1"

MAX_SELECTIONS = 20          # sivuston raja valinnoille yhdellä lapulla
MIN_EV = 0.92                # todennäköisyys * kerroin vähintään tämä (karsii huonot hinnat)
ALLOW_PLAYER_UNDERS = False  # pelaajien alle-vedot pois

SLIP_TIERS = {
    "chilli": {"emoji": "🧊", "min_odds": 100, "max_odds": 1_000,
               "min_prob": 0.55, "max_per_game": 2},
    "perus":  {"emoji": "⚖️", "min_odds": 1_000, "max_odds": 8_000,
               "min_prob": 0.35, "max_per_game": 3},
    "testo":  {"emoji": "🔥", "min_odds": 75_000, "max_odds": 250_000,
               "min_prob": 0.15, "max_per_game": 4},
}

# Pelaajatilastot, jotka voidaan tarkistaa boxscoresta
PLAYER_STATS = {
    "goals": "maalit",
    "points": "pisteet",
    "assists": "syöttöpisteet",
    "shots_on_goal": "laukaukset maaliin",
    "goalie_saves": "torjunnat",
}
YES_TEXT = {
    "goals": "tekee maalin",
    "points": "tekee pisteen",
    "assists": "tekee syöttöpisteen",
    "shots_on_goal": "laukoo maalia kohti",
    "goalie_saves": "torjuu",
}

COLD_STREAK_LOOKBACK_DAYS = 5
COLD_STREAK_MIN_SAMPLES = 15
COLD_STREAK_THRESHOLD = 0.30

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")


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
        pass


def save_sent_picks(conn, tier_key, result, sent_date):
    if result is None:
        return
    for leg in result["legs"]:
        selections = [
            {k: s.get(k) for k in ("type", "team", "point", "side", "player", "stat")}
            for s in leg["selections"]
        ]
        conn.execute("""
            INSERT INTO sent_picks
            (sent_date, tier, game_id, home_team, away_team, commence_time,
             pick_description, price, selections_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (sent_date, tier_key, leg["game_id"], leg["home"], leg["away"], leg["commence"],
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
                f"{tier.upper()}: peleistä osunut vain {hits / total * 100:.0f}% "
                f"viimeisen {COLD_STREAK_LOOKBACK_DAYS} päivän aikana ({hits}/{total})"
            )
    return warnings


# ---------- valinnat SGO-datasta ----------

def is_line_market(key):
    return key in ("spreads", "totals", "team_totals") or (key.startswith("player_") and key.endswith("_ou"))


def estimate_prob(row, book):
    """Linjamarkkinoille todennäköisyys saman linjan molemmista puolista
    marginaali poistettuna (SGO:n reilu kerroin voi koskea eri linjaa).
    Kyllä/ei-vedoille ja voittajalle SGO:n reilu kerroin."""
    event_id, _, _, _, key, subject, _, side, point, price, fair = row
    if is_line_market(key):
        if point is None:
            return None
        if key == "spreads":
            other = {"home": "away", "away": "home"}.get(side)
            opp = book.get((event_id, key, None, other, -point))
        else:
            other = {"over": "under", "under": "over"}.get(side)
            opp = book.get((event_id, key, subject, other, point))
        if not opp or opp <= 1.0:
            return None
        a, b = 1 / price, 1 / opp
        return a / (a + b)
    if fair and fair > 1.0:
        return 1 / fair
    return None


def make_selection(row, prob):
    (event_id, home, away, commence, key, subject, subject_team,
     side, point, price, fair) = row
    if not price or price <= 1.01 or prob is None:
        return None
    base = {"event_id": event_id, "price": price, "prob": prob, "ev": prob * price,
            "point": point, "side": side, "team": None, "player": None, "stat": None,
            "positive": False}

    if key in ("h2h", "spreads"):
        team = home if side == "home" else away if side == "away" else None
        if team is None or (key == "spreads" and point is None):
            return None
        desc = f"{team} voittaa" if key == "h2h" else f"{team} tasoitus {point:+.1f}"
        return {**base, "type": "h2h" if key == "h2h" else "spread", "team": team,
                "description": desc, "dim": ("result",)}

    if key == "totals" and point is not None and side in ("over", "under"):
        desc = f"{'Yli' if side == 'over' else 'Alle'} {point:g} maalia"
        return {**base, "type": "total", "description": desc, "dim": ("goals",)}

    if key == "team_totals" and point is not None and side in ("over", "under"):
        desc = f"{subject} {'yli' if side == 'over' else 'alle'} {point:g} maalia"
        return {**base, "type": "team_total", "team": subject, "description": desc,
                "dim": ("goals",)}

    if key == "team_bothTeamsScored_yn" and side in ("yes", "no"):
        desc = "Molemmat joukkueet tekevät maalin" if side == "yes" else "Ainakin toinen joukkue jää nollille"
        return {**base, "type": "btts", "description": desc, "dim": ("btts",)}

    if key.startswith("player_"):
        stat, _, bet = key[len("player_"):].rpartition("_")
        if stat not in PLAYER_STATS or not subject:
            return None
        if bet == "yn":
            if side != "yes":
                return None
            desc = f"{subject} {YES_TEXT[stat]}"
        elif bet == "ou" and point is not None:
            if side == "under" and not ALLOW_PLAYER_UNDERS:
                return None
            if side == "over":
                desc = f"{subject}: {PLAYER_STATS[stat]} {math.floor(point) + 1}+"
            elif side == "under":
                desc = f"{subject}: {PLAYER_STATS[stat]} alle {point:g}"
            else:
                return None
        else:
            return None
        return {**base, "type": "player", "team": subject_team, "player": subject, "stat": stat,
                "positive": side in ("yes", "over"), "description": desc,
                "dim": ("player", subject)}
    return None


def load_games(conn, now_utc):
    window_start, window_end = get_next_night_window(now_utc)
    since = (now_utc - timedelta(days=2)).isoformat()
    rows = conn.execute("""
        WITH latest AS (
            SELECT event_id, MAX(fetched_at) AS mf
            FROM sgo_odds WHERE fetched_at >= ?
            GROUP BY event_id
        )
        SELECT o.event_id, o.home_team, o.away_team, o.commence_time, o.market_key,
               o.subject, o.subject_team, o.side, o.point, o.price, o.fair_price
        FROM sgo_odds o
        JOIN latest l ON o.event_id = l.event_id AND o.fetched_at = l.mf
    """, (since,)).fetchall()

    # Hakemisto vastapuolen kertoimen löytämiseksi (sisältää myös alle-puolet)
    book = {}
    for r in rows:
        event_id, key, subject, side, point, price = r[0], r[4], r[5], r[7], r[8], r[9]
        subj = None if key == "spreads" else subject
        book[(event_id, key, subj, side, point)] = price

    games = {}
    for row in rows:
        commence = parse_utc(row[3])
        if not (window_start <= commence <= window_end and commence > now_utc):
            continue
        sel = make_selection(row, estimate_prob(row, book))
        if sel is None:
            continue
        g = games.setdefault(row[0], {"event_id": row[0], "home": row[1], "away": row[2],
                                      "commence": row[3], "selections": []})
        g["selections"].append(sel)
    return games


# ---------- lapun kokoaminen ----------

def cost_ratio(sel):
    """Kuinka paljon todennäköisyyttä valinta 'maksaa' suhteessa kertoimen
    kasvuun. Pienempi = parempi."""
    return -math.log(sel["prob"]) / math.log(sel["price"])


def conflicts(sel, gs, cfg):
    if gs["count"] >= cfg["max_per_game"]:
        return True
    if sel["dim"] in gs["dims"]:
        return True
    if (sel["type"] == "player" and sel["positive"] and gs["result_team"]
            and sel["team"] and sel["team"] != gs["result_team"]):
        return True
    if sel["type"] in ("h2h", "spread") and any(t != sel["team"] for t in gs["pos_teams"]):
        return True
    return False


def build_slip(games, cfg):
    pool = [
        s for g in games.values() for s in g["selections"]
        if s["prob"] >= cfg["min_prob"] and s["ev"] >= MIN_EV
    ]
    pool.sort(key=cost_ratio)

    chosen, total, state = [], 1.0, {}
    for s in pool:
        if total >= cfg["min_odds"] or len(chosen) >= MAX_SELECTIONS:
            break
        if total * s["price"] > cfg["max_odds"]:
            continue
        gs = state.setdefault(s["event_id"], {"count": 0, "dims": set(),
                                              "result_team": None, "pos_teams": set()})
        if conflicts(s, gs, cfg):
            continue
        chosen.append(s)
        total *= s["price"]
        gs["count"] += 1
        gs["dims"].add(s["dim"])
        if s["type"] in ("h2h", "spread"):
            gs["result_team"] = s["team"]
        if s["type"] == "player" and s["positive"] and s["team"]:
            gs["pos_teams"].add(s["team"])

    if not chosen:
        return None

    by_game = {}
    for s in chosen:
        by_game.setdefault(s["event_id"], []).append(s)
    legs = []
    for eid, sels in by_game.items():
        g = games[eid]
        legs.append({
            "game_id": eid, "home": g["home"], "away": g["away"],
            "match": f"{g['home']} vs {g['away']}", "commence": g["commence"],
            "selections": sels,
            "pick": " + ".join(s["description"] for s in sels),
            "price": prod(s["price"] for s in sels),
        })
    legs.sort(key=lambda l: l["commence"])

    return {
        "legs": legs,
        "n_sel": len(chosen),
        "total_odds": total,
        "total_prob": prod(s["prob"] for s in chosen),
        "reached_target": total >= cfg["min_odds"],
    }


# ---------- viestin muotoilu ----------

def escape_markdown(text):
    for char in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(char, f"\\{char}")
    return text


def format_date_range(items):
    dates = sorted({parse_utc(i["commence"]).astimezone(FINLAND_TZ).date() for i in items})
    if not dates:
        return ""
    first, last = dates[0], dates[-1]
    if first == last:
        return first.strftime("%d.%m.%Y")
    return f"{first.strftime('%d.%m')}-{last.strftime('%d.%m.%Y')}"


def format_slip_markdown(tier_key, cfg, result):
    emoji, name = cfg["emoji"], tier_key.upper()
    if result is None:
        return f"{emoji} *{name}*\n_Ei löytynyt sopivaa lappua tälle riskitasolle\\._"

    status = "✅" if result["reached_target"] else "⚠️"
    odds = escape_markdown(f"{result['total_odds']:.1f}")
    chance = f"Arvioitu todennäköisyys n. 1/{1 / result['total_prob']:,.0f}".replace(",", " ")
    lines = [
        f"{emoji} *{name}* {status}",
        f"*{result['n_sel']} valintaa, {len(result['legs'])} peliä* — yhteiskerroin __*{odds}*__",
        "_" + escape_markdown(chance) + "_",
        "",
    ]
    for i, leg in enumerate(result["legs"], start=1):
        lines.append(f"*{i}\\.* {escape_markdown(leg['match'])}")
        for s in leg["selections"]:
            price = escape_markdown(f"{s['price']:.2f}")
            lines.append(f"    • {escape_markdown(s['description'])} @ __{price}__")
        lines.append("")
    return "\n".join(lines)


def format_results_report(summary):
    if not summary:
        return None
    lines = ["📊 *Edellisyön tulokset*", ""]
    for tier, hits, checked, total in summary:
        hits, checked = hits or 0, checked or 0
        emoji = SLIP_TIERS.get(tier, {}).get("emoji", "")
        if checked < total:
            status = f"{total - checked} peliä vielä tarkistamatta"
        elif hits == total:
            status = "✅ lappu meni läpi!"
        else:
            status = "✗ lappu ei mennyt läpi"
        lines.append(escape_markdown(f"{emoji} {tier.upper()}: {hits}/{total} peliä osui — {status}"))
    lines.append("")
    return "\n".join(lines)


def format_cold_streak_warning(conn):
    warnings = get_cold_streak_warnings(conn)
    if not warnings:
        return None
    return "\n".join(["🥶 *Huomio*", ""] + [escape_markdown(w) for w in warnings] + [""])


# ---------- Telegram ----------

def send_telegram_message(text):
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
    try:
        requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", data={
            "chat_id": chat_id,
            "text": f"⚠️ Botti epäonnistui tänään:\n\n{error_text[:500]}",
        })
    except Exception:
        pass


# ---------- pääohjelma ----------

if __name__ == "__main__":
    try:
        if os.getenv("FAKE_NOW") and not DRY_RUN:
            sys.exit("FAKE_NOW vaatii DRY_RUN=1.")

        conn = init_state_table()
        init_sent_picks_table()

        if not DRY_RUN and already_sent_today(conn):
            print("Viesti jo lähetetty tänään - ohitetaan tämä ajo.")
            sys.exit(0)

        print("Tarkistetaan edellisyön tulokset...")
        check_and_update_results()
        results_text = format_results_report(get_results_summary())
        cold_streak_text = format_cold_streak_warning(conn)

        now_utc = get_now_utc()
        games = load_games(conn, now_utc)
        n_sel = sum(len(g["selections"]) for g in games.values())
        print(f"Illan pelejä: {len(games)}, valintoja poolissa: {n_sel}")

        message_parts = []
        if results_text:
            message_parts.append(results_text)
        if cold_streak_text:
            message_parts.append(cold_streak_text)

        tier_results = {}
        if not games:
            message_parts.append("🏒 *Ei NHL\\-otteluita tälle yölle\\.*")
        else:
            date_range = escape_markdown(format_date_range(list(games.values())))
            message_parts.append(f"🏒 *NHL LAPUT — {date_range}* 🏒")
            message_parts.append("")

            for tier_key, cfg in SLIP_TIERS.items():
                result = build_slip(games, cfg)
                tier_results[tier_key] = result
                message_parts.append(format_slip_markdown(tier_key, cfg, result))
                message_parts.append("")
                if result:
                    print(f"--- {tier_key.upper()}: {result['n_sel']} valintaa, "
                          f"{len(result['legs'])} peliä, kerroin {result['total_odds']:.1f}")
                else:
                    print(f"--- {tier_key.upper()}: ei lappua")

            note = ("Kertoimet ovat kirjojen konsensuksesta laskettuja tuloja. Saman pelin "
                    "yhdistelmät hinnoitellaan sivustolla yleensä matalammiksi - tarkista kerroin.")
            message_parts.append("_" + escape_markdown(note) + "_")

        message = "\n".join(message_parts)

        if DRY_RUN:
            print("\n=== DRY RUN: viesti ===\n")
            print(message.replace("\\", ""))
            print("\n--- Valinnat: todennäköisyys ja EV ---")
            for tier, res in tier_results.items():
                for leg in (res["legs"] if res else []):
                    for s in leg["selections"]:
                        print(f"[{tier}] {s['description']} @ {s['price']:.2f} | "
                              f"p {s['prob']*100:.0f}% | EV {s['ev']:.2f}")
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