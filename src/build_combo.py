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
    build_h2h_rationale,
)

load_dotenv()

MIN_EDGE = 0.05  # vähintään 5 prosenttiyksikön edge ennen kuin mallin valinta korvaa bookmakerin suosikin
MAX_LEGS = 20
MAX_ODDS = 250_000
TARGET_MIN_ODDS = 10_000

FINLAND_TZ = ZoneInfo("Europe/Helsinki")
NIGHT_START_HOUR = 18
NIGHT_END_HOUR = 6

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")


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
    """Muodostaa h2h-valinnan per ottelu, käyttäen omaa kalibroitua
    mallia PÄÄTÖKSENTEKOON. Vaatii vähintään MIN_EDGE-suuruisen edgen
    ennen kuin malli korvaa bookmakerin oman suosikin - pienemmällä
    erolla käytetään turvallisempaa bookmaker-suosikkia, koska pieni
    edge voi johtua kalibroinnin epätarkkuudesta, ei todellisesta
    virhehinnoittelusta."""
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
                    {"description": f"{best_outcome} voittaa (fallback - ei omaa mallia)",
                     "market_type": "h2h", "price": best_price}
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
            # Riittävän suuri edge löytyi - luotetaan malliin
            if home_edge >= away_edge:
                pick, price, prob = home, home_price, calibrated_prob
            else:
                pick, price, prob = away, away_price, 1 - calibrated_prob
            rationale = build_h2h_rationale(pick, prob, price, lam_home, lam_away)
            description = f"{pick} voittaa"
        else:
            # Ei riittävää edgeä - käytetään turvallisempaa bookmaker-suosikkia
            pick, price = min(
                [(home, home_price), (away, away_price)], key=lambda x: x[1]
            )
            prob = calibrated_prob if pick == home else (1 - calibrated_prob)
            rationale = (
                f"{pick} (bookmaker-suosikki, ei riittävää edgeä mallin mukaan: "
                f"{best_edge*100:+.0f}pp < {MIN_EDGE*100:.0f}pp kynnys)"
            )
            description = f"{pick} voittaa"

        selections_by_game[game_id] = {
            "match": f"{home} vs {away}",
            "commence": g["commence"],
            "selections": [
                {"description": description, "market_type": "h2h",
                 "price": price, "rationale": rationale}
            ],
        }

    return selections_by_game

def add_mock_player_props(selections_by_game):
    """MOCK-DATA: korvataan myöhemmin oikealla haulla kauden alkaessa."""
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
        rationales = [sel["rationale"] for sel in g["selections"] if "rationale" in sel]

        legs.append({
            "game_id": game_id,
            "match": g["match"],
            "commence": g["commence"],
            "pick": description,
            "price": combined_price,
            "implied_prob": 1 / combined_price,
            "rationale": " | ".join(rationales) if rationales else None,
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


def build_slip(legs):
    sorted_legs = sorted(legs, key=lambda x: -x["implied_prob"])
    selected = []
    total_odds = 1.0

    for leg in sorted_legs:
        if len(selected) >= MAX_LEGS:
            break
        candidate_odds = total_odds * leg["price"]
        if candidate_odds > MAX_ODDS:
            continue
        selected.append(leg)
        total_odds = candidate_odds
        if total_odds >= TARGET_MIN_ODDS:
            break

    if not selected:
        return None

    total_prob = prod(leg["implied_prob"] for leg in selected)
    reached_target = total_odds >= TARGET_MIN_ODDS

    return {
        "legs": selected,
        "total_odds": total_odds,
        "total_prob": total_prob,
        "reached_target": reached_target,
    }


def send_telegram_message(text):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    response = requests.post(url, data={"chat_id": chat_id, "text": text})
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


    result = build_slip(legs)

    if result is None:
        message = "Ei löytynyt yhtään ottelua tälle yölle."
    else:
        status = "Tavoite saavutettu" if result["reached_target"] else "Hiljainen yö"
        lines = [f"{status} - {len(result['legs'])} kohdetta", ""]
        for leg in result["legs"]:
            lines.append(f"- {leg['match']}")
            lines.append(f"  {leg['pick']} @ {leg['price']:.2f}")
            if leg["rationale"]:
                lines.append(f"  ({leg['rationale']})")
        lines.append("")
        lines.append(f"Yhteiskerroin: {result['total_odds']:.1f}")
        message = "\n".join(lines)

    print(message)
    send_telegram_message(message)