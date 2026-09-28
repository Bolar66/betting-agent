import requests

TEAMS = ["BOS", "NYR", "VGK", "CHI", "OTT", "FLA", "CAR", "TOR"]
LOOK_FOR = ["Pavel Dorofeyev", "Brady Tkachuk", "Jack Eichel", "David Pastrnak"]

found = {name: [] for name in LOOK_FOR}

for ab in TEAMS:
    r = requests.get(f"https://api-web.nhle.com/v1/roster/{ab}/current", timeout=30)
    print(f"{ab}: HTTP {r.status_code}")
    if r.status_code != 200:
        continue
    data = r.json()
    print("  avaimet:", list(data.keys()))
    for group, players in data.items():
        if not isinstance(players, list):
            continue
        names = []
        for p in players:
            try:
                name = f"{p['firstName']['default']} {p['lastName']['default']}"
            except (KeyError, TypeError):
                continue
            names.append(name)
            if name in found:
                found[name].append(ab)
        print(f"  {group}: {len(players)} pelaajaa, esim. {names[:3]}")

print("\n--- Missä joukkueessa ---")
for name, teams in found.items():
    print(f"{name}: {teams or 'ei löytynyt näistä joukkueista'}")