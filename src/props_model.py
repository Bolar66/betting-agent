import requests

# TODO: nämä ID:t pitää hakea/varmistaa oikeiksi ennen kuin niitä käytetään
# tuotannossa. Vain McDavid on tässä varmistettu toimivaksi tätä projektia
# varten aiemmin testatulla ID:llä.
KNOWN_PLAYER_IDS = {
    "Connor McDavid": 8478402,
}

LEAGUE_AVG_ANYTIME_GOAL_RATE = 0.35  # karkea NHL-liigakeskiarvo, tarkennettavissa
SHRINKAGE_PSEUDO_COUNT = 8  # kuinka voimakkaasti kutistetaan kohti keskiarvoa


def get_player_game_log(player_id, season, game_type=2):
    url = f"https://api-web.nhle.com/v1/player/{player_id}/game-log/{season}/{game_type}"
    response = requests.get(url)
    response.raise_for_status()
    return response.json()


def calculate_calibrated_goal_probability(player_id, season, last_n=10):
    """Laskee kalibroidun maalintekotodennäköisyyden empiirisellä
    Bayes-kutistuksella kohti liigan keskiarvoa - tämä on kevyempi
    kalibrointi kuin h2h-mallin logistinen regressio, koska yksittäisen
    pelaajan otoskoko on aina pieni. Pienellä otoksella (esim. 3 ottelua)
    tulos kutistuu voimakkaasti kohti keskiarvoa; suurella otoksella
    (esim. 40 ottelua) pelaajan oma taso hallitsee."""
    log = get_player_game_log(player_id, season)
    games = log.get("gameLog", [])[:last_n]

    if not games:
        return LEAGUE_AVG_ANYTIME_GOAL_RATE, 0

    goals_games = sum(1 for g in games if g.get("goals", 0) > 0)
    n = len(games)

    shrunk_rate = (
        goals_games + SHRINKAGE_PSEUDO_COUNT * LEAGUE_AVG_ANYTIME_GOAL_RATE
    ) / (n + SHRINKAGE_PSEUDO_COUNT)

    return shrunk_rate, n


def calculate_prop_edge(model_probability, bookmaker_odds):
    implied_prob = 1 / bookmaker_odds
    return model_probability - implied_prob


def build_prop_rationale(player_name, model_prob, bookmaker_odds, sample_size):
    implied_prob = 1 / bookmaker_odds
    edge = model_prob - implied_prob
    return (
        f"{player_name}: kalibroitu arvio {model_prob*100:.0f}% "
        f"(otos {sample_size} ottelua, kutistettu liigakeskiarvoa kohti), "
        f"bookmaker implikoi {implied_prob*100:.0f}%. Edge: {edge*100:+.0f}pp."
    )