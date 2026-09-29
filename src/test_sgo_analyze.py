import json
from collections import Counter

with open("sgo_sample.json", encoding="utf-8") as f:
    data = json.load(f)

print("notice:", data.get("notice"))

ev = data["data"][0]
print("\nteams:", json.dumps(ev.get("teams"), ensure_ascii=False)[:600])

players = ev.get("players") or {}
print(f"\nPelaajia: {len(players)}")
for pid, p in list(players.items())[:2]:
    print(pid, json.dumps(p, ensure_ascii=False)[:300])

team_ids = {"home", "away", "all"}
player_odds = {k: v for k, v in ev["odds"].items() if v.get("statEntityID") not in team_ids}
print(f"\nPelaajakertoimia: {len(player_odds)} / {len(ev['odds'])}")

counts = Counter((v["statID"], v["betTypeID"], v["periodID"]) for v in player_odds.values())
print("\nPelaajamarkkinat (statID, betType, period): määrä")
for key, n in counts.most_common(25):
    print(f"  {key}: {n}")

def n_books(v):
    return sum(1 for b in (v.get("byBookmaker") or {}).values() if b.get("available"))

for stat in ("goals", "points", "shots_onGoal"):
    ex = next((v for v in player_odds.values() if v["statID"] == stat and v["periodID"] == "game"), None)
    if ex:
        print(f"\nEsimerkki ({stat}): {ex['oddID']} | {ex.get('marketName')} | "
              f"bookOdds {ex.get('bookOdds')} | fair {ex.get('fairOdds')} | "
              f"linja {ex.get('bookOverUnder')} | kirjoja {n_books(ex)}")