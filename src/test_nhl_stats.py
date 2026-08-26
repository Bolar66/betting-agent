import requests

def get_player_game_log(player_id, season="20242025", game_type=2):
    """Hakee pelaajan ottelukohtaiset tilastot.
    game_type: 2=runkosarja, 3=playoffit."""
    url = f"https://api-web.nhle.com/v1/player/{player_id}/game-log/{season}/{game_type}"
    response = requests.get(url)
    response.raise_for_status()
    return response.json()


def calculate_goal_rate(game_log, last_n_games=10):
    """Laskee kuinka suuressa osassa viimeisimmistä N ottelusta
    pelaaja teki maalin."""
    games = game_log.get("gameLog", [])[:last_n_games]
    if not games:
        return None
    games_with_goal = sum(1 for g in games if g.get("goals", 0) > 0)
    return games_with_goal / len(games), len(games)


if __name__ == "__main__":
    mcdavid_id = 8478402
    log = get_player_game_log(mcdavid_id)

    print(f"Otteluita löytyi: {len(log.get('gameLog', []))}")

    result = calculate_goal_rate(log)
    if result:
        rate, n = result
        print(f"McDavid teki maalin {rate*100:.1f}% viimeisimmistä {n} ottelusta")
    else:
        print("Ei löytynyt otteludataa.")

    # Tulostetaan myös raaka data ensimmäisestä ottelusta, jotta nähdään
    # mitä kenttiä on tarjolla jatkokehitystä varten
    if log.get("gameLog"):
        print("\nEsimerkkiottelu (raakadata):")
        print(log["gameLog"][0])