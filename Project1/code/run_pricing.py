"""Pricing with the fitted curves: worked bond example, out-of-sample pricing, hedging, a bond option.  (~3 min)

    .venv/bin/python code/run_pricing.py      -> data/processed/pricing.pkl   (needs results.pkl)"""
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

sys.path.insert(0, str(Path(__file__).resolve().parent))
from yc import data, fit, models, pricing as pr  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATES = ["2006-12-29", "2021-06-30", "2023-06-30", "2024-09-30", "2026-06-30"]
HEDGE_SETS = {  # newest 10y (target), 2y and 30y issues on each start date (CRSP KYTREASNO)
    "2019-06-28": {"target": 207362.0, "h2": 207359.0, "h30": 207363.0},
    "2021-06-30": {"target": 207717.0, "h2": 207729.0, "h30": 207719.0},
    "2023-06-30": {"target": 208060.0, "h2": 208073.0, "h30": 208062.0},
}


def main():
    R = pickle.load(open(ROOT / "data/processed/results.pkl", "rb"))
    out = {}
    # 1. worked example: the 10-year note 3.375% May 2033 on 30 Jun 2023
    d = "2023-06-30"
    cs = data.cross_section(d)
    b = cs["bonds"]
    i = b.index[b.TCUSIP == "91282CHC"][0]
    t, cf = cs["T"][i][cs["CF"][i] > 0], cs["CF"][i][cs["CF"][i] > 0]
    curves = {}
    for m, p in [("Svensson", list(R["per_date"][(d, "Svensson")][0].values())),
                 ("Vasicek", list(R["per_date"][(d, "Vasicek")][0].values())),
                 ("G2++ (one parameter set)", list(R["shared"]["G2++"][0].loc[pd.Timestamp(d)].values))]:
        mm = "G2++" if m.startswith("G2++") else m
        z = models.MODELS[mm](t, p)
        curves[m] = dict(params=p, model=mm, zero=z, df=np.exp(-z * t), pv=cf * np.exp(-z * t))
    out["example"] = dict(bond=b.loc[i].to_dict(), t=t, cf=cf, curves=curves, date=d)
    # 2. out-of-sample: 5-fold hold-out on each date
    rows = []
    for dd in DATES:
        c = data.cross_section(dd)
        for m in ["Nelson-Siegel", "Svensson", "Vasicek", "CIR", "G2++"]:
            e = pr.holdout(c, m)
            rows.append(dict(date=dd, model=m, oos=np.sqrt((e.err_bp ** 2).mean()),
                             ins=fit.summary(R["per_date"][(dd, m)][1])["RMSE (bp)"]))
            print(dd, m, round(rows[-1]["ins"], 1), round(rows[-1]["oos"], 1), flush=True)
    out["holdout"] = pd.DataFrame(rows)
    # 3. hedging a 10-year note for a year
    th = R["panel_fits"]["G2++"][0]
    out["hedge"] = {s: pr.hedge_test(s, ids, th["a1"], th["a2"]) for s, ids in HEDGE_SETS.items()}
    # 4. a 1-year option on a 10-year zero-coupon bond, 30 Jun 2023
    X = R["panel_fits"]["G2++"][1]
    xt = X.loc[d, ["x1", "x2"]].values / 100
    T, S = 1.0, 11.0
    PT, PS = pr.g2_P(T, xt, th), pr.g2_P(S, xt, th)
    fwd_y = -np.log(PS / PT) / (S - T)
    cmt = data.load_cmt()
    dy = cmt["10y"].loc["2022-07-01":d].dropna().diff().dropna() * 100
    ev = pd.read_csv(ROOT / "data/raw/events/event_calendar.csv", parse_dates=["date"])
    evd = set(ev[ev.time_et.isin(["08:30", "14:00"])].date)
    vols = {"fitted to the curve (Q)": pr.normal_vol_from_sigma(pr.g2_sigma_p(T, S, th), T, S),
            "realised, past year, excluding release days": dy[~dy.index.isin(evd)].std() * np.sqrt(252),
            "realised, past year, all days": dy.std() * np.sqrt(252)}
    strikes_y = np.linspace(fwd_y - 0.015, fwd_y + 0.015, 31)
    K = np.exp(-strikes_y * (S - T))
    prices = {}
    for lab, v in vols.items():
        sig = v / 1e4 * (S - T) * np.sqrt(T)
        h = np.log(PS / (K * PT)) / sig + sig / 2
        prices[lab] = (PS * norm.cdf(h) - K * PT * norm.cdf(h - sig)) * 100          # per 100 face
    mc_k = strikes_y[::5]
    mc = [pr.g2_zbc_mc(np.exp(-k * (S - T)), T, S, xt, th, n=100_000, steps=100) for k in mc_k]
    out["option"] = dict(T=T, S=S, PT=PT, PS=PS, fwd_y=fwd_y, vols=vols, strikes_y=strikes_y, prices=prices,
                         closed_q=pr.g2_zbc(np.exp(-strikes_y * (S - T)), T, S, xt, th) * 100,
                         mc_strikes=mc_k, mc=[(m * 100, s * 100) for m, s in mc])
    pickle.dump(out, open(ROOT / "data/processed/pricing.pkl", "wb"))
    print("vols", {k: round(v, 1) for k, v in vols.items()})


if __name__ == "__main__":
    main()
