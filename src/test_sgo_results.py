import os
import json
import requests
from datetime import timedelta
from dotenv import load_dotenv

from night_window import get_now_utc, get_next_night_window

load_dotenv()

API_KEY = os.getenv("SGO_API_KEY")
BASE = "https://api.sportsgameodds.com/v2"

start, end = get_next_night_window(get_now_utc() - timedelta(days=1))
print(f"Eilisen yön ikkuna: {start} - {end}")

r = requests.get(f"{BASE}/events", params={
    "apiKey": API_KEY, "leagueID": "NHL", "limit": 20,
    "startsAfter": start.isoformat(), "startsBefore": end.isoformat(),
}, timeout=60)
print("HTTP", r.status_code)
if r.status_code != 200:
    print(r.text[:500])
    raise SystemExit

events = r.json().get("data", [])
with open("sgo_results_sample.json", "w", encoding="utf-8") as f:
    json.dump(events, f, ensure_ascii=False, indent=2)
print(f"Otteluita: {len(events)} (tallennettu sgo_results_sample.json)")

if events:
    ev = events[0]
    print("\nstatus:", json.dumps(ev.get("status"), ensure_ascii=False)[:400])
    results = ev.get("results") or {}
    print("\nresults-avaimet (tasot):", list(results.keys())[:10])
    game = results.get("game", {})
    print("game-avaimet:", list(game.keys())[:12])
    for key in list(game.keys())[:3]:
        print(f"\n{key}:", json.dumps(game[key], ensure_ascii=False)[:500])