import os
import requests
import libsql
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ODDS_API_KEY")
BASE_URL = "https://api.the-odds-api.com/v4"

conn = libsql.connect(os.getenv("TURSO_DATABASE_URL"), auth_token=os.getenv("TURSO_AUTH_TOKEN"))
cursor = conn.execute("""
    SELECT DISTINCT game_id, home_team, away_team, commence_time
    FROM odds_snapshots
    ORDER BY commence_time ASC
""")
events = cursor.fetchall()[:3]  # vain 3 ensimmäistä, säästetään krediittejä

for game_id, home, away, commence in events:
    url = f"{BASE_URL}/sports/icehockey_nhl/events/{game_id}/odds"
    params = {
        "apiKey": API_KEY,
        "regions": "us",
        "markets": "player_goal_scorer_anytime,player_points",
        "oddsFormat": "decimal",
    }
    r = requests.get(url, params=params)
    print(f"\n{home} vs {away} ({commence}) -> HTTP {r.status_code}")
    if r.status_code != 200:
        print(r.text[:200])
        continue
    data = r.json()
    books = data.get("bookmakers", [])
    print(f"Bookmakereita: {len(books)}")
    for b in books[:1]:
        for m in b.get("markets", []):
            print(f"  {b['title']} / {m['key']}: {len(m['outcomes'])} outcomea")
            for o in m["outcomes"][:3]:
                print(f"    {o}")
    print("Krediittejä jäljellä:", r.headers.get("x-requests-remaining"))