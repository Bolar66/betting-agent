import requests


def get_player_game_log(player_id, season="20242025", game_type=2):
    """Hakee pelaajan ottelukohtaiset tilastot."""
    url = f"https://api-web.nhle.com/v1/player/{player_id}/game-log/{season}/{game_type}"
    response = requests.get(url)
    response.raise_for_status()
    return response.json()


def calculate_scoring_rates(game_log, last_n_games=10):
    """Laskee pelaajan maali- ja pistetodennäköisyyden viimeisimmistä
    N ottelusta."""
    games = game_log.get("gameLog", [])[:last_n_games]
    if not games:
        return None

    games_with_goal = sum(1 for g in games if g.get("goals", 0) > 0)
    games_with_point = sum(1 for g in games if g.get("points", 0) > 0)

    return {
        "goal_rate": games_with_goal / len(games),
        "point_rate": games_with_point / len(games),
        "sample_size": len(games),
    }


def get_team_summary_stats(season="20242025"):
    """Hakee kaikkien joukkueiden tilastot yhdeltä kaudelta,
    raakana - tutkitaan ensin mitä kenttiä data sisältää."""
    url = "https://api.nhle.com/stats/rest/en/team/summary"
    params = {"cayenneExp": f"seasonId={season} and gameTypeId=2"}
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()


def calculate_edge(model_probability, bookmaker_odds):
    """Laskee eron oman arvion ja bookmakerin implisiittisen
    todennäköisyyden välillä."""
    implied_prob = 1 / bookmaker_odds
    return model_probability - implied_prob


if __name__ == "__main__":
    # 1. Testataan pelaajan oma muoto
    mcdavid_id = 8478402
    log = get_player_game_log(mcdavid_id)
    rates = calculate_scoring_rates(log)
    print(f"McDavid - maalintekoprosentti: {rates['goal_rate']*100:.1f}%")
    print(f"McDavid - pistetekoprosentti: {rates['point_rate']*100:.1f}%")

    # 2. Testataan joukkuetilastot - katsotaan raakadata ensin
    print("\n--- Joukkuetilastot (raakadata, 1. rivi) ---")
    team_data = get_team_summary_stats()
    if team_data.get("data"):
        print(team_data["data"][0])
        print(f"\nYhteensä {len(team_data['data'])} joukkuetta löytyi.")
    else:
        print("Ei dataa löytynyt - tarkistetaan API-kutsu.")