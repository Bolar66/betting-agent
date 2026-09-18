import os
import math
import libsql
import requests
import pandas as pd
import numpy as np
from datetime import datetime
from dotenv import load_dotenv
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline

load_dotenv()

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

RECENT_FORM_WINDOW = 10

# Staattinen kartta joukkueiden täysistä nimistä (Odds API / NHL team/summary
# -muoto) lyhenteisiin (NHL:n schedule-endpointin vaatima muoto).
# Tämä on vakaata, yleisesti tiedossa olevaa dataa, ei riipu mistään APIsta.
TEAM_ABBREV_MAP = {
    "Anaheim Ducks": "ANA", "Boston Bruins": "BOS", "Buffalo Sabres": "BUF",
    "Calgary Flames": "CGY", "Carolina Hurricanes": "CAR", "Chicago Blackhawks": "CHI",
    "Colorado Avalanche": "COL", "Columbus Blue Jackets": "CBJ", "Dallas Stars": "DAL",
    "Detroit Red Wings": "DET", "Edmonton Oilers": "EDM", "Florida Panthers": "FLA",
    "Los Angeles Kings": "LAK", "Minnesota Wild": "MIN", "Montréal Canadiens": "MTL",
    "Montreal Canadiens": "MTL", "Nashville Predators": "NSH", "New Jersey Devils": "NJD",
    "New York Islanders": "NYI", "New York Rangers": "NYR", "Ottawa Senators": "OTT",
    "Philadelphia Flyers": "PHI", "Pittsburgh Penguins": "PIT", "San Jose Sharks": "SJS",
    "Seattle Kraken": "SEA", "St Louis Blues": "STL", "St. Louis Blues": "STL",
    "Tampa Bay Lightning": "TBL", "Toronto Maple Leafs": "TOR", "Utah Mammoth": "UTA",
    "Vancouver Canucks": "VAN", "Vegas Golden Knights": "VGK", "Washington Capitals": "WSH",
    "Winnipeg Jets": "WPG",
}


def get_connection():
    return libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)


def get_current_nhl_season():
    """Laskee kuluvan NHL-kauden tunnisteen automaattisesti nykyhetkestä.
    NHL-kausi alkaa syys-lokakuussa: heinäkuu-joulukuu tarkoittaa että
    kausi alkoi tänä vuonna, tammikuu-kesäkuu että se alkoi edellisenä
    vuonna."""
    now = datetime.now()
    start_year = now.year if now.month >= 7 else now.year - 1
    return f"{start_year}{start_year + 1}"


def poisson_pmf(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def poisson_home_win_prob(lambda_home, lambda_away, max_goals=10):
    home_win, tie = 0.0, 0.0
    for i in range(max_goals + 1):
        p_i = poisson_pmf(i, lambda_home)
        for j in range(max_goals + 1):
            p_j = poisson_pmf(j, lambda_away)
            p = p_i * p_j
            if i > j:
                home_win += p
            elif i == j:
                tie += p
    return home_win + tie * 0.5


def _load_historical_games():
    conn = get_connection()
    cursor = conn.execute("""
        SELECT game_id, home_team, away_team, home_score, away_score, home_win
        FROM historical_games ORDER BY date ASC
    """)
    rows = cursor.fetchall()
    columns = ["game_id", "home_team", "away_team", "home_score", "away_score", "home_win"]
    return pd.DataFrame(rows, columns=columns)


def train_calibration_model():
    """Kouluttaa kevyen kalibrointimallin: syötteenä Poisson-mallin
    raaka ennuste, tavoitteena toteutunut lopputulos."""
    df = _load_historical_games()

    home_history, away_history = {}, {}
    league_avg_goals = (df["home_score"].sum() + df["away_score"].sum()) / (len(df) * 2)

    poisson_probs, outcomes = [], []
    for _, row in df.iterrows():
        home, away = row["home_team"], row["away_team"]
        home_home = home_history.get(home, [])
        away_away = away_history.get(away, [])

        if len(home_home) >= 3 and len(away_away) >= 3:
            home_gf = np.mean([g[0] for g in home_home])
            home_ga = np.mean([g[1] for g in home_home])
            away_gf = np.mean([g[0] for g in away_away])
            away_ga = np.mean([g[1] for g in away_away])

            lambda_home = league_avg_goals * (home_gf / league_avg_goals) * (away_ga / league_avg_goals)
            lambda_away = league_avg_goals * (away_gf / league_avg_goals) * (home_ga / league_avg_goals)
            prob = poisson_home_win_prob(lambda_home, lambda_away)

            poisson_probs.append(prob)
            outcomes.append(row["home_win"])

        home_history.setdefault(home, []).append((row["home_score"], row["away_score"]))
        away_history.setdefault(away, []).append((row["away_score"], row["home_score"]))

    X = pd.DataFrame({"poisson_prob": poisson_probs})
    y = pd.Series(outcomes)

    model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000))
    model.fit(X, y)
    return model


