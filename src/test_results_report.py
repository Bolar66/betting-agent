import os
import libsql
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

FINLAND_TZ = ZoneInfo("Europe/Helsinki")
TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

conn = libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)

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

yesterday = (datetime.now(FINLAND_TZ).date() - timedelta(days=1)).isoformat()

test_data = [
    (yesterday, "chilli", "fake1", "Edmonton Oilers", "Vancouver Canucks", "2026-01-01T00:00:00Z", "Edmonton Oilers voittaa", 1.5, 1, 1),
    (yesterday, "chilli", "fake2", "Toronto Maple Leafs", "Montreal Canadiens", "2026-01-01T00:00:00Z", "Toronto Maple Leafs voittaa", 1.6, 1, 0),
    (yesterday, "perus", "fake3", "Boston Bruins", "New York Rangers", "2026-01-01T00:00:00Z", "Boston Bruins voittaa", 1.7, 1, 1),
    (yesterday, "perus", "fake4", "Carolina Hurricanes", "Florida Panthers", "2026-01-01T00:00:00Z", "Carolina Hurricanes voittaa", 1.8, 1, 1),
    (yesterday, "testo", "fake5", "Vegas Golden Knights", "Chicago Blackhawks", "2026-01-01T00:00:00Z", "Vegas Golden Knights voittaa", 1.4, 1, 0),
]

for row in test_data:
    conn.execute("""
        INSERT INTO sent_picks
        (sent_date, tier, game_id, home_team, away_team, commence_time, pick_description, price, checked, hit)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, row)

conn.commit()
print(f"Lisätty {len(test_data)} testiriviä päivämäärälle {yesterday}")