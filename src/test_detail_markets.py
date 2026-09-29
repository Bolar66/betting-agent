import os
import requests
import libsql
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ODDS_API_KEY")
BASE_URL = "https://api.the-odds-api.com/v4"

conn = libsql.connect(os.getenv("TURSO_DATABASE_URL"), auth_token=os.getenv("TURSO_AUTH_TOKEN"))
now = datetime.now(timezone.utc).isoformat()
row = conn.execute("""
    SELECT game_id, home_team, away_team, commence_time FROM odds_snapshots
    WHERE commence_time > ? ORDER BY commence_time ASC LIMIT 1
""", (now,)).fetchone()
game_id, home, away, commence = row
print(f"Testataan: {home} vs {away} ({commence})\n")

markets = ["player_points", "player_shots_on_goal", "player_assists", "player_total_saves"]

for market in markets:
    r = requests.get(
        f"{BASE_URL}/sports/icehockey_nhl/events/{game_id}/odds",
        params={"apiKey": API_KEY, "regions": "us", "markets": market, "oddsFormat": "decimal"},
        timeout=30,
    )
    print(f"--- {market} -> HTTP {r.status_code} ---")
    if r.status_code != 200:
        print(r.text[:200])
        continue
    books = r.json().get("bookmakers", [])
    print(f"Kirjoja: {len(books)} -> {[b['title'] for b in books]}")
    for b in books[:1]:
        for m in b.get("markets", []):
            print(f"  {b['title']} / {m['key']}: {len(m['outcomes'])} outcomea")
            for o in m["outcomes"][:4]:
                print(f"    {o}")
    print("Krediittejä jäljellä:", r.headers.get("x-requests-remaining"), "\n")