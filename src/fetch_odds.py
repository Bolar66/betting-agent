import os
import sys
import math
import requests
import libsql
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

from night_window import get_now_utc, get_next_night_window, today_fi, parse_utc

load_dotenv()

API_KEY = os.getenv("ODDS_API_KEY")
BASE_URL = "https://api.the-odds-api.com/v4"
SPORT = "icehockey_nhl"

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

MAIN_MARKETS = "h2h,spreads,totals"
PROPS_MARKET = "player_goal_scorer_anytime"
ALT_MARKET = "alternate_spreads"

FETCH_PROPS = True
CREDIT_RESERVE = 15        # tämän alle ei mennä valinnaisilla hauilla
STORE_HORIZON_HOURS = 40   # tallennetaan vain seuraavan ~40 h ottelut
DRY_RUN = os.getenv("DRY_RUN") == "1"

COLUMNS = ("(game_id, sport_key, home_team, away_team, commence_time, bookmaker, "
           "outcome_name, price, fetched_at, market_key, point)")


def get_connection():
    return libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)


def init_db():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS odds_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT NOT NULL,
            sport_key TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            commence_time TEXT NOT NULL,
            bookmaker TEXT NOT NULL,
            outcome_name TEXT NOT NULL,
            price REAL NOT NULL,
            fetched_at TEXT NOT NULL,
            market_key TEXT DEFAULT 'h2h',
            point REAL
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()

    # Vanhan taulun päivitys: lisätään uudet sarakkeet jos puuttuvat.
    for column_def in ("market_key TEXT DEFAULT 'h2h'", "point REAL"):
        try:
            c = get_connection()
            c.execute(f"ALTER TABLE odds_snapshots ADD COLUMN {column_def}")
            c.commit()
        except Exception:
            pass  # sarake on jo olemassa
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


def api_get(path, params):
    params = {**params, "apiKey": API_KEY}
    response = requests.get(f"{BASE_URL}{path}", params=params, timeout=30)
    response.raise_for_status()
    remaining = response.headers.get("x-requests-remaining")
    try:
        remaining = int(float(remaining))
    except (TypeError, ValueError):
        remaining = None
    return response.json(), remaining


def days_until_credit_reset():
    """Päiviä seuraavaan krediittien nollaukseen (kuukauden 1. päivä, UTC),
    tämä päivä mukaan lukien."""
    now = datetime.now(timezone.utc)
    if now.month == 12:
        next_reset = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_reset = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    return max(math.ceil((next_reset - now).total_seconds() / 86400), 1)


def outcome_label(market_key, outcome):
    if market_key == PROPS_MARKET:
        if outcome.get("name") != "Yes":
            return None
        return outcome.get("description")
    return outcome.get("name")


def event_to_rows(event, fetched_at):
    rows = []
    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            market_key = market["key"]
            for outcome in market.get("outcomes", []):
                label = outcome_label(market_key, outcome)
                if label is None:
                    continue
                rows.append((
                    event["id"], event["sport_key"], event["home_team"],
                    event["away_team"], event["commence_time"], bookmaker["title"],
                    label, outcome["price"], fetched_at, market_key,
                    outcome.get("point"),
                ))
    return rows


def insert_rows(conn, rows, chunk=50):
    """Lisää rivit isoissa erissä (yksi verkkokutsu / 50 riviä)."""
    for i in range(0, len(rows), chunk):
        part = rows[i:i + chunk]
        placeholders = ",".join(["(?,?,?,?,?,?,?,?,?,?,?)"] * len(part))
        params = tuple(v for row in part for v in row)
        conn.execute(f"INSERT INTO odds_snapshots {COLUMNS} VALUES {placeholders}", params)
    conn.commit()


def fetch_event_market(conn, events, market, remaining, fetched_at):
    for event in events:
        if remaining is not None and remaining <= CREDIT_RESERVE:
            print(f"Krediittireservi saavutettu, {market}-haku keskeytetty.")
            break
        try:
            data, left = api_get(
                f"/sports/{SPORT}/events/{event['id']}/odds",
                {"regions": "us", "markets": market, "oddsFormat": "decimal"},
            )
        except Exception as e:
            print(f"  {event['home_team']} vs {event['away_team']} ({market}) epäonnistui: {e}")
            continue
        if left is not None:
            remaining = left
        rows = event_to_rows(data, fetched_at)
        insert_rows(conn, rows)
        print(f"  {event['home_team']} vs {event['away_team']} ({market}): {len(rows)} riviä")
    return remaining


def affordable(remaining, today_cost, per_day_cost, future_days):
    """Riittääkö krediittejä tämän päivän haulle ja kuun jäljellä olevien
    päivien vastaavalle kulutukselle (reservi mukana)?"""
    return remaining - today_cost >= future_days * per_day_cost + CREDIT_RESERVE


def fetch_extras(conn, tonight, remaining):
    """Valinnaiset haut: propsit ensin, sitten alt-spreadit.
    Kumpikin haetaan vain jos krediittibudjetti riittää kuun loppuun asti."""
    n = len(tonight)
    future_days = days_until_credit_reset() - 1
    fetched_at = datetime.now(timezone.utc).isoformat()

    if FETCH_PROPS:
        if affordable(remaining, n, 3 + n, future_days):
            print("Haetaan pelaajapropsit...")
            remaining = fetch_event_market(conn, tonight, PROPS_MARKET, remaining, fetched_at)
        else:
            print("Propsit ohitettu: krediittibudjetti ei riitä.")

    daily_full = 3 + (2 * n if FETCH_PROPS else n)
    if affordable(remaining, n, daily_full, future_days):
        print("Haetaan alt-spreadit...")
        remaining = fetch_event_market(conn, tonight, ALT_MARKET, remaining, fetched_at)
    else:
        print("Alt-markkinat ohitettu: krediittibudjetti ei riitä.")
    return remaining


if __name__ == "__main__":
    conn = init_db()

    # Krediittisuoja: kun päivän viesti on jo lähetetty, ei haeta enää mitään.
    if not DRY_RUN and get_state(conn, "last_sent_date") == today_fi().isoformat():
        print("Viesti jo lähetetty tänään - ohitetaan haku.")
        sys.exit(0)

    now = get_now_utc()
    events, remaining = api_get(
        f"/sports/{SPORT}/odds",
        {"regions": "us", "markets": MAIN_MARKETS, "oddsFormat": "decimal"},
    )
    print(f"Löytyi {len(events)} tulevaa ottelua. Krediittejä jäljellä: {remaining}")

    horizon = now + timedelta(hours=STORE_HORIZON_HOURS)
    soon = [e for e in events if now < parse_utc(e["commence_time"]) <= horizon]

    fetched_at = datetime.now(timezone.utc).isoformat()  # aina oikea kello
    rows = []
    for e in soon:
        rows.extend(event_to_rows(e, fetched_at))
    insert_rows(conn, rows)
    print(f"Tallennettu {len(rows)} riviä ({len(soon)} ottelua).")

    window_start, window_end = get_next_night_window(now)
    tonight = [e for e in soon if window_start <= parse_utc(e["commence_time"]) <= window_end]
    print(f"Illan ottelut: {len(tonight)}")

    if remaining is not None and tonight:
        remaining = fetch_extras(conn, tonight, remaining)

    if remaining is not None:
        set_state(conn, "credits_remaining", str(remaining))
    print(f"Krediittejä jäljellä: {remaining}")