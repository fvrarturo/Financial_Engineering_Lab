"""Run every model fit and save the results for make_figures.py.  (~1 minute)

    .venv/bin/python code/run_analysis.py

Outputs data/processed/results.pkl."""
import pickle
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from yc import data, fit, timeseries as ts, pq  # noqa: E402
import pandas as pd  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "data" / "processed"
DATES = ["2006-12-29", "2021-06-30", "2023-06-30", "2024-09-30", "2026-06-30"]
MODELS = ["Nelson-Siegel", "Svensson", "Vasicek", "CIR", "G2++"]


def main():
    R = {"dates": DATES}
    css = {d: data.cross_section(d) for d in DATES}
    R["bonds"] = {d: cs["bonds"] for d, cs in css.items()}
    # 1. each model on each date, all parameters free
    R["per_date"] = {}
    for d in DATES:
        for m in MODELS:
            p, e = fit.fit_date(m, css[d])
            R["per_date"][(d, m)] = (p, e)
            print(f"{d} {m:14s} RMSE {fit.summary(e)['RMSE (bp)']:5.1f}bp", flush=True)
    # 2. one structural parameter set for the five dates
    R["shared"] = {m: fit.fit_shared(m, [css[d] for d in DATES]) for m in ["Vasicek", "CIR", "G2++"]}
    # 3. through time: monthly zero curves 1990-2026
    P = ts.monthly_zero_panel()
    R["panel"] = P
    starts = {"Vasicek": [[0.1, 0.05, 0.01], [0.5, 0.05, 0.02]],
              "G2++": [[0.02, 0.5, 0.01, 0.015, -0.7, 0.05], [0.05, 1.0, 0.01, 0.02, -0.9, 0.06],
                       [0.01, 0.3, 0.008, 0.012, 0.0, 0.05]],
              "Dynamic NS": [[1.0], [2.0], [4.0]]}
    R["panel_fits"] = {m: ts.fit_shared_panel(m, P, x0) for m, x0 in starts.items()}
    R["vasicek_monthly"] = ts.fit_vasicek_each_month(P)
    for m, (th, X, F) in R["panel_fits"].items():
        print(m, {k: round(v, 4) for k, v in th.items()}, f"RMSE {np.sqrt((((F - P) * 100) ** 2).values.mean()):.1f}bp")
    # 4. P vs Q: real-world factor dynamics, term premium, forecasts
    g = pq.G2PQ(R["panel_fits"]["G2++"], P)
    acm = pd.read_excel(OUT.parent / "raw" / "acm" / "ACMTermPremium.xls", "ACM Monthly")
    acm["DATE"] = pd.to_datetime(acm.DATE, format="%d-%b-%Y")
    R["acm"] = acm.set_index("DATE").resample("ME").last()
    R["pq_params"] = g.params()
    R["decomp10"] = g.decomposition(10)
    R["paths"] = {d: g.paths(d, 40) for d in ["2006-12-31", "2019-08-31", "2023-06-30"]}
    R["forecasts"] = g.forecasts()
    print("P vs Q:", {k: (np.round(v, 4).tolist() if hasattr(v, "__len__") else round(float(v), 4)) for k, v in R["pq_params"].items()})
    OUT.mkdir(parents=True, exist_ok=True)
    pickle.dump(R, open(OUT / "results.pkl", "wb"))
    print("saved", OUT / "results.pkl")


if __name__ == "__main__":
    main()
