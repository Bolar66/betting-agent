import requests
import time

SEASONS = ["20222023", "20232024", "20242025"]  # 3 viime kautta


def get_all_team_abbrevs():
    """Hakee kaikkien NHL-joukkueiden lyhenteet."""
    url = "https://api-web.nhle.com/v1/standings/now"
    response = requests.get(url)
    response.raise_for_status()
    data = response.json()
    return [team["teamAbbrev"]["default"] for team in data.get("standings", [])]


def get_team_schedule(team_abbrev, season):
    """Hakee joukkueen koko kauden ottelut tuloksineen."""
    url = f"https://api-web.nhle.com/v1/club-schedule-season/{team_abbrev}/{season}"
    response = requests.get(url)
    response.raise_for_status()
    return response.json().get("games", [])


def extract_game_result(game):
    """Poimii yhden ottelun oleellisen datan raakavastauksesta.
    Palauttaa None jos ottelua ei ole vielä pelattu."""
    if game.get("gameState") != "OFF":
        return None

    home = game["homeTeam"]
    away = game["awayTeam"]

    return {
        "game_id": game["id"],
        "season": game["season"],
        "date": game["gameDate"],
        "home_team": home["abbrev"],
        "away_team": away["abbrev"],
        "home_score": home["score"],
        "away_score": away["score"],
        "home_win": 1 if home["score"] > away["score"] else 0,
        "went_to_ot_so": game.get("periodDescriptor", {}).get("periodType") != "REG",
    }


def collect_all_games(seasons):
    """Kerää kaikkien joukkueiden kaikki ottelut annetuilta kausilta,
    poistaen duplikaatit (sama ottelu näkyy sekä koti- että
    vierasjoukkueen aikataulussa)."""
    teams = get_all_team_abbrevs()
    seen_game_ids = set()
    all_games = []

    for season in seasons:
        print(f"Haetaan kausi {season}...")
        for team in teams:
            try:
                games = get_team_schedule(team, season)
            except requests.exceptions.HTTPError:
                print(f"  Ohitettu: {team} ({season}) - ei dataa")
                continue

            for game in games:
                result = extract_game_result(game)
                if result and result["game_id"] not in seen_game_ids:
                    seen_game_ids.add(result["game_id"])
                    all_games.append(result)

            time.sleep(0.1)  # kohtelias tauko API:lle

    return all_games


if __name__ == "__main__":
    games = collect_all_games(SEASONS)
    print(f"\nYhteensä {len(games)} uniikkia ottelua kerätty.")
    print("\nEsimerkki:")
    print(games[0])