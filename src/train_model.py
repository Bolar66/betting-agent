import os
import math
import libsql
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import accuracy_score, brier_score_loss

load_dotenv()

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

RECENT_FORM_WINDOW = 10


def get_connection():
    return libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)


def load_games():
    conn = get_connection()
    cursor = conn.execute("""
        SELECT game_id, season, date, home_team, away_team,
               home_score, away_score, home_win
        FROM historical_games
        ORDER BY date ASC
    """)
    rows = cursor.fetchall()
    columns = ["game_id", "season", "date", "home_team", "away_team",
               "home_score", "away_score", "home_win"]
    return pd.DataFrame(rows, columns=columns)


def poisson_pmf(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def poisson_home_win_prob(lambda_home, lambda_away, max_goals=10):
    home_win, away_win, tie = 0.0, 0.0, 0.0
    for i in range(max_goals + 1):
        p_i = poisson_pmf(i, lambda_home)
        for j in range(max_goals + 1):
            p_j = poisson_pmf(j, lambda_away)
            p = p_i * p_j
            if i > j:
                home_win += p
            elif i < j:
                away_win += p
            else:
                tie += p
    return home_win + tie * 0.5


def compute_features(df):
    """Laskee piirteet, mukaan lukien form_delta (viimeaikaisen muodon
    JA pitkän aikavälin keskiarvon EROTUS, ei molempia raakana)."""
    home_history = {}
    away_history = {}
    all_history = {}

    league_avg_goals = (df["home_score"].sum() + df["away_score"].sum()) / (len(df) * 2)

    features = []
    for _, row in df.iterrows():
        home, away = row["home_team"], row["away_team"]

        home_home = home_history.get(home, [])
        away_away = away_history.get(away, [])
        home_all = all_history.get(home, [])
        away_all = all_history.get(away, [])

        has_enough_data = (
            len(home_home) >= 3 and len(away_away) >= 3
            and len(home_all) >= RECENT_FORM_WINDOW
            and len(away_all) >= RECENT_FORM_WINDOW
        )

        if has_enough_data:
            home_gf = np.mean([g[0] for g in home_home])
            home_ga = np.mean([g[1] for g in home_home])
            away_gf = np.mean([g[0] for g in away_away])
            away_ga = np.mean([g[1] for g in away_away])

            home_recent = home_all[-RECENT_FORM_WINDOW:]
            away_recent = away_all[-RECENT_FORM_WINDOW:]
            home_recent_gf = np.mean([g[0] for g in home_recent])
            away_recent_gf = np.mean([g[0] for g in away_recent])

            # Form delta: positiivinen = joukkue on kuumana juuri nyt
            # verrattuna omaan pitkän aikavälin tasoonsa
            home_form_delta = home_recent_gf - home_gf
            away_form_delta = away_recent_gf - away_gf

            attack_home = home_gf / league_avg_goals
            defense_away = away_ga / league_avg_goals
            attack_away = away_gf / league_avg_goals
            defense_home = home_ga / league_avg_goals

            lambda_home = league_avg_goals * attack_home * defense_away
            lambda_away = league_avg_goals * attack_away * defense_home
            poisson_prob = poisson_home_win_prob(lambda_home, lambda_away)

            features.append({
                "game_id": row["game_id"],
                "home_gf_at_home": home_gf,
                "home_ga_at_home": home_ga,
                "away_gf_at_away": away_gf,
                "away_ga_at_away": away_ga,
                "home_form_delta": home_form_delta,
                "away_form_delta": away_form_delta,
                "poisson_home_win_prob": poisson_prob,
                "home_win": row["home_win"],
            })

        home_history.setdefault(home, []).append((row["home_score"], row["away_score"]))
        away_history.setdefault(away, []).append((row["away_score"], row["home_score"]))
        all_history.setdefault(home, []).append((row["home_score"], row["away_score"]))
        all_history.setdefault(away, []).append((row["away_score"], row["home_score"]))

    return pd.DataFrame(features)


def evaluate_model(model, X, y, model_name, use_scaling=True):
    """Ristiinvalidoi mallin, valinnaisesti StandardScaler-pipelinella."""
    if use_scaling:
        pipeline = make_pipeline(StandardScaler(), model)
    else:
        pipeline = model

    tscv = TimeSeriesSplit(n_splits=5)
    accuracies, briers = [], []

    for train_idx, test_idx in tscv.split(X):
        X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
        y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]

        pipeline.fit(X_train, y_train)
        y_pred = pipeline.predict(X_test)
        y_pred_prob = pipeline.predict_proba(X_test)[:, 1]

        accuracies.append(accuracy_score(y_test, y_pred))
        briers.append(brier_score_loss(y_test, y_pred_prob))

    mean_acc = np.mean(accuracies)
    mean_brier = np.mean(briers)
    print(f"{model_name}: tarkkuus {mean_acc*100:.1f}% "
          f"(hajonta {np.std(accuracies)*100:.1f}pp), brier {mean_brier:.4f}")
    return mean_acc, mean_brier


if __name__ == "__main__":
    print("Ladataan otteludata...")
    df = load_games()
    print(f"Ladattu {len(df)} ottelua.")

    print(f"Lasketaan piirteet (koti/vieras + form_delta + Poisson)...")
    feature_df = compute_features(df)
    print(f"Käyttökelpoisia otteluita: {len(feature_df)}\n")

    all_features = [
        "home_gf_at_home", "home_ga_at_home",
        "away_gf_at_away", "away_ga_at_away",
        "home_form_delta", "away_form_delta",
        "poisson_home_win_prob",
    ]
    y = feature_df["home_win"]

    print("--- Eri piirreyhdistelmät (skaalattuna, C=1.0) ---")
    combos = {
        "Vain koti/vieras": ["home_gf_at_home", "home_ga_at_home", "away_gf_at_away", "away_ga_at_away"],
        "Koti/vieras + form_delta": ["home_gf_at_home", "home_ga_at_home", "away_gf_at_away",
                                       "away_ga_at_away", "home_form_delta", "away_form_delta"],
        "Koti/vieras + Poisson": ["home_gf_at_home", "home_ga_at_home", "away_gf_at_away",
                                    "away_ga_at_away", "poisson_home_win_prob"],
        "Kaikki piirteet": all_features,
        "Pelkkä Poisson-piirre": ["poisson_home_win_prob"],
    }

    best_combo_name = None
    best_acc = 0
    for name, cols in combos.items():
        X = feature_df[cols]
        acc, _ = evaluate_model(LogisticRegression(max_iter=1000), X, y, name)
        if acc > best_acc:
            best_acc = acc
            best_combo_name = name

    print(f"\nParas yhdistelmä: '{best_combo_name}' ({best_acc*100:.1f}%)")

    print("\n--- C-parametrin haku parhaalla piirreyhdistelmällä ---")
    X_best = feature_df[combos[best_combo_name]]
    for C in [0.01, 0.1, 1.0, 10.0, 100.0]:
        evaluate_model(LogisticRegression(max_iter=1000, C=C), X_best, y, f"C={C}")

    print("\n--- Vertailu: pelkkä Poisson-malli (ei ML:ää) ---")
    poisson_pred = (feature_df["poisson_home_win_prob"] > 0.5).astype(int)
    poisson_acc = accuracy_score(y, poisson_pred)
    poisson_brier = brier_score_loss(y, feature_df["poisson_home_win_prob"])
    print(f"Tarkkuus: {poisson_acc*100:.1f}%, Brier: {poisson_brier:.4f}")