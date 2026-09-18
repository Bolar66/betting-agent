import os
import libsql
import requests
import traceback
from math import prod
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from win_model import (
    train_calibration_model,
    get_current_team_ratings,
    get_current_nhl_season,
    predict_win_probability,
    calculate_edge,
    build_h2h_rationale,
    get_head_to_head_note,
)
from props_model import (
    KNOWN_PLAYER_IDS,
    calculate_calibrated_goal_probability,
    calculate_prop_edge,
)
from check_results import check_and_update_results, get_results_summary

load_dotenv()

MAX_LEGS = 20
MAX_ODDS = 250_000
TARGET_MIN_ODDS = 10_000
MIN_EDGE = 0.05

COLD_STREAK_LOOKBACK_DAYS = 5
COLD_STREAK_MIN_SAMPLES = 3
COLD_STREAK_THRESHOLD = 0.30

FINLAND_TZ = ZoneInfo("Europe/Helsinki")
NIGHT_START_HOUR = 18
NIGHT_END_HOUR = 6

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

SLIP_TIERS = {
    "chilli": {"emoji": "🧊", "min_odds": 50, "max_odds": 1_000, "min_legs": 2, "max_legs": 6},
    "perus":  {"emoji": "⚖️", "min_odds": 10_000, "max_odds": 100_000, "min_legs": 6, "max_legs": 15},
    "testo":  {"emoji": "🔥", "min_odds": 75_000, "max_odds": 250_000, "min_legs": 12, "max_legs": 20},
}


def get_connection():
    return libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)


def init_bot_state_table():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    return conn


def already_sent_today():
    conn = init_bot_state_table()
    cursor = conn.execute("SELECT value FROM bot_state WHERE key = 'last_sent_date'")
    row = cursor.fetchone()
    today = datetime.now(FINLAND_TZ).date().isoformat()
    return row is not None and row[0] == today


def mark_sent_today():
    conn = init_bot_state_table()
    today = datetime.now(FINLAND_TZ).date().isoformat()
    conn.execute("""
        INSERT INTO bot_state (key, value) VALUES ('last_sent_date', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (today,))
    conn.commit()


def init_sent_picks_table(conn):
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
            hit INTEGER DEFAULT NULL
        )
    """)
    conn.commit()


