"""Starting states.

Under GJR-GARCH the one-day-ahead conditional variance sigma_0^2 is a sufficient statistic for the
equity part of the future (the sign of the last shock is already in it), and the Vasicek rate state
is r_0. So a starting condition is the pair (sigma_0, r_0), both observable at t=0.

Conventions: ``sigma_next_*`` at date t is the conditional vol for the next trading day, given data up
to and including t. That is the time-0 state if the strategy is switched on at the close of t.
Annualised with N = trading_days_per_year (252).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def filtered_history(frame: pd.DataFrame, s2_full: np.ndarray, s2_exp: np.ndarray,
                     gap_flags: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """s2_* have length n+1 (element t = variance for day t; element n = after the last day)."""
    N = cfg["trading_days_per_year"]
    sd = cfg["states"]["secondary_descriptors"]
    out = pd.DataFrame(index=frame.index)
    out["sigma_ann_full"] = np.sqrt(s2_full[:-1] * N)
    out["sigma_next_ann_full"] = np.sqrt(s2_full[1:] * N)
    out["sigma_ann_exp"] = np.sqrt(s2_exp[:-1] * N)
    out["sigma_next_ann_exp"] = np.sqrt(s2_exp[1:] * N)
    out["rf_ann"] = frame["rf_ann"]
    out[f"rv{sd['realised_vol_days']}_ann"] = frame["x"].rolling(sd["realised_vol_days"]).std() * np.sqrt(N)
    w = np.exp(np.log1p(frame["r"]).cumsum())
    out[f"dd{sd['drawdown_days']}"] = w / w.rolling(sd["drawdown_days"], min_periods=1).max() - 1
    out["gap_flag"] = out.index.isin(gap_flags.index)
    return out


def named_states(filt: pd.DataFrame, cfg: dict, r0_named: float) -> pd.DataFrame:
    sig = filt["sigma_next_ann_full"].dropna()
    rows = []
    for name, pct in cfg["states"]["named_percentiles"].items():
        if pct == "max":
            v, d = float(sig.max()), f"historical max of filtered sigma ({sig.idxmax().date()})"
        else:
            v, d = float(np.percentile(sig, pct)), f"P{pct} of filtered sigma"
        rows.append({"state_name": name, "sigma0_ann": v, "r0_ann": r0_named,
                     "definition": d + "; r0 = median rf_ann of estimation window",
                     "source": "named"})
    named = pd.DataFrame(rows)
    grid = [{"state_name": f"{r.state_name}@r{g:.2f}", "sigma0_ann": r.sigma0_ann, "r0_ann": g,
             "definition": f"{r.state_name} sigma0 x rate grid", "source": "named_x_rate_grid"}
            for r in named.itertuples() for g in cfg["states"]["rate_grid"]]
    cap = float(sig.max()) * cfg["states"]["dense_cap_mult"]
    dense_vals = sorted({min(v, cap) for v in cfg["states"]["dense_sigma_grid"]})
    dense = [{"state_name": f"dense_s{v:.3f}@r{g:.2f}", "sigma0_ann": v, "r0_ann": g,
              "definition": "dense grid" + (" (clipped at max x %.2f)" % cfg["states"]["dense_cap_mult"] if v == cap else ""),
              "source": "dense_grid"}
             for v in dense_vals for g in [r0_named] + list(cfg["states"]["rate_grid"])]
    return pd.concat([named, pd.DataFrame(grid), pd.DataFrame(dense)], ignore_index=True)


def anchor_states(filt: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    rows = []
    sd = cfg["states"]["secondary_descriptors"]
    for d in cfg["states"]["anchor_dates"]:
        ts = filt.index[-1] if d == "latest" else pd.Timestamp(d)
        if ts < filt.index[0] or ts > filt.index[-1]:
            continue
        if ts not in filt.index:  # use last trading day on or before
            ts = filt.index[filt.index.get_indexer([ts], method="pad")[0]]
        row = filt.loc[ts]
        rows.append({"state_name": f"anchor_{ts.date()}", "sigma0_ann": float(row["sigma_next_ann_full"]),
                     "r0_ann": float(row["rf_ann"]),
                     "definition": f"state at close of {ts.date()} (full-sample params)",
                     "source": "anchor", "sigma0_ann_expanding": float(row["sigma_next_ann_exp"]),
                     f"rv{sd['realised_vol_days']}_ann": float(row[f"rv{sd['realised_vol_days']}_ann"]),
                     f"dd{sd['drawdown_days']}": float(row[f"dd{sd['drawdown_days']}"])})
    return pd.DataFrame(rows)


def bucket_edges(filt: pd.DataFrame, cfg: dict) -> dict:
    sig = filt["sigma_next_ann_full"].dropna()
    return {k: (float(np.percentile(sig, a)), float(np.percentile(sig, b)) if b < 100 else np.inf)
            for k, (a, b) in cfg["validation"]["pit"]["buckets"].items()}


def assign_bucket(sigma_ann: np.ndarray, edges: dict) -> np.ndarray:
    out = np.empty(len(sigma_ann), dtype=object)
    for k, (lo, hi) in edges.items():
        out[(sigma_ann >= lo) & (sigma_ann < hi)] = k
    first = list(edges)[0]
    out[sigma_ann < edges[first][0]] = first
    return out


def assign_era(dates: pd.DatetimeIndex, cfg: dict) -> np.ndarray:
    out = np.empty(len(dates), dtype=object)
    for label, a, b in cfg["validation"]["pit"]["eras"]:
        m = np.ones(len(dates), bool)
        if a:
            m &= dates >= pd.Timestamp(a)
        if b:
            m &= dates <= pd.Timestamp(b)
        out[m] = label
    return out
