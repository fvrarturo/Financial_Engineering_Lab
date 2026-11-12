"""Robustness: (1) out-of-sample test of the one-parameter-set models, (2) the long sample 1971-2026 and its term
premium vs the NY Fed's, (3) does the curve, or its expected-rates / term-premium parts, predict recessions?  (~1 min)

    .venv/bin/python code/run_robustness.py     -> data/processed/robustness.pkl"""
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm
from sklearn.metrics import roc_auc_score

sys.path.insert(0, str(Path(__file__).resolve().parent))
from yc import data, pq, timeseries as ts  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STARTS = {"Vasicek": [[0.1, 0.05, 0.01], [0.5, 0.05, 0.02], [0.05, 0.08, 0.01]],
          "G2++": [[0.02, 0.5, 0.01, 0.015, -0.7, 0.05], [0.05, 1.0, 0.01, 0.02, -0.9, 0.06],
                   [0.01, 0.3, 0.008, 0.012, 0.0, 0.05], [0.03, 0.4, 0.016, 0.03, -0.99, 0.1]],
          "Dynamic NS": [[1.0], [2.0], [4.0]]}
CUT = "2015-12-31"


def rmse_by_month(F, P):
    return np.sqrt((((F - P) * 100) ** 2).mean(1))


def main():
    out = {}
    P = ts.monthly_zero_panel(long=True)
    out["panel_long"] = P
    cm = data.load_cmt()
    spread_m = ((cm["10y"] - cm["3m"]).resample("ME").mean()).reindex(P.index)
    out["inverted"] = spread_m < 0
    # 1. full long sample, and parameters frozen at end-2015
    out["full"] = {m: ts.fit_shared_panel(m, P, x0) for m, x0 in STARTS.items()}
    out["frozen"] = {m: ts.fit_shared_panel(m, P, x0, fit_index=P.loc[:CUT].index) for m, x0 in STARTS.items()}
    out["rmse_full"] = pd.DataFrame({m: rmse_by_month(v[2], P) for m, v in out["full"].items()})
    out["rmse_frozen"] = pd.DataFrame({m: rmse_by_month(v[2], P) for m, v in out["frozen"].items()})
    halves = {}
    for m in ["Vasicek", "G2++"]:
        A = ts.fit_shared_panel(m, P, STARTS[m], fit_index=P.loc[:"1998-12-31"].index)
        B = ts.fit_shared_panel(m, P, STARTS[m], fit_index=P.loc["1999-01-01":].index)
        halves[m] = dict(params_early=A[0], params_late=B[0], rmse_early_params=rmse_by_month(A[2], P),
                         rmse_late_params=rmse_by_month(B[2], P))
    out["halves"] = halves
    # 2. real-world dynamics and term premium on 1971-2026
    g = pq.G2PQ(out["full"]["G2++"], P)
    out["pq_params_long"] = g.params()
    D = g.decomposition(10)
    out["decomp_long"] = D
    # 3. recession probits: recession in month t+12 on the spread, its expected-rates part and its term premium
    rec = pd.Series(0, index=P.index)
    for a, b in data.NBER_RECESSIONS:
        rec[(rec.index >= a) & (rec.index <= pd.Timestamp(b) + pd.offsets.MonthEnd(0))] = 1
    out["recession"] = rec
    X = pd.DataFrame({"y": rec.shift(-12), "spread": P[10.0] - P[0.25], "expected": D.expect - P[0.25],
                      "term premium": P[10.0] - D.expect}, index=P.index)
    est = X.dropna().loc[:"2025-10-31"]          # last month whose 12-month-ahead outcome is known
    models = {"spread": ["spread"], "expected part": ["expected"], "term premium": ["term premium"],
              "both parts": ["expected", "term premium"]}
    pr = {}
    for name, cols in models.items():
        m = sm.Probit(est.y, sm.add_constant(est[cols])).fit(disp=0, cov_type="HAC", cov_kwds={"maxlags": 12})
        Xall = sm.add_constant(X[cols].dropna(), has_constant="add")
        pr[name] = dict(params=m.params, z=m.params / m.bse, pseudo_r2=m.prsquared,
                        auc=roc_auc_score(est.y, m.predict(sm.add_constant(est[cols]))), prob=m.predict(Xall))
    out["probit"] = pr
    # real-time check: expected-rates part from a VAR estimated only on data up to each month (from 1985)
    x, d0 = g.x, g.th["delta"]
    rt = {}
    for i, t in enumerate(P.index):
        if t < pd.Timestamp("1985-01-01"):
            continue
        c, Phi, _ = pq.var1(x[: i + 1])
        rt[t] = (d0 + pq.iterate(c, Phi, x[i], 119).sum(1)).mean() * 100
    rt = pd.Series(rt)
    X2 = pd.DataFrame({"y": rec.shift(-12), "spread": P[10.0] - P[0.25], "expected": rt - P[0.25],
                       "term premium": P[10.0] - rt}).dropna().loc[:"2025-10-31"]
    pr_rt = {}
    for name, cols in [("spread", ["spread"]), ("expected part", ["expected"]), ("both parts", ["expected", "term premium"])]:
        m = sm.Probit(X2.y, sm.add_constant(X2[cols])).fit(disp=0, cov_type="HAC", cov_kwds={"maxlags": 12})
        pr_rt[name] = dict(params=m.params, z=m.params / m.bse, pseudo_r2=m.prsquared,
                           auc=roc_auc_score(X2.y, m.predict(sm.add_constant(X2[cols]))),
                           may2023=float(m.predict(sm.add_constant(X2[cols]).loc[["2023-05-31"]]).iloc[0]))
    out["probit_realtime"] = pr_rt
    pickle.dump(out, open(ROOT / "data/processed/robustness.pkl", "wb"))
    for k, v in pr.items():
        print(k, {kk: round(float(vv), 3) for kk, vv in v["params"].items()}, "pR2", round(v["pseudo_r2"], 3),
              "AUC", round(v["auc"], 3), "May23", round(float(v["prob"].loc["2023-05-31"]), 2))


if __name__ == "__main__":
    main()
