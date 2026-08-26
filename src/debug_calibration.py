import pandas as pd
from win_model import train_calibration_model

print("Koulutetaan kalibrointimalli...")
model = train_calibration_model()

# Katsotaan mallin sisäiset parametrit
logreg = model.named_steps["logisticregression"]
scaler = model.named_steps["standardscaler"]

print(f"\nScaler mean: {scaler.mean_}")
print(f"Scaler scale (keskihajonta): {scaler.scale_}")
print(f"LogisticRegression coef: {logreg.coef_}")
print(f"LogisticRegression intercept: {logreg.intercept_}")

# Testataan mallia sarjalla syöttöarvoja 0.1:sta 0.9:ään
print("\n--- Kalibrointi eri syöttöarvoilla ---")
print(f"{'Raaka Poisson':>15} | {'Kalibroitu':>12}")
for raw_prob in [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
    X_test = pd.DataFrame({"poisson_prob": [raw_prob]})
    calibrated = model.predict_proba(X_test)[0, 1]
    print(f"{raw_prob:>15.2f} | {calibrated:>12.3f}")