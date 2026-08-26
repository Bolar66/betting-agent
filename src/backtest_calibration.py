import math
import pandas as pd
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.model_selection import TimeSeriesSplit
from win_model import get_connection


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


def load_games():
    conn = get_connection()
    cursor = conn.execute("""
        SELECT game_id, home_team, away_team, home_score, away_score, home_win
        FROM historical_games ORDER BY date ASC
    """)
    rows = cursor.fetchall()
    columns = ["game_id", "home_team", "away_team", "home_score", "away_score", "home_win"]
    return pd.DataFrame(rows, columns=columns)


def compute_poisson_features(df):
    """Sama piirrelaskenta kuin aiemmin - vain poisson_prob, koska
    se osoittautui parhaaksi yksittäiseksi piirteeksi."""
    home_history, away_history = {}, {}
    league_avg_goals = (df["home_score"].sum() + df["away_score"].sum()) / (len(df) * 2)

    poisson_probs, outcomes, game_ids = [], [], []
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
            game_ids.append(row["game_id"])

        home_history.setdefault(home, []).append((row["home_score"], row["away_score"]))
        away_history.setdefault(away, []).append((row["away_score"], row["home_score"]))

    return pd.DataFrame({
        "game_id": game_ids,
        "poisson_prob": poisson_probs,
        "home_win": outcomes,
    })


def get_out_of_fold_predictions(feature_df):
    """Kriittinen kohta: käytetään VAIN out-of-fold-ennusteita, eli
    jokainen ennuste on tehty mallilla joka EI ole nähnyt kyseistä
    ottelua koulutuksessa. Tämä on ainoa rehellinen tapa arvioida
    kalibrointia - muuten malli 'huijaisi' arviossa."""
    X = feature_df[["poisson_prob"]]
    y = feature_df["home_win"]

    tscv = TimeSeriesSplit(n_splits=5)
    all_predictions = []
    all_actuals = []

    for train_idx, test_idx in tscv.split(X):
        X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
        y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]

        model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000))
        model.fit(X_train, y_train)
        preds = model.predict_proba(X_test)[:, 1]

        all_predictions.extend(preds)
        all_actuals.extend(y_test.values)

    return np.array(all_predictions), np.array(all_actuals)


def reliability_check(predictions, actuals, n_bins=10):
    """Jakaa ennusteet koreihin ja vertaa ennustettua todennäköisyyttä
    toteutuneeseen voitto-osuuteen kussakin korissa."""
    bins = np.linspace(0, 1, n_bins + 1)
    print(f"{'Kori':>15} | {'N':>5} | {'Ennustettu ka.':>15} | {'Toteutunut %':>13}")
    print("-" * 60)

    for i in range(n_bins):
        lo, hi = bins[i], bins[i + 1]
        mask = (predictions >= lo) & (predictions < hi)
        if mask.sum() == 0:
            continue
        avg_pred = predictions[mask].mean()
        actual_rate = actuals[mask].mean()
        n = mask.sum()
        print(f"{lo:.2f}-{hi:.2f}      | {n:>5} | {avg_pred:>15.3f} | {actual_rate:>13.3f}")


if __name__ == "__main__":
    print("Ladataan dataa ja lasketaan piirteet...")
    df = load_games()
    feature_df = compute_poisson_features(df)
    print(f"Käyttökelpoisia otteluita: {len(feature_df)}\n")

    print("Lasketaan out-of-fold-ennusteet (rehellinen, ei tietovuotoa)...")
    predictions, actuals = get_out_of_fold_predictions(feature_df)

    print("\n--- Luotettavuuskäyrä (Reliability Check) ---")
    print("Jos malli on hyvin kalibroitu, 'Ennustettu ka.' ja")
    print("'Toteutunut %' pitäisi olla lähellä toisiaan joka korissa.\n")
    reliability_check(predictions, actuals)