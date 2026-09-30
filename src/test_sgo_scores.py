import json

with open("sgo_results_sample.json", encoding="utf-8") as f:
    events = json.load(f)

ev = events[0]
home = ev["teams"]["home"]["names"]["long"]
away = ev["teams"]["away"]["names"]["long"]
odds = ev.get("odds") or {}
print(f"{home} vs {away} | kertoimia: {len(odds)}")

if odds:
    sample = next(iter(odds.values()))
    print("Kertoimen avaimet:", list(sample.keys()))

with_score = [v for v in odds.values() if "score" in v]
print(f"\nKertoimia, joissa score-kenttä: {len(with_score)} / {len(odds)}")

team_ids = {"home", "away", "all"}
players = [v for v in with_score if v.get("statEntityID") not in team_ids]
print(f"Näistä pelaajavetoja: {len(players)}")
for v in players[:8]:
    print(f"  {v['oddID']} -> score {v.get('score')} | scoringSupported {v.get('scoringSupported')}")