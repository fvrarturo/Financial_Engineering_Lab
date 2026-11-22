#!/usr/bin/env python
"""Stage 2b: daily VaR backtest of the innovation tail (decides the role of the `fat_tails` scenario).

For each asset, the base model's one-step-ahead standardised residuals z_t = (x_t - mu) / sigma_t (SPEC 02,
results/model/zhat_{asset}.npy, in-sample filter) are compared with the VaR quantile of a unit-variance
Student t:  exceedance if z_t < t_nu^{-1}(p) sqrt((nu - 2) / nu).
Levels p = 1% and 0.1%; nu = fitted, nu = 3, and the fat_tails scenario's nu (max(nu_min, nu + nu_shift)).
Tests: Kupiec unconditional coverage (LR_uc ~ chi2(1)) and Christoffersen independence (LR_ind ~ chi2(1)).

Decision rule (logged in DECISIONS.md): nu = 3 is "rejected" for an asset if the Kupiec test rejects at 5%
at either level. If nu = 3 is rejected for us_mkt, fat_tails is a stress test outside the tuning/worst-case
set (with jump and mu_*); otherwise it joins the robust set.

Output: results/model/var_backtest.json and var_backtest.csv
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gf.data import load_config, resolve  # noqa: E402

LEVELS = (0.01, 0.001)


def kupiec(n_exc: int, n: int, p: float) -> tuple[float, float]:
    """LR_uc and its chi2(1) p-value."""
    x, ph = n_exc, n_exc / n
    ll0 = (n - x) * math.log(1 - p) + x * math.log(p)
    ll1 = ((n - x) * math.log(1 - ph) if x < n else 0.0) + (x * math.log(ph) if x > 0 else 0.0)
    lr = -2 * (ll0 - ll1)
    return lr, float(stats.chi2.sf(lr, 1))


def christoffersen_ind(hits: np.ndarray) -> tuple[float, float]:
    """Independence LR (first-order Markov) and p-value; NaN if a transition count is zero."""
    h0, h1 = hits[:-1], hits[1:]
    n00 = int(np.sum(~h0 & ~h1)); n01 = int(np.sum(~h0 & h1))
    n10 = int(np.sum(h0 & ~h1)); n11 = int(np.sum(h0 & h1))
    if n01 + n11 == 0:
        return float("nan"), float("nan")
    p01 = n01 / max(n00 + n01, 1); p11 = n11 / max(n10 + n11, 1); p = (n01 + n11) / (n00 + n01 + n10 + n11)

    def ll(a, b, q):
        return (a * math.log(1 - q) if a else 0.0) + (b * math.log(q) if b and q > 0 else 0.0)
    lr = -2 * (ll(n00 + n10, n01 + n11, p) - ll(n00, n01, p01) - ll(n10, n11, p11))
    return lr, float(stats.chi2.sf(lr, 1))


def t_quantile_unit(p: float, nu: float) -> float:
    return float(stats.t.ppf(p, nu) * math.sqrt((nu - 2) / nu))


def run_asset(cfg, asset):
    res = resolve(cfg, cfg["model"]["results_dir"])
    params = json.loads((res / f"params_{asset}.json").read_text())
    z = np.load(res / f"zhat_{asset}.npy")
    z = z[np.isfinite(z)]
    n = len(z)
    nu_fit = float(params["params"]["nu"])
    fsc = cfg["mc"]["scenarios"]["fat_tails"]
    nu_scen = max(fsc.get("nu_min", 3.0), nu_fit + fsc["nu_shift"])
    rows = []
    for label, nu in (("fitted", nu_fit), ("nu3", 3.0), ("fat_tails_scenario", nu_scen)):
        for p in LEVELS:
            q = t_quantile_unit(p, nu)
            hits = z < q
            x = int(hits.sum())
            lr, pv = kupiec(x, n, p)
            lri, pvi = christoffersen_ind(hits)
            rows.append({"asset": asset, "nu_label": label, "nu": nu, "level": p, "n": n, "z_quantile": q,
                         "exceedances": x, "expected": p * n, "ratio": x / (p * n), "kupiec_LR": lr,
                         "kupiec_p": pv, "ind_LR": lri, "ind_p": pvi, "reject_5pct": pv < 0.05,
                         "direction": "too many" if x > p * n else "too few"})
    return rows


def main():
    cfg = load_config()
    assets = cfg["data"]["assets"]
    assets = list(assets) if not isinstance(assets, dict) else list(assets.keys())
    rows = [r for a in assets for r in run_asset(cfg, a)]
    df = pd.DataFrame(rows)
    out = resolve(cfg, cfg["model"]["results_dir"])
    df.to_csv(out / "var_backtest.csv", index=False)
    verdict = {}
    for a, g in df.groupby("asset"):
        g3 = g[g.nu_label == "nu3"]
        gs = g[g.nu_label == "fat_tails_scenario"]
        gf = g[g.nu_label == "fitted"]
        verdict[a] = {"nu_fitted": float(gf.nu.iloc[0]), "nu_scenario": float(gs.nu.iloc[0]),
                      "nu3_rejected": bool(g3.reject_5pct.any()),
                      "scenario_nu_rejected": bool(gs.reject_5pct.any()),
                      "fitted_rejected": bool(gf.reject_5pct.any())}
    us = verdict.get("us_mkt", {})
    decision = ("fat_tails is a stress test outside the robust tuning set" if us.get("nu3_rejected")
                else "fat_tails joins the robust tuning set")
    (out / "var_backtest.json").write_text(json.dumps({"rule": "nu=3 rejected if Kupiec p<0.05 at 1% or 0.1%",
                                                       "decision_us_mkt": decision, "assets": verdict,
                                                       "rows": df.to_dict("records")}, indent=1, default=float))
    pd.set_option("display.width", 220)
    cols = ["asset", "nu_label", "nu", "level", "exceedances", "expected", "ratio", "kupiec_p", "ind_p"]
    print(df[cols].to_string(index=False, float_format=lambda v: f"{v:.4g}"))
    print(json.dumps(verdict, indent=1))
    print("decision:", decision)


if __name__ == "__main__":
    main()
