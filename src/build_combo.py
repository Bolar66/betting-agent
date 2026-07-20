import os
import sqlite3
import requests
from math import prod
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

DB_PATH = "data/odds.db"

MAX_LEGS = 20
MAX_ODDS = 250_000
TARGET_MIN_ODDS = 10_000

FINLAND_TZ = ZoneInfo("Europe/Helsinki")
NIGHT_START_HOUR = 18
NIGHT_END_HOUR = 6


def get_latest_odds():
    """Hakee jokaiselle ottelulle vain uusimman snapshotin."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
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
    rows = cursor.fetchall()
    conn.close()
    return rows


def build_h2h_selections(rows):
    """Muodostaa moneyline-valinnan (suosikki) per ottelu,
    ryhmiteltynä game_id:n mukaan bet builderia varten."""
    games = {}
    for game_id, home, away, commence, outcome_name, price in rows:
        games.setdefault(game_id, {
            "home": home, "away": away, "commence": commence, "outcomes": []
        })
        games[game_id]["outcomes"].append((outcome_name, price))

    selections_by_game = {}
    for game_id, g in games.items():
        best_outcome, best_price = min(g["outcomes"], key=lambda x: x[1])
        selections_by_game[game_id] = {
            "match": f"{g['home']} vs {g['away']}",
            "commence": g["commence"],
            "selections": [
                {"description": f"{best_outcome} voittaa", "market_type": "h2h", "price": best_price}
            ],
        }
    return selections_by_game


def add_mock_player_props(selections_by_game):
    """MOCK-DATA: lisää player prop -valintoja olemassa oleviin
    otteluihin. Korvataan myöhemmin oikealla fetch_player_props.py
    -kutsulla kun kausi alkaa ja propsit ovat saatavilla."""
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
    """Yhdistää jokaisen ottelun kaikki valinnat yhdeksi 'bet builder'
    -legiksi: yhteiskerroin = valintojen kertoimien tulo."""
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
    """Laskee seuraavan/käynnissä olevan 18:00-06:00 -ikkunan
    (Suomen aikaa) alku- ja loppuajan UTC:na."""
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
    """Rajaa legit lähimpään tulevaan/käynnissä olevaan NHL-yöhön
    (klo 18-06 Suomen aikaa)."""
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
    """Rakentaa lapun annetuista legeistä. Ei koskaan ylitä 250k,
    pyrkii 10k+, palauttaa parhaan mahdollisen myös hiljaisina öinä."""
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
    """Lähettää viestin Telegram-bottiin."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    response = requests.post(url, data={"chat_id": chat_id, "text": text})
    response.raise_for_status()


if __name__ == "__main__":
    rows = get_latest_odds()
    selections_by_game = build_h2h_selections(rows)
    selections_by_game = add_mock_player_props(selections_by_game)  # TODO: korvaa oikealla haulla

    legs = build_bet_builder_legs(selections_by_game)
    legs, window_start, window_end = filter_to_next_night(
        legs, now_utc=datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
    )    

    result = build_slip(legs)

    if result is None:
        message = "Ei löytynyt yhtään ottelua tälle yölle."
    else:
        status = "Tavoite saavutettu" if result["reached_target"] else "Hiljainen yö"
        lines = [f"{status} - {len(result['legs'])} kohdetta", ""]
        for leg in result["legs"]:
            lines.append(f"- {leg['match']}")
            lines.append(f"  {leg['pick']} @ {leg['price']:.2f}")
        lines.append("")
        lines.append(f"Yhteiskerroin: {result['total_odds']:.1f}")
        message = "\n".join(lines)

    print(message)
    send_telegram_message(message)