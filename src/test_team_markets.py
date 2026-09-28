import os
import requests
import libsql
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ODDS_API_KEY")
BASE_URL = "https://api.the-odds-api.com/v4"

conn = libsql.connect(os.getenv("TURSO_DATABASE_URL"), auth_token=os.getenv("TURSO_AUTH_TOKEN"))
cursor = conn.execute("""
    SELECT game_id, home_team, away_team FROM odds_snapshots
    ORDER BY commence_time ASC LIMIT 1
""")
game_id, home, away = cursor.fetchone()
print(f"Testataan ottelulla: {home} vs {away}\n")

markets = ["spreads", "totals", "team_totals", "alternate_spreads", "alternate_totals"]

for market in markets:
    url = f"{BASE_URL}/sports/icehockey_nhl/events/{game_id}/odds"
    params = {"apiKey": API_KEY, "regions": "us", "markets": market, "oddsFormat": "decimal"}
    r = requests.get(url, params=params)
    print(f"--- {market} -> HTTP {r.status_code} ---")
    if r.status_code != 200:
        print(r.text[:200])
        continue
    books = r.json().get("bookmakers", [])
    print(f"Bookmakereita: {len(books)}")
    for b in books[:1]:
        for m in b.get("markets", []):
            print(f"  {b['title']} / {m['key']}: {len(m['outcomes'])} outcomea")
            for o in m["outcomes"][:4]:
                print(f"    {o}")
    print("Krediittejä jäljellä:", r.headers.get("x-requests-remaining"))
    print()