def get_current_team_ratings(season=None):
    """Hakee kuluvan (tai annetun) kauden joukkuevahvuudet.
    Jos season on None, käytetään get_current_nhl_season()-funktiota."""
    if season is None:
        season = get_current_nhl_season()

    url = "https://api.nhle.com/stats/rest/en/team/summary"
    params = {"cayenneExp": f"seasonId={season} and gameTypeId=2"}
    response = requests.get(url, params=params)
    response.raise_for_status()
    team_stats = response.json().get("data", [])

    if not team_stats:
        # Kuluvalta kaudelta ei vielä dataa (esim. kauden alussa) -
        # käytetään edellistä kautta fallbackina
        prev_start = int(season[:4]) - 1
        fallback_season = f"{prev_start}{prev_start + 1}"
        params = {"cayenneExp": f"seasonId={fallback_season} and gameTypeId=2"}
        response = requests.get(url, params=params)
        response.raise_for_status()
        team_stats = response.json().get("data", [])

    league_avg_gf = sum(t["goalsForPerGame"] for t in team_stats) / len(team_stats)
    league_avg_ga = sum(t["goalsAgainstPerGame"] for t in team_stats) / len(team_stats)

    ratings = {}
    for t in team_stats:
        ratings[t["teamFullName"]] = {
            "gf": t["goalsForPerGame"],
            "ga": t["goalsAgainstPerGame"],
        }
    return ratings, league_avg_gf


_schedule_cache = {}


def get_rest_days(team_full_name, game_date, season):
    """Laskee montako päivää edellisestä ottelusta on kulunut annettuun
    peliin mennessä. Palauttaa None jos ei löydy dataa (esim. kauden
    ensimmäinen ottelu tai tuntematon joukkue)."""
    abbrev = TEAM_ABBREV_MAP.get(team_full_name)
    if abbrev is None:
        return None

    cache_key = f"{abbrev}_{season}"
    if cache_key not in _schedule_cache:
        url = f"https://api-web.nhle.com/v1/club-schedule-season/{abbrev}/{season}"
        try:
            response = requests.get(url)
            response.raise_for_status()
            _schedule_cache[cache_key] = response.json().get("games", [])
        except requests.exceptions.HTTPError:
            _schedule_cache[cache_key] = []

    games = _schedule_cache[cache_key]
    played = [g for g in games if g.get("gameState") == "OFF"]
    prior = [
        g for g in played
        if datetime.fromisoformat(g["gameDate"]).date() < game_date
    ]
    if not prior:
        return None

    last_game_date = max(datetime.fromisoformat(g["gameDate"]).date() for g in prior)
    return (game_date - last_game_date).days