def save_sent_picks(conn, tier_key, result, sent_date):
    if result is None:
        return
    for leg in result["legs"]:
        home, away = leg["match"].split(" vs ")
        conn.execute("""
            INSERT INTO sent_picks
            (sent_date, tier, game_id, home_team, away_team, commence_time, pick_description, price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (sent_date, tier_key, leg["game_id"], home, away, leg["commence"], leg["pick"], leg["price"]))
    conn.commit()


def get_cold_streak_warnings(conn):
    """Tarkistaa onko jokin taso osunut heikosti viimeisimpien
    COLD_STREAK_LOOKBACK_DAYS päivän aikana. Palauttaa listan
    varoitustekstejä (tyhjä lista jos ei mitään huomautettavaa)."""
    cutoff = (datetime.now(FINLAND_TZ).date() - timedelta(days=COLD_STREAK_LOOKBACK_DAYS)).isoformat()

    cursor = conn.execute("""
        SELECT tier, SUM(hit), COUNT(*)
        FROM sent_picks
        WHERE checked = 1 AND sent_date >= ?
        GROUP BY tier
    """, (cutoff,))

    warnings = []
    for tier, hits, total in cursor.fetchall():
        hits = hits or 0
        if total >= COLD_STREAK_MIN_SAMPLES:
            rate = hits / total
            if rate < COLD_STREAK_THRESHOLD:
                warnings.append(
                    f"{tier.upper()} on osunut vain {rate*100:.0f}% "
                    f"viimeisen {COLD_STREAK_LOOKBACK_DAYS} päivän aikana ({hits}/{total})"
                )
    return warnings


def format_cold_streak_warning(conn):
    warnings = get_cold_streak_warnings(conn)
    if not warnings:
        return None
    lines = ["🥶 *Huomio*", ""]
    for w in warnings:
        lines.append(escape_markdown(w))
    lines.append("")
    return "\n".join(lines)


def get_latest_odds():
    conn = get_connection()
    cursor = conn.execute("""
        WITH latest AS (
            SELECT game_id, MAX(fetched_at) AS max_fetched
            FROM odds_snapshots
            GROUP BY game_id
        )
        SELECT o.game_id, o.home_team, o.away_team, o.commence_time,
               o.outcome_name, AVG(o.price) AS avg_price
        FROM odds_snapshots o
        JOIN latest l ON o.game_id = l.game_id AND o.fetched_at = l.max_fetched
        GROUP BY o.game_id, o.outcome_name
    """)
    return cursor.fetchall()


def build_h2h_selections_with_model(rows, ratings, league_avg, calibration_model, season):
    games = {}
    for game_id, home, away, commence, outcome_name, price in rows:
        games.setdefault(game_id, {
            "home": home, "away": away, "commence": commence, "outcomes": {}
        })
        games[game_id]["outcomes"][outcome_name] = price

    selections_by_game = {}
    for game_id, g in games.items():
        home, away = g["home"], g["away"]

        if home not in ratings or away not in ratings:
            best_outcome, best_price = min(g["outcomes"].items(), key=lambda x: x[1])
            selections_by_game[game_id] = {
                "match": f"{home} vs {away}",
                "commence": g["commence"],
                "selections": [
                    {"description": f"{best_outcome} voittaa", "market_type": "h2h", "price": best_price}
                ],
            }
            continue

        game_date = datetime.fromisoformat(g["commence"].replace("Z", "+00:00")).astimezone(FINLAND_TZ).date()

        calibrated_prob, raw_prob, lam_home, lam_away = predict_win_probability(
            home, away, ratings, league_avg, calibration_model,
            game_date=game_date, season=season
        )

        home_price = g["outcomes"].get(home)
        away_price = g["outcomes"].get(away)

        if home_price is None or away_price is None:
            continue

        home_edge = calculate_edge(calibrated_prob, home_price)
        away_edge = calculate_edge(1 - calibrated_prob, away_price)
        best_edge = max(home_edge, away_edge)

        if best_edge >= MIN_EDGE:
            pick, price = (home, home_price) if home_edge >= away_edge else (away, away_price)
        else:
            pick, price = min([(home, home_price), (away, away_price)], key=lambda x: x[1])

        selections_by_game[game_id] = {
            "match": f"{home} vs {away}",
            "commence": g["commence"],
            "selections": [
                {"description": f"{pick} voittaa", "market_type": "h2h", "price": price}
            ],
        }

    return selections_by_game


def add_mock_player_props(selections_by_game, season):
    """MOCK-DATA: korvataan oikealla haulla kun kausi alkaa. Vain
    McDavidilla on varmistettu pelaaja-ID, joten vain hänelle lasketaan
    kalibroitu todennäköisyys - muut käyttävät suoraan bookmaker-hintaa."""
    mock_props = {
        "9de33ce1013f2a3375dbded2c9fbc7d6": [
            {"description": "Sebastian Aho tekee maalin", "market_type": "player_goal_scorer_anytime",
             "price": 2.20, "player_name": "Sebastian Aho"},
        ],
        "485b295347cb22f002e014cb87813ed7": [
            {"description": "Auston Matthews yli 0.5 pistettä", "market_type": "player_points",
             "price": 1.75, "player_name": "Auston Matthews"},
        ],
        "ac3cb11040dbdb0d2ccfa53e7d6b6cda": [
            {"description": "David Pastrnak tekee maalin", "market_type": "player_goal_scorer_anytime",
             "price": 2.05, "player_name": "David Pastrnak"},
        ],
    }
    for game_id, props in mock_props.items():
        if game_id not in selections_by_game:
            continue
        for prop in props:
            player_name = prop.get("player_name")
            player_id = KNOWN_PLAYER_IDS.get(player_name)
            if player_id and prop["market_type"] == "player_goal_scorer_anytime":
                model_prob, sample_size = calculate_calibrated_goal_probability(player_id, season)
                prop_edge = calculate_prop_edge(model_prob, prop["price"])
                prop["_model_prob"] = model_prob
                prop["_edge"] = prop_edge
            selections_by_game[game_id]["selections"].append(prop)
    return selections_by_game


def build_bet_builder_legs(selections_by_game):
    legs = []
    for game_id, g in selections_by_game.items():
        combined_price = prod(sel["price"] for sel in g["selections"])
        description = " + ".join(sel["description"] for sel in g["selections"])
        legs.append({
            "game_id": game_id,
            "match": g["match"],
            "commence": g["commence"],
            "pick": description,
            "price": combined_price,
            "implied_prob": 1 / combined_price,
        })
    return legs


def get_next_night_window(now_utc):
    now_fi = now_utc.astimezone(FINLAND_TZ)
    today_night_start = now_fi.replace(hour=NIGHT_START_HOUR, minute=0, second=0, microsecond=0)

    if now_fi.hour < NIGHT_END_HOUR:
        night_start = today_night_start - timedelta(days=1)
    else:
        night_start = today_night_start

    night_end = (night_start + timedelta(days=1)).replace(
        hour=NIGHT_END_HOUR, minute=0, second=0, microsecond=0
    )
    return night_start.astimezone(timezone.utc), night_end.astimezone(timezone.utc)


def filter_to_next_night(legs, now_utc=None):
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    window_start, window_end = get_next_night_window(now_utc)

    in_window = []
    for leg in legs:
        commence = datetime.fromisoformat(leg["commence"].replace("Z", "+00:00"))
        if window_start <= commence <= window_end and commence > now_utc:
            in_window.append(leg)
    return in_window, window_start, window_end


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

    total_prob = prod(leg["implied_prob"] for leg in selected)
    return {
        "legs": selected,
        "total_odds": total_odds,
        "total_prob": total_prob,
        "reached_target": total_odds >= min_odds,
    }


def format_date_range(legs):
    dates = sorted({
        datetime.fromisoformat(leg["commence"].replace("Z", "+00:00"))
        .astimezone(FINLAND_TZ).date()
        for leg in legs
    })
    if not dates:
        return ""
    first, last = dates[0], dates[-1]
    if first == last:
        return first.strftime("%d.%m.%Y")
    else:
        return f"{first.strftime('%d.%m')}-{last.strftime('%d.%m.%Y')}"


def escape_markdown(text):
    special_chars = r"_*[]()~`>#+-=|{}.!"
    for char in special_chars:
        text = text.replace(char, f"\\{char}")
    return text


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
        "",
    ]

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
        hits = hits or 0
        lines.append(escape_markdown(f"{tier.upper()}: {hits}/{total} osui"))
    lines.append("")
    return "\n".join(lines)


def send_telegram_message(text):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    response = requests.post(url, data={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "MarkdownV2",
    })
    response.raise_for_status()


def send_error_alert(error_text):
    """Lähettää virheilmoituksen ilman Markdown-muotoilua, jotta itse
    virheviesti ei kaada myös tätä lähetystä."""
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


if __name__ == "__main__":
    try:
        if already_sent_today():
            print("Viesti jo lähetetty tänään - ohitetaan tämä ajo.")
            exit(0)

        conn = get_connection()
        init_sent_picks_table(conn)

        print("Tarkistetaan edellisyön tulokset...")
        check_and_update_results()
        results_summary = get_results_summary()
        results_text = format_results_report(results_summary)
        cold_streak_text = format_cold_streak_warning(conn)

        season = get_current_nhl_season()
        print(f"Käytetään kautta: {season}")

        print("Koulutetaan kalibrointimalli...")
        calibration_model = train_calibration_model()
        ratings, league_avg = get_current_team_ratings(season)

        rows = get_latest_odds()
        selections_by_game = build_h2h_selections_with_model(rows, ratings, league_avg, calibration_model, season)
        selections_by_game = add_mock_player_props(selections_by_game, season)  # TODO: korvaa oikealla haulla

        legs = build_bet_builder_legs(selections_by_game)
        legs, window_start, window_end = filter_to_next_night(legs)


        print(f"Löytyi {len(legs)} legiä tälle yölle.\n")

        date_range = escape_markdown(format_date_range(legs))

        message_parts = []
        if results_text:
            message_parts.append(results_text)
        if cold_streak_text:
            message_parts.append(cold_streak_text)
        message_parts.append(f"🏒 *NHL LAPUT — {date_range}* 🏒")
        message_parts.append("")

        today = datetime.now(FINLAND_TZ).date().isoformat()

        for tier_key, tier_config in SLIP_TIERS.items():
            result = build_slip_tiered(
                legs, tier_config["min_odds"], tier_config["max_odds"],
                tier_config["min_legs"], tier_config["max_legs"]
            )
            message_parts.append(format_slip_markdown(tier_key, tier_config, result))
            message_parts.append("")
            save_sent_picks(conn, tier_key, result, today)

            print(f"--- {tier_key.upper()} ---")
            if result:
                print(f"{len(result['legs'])} kohdetta, kerroin {result['total_odds']:.1f}")
            else:
                print("Ei löytynyt sopivaa lappua.")

        message = "\n".join(message_parts)
        send_telegram_message(message)
        mark_sent_today()

    except Exception as e:
        error_details = traceback.format_exc()
        print(error_details)
        send_error_alert(f"{type(e).__name__}: {str(e)}")