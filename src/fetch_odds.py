import os
import sqlite3
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ODDS_API_KEY")
BASE_URL = "https://api.the-odds-api.com/v4"
DB_PATH = "data/odds.db"


def get_nhl_odds():
    """Hakee NHL:n moneyline-kertoimet (h2h-markkina)."""
    url = f"{BASE_URL}/sports/icehockey_nhl/odds"
    params = {
        "apiKey": API_KEY,
        "regions": "us",
        "markets": "h2h",
        "oddsFormat": "decimal",
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()


def init_db():
    """Luo tietokannan ja taulun jos niitä ei vielä ole."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
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
            fetched_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def save_odds(games):
    """Tallentaa haetun datan tietokantaan. Jokainen ajo lisää uuden
    'snapshotin' (rivit fetched_at-aikaleimalla), eli historia säilyy
    eikä vanhaa dataa ylikirjoiteta."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    fetched_at = datetime.now(timezone.utc).isoformat()

    rows_inserted = 0
    for game in games:
        for bookmaker in game.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                for outcome in market.get("outcomes", []):
                    cursor.execute("""
                        INSERT INTO odds_snapshots (
                            game_id, sport_key, home_team, away_team,
                            commence_time, bookmaker, outcome_name,
                            price, fetched_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        game["id"],
                        game["sport_key"],
                        game["home_team"],
                        game["away_team"],
                        game["commence_time"],
                        bookmaker["title"],
                        outcome["name"],
                        outcome["price"],
                        fetched_at,
                    ))
                    rows_inserted += 1

    conn.commit()
    conn.close()
    return rows_inserted


if __name__ == "__main__":
    init_db()
    games = get_nhl_odds()
    print(f"Löytyi {len(games)} tulevaa ottelua.")

    rows = save_odds(games)
    print(f"Tallennettu {rows} riviä tietokantaan ({DB_PATH}).")