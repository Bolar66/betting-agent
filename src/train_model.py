import os
import libsql
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, brier_score_loss
    from sklearn.model_selection import TimeSeriesSplit, cross_val_score

load_dotenv()

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")


def get_connection():
    return libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)


def load_games():
    """Hakee kaikki historialliset ottelut aikajärjestyksessä."""
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


def compute_rolling_features(df):
    """Laskee jokaiselle ottelulle kummankin joukkueen hyökkäys-/
    puolustusvoiman ERIKSEEN koti- ja vierasotteluille, käyttäen
    VAIN sitä ennen pelattuja otteluita (ei tietovuotoa)."""
    home_history = {}  # team -> lista aiemmista kotiotteluista (gf, ga)
    away_history = {}  # team -> lista aiemmista vierasotteluista (gf, ga)

    features = []
    for _, row in df.iterrows():
        home, away = row["home_team"], row["away_team"]

        home_home_games = home_history.get(home, [])
        away_away_games = away_history.get(away, [])

        # Vaaditaan vähintään 3 aiempaa ottelua kummastakin (koti/vieras
        # erikseen, joten pienempi kynnys kuin ennen koska otoskoko on
        # puolet pienempi per kategoria)
        if len(home_home_games) >= 3 and len(away_away_games) >= 3:
            home_gf = np.mean([g[0] for g in home_home_games])
            home_ga = np.mean([g[1] for g in home_home_games])
            away_gf = np.mean([g[0] for g in away_away_games])
            away_ga = np.mean([g[1] for g in away_away_games])

            features.append({
                "game_id": row["game_id"],
                "home_gf_at_home": home_gf,
                "home_ga_at_home": home_ga,
                "away_gf_at_away": away_gf,
                "away_ga_at_away": away_ga,
                "home_win": row["home_win"],
            })

        # Päivitetään historia VASTA tämän ottelun käsittelyn jälkeen
        home_history.setdefault(home, []).append((row["home_score"], row["away_score"]))
        away_history.setdefault(away, []).append((row["away_score"], row["home_score"]))

    return pd.DataFrame(features)


if __name__ == "__main__":
    print("Ladataan otteludata...")
    df = load_games()
    print(f"Ladattu {len(df)} ottelua.")

    print("Lasketaan piirteet (ilman tietovuotoa)...")
    feature_df = compute_rolling_features(df)
    print(f"Käyttökelpoisia otteluita (>=5 aiempaa peliä molemmilla): {len(feature_df)}")

    X = feature_df[["home_gf_avg", "home_ga_avg", "away_gf_avg", "away_ga_avg"]]
    y = feature_df["home_win"]

    # TimeSeriesSplit: jokainen "fold" kouluttaa menneellä datalla ja
    # testaa seuraavalla ajanjaksolla - realistisempi simulaatio kuin
    # satunnainen jako, koska emme koskaan testaa menneisyydellä
    # tulevaisuuden datalla koulutettua mallia.
    tscv = TimeSeriesSplit(n_splits=5)
    model = LogisticRegression()

    accuracies = []
    briers = []

    for fold, (train_idx, test_idx) in enumerate(tscv.split(X), start=1):
    X = feature_df[["home_gf_at_home", "home_ga_at_home", "away_gf_at_away", "away_ga_at_away"]]
    y = feature_df["home_win"]

        model.fit(X_train, y_train)
        y_pred = model.predict(X_test)
        y_pred_prob = model.predict_proba(X_test)[:, 1]

        acc = accuracy_score(y_test, y_pred)
        brier = brier_score_loss(y_test, y_pred_prob)
        baseline = y_test.mean()

        accuracies.append(acc)
        briers.append(brier)

        print(f"Fold {fold}: tarkkuus {acc*100:.1f}% "
              f"(baseline {baseline*100:.1f}%), brier {brier:.4f}, "
              f"testissä {len(test_idx)} ottelua")

    print(f"\n--- Yhteenveto (5 foldia) ---")
    print(f"Keskimääräinen tarkkuus: {np.mean(accuracies)*100:.1f}% "
          f"(keskihajonta {np.std(accuracies)*100:.1f}pp)")
    print(f"Keskimääräinen Brier score: {np.mean(briers):.4f}")