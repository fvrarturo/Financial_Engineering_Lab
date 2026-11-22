#!/usr/bin/env python
"""Stage 4: historical backtest of every strategy (SPEC 03 §5 + addendum item 5).

For each asset, every monthly start date and horizon, run the strategies on the real return path.
The investor's estimates are re-estimated every 5 years (config backtest.refit_every_days) on data
strictly before the refit date: GJR-t parameters (conditional vol filter), premium, unconditional vol, and the
Vasicek model behind the rate-robust floor. A start date uses the estimates of the latest refit on or before it.
Knobs: the state-dependent knob function (and the uniform knob) from results/tuned/{asset}_robust.json (base-only tuned: 'state_base'),
evaluated at each start's sigma_0. That function was tuned on the full-sample model, so the knob carries a mild
look-ahead (logged in DECISIONS.md); the investor's estimates do not.
Starting states are bucketed by *expanding-window* percentiles of the investor's sigma history (no look-ahead).
Overlapping windows: n_eff = n_starts x 21 / T_days. This is weak evidence on its own; it complements the PIT.

Usage: python scripts/04_backtest.py --asset us_mkt|industries|all [--quick]
Outputs: results[/quick]/backtest/per_start.parquet, summary.parquet, fits_{asset}.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gf import model as M  # noqa: E402
from gf.data import load_config, read_asset_frame, resolve  # noqa: E402
from gf.engine import ArrayMarket, run_engine  # noqa: E402
from gf.metrics import ce  # noqa: E402
from gf.strategies import make_strategy  # noqa: E402
from gf.tune import KnobFunction  # noqa: E402
from gf.validation import rate_params_at, wilson_ci  # noqa: E402

KNOBBED = ["cppi", "cppi_vol", "obpi_merton"]


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def model_series(cfg, asset, params):
    """Frame and gap-scaled x exactly as SPEC 02 used them (fallback window + closure-gap scaling)."""
    full_all = read_asset_frame(cfg, asset, apply_window=False)
    start = (params.get("fallback") or {}).get("start_used")
    full = full_all.loc[start:] if start else full_all
    dc = json.loads((resolve(cfg, cfg["checks"]["report_dir"]) / "data_check.json").read_text())
    fl = M.gap_flags(full.index, dc["assets"][asset]["checks"]["dates"]["gaps"], cfg["model"]["gaps"]["flag_calendar_days"])
    x = M.adjust_for_gaps(full["x"], fl, params["gap_variant"])
    return full, x, full_all


def run_asset(cfg, asset, root, quick):
    t0 = time.time()
    P, bc = cfg["problem"], cfg["backtest"]
    N = cfg["trading_days_per_year"]
    params = json.loads((resolve(cfg, cfg["model"]["results_dir"]) / f"params_{asset}.json").read_text())
    tf = root / "tuned" / f"{asset}_robust.json"
    tuned = json.loads((tf if tf.exists() else root / "tuned" / f"{asset}_base.json").read_text())
    kind, name = params["kind"], params["model"]
    frame, x, full_all = model_series(cfg, asset, params)
    n = len(frame)
    log(f"{asset}: expanding refits every {bc['refit_every_days']} days (first on {bc['min_obs']} obs)")
    fits = M.expanding_fits(x, name, bc["min_obs"], bc["refit_every_days"], cfg["model"]["scale"])
    S2 = M.filter_variance(x.to_numpy(), [f["params"] for f in fits], kind, [f["backcast"] for f in fits])  # (n+1, J)
    s2_exp = M.expanding_sigma2(x.to_numpy(), fits, kind)
    ks = np.array([f["k"] for f in fits])
    block_info = []
    for j, f in enumerate(fits):
        upto = frame.iloc[: f["k"]]
        prem = M.premium_estimate(upto, cfg["model"]["premium"], N)
        rp = rate_params_at(upto, cfg, full_all.loc[: upto.index[-1]])   # rates: full rf history
        block_info.append({"k": int(f["k"]), "date": f["date_first_use"], "mu_e_ann": prem["annual_excess_used"],
                           "mu_raw": prem["raw_annual"], "sigma_bar_ann": M.uncond_vol_ann(f["params"], kind, "t", N),
                           "persistence": M.persistence(f["params"], kind), "nu": f["params"]["nu"],
                           "rate_model": {"kappa": rp["kappa"], "theta": rp["theta"], "s": rp["s"]},
                           "converged": f["converged"]})
    # monthly start dates from the first refit
    idx = frame.index
    month_first = np.nonzero(np.r_[True, idx.month[1:] != idx.month[:-1]])[0]
    starts_all = month_first[month_first >= ks[0]]
    edges = bc["buckets"]
    sig_exp = np.sqrt(s2_exp[:n] * N)
    r, rf, rfa = frame["r"].to_numpy(), frame["rf"].to_numpy(), frame["rf_ann"].to_numpy()
    H = tuned["horizons"]
    rows = []
    vol_in = cfg["strategies"]["obpi_vol_input"]
    for T in H:
        st = starts_all[starts_all + T <= n]
        blk = np.searchsorted(ks, st, side="right") - 1
        for j in np.unique(blk):
            s_j = st[blk == j]
            if len(s_j) == 0:
                continue
            bi = block_info[j]
            win = s_j[:, None] + np.arange(T)[None, :]
            sig_d = np.sqrt(S2[win, j])
            sigma0 = sig_d[:, 0] * math.sqrt(N)
            r0 = rfa[s_j]
            mk = ArrayMarket(r[win], rf[win], sig_d, rfa[win])
            info = {"mu_e_ann": bi["mu_e_ann"], "sigma_bar_ann": bi["sigma_bar_ann"], "sigma0_ann": sigma0,
                    "gamma": P["gamma"], "N": N, "rate_model": bi["rate_model"], "floor": cfg["strategies"]["floor"]}
            strats, meta = [], []
            for s in ["cash", "merton_const", "merton_vol"]:
                strats.append(make_strategy(s, T))
                meta.append((s, ["none"]))
            for s in KNOBBED:
                a = tuned["strategies"][s]
                fn = KnobFunction.from_dict(a["state_fn"])
                kv = [np.full(len(s_j), a["uniform"]["value"]), fn(sigma0, T)]
                modes = ["uniform", "state"]
                if "base_strategies" in tuned:          # base-only tuned comparison row
                    kv.append(KnobFunction.from_dict(tuned["base_strategies"][s]["state_fn"])(sigma0, T))
                    modes.append("state_base")
                if s == "obpi_merton":
                    kv.append(np.ones(len(s_j)))
                    modes.append("untuned")
                extra = {"vol_input": vol_in} if s == "obpi_merton" else {}
                strats.append(make_strategy(s, T, np.vstack(kv), labels=modes, cfg=cfg, **extra))
                meta.append((s, modes))
            state0 = {"sigma0_ann": sigma0, "r0_ann": r0}
            out = run_engine(mk, strats, P["W0"], P["K"], P["gamma"], state0, info, cfg["engine"])
            # expanding-percentile bucket of sigma_0 (history up to the start date only)
            pct = np.array([np.mean(sig_exp[ks[0]:s][np.isfinite(sig_exp[ks[0]:s])] <= sg) * 100
                            if s > ks[0] else 50.0 for s, sg in zip(s_j, sigma0)])
            bucket = np.select([pct < edges["normal"][0], pct < edges["elevated"][0], pct < edges["crisis"][0]],
                               ["calm", "normal", "elevated"], "crisis")
            dates = idx[s_j]
            for (s, modes), res in zip(meta, out):
                for i, mode in enumerate(modes):
                    W = res["W_T"][i]
                    kvals = res["knob_values"][i] if res["knob_values"].ndim == 2 else np.full(len(s_j), res["knob_values"][i])
                    rows.append(pd.DataFrame({
                        "asset": asset, "start": dates, "T_days": T, "T": T / N, "strategy": s, "tune_mode": mode,
                        "knob": kvals if s in KNOBBED else np.nan, "W_T": W, "breach": W < P["K"],
                        "sigma0": sigma0, "r0": r0, "sigma0_pct_expanding": pct, "bucket": bucket,
                        "decade": (dates.year // 10 * 10).astype(str) + "s", "refit_date": bi["date"],
                        "cross": res["cross_own"][i], "phi_avg": res["phi_mean_path"][i]}))
    per = pd.concat(rows, ignore_index=True)
    log(f"{asset}: {per.start.nunique()} start dates, {len(per)} rows in {time.time() - t0:.0f}s")
    return per, {"asset": asset, "refits": block_info, "n_obs": n, "first_start": str(idx[starts_all[0]].date()),
                 "fallback_start": (params.get("fallback") or {}).get("start_used"),
                 "knob_source": f"tuned/{tf.name if tf.exists() else asset + '_base.json'} (full-sample model; see DECISIONS)"}


def summarise(per: pd.DataFrame, K: float, W0: float, N: int = 252) -> pd.DataFrame:
    out = []
    keys = ["asset", "strategy", "tune_mode", "T_days"]
    for gtype, gcol in [("overall", None), ("bucket", "bucket"), ("decade", "decade")]:
        cols = keys + ([gcol] if gcol else [])
        for k, d in per.groupby(cols):
            n = len(d)
            T = d.T_days.iloc[0]
            n_eff = n * 21 / T
            b = d.breach.mean()
            lo, hi = wilson_ci(b, n_eff)
            c = ce(d.W_T.to_numpy(), 2.0)
            out.append({**dict(zip(cols, k)), "group_type": gtype, "group": k[-1] if gcol else "all",
                        "n_starts": n, "n_eff": n_eff, "breach": b, "breach_lo": lo, "breach_hi": hi,
                        "ce": c, "ce_ann": (c / W0) ** (N / T) - 1, "mean_W": d.W_T.mean(),
                        "q1%": d.W_T.quantile(0.01), "phi_avg": d.phi_avg.mean(),
                        "gap_share": float((d.cross[d.breach] == 1).mean()) if d.breach.any() else np.nan})
    s = pd.DataFrame(out)
    s["note"] = "overlapping windows: weak evidence on its own; complements the PIT test"
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="us_mkt")
    ap.add_argument("--quick", action="store_true", help="use results/quick (tuned knobs from the quick run)")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    a = ap.parse_args()
    cfg = load_config(a.config)
    inds = [k for k in cfg["data"]["assets"] if k.startswith("ind_")]
    assets = {"all": ["us_mkt", *inds], "industries": inds}.get(a.asset, [a.asset])
    root = resolve(cfg, "results/quick") if a.quick else resolve(cfg, "results")
    d = root / "backtest"
    d.mkdir(parents=True, exist_ok=True)
    pers, metas = [], {}
    for asset in assets:
        per, meta = run_asset(cfg, asset, root, a.quick)
        pers.append(per)
        metas[asset] = meta
        (d / f"fits_{asset}.json").write_text(json.dumps(meta, indent=1, default=str))
    per = pd.concat(pers, ignore_index=True)
    summ = summarise(per, cfg["problem"]["K"], cfg["problem"]["W0"])
    for name, df in [("per_start", per), ("summary", summ)]:
        p = d / f"{name}.parquet"
        if p.exists():
            old = pd.read_parquet(p)
            df = pd.concat([old[~old.asset.isin(assets)], df], ignore_index=True)
        df.to_parquet(p, index=False)
    log(f"backtest written to {d}")


if __name__ == "__main__":
    main()