def get_fatigue_multiplier(rest_days):
    """Heuristinen (ei datasta opittu) kerroin hyökkäysvoimaan lepopäivien
    perusteella. Back-to-back (0 päivää lepoa) heikentää hieman,
    pitkä lepo (3+ päivää) vahvistaa hieman. Nämä kertoimet ovat
    karkeita nyrkkisääntöjä, ei tilastollisesti sovitettuja - syytä
    tarkentaa kun omaa dataa kertyy."""
    if rest_days is None:
        return 1.0
    if rest_days == 0:
        return 0.95
    if rest_days >= 3:
        return 1.03
    return 1.0


def get_head_to_head_note(home_full, away_full, limit=10):
    """Hakee viimeisimmät kohtaamiset näiden joukkueiden väliltä
    historiadatasta. Palauttaa (home_team_wins, total_games) tai None.
    HUOM: käytetään vain rationale-tekstiin, EI muuteta mallin
    ennustetta tällä - pieni otoskoko (usein 2-6 ottelua) ei riitä
    luotettavaan mallin säätöön."""
    home_abbrev = TEAM_ABBREV_MAP.get(home_full)
    away_abbrev = TEAM_ABBREV_MAP.get(away_full)
    if not home_abbrev or not away_abbrev:
        return None

    conn = get_connection()
    cursor = conn.execute("""
        SELECT home_team, away_team, home_win FROM historical_games
        WHERE (home_team = ? AND away_team = ?) OR (home_team = ? AND away_team = ?)
        ORDER BY date DESC LIMIT ?
    """, (home_abbrev, away_abbrev, away_abbrev, home_abbrev, limit))
    rows = cursor.fetchall()
    if not rows:
        return None

    home_side_wins = sum(
        1 for h, a, hw in rows
        if (h == home_abbrev and hw == 1) or (h == away_abbrev and hw == 0)
    )
    return home_side_wins, len(rows)


def predict_win_probability(home_team, away_team, ratings, league_avg_goals,
                             calibration_model, game_date=None, season=None):
    """Palauttaa kalibroidun kotivoitto-todennäköisyyden. Sisältää nyt
    valinnaisen lepopäivä-/matkustuskorjauksen (game_date ja season
    annettuna), muuten toimii kuten ennen."""
    home = ratings[home_team]
    away = ratings[away_team]

    home_attack = home["gf"] / league_avg_goals
    away_defense = away["ga"] / league_avg_goals
    away_attack = away["gf"] / league_avg_goals
    home_defense = home["ga"] / league_avg_goals

    if game_date is not None and season is not None:
        home_rest = get_rest_days(home_team, game_date, season)
        away_rest = get_rest_days(away_team, game_date, season)
        home_attack *= get_fatigue_multiplier(home_rest)
        away_attack *= get_fatigue_multiplier(away_rest)

    lambda_home = league_avg_goals * home_attack * away_defense
    lambda_away = league_avg_goals * away_attack * home_defense

    raw_prob = poisson_home_win_prob(lambda_home, lambda_away)

    X_pred = pd.DataFrame({"poisson_prob": [raw_prob]})
    calibrated_prob = calibration_model.predict_proba(X_pred)[0, 1]

    return calibrated_prob, raw_prob, lambda_home, lambda_away


def calculate_edge(model_probability, bookmaker_odds):
    implied_prob = 1 / bookmaker_odds
    return model_probability - implied_prob


def build_h2h_rationale(team_name, calibrated_prob, bookmaker_odds, lambda_for, lambda_against, h2h_note=None):
    implied_prob = 1 / bookmaker_odds
    edge = calibrated_prob - implied_prob
    text = (
        f"{team_name}: malli ennustaa {calibrated_prob*100:.0f}% voittotodennäköisyyden "
        f"(odotetut maalit {lambda_for:.1f}-{lambda_against:.1f}), "
        f"bookmakerin kerroin implikoi {implied_prob*100:.0f}%. Edge: {edge*100:+.0f}pp."
    )
    if h2h_note is not None:
        wins, total = h2h_note
        text += f" Viim. {total} kohtaamista: {wins}/{total} kotijoukkueelle."
    return text