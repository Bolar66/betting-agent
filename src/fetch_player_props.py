import os
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ODDS_API_KEY")
BASE_URL = "https://api.the-odds-api.com/v4"

# Nämä markkinan nimet pitää VARMISTAA The Odds API:n dokumentaatiosta
# (https://the-odds-api.com/sports-odds-data/betting-markets.html)
# ennen kuin luotetaan niihin sokeasti - en pysty tarkistamaan tätä
# ilman API-kutsua, joten testataan ja korjataan jos 422/400 tulee.
PLAYER_PROP_MARKETS = "player_goal_scorer_anytime,player_points"


def get_player_props(event_id):
    """Hakee yhden ottelun pelaajakohtaiset propsit."""
    url = f"{BASE_URL}/sports/icehockey_nhl/events/{event_id}/odds"
    params = {
        "apiKey": API_KEY,
        "regions": "us",
        "markets": PLAYER_PROP_MARKETS,
        "oddsFormat": "decimal",
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    # Testaa yhdellä oikealla event_id:llä datastasi
    test_event_id = "9de33ce1013f2a3375dbded2c9fbc7d6"  # Carolina vs Florida
    data = get_player_props(test_event_id)

    for bookmaker in data.get("bookmakers", []):
        print(f"\n{bookmaker['title']}:")
        for market in bookmaker.get("markets", []):
            print(f"  Markkina: {market['key']}")
            for outcome in market.get("outcomes", []):
                name = outcome.get("name")
                desc = outcome.get("description", "")
                price = outcome.get("price")
                print(f"    {desc} {name}: {price}")

if __name__ == "__main__":
    test_event_id = "9de33ce1013f2a3375dbded2c9fbc7d6"
    data = get_player_props(test_event_id)

    print("RAAKA VASTAUS:")
    print(data)
    print()

    for bookmaker in data.get("bookmakers", []):
        print(f"\n{bookmaker['title']}:")
        for market in bookmaker.get("markets", []):
            print(f"  Markkina: {market['key']}")
            for outcome in market.get("outcomes", []):
                name = outcome.get("name")
                desc = outcome.get("description", "")
                price = outcome.get("price")
                print(f"    {desc} {name}: {price}")