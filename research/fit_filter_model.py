"""Fit regularized models to predict snipe WIN from the full filter set
(direction, edge, distance, time-to-close, trend) with honest cross-validation.

With only ~47 trades the whole point is to NOT overfit: every model is scored by
repeated stratified k-fold CV (out-of-fold), and we compare a strongly
regularized logistic regression against tree ensembles and a no-skill baseline.
If the complex models don't beat the baseline OOS, the honest finding is that the
features don't carry reliable predictive signal at this sample size.

Run:  .venv/bin/python -m research.fit_filter_model
"""
import warnings
warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression, LogisticRegressionCV
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.model_selection import RepeatedStratifiedKFold, cross_val_score
from sklearn.metrics import roc_auc_score

FEATURES = ["edge", "dist", "t_rem", "trend_aligned", "is_up"]
SEED = 0


def main():
    df = pd.read_csv("research/filter_features.csv")
    X = df[FEATURES].values
    y = df["won"].values
    n, base = len(df), y.mean()
    print(f"n={n} trades, win rate {base:.1%} "
          f"(no-skill accuracy = {max(base, 1 - base):.1%}, AUC = 0.50)\n")

    cv = RepeatedStratifiedKFold(n_splits=5, n_repeats=40, random_state=SEED)
    models = {
        "Logistic L2 (C=0.5)":
            make_pipeline(StandardScaler(),
                          LogisticRegression(penalty="l2", C=0.5, max_iter=2000)),
        "Logistic L1 (C=0.5)":
            make_pipeline(StandardScaler(),
                          LogisticRegression(penalty="l1", solver="liblinear",
                                             C=0.5, max_iter=2000)),
        "RandomForest (depth3)":
            RandomForestClassifier(n_estimators=400, max_depth=3,
                                   min_samples_leaf=4, random_state=SEED),
        "GradientBoosting (xgb-like)":
            GradientBoostingClassifier(n_estimators=120, max_depth=2,
                                       learning_rate=0.05, random_state=SEED),
    }
    print(f"{'model':30} {'CV AUC (mean±std)':>22} {'CV acc':>9}")
    print("-" * 64)
    for name, m in models.items():
        auc = cross_val_score(m, X, y, cv=cv, scoring="roc_auc")
        acc = cross_val_score(m, X, y, cv=cv, scoring="accuracy")
        print(f"{name:30} {auc.mean():>10.3f} ± {auc.std():<7.3f} "
              f"{acc.mean():>8.1%}")
    print(f"{'baseline (predict majority)':30} {0.5:>10.3f}          "
          f"{max(base, 1 - base):>8.1%}")

    print("\n--- single-feature separability (|AUC-0.5|; OOF-free, full data) ---")
    for f in FEATURES:
        a = roc_auc_score(y, df[f].values)
        a = max(a, 1 - a)
        print(f"  {f:16} AUC {a:.3f}")

    print("\n--- regularized logistic coefficients (standardized X, full fit) ---")
    sc = StandardScaler().fit(X)
    lrcv = LogisticRegressionCV(Cs=20, cv=5, penalty="l2", max_iter=4000,
                                scoring="roc_auc", random_state=SEED)
    lrcv.fit(sc.transform(X), y)
    coefs = sorted(zip(FEATURES, lrcv.coef_[0]), key=lambda t: -abs(t[1]))
    print(f"  (chosen C = {lrcv.C_[0]:.3g}; larger |coef| = stronger pull on "
          f"P(win))")
    for f, c in coefs:
        direction = "more likely WIN" if c > 0 else "more likely LOSS"
        print(f"  {f:16} {c:+.3f}  -> higher {f} => {direction}")

    print("\n--- profit lens: mean pnl by sign of the top trend feature ---")
    for label, mask in [("trend WITH bet (aligned>0)", df.trend_aligned > 0),
                        ("trend AGAINST bet (aligned<0)", df.trend_aligned < 0)]:
        s = df[mask]
        if len(s):
            print(f"  {label:30} n={len(s):>3}  win {s.won.mean():.0%}  "
                  f"pnl/trade {s.pnl.mean():+.2f}")


if __name__ == "__main__":
    main()
