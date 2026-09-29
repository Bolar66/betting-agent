import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("SGO_API_KEY")
BASE = "https://api.sportsgameodds.com/v2"

r = requests.get(
    f"{BASE}/events",
    params={"apiKey": API_KEY, "leagueID": "NHL", "oddsAvailable": "true", "limit": 3},
    timeout=30,
)
print("HTTP", r.status_code)
if r.status_code != 200:
    print(r.text[:500])
    raise SystemExit

data = r.json()
with open("sgo_sample.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print("Koko vastaus tallennettu: sgo_sample.json")

print("Ylätason avaimet:", list(data.keys()))
events = data.get("data", [])
print("Otteluita:", len(events))

if events:
    ev = events[0]
    print("\nOttelun avaimet:", list(ev.keys()))
    odds = ev.get("odds")
    if isinstance(odds, dict):
        keys = list(odds.keys())
        print(f"Kertoimia: {len(keys)}")
        print("Esimerkkiavaimia:", keys[:15])
        print("\nEsimerkki yhdestä kertoimesta:")
        print(json.dumps(odds[keys[0]], ensure_ascii=False, indent=2)[:1500])
    else:
        print("odds-kentän tyyppi:", type(odds))

u = requests.get(f"{BASE}/account/usage", params={"apiKey": API_KEY}, timeout=30)
print("\n--- Käyttö ---")
print("HTTP", u.status_code)
print(u.text[:800])