import os
import libsql
import requests
from math import prod
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from win_model import (
    train_calibration_model,
    get_current_team_ratings,
    predict_win_probability,
    calculate_edge,
)

load_dotenv()

MAX_LEGS = 20
MAX_ODDS = 250_000
TARGET_MIN_ODDS = 10_000
MIN_EDGE = 0.05

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


def is_correct_run_time():
    now_fi = datetime.now(FINLAND_TZ)
    return now_fi.hour == 12


def get_connection():
    return libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)


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


def build_h2h_selections_with_model(rows, ratings, league_avg, calibration_model):
    """Muodostaa h2h-valinnan per ottelu käyttäen kalibroitua mallia.
    Vaatii vähintään MIN_EDGE-suuruisen edgen ennen kuin malli
    korvaa bookmakerin oman suosikin."""
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

        calibrated_prob, raw_prob, lam_home, lam_away = predict_win_probability(
            home, away, ratings, league_avg, calibration_model
        )

        home_price = g["outcomes"].get(home)
        away_price = g["outcomes"].get(away)

        if home_price is None or away_price is None:
            continue

        home_edge = calculate_edge(calibrated_prob, home_price)
        away_edge = calculate_edge(1 - calibrated_prob, away_price)
        best_edge = max(home_edge, away_edge)

        if best_edge >= MIN_EDGE:
            if home_edge >= away_edge:
                pick, price = home, home_price
            else:
                pick, price = away, away_price
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


def add_mock_player_props(selections_by_game):
    """MOCK-DATA: korvataan oikealla haulla kun kausi alkaa."""
    mock_props = {
        "9de33ce1013f2a3375dbded2c9fbc7d6": [
            {"description": "Sebastian Aho tekee maalin", "market_type": "player_goal_scorer_anytime", "price": 2.20},
        ],
        "485b295347cb22f002e014cb87813ed7": [
            {"description": "Auston Matthews yli 0.5 pistettä", "market_type": "player_points", "price": 1.75},
        ],
        "ac3cb11040dbdb0d2ccfa53e7d6b6cda": [
            {"description": "David Pastrnak tekee maalin", "market_type": "player_goal_scorer_anytime", "price": 2.05},
        ],
    }
    for game_id, props in mock_props.items():
        if game_id in selections_by_game:
            selections_by_game[game_id]["selections"].extend(props)
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
    """Rakentaa lapun annetulla kerroin-haarukalla ja legimäärillä."""
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
    """Muodostaa päivämääräalueen tekstin legien commence_time-kentistä."""
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
    """Escapee MarkdownV2:n erikoismerkit tavallisesta tekstistä."""
    special_chars = r"_*[]()~`>#+-=|{}.!"
    for char in special_chars:
        text = text.replace(char, f"\\{char}")
    return text


def format_slip_markdown(tier_key, tier_config, result):
    """Muotoilee yhden lapun Telegramin MarkdownV2-tyyliin,
    kohteet numeroituina, kerroin alleviivattuna."""
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


if __name__ == "__main__":
    if not is_correct_run_time():
        print("Ei oikea ajoaika Suomen aikaa - ohitetaan tämä ajo.")
        exit(0)

    print("Koulutetaan kalibrointimalli...")
    calibration_model = train_calibration_model()
    ratings, league_avg = get_current_team_ratings()

    rows = get_latest_odds()
    selections_by_game = build_h2h_selections_with_model(rows, ratings, league_avg, calibration_model)
    selections_by_game = add_mock_player_props(selections_by_game)  # TODO: korvaa oikealla haulla

    legs = build_bet_builder_legs(selections_by_game)
    legs, window_start, window_end = filter_to_next_night(legs)

    print(f"Löytyi {len(legs)} legiä tälle yölle.\n")

    date_range = escape_markdown(format_date_range(legs))
    message_parts = [f"🏒 *NHL LAPUT — {date_range}* 🏒", ""]

    for tier_key, tier_config in SLIP_TIERS.items():
        result = build_slip_tiered(
            legs, tier_config["min_odds"], tier_config["max_odds"],
            tier_config["min_legs"], tier_config["max_legs"]
        )
        message_parts.append(format_slip_markdown(tier_key, tier_config, result))
        message_parts.append("")

        print(f"--- {tier_key.upper()} ---")
        if result:
            print(f"{len(result['legs'])} kohdetta, kerroin {result['total_odds']:.1f}")
        else:
            print("Ei löytynyt sopivaa lappua.")

    message = "\n".join(message_parts)
    send_telegram_message(message)