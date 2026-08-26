import os
import math
import libsql
import requests
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline

load_dotenv()

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

RECENT_FORM_WINDOW = 10


def get_connection():
    return libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)


def poisson_pmf(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def poisson_home_win_prob(lambda_home, lambda_away, max_goals=10):
    """Poisson-mallin raaka kotivoitto-todennäköisyys (tasapeli 50/50,
    koska NHL ei koskaan pääty tasapeliin)."""
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
        FROM historical_games
        ORDER BY date ASC
    """)
    rows = cursor.fetchall()
    columns = ["game_id", "home_team", "away_team", "home_score", "away_score", "home_win"]
    return pd.DataFrame(rows, columns=columns)


def train_calibration_model():
    """Kouluttaa kevyen kalibrointimallin: syötteenä Poisson-mallin
    raaka ennuste, tavoitteena toteutunut lopputulos. Koulutetaan
    joka ajolla historiadatasta - kevyt operaatio, ei vaadi
    mallitiedoston tallennusta."""
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


def get_current_team_ratings(season="20242025"):
    """Hakee tuoreimman saatavilla olevan kauden joukkuevahvuudet
    (hyökkäys/puolustus liigan keskiarvoon suhteutettuna)."""
    url = "https://api.nhle.com/stats/rest/en/team/summary"
    params = {"cayenneExp": f"seasonId={season} and gameTypeId=2"}
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


def predict_win_probability(home_team, away_team, ratings, league_avg_goals, calibration_model):
    """Palauttaa kalibroidun kotivoitto-todennäköisyyden annetulle
    ottelulle. Vaatii team-nimien täsmäävän ratings-dictin avaimiin
    (samat kuin NHL:n team/summary-endpointin teamFullName)."""
    home = ratings[home_team]
    away = ratings[away_team]

    lambda_home = league_avg_goals * (home["gf"] / league_avg_goals) * (away["ga"] / league_avg_goals)
    lambda_away = league_avg_goals * (away["gf"] / league_avg_goals) * (home["ga"] / league_avg_goals)

    raw_prob = poisson_home_win_prob(lambda_home, lambda_away)

    X_pred = pd.DataFrame({"poisson_prob": [raw_prob]})
    calibrated_prob = calibration_model.predict_proba(X_pred)[0, 1]

    return calibrated_prob, raw_prob, lambda_home, lambda_away


def calculate_edge(model_probability, bookmaker_odds):
    """Ero oman (kalibroidun) arvion ja bookmakerin implisiittisen
    todennäköisyyden välillä."""
    implied_prob = 1 / bookmaker_odds
    return model_probability - implied_prob


def build_h2h_rationale(team_name, calibrated_prob, bookmaker_odds, lambda_for, lambda_against):
    """Sanallinen perustelu h2h-vedolle."""
    implied_prob = 1 / bookmaker_odds
    edge = calibrated_prob - implied_prob
    return (
        f"{team_name}: malli ennustaa {calibrated_prob*100:.0f}% voittotodennäköisyyden "
        f"(odotetut maalit {lambda_for:.1f}-{lambda_against:.1f}), "
        f"bookmakerin kerroin implikoi {implied_prob*100:.0f}%. Edge: {edge*100:+.0f}pp."
    )


if __name__ == "__main__":
    print("Koulutetaan kalibrointimalli historiadatasta...")
    calibration_model = train_calibration_model()
    print("Valmis.")

    print("\nHaetaan tuoreimmat joukkuevahvuudet...")
    ratings, league_avg = get_current_team_ratings()
    print(f"Liigan keskiarvo: {league_avg:.2f} maalia/ottelu")

    home, away = "Edmonton Oilers", "Vancouver Canucks"
    calibrated, raw, lam_home, lam_away = predict_win_probability(
        home, away, ratings, league_avg, calibration_model
    )

    print(f"\n{home} vs {away}")
    print(f"Odotetut maalit: {lam_home:.2f} - {lam_away:.2f}")
    print(f"Raaka Poisson-todennäköisyys: {raw*100:.1f}%")
    print(f"Kalibroitu todennäköisyys: {calibrated*100:.1f}%")

    example_odds = 1.37
    print(f"\n{build_h2h_rationale(home, calibrated, example_odds, lam_home, lam_away)}")