import os
import libsql
from dotenv import load_dotenv
from fetch_historical_games import collect_all_games, SEASONS

load_dotenv()

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")


def get_connection():
    return libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)


def init_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS historical_games (
            game_id INTEGER PRIMARY KEY,
            season INTEGER NOT NULL,
            date TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            home_score INTEGER NOT NULL,
            away_score INTEGER NOT NULL,
            home_win INTEGER NOT NULL,
            went_to_ot_so INTEGER NOT NULL
        )
    """)
    conn.commit()


def save_games(conn, games):
    rows_inserted = 0
    for g in games:
        conn.execute("""
            INSERT OR IGNORE INTO historical_games (
                game_id, season, date, home_team, away_team,
                home_score, away_score, home_win, went_to_ot_so
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            g["game_id"], g["season"], g["date"],
            g["home_team"], g["away_team"],
            g["home_score"], g["away_score"],
            g["home_win"], int(g["went_to_ot_so"]),
        ))
        rows_inserted += 1
    conn.commit()
    return rows_inserted


if __name__ == "__main__":
    conn = get_connection()
    init_table(conn)

    games = collect_all_games(SEASONS)
    rows = save_games(conn, games)
    print(f"Tallennettu {rows} ottelua Turso-tietokantaan.")