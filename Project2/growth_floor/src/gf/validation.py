"""Model validation (Spec 02, section 5): PIT calibration, VaR backtests, stylised facts.

Heavy jobs (expanding refits, PIT) are module-level functions so they can run in a process pool.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import arch
import numpy as np
import pandas as pd
from scipy import stats

from . import model as M
from .simulate import simulate_paths
from .states import assign_bucket, assign_era


# --------------------------------------------------------------------------- statistics helpers
def wilson_ci(p_hat: float, n: float, level: float = 0.95) -> tuple[float, float]:
    if n <= 0:
        return (float("nan"), float("nan"))
    z = stats.norm.ppf(0.5 + level / 2)
    den = 1 + z * z / n
    c = (p_hat + z * z / (2 * n)) / den
    h = z * math.sqrt(p_hat * (1 - p_hat) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - h), min(1.0, c + h))


def kupiec(hits: np.ndarray, p: float) -> dict:
    n, x = len(hits), int(hits.sum())
    pi = x / n if n else float("nan")
    def ll(q):
        return (n - x) * math.log(1 - q) + (x * math.log(q) if x else 0.0)
    lr = -2 * (ll(p) - (ll(pi) if 0 < pi < 1 else 0.0)) if n else float("nan")
    return {"n": n, "hits": x, "rate": pi, "expected": p, "LR_uc": lr, "p_uc": float(stats.chi2.sf(lr, 1))}


def christoffersen(hits: np.ndarray) -> dict:
    h = hits.astype(int)
    a, b = h[:-1], h[1:]
    n00, n01 = int(((a == 0) & (b == 0)).sum()), int(((a == 0) & (b == 1)).sum())
    n10, n11 = int(((a == 1) & (b == 0)).sum()), int(((a == 1) & (b == 1)).sum())
    p01 = n01 / max(n00 + n01, 1)
    p11 = n11 / max(n10 + n11, 1)
    p1 = (n01 + n11) / max(n00 + n01 + n10 + n11, 1)
    def lg(q, k):
        return k * math.log(q) if k and q > 0 else 0.0
    l0 = lg(1 - p1, n00 + n10) + lg(p1, n01 + n11)
    l1 = lg(1 - p01, n00) + lg(p01, n01) + lg(1 - p11, n10) + lg(p11, n11)
    lr = -2 * (l0 - l1)
    return {"n01": n01, "n11": n11, "p01": p01, "p11": p11, "LR_ind": lr, "p_ind": float(stats.chi2.sf(lr, 1))}


def acf(x: np.ndarray, lags: int) -> np.ndarray:
    x = np.asarray(x, float) - np.nanmean(x)
    v = np.dot(x, x)
    return np.array([np.dot(x[:-k], x[k:]) / v for k in range(1, lags + 1)])


def max_drawdown_windows(logret: np.ndarray, w: int, step: int) -> np.ndarray:
    """Max drawdown (as a simple return, <= 0) of the cumulative log path within windows of w days."""
    c = np.concatenate([[0.0], np.cumsum(logret)])
    out = []
    for s in range(0, len(logret) - w + 1, step):
        seg = c[s:s + w + 1] - c[s]
        out.append(np.expm1((seg - np.maximum.accumulate(seg)).min()))
    return np.array(out)


def window_sums(logret: np.ndarray, w: int, step: int) -> np.ndarray:
    c = np.concatenate([[0.0], np.cumsum(logret)])
    s = np.arange(0, len(logret) - w + 1, step)
    return c[s + w] - c[s]


def std_t_ppf(p, nu):
    return stats.norm.ppf(p) if not np.isfinite(nu) else stats.t.ppf(p, nu) * math.sqrt((nu - 2) / nu)


def std_t_cdf(z, nu):
    return stats.norm.cdf(z) if not np.isfinite(nu) else stats.t.cdf(z / math.sqrt((nu - 2) / nu), nu)


# --------------------------------------------------------------------------- expanding refits (cached)
def _cache_key(x: np.ndarray, *parts) -> str:
    h = hashlib.sha256(np.ascontiguousarray(np.nan_to_num(x, nan=-9e9)).tobytes())
    h.update(json.dumps([*parts, arch.__version__], default=str).encode())
    return h.hexdigest()[:16]


def expanding_job(args: dict) -> dict:
    """args: x (np.ndarray), dates (list[str]), name, min_obs, step, window, scale, cache_dir, tag."""
    x = pd.Series(args["x"], index=pd.DatetimeIndex(args["dates"]))
    key = _cache_key(args["x"], args["name"], args["min_obs"], args["step"], args.get("window"), args["scale"])
    path = Path(args["cache_dir"]) / f"{args['tag']}_{key}.json"
    if path.exists():
        return {"tag": args["tag"], "fits": json.loads(path.read_text()), "cached": True}
    fits = M.expanding_fits(x.dropna() if args.get("dropna") else x, args["name"], args["min_obs"],
                            args["step"], args["scale"], window=args.get("window"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fits, default=float))
    return {"tag": args["tag"], "fits": fits, "cached": False}


# --------------------------------------------------------------------------- PIT
def pit_starts(index: pd.DatetimeIndex, first_ok: int, month_step: int) -> list[int]:
    per = index.to_period("M")
    first_of_month = np.r_[True, per[1:] != per[:-1]]
    months = per.month
    cand = np.where(first_of_month & ((months - 1) % month_step == 0))[0]
    return [int(t) for t in cand if t + 1 >= first_ok]


def rate_params_at(frame_upto: pd.DataFrame, cfg: dict, rate_frame: pd.DataFrame | None = None) -> dict:
    """Rate model fitted on rate_frame (default frame_upto); equity-rate correlation from frame_upto."""
    rm = M.monthly_rate(frame_upto if rate_frame is None else rate_frame)
    start = pd.Period(cfg["rate"]["start_date"], "M") if cfg["rate"].get("start_date") else rm.index[0]
    use = rm[rm.index >= start] if (rm.index >= start).sum() >= cfg["rate"]["pit_min_months"] else rm
    vas = M.fit_vasicek(use)
    cor = M.rate_equity_correlation(frame_upto, vas, cfg["rate"].get("corr_alignment", "same_month"))
    return {"mode": cfg["rate"]["mode"], "kappa": vas["kappa"], "theta": vas["theta"], "s": vas["s"],
            "rho": cor["rho"] if np.isfinite(cor["rho"]) else 0.0,
            "floor_at_zero": cfg["rate"]["floor_at_zero"]}


def pit_job(args: dict) -> dict:
    """Conditional tail calibration for one model/variant, using expanding-window everything.

    args: frame_path, x_model (np.ndarray), fits, name, innovations, cfg, edges, tag.
    """
    cfg = args["cfg"]
    pc = cfg["validation"]["pit"]
    N = cfg["trading_days_per_year"]
    raw = pd.read_parquet(args["frame_path"])
    frame = raw.loc[args["start_date"]:] if args.get("start_date") else raw
    x_model = np.asarray(args["x_model"])
    spec = M.MODEL_SPECS[args["name"]]
    kind = spec["kind"]
    fits = args["fits"]
    s2_exp = M.expanding_sigma2(x_model, fits, kind)
    k0 = fits[0]["k"]
    H = pc["horizons"]
    Hmax = max(H)
    n = len(frame)
    logr = np.log1p(frame["r"].to_numpy())
    clog = np.concatenate([[0.0], np.cumsum(logr)])
    starts = [t for t in pit_starts(frame.index, k0, pc["start_month_step"]) if t + min(H) <= n - 1]
    rows = []
    seed0 = int(cfg["model"]["seed"]) + 10_000
    for i, t in enumerate(starts):
        f = M.fits_at(fits, t + 1)
        p = {**f["params"], "kind": kind}
        upto = frame.iloc[: t + 1]
        prem = M.premium_estimate(upto, cfg["model"]["premium"], N)
        prem_p = M.premium_params(prem, s2_exp[k0: t + 2], N)
        rate_p = rate_params_at(upto, cfg, raw.loc[: upto.index[-1]])
        if cfg["rate"]["mode"] == "constant":
            rate_p["mode"] = "constant"
        state0 = {"sigma0_ann": math.sqrt(s2_exp[t + 1] * N), "r0_ann": float(frame["rf_ann"].iloc[t])}
        zpool = None
        if args["innovations"] == "fhs":
            zpool = (x_model[k0: t + 1] - p["mu"]) / np.sqrt(s2_exp[k0: t + 1])
        sp = simulate_paths(p, rate_p, state0, Hmax, pc["n_paths"], seed0 + i,
                            innovations=args["innovations"], premium=prem_p, zpool=zpool, N=N)
        cum = sp.cum_log_total
        row = {"date": str(frame.index[t].date()), "t": t, "sigma0_ann": state0["sigma0_ann"],
               "r0_ann": state0["r0_ann"], "n_clipped": sp.meta["n_clipped"]}
        for h in H:
            if t + h <= n - 1:
                real = clog[t + 1 + h] - clog[t + 1]
                sim = cum[:, h - 1]
                row[f"u{h}"] = float((sim <= real).mean())
                row[f"real{h}"] = float(real)
                row[f"q02_{h}"] = float(np.quantile(sim, 0.02))
                row[f"q05_{h}"] = float(np.quantile(sim, 0.05))
            else:
                row[f"u{h}"] = np.nan
        rows.append(row)
    df = pd.DataFrame(rows)
    df["bucket"] = assign_bucket(df["sigma0_ann"].to_numpy(), args["edges"])
    df["era"] = assign_era(pd.DatetimeIndex(df["date"]), cfg)
    return {"tag": args["tag"], "pit": df.to_dict(orient="list")}


def pit_table(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Observed tail frequencies vs nominal, overall / by bucket / by era, with overlap-aware CIs."""
    pc = cfg["validation"]["pit"]
    step_days = pc["start_month_step"] * cfg["trading_days_per_year"] / 12
    groups = [("overall", "all", df)]
    groups += [("bucket", b, df[df["bucket"] == b]) for b in pc["buckets"]]
    groups += [("bucket_group", g, df[df["bucket"].isin(members)])
               for g, members in (pc.get("bucket_groups") or {}).items()]
    groups += [("era", e[0], df[df["era"] == e[0]]) for e in pc["eras"]]
    rows = []
    for gtype, g, d in groups:
        for h in pc["horizons"]:
            u = d[f"u{h}"].dropna().to_numpy()
            n = len(u)
            n_eff = min(n, n * step_days / h)
            for lv in pc["levels"]:
                k = int((u < lv).sum())
                ph = k / n if n else float("nan")
                lo, hi = wilson_ci(ph, n_eff, pc["ci_level"]) if n else (np.nan, np.nan)
                if not n:
                    verdict = "no data"
                elif n_eff < pc.get("min_n_eff_verdict", 0):
                    verdict = "inconclusive"
                elif lv < lo:
                    verdict = "too thin"
                elif lv > hi:
                    verdict = "too fat"
                else:
                    verdict = "consistent"
                rows.append({"group_type": gtype, "group": g, "H": h, "level": lv, "n_starts": n,
                             "n_eff": n_eff, "count": k, "freq": ph, "ci_lo": lo, "ci_hi": hi,
                             "verdict": verdict})
    return pd.DataFrame(rows)


def pit_score(tab: pd.DataFrame) -> float:
    """Calibration score: mean |freq - nominal| / nominal over bucket rows at levels <= 0.05."""
    t = tab[(tab.group_type == "bucket") & (tab.level <= 0.05) & (tab.n_starts > 0)]
    return float((np.abs(t.freq - t.level) / t.level).mean())


def group_verdict(tab: pd.DataFrame, gtype: str, group: str, H: int) -> str:
    v = tab[(tab.group_type == gtype) & (tab.group == group) & (tab.H == H)]["verdict"].tolist()
    if not v or all(x == "no data" for x in v):
        return "no data"
    if all(x == "inconclusive" for x in v):
        return "inconclusive: too few independent windows to test (CI would include 0% and >20%)"
    thin, fat = "too thin" in v, "too fat" in v
    if thin and fat:
        return "mixed"
    if thin:
        return "too thin (more realised left-tail outcomes than the model predicts)"
    if fat:
        return "too fat (fewer realised left-tail outcomes than the model predicts)"
    w = tab[(tab.group_type == gtype) & (tab.group == group) & (tab.H == H) & (tab.level == 0.02)]
    width = float((w.ci_hi - w.ci_lo).iloc[0]) if len(w) else float("nan")
    return (f"no evidence of miscalibration (nominal inside the CI at all levels), but low power: "
            f"the CI at the 2% level spans {width:.0%}")


# --------------------------------------------------------------------------- one-day VaR backtest
def var_backtest(x: np.ndarray, s2_exp: np.ndarray, fits: list[dict], kind: str, levels, buckets: np.ndarray,
                 gap_scale: np.ndarray | None = None) -> pd.DataFrame:
    n = len(x)
    k0 = fits[0]["k"]
    mu = np.full(n, np.nan)
    nu = np.full(n, np.inf)
    for j, f in enumerate(fits):
        k_end = fits[j + 1]["k"] if j + 1 < len(fits) else n
        mu[f["k"]:k_end] = f["params"]["mu"]
        nu[f["k"]:k_end] = f["params"].get("nu", np.inf)
    sig = np.sqrt(s2_exp[:n])
    if gap_scale is not None:
        sig = sig * gap_scale
        mu = mu * gap_scale ** 2
    idx = np.arange(k0, n)
    rows = []
    for p in levels:
        ppf = {v: std_t_ppf(p, v) for v in np.unique(nu[idx])}
        q = mu[idx] + sig[idx] * np.array([ppf[v] for v in nu[idx]])
        hits = x[idx] < q
        for g in ["all"] + sorted(set(buckets[idx]) - {None}):
            m = np.ones(len(idx), bool) if g == "all" else (buckets[idx] == g)
            h = hits[m]
            r = {"level": p, "bucket": g, **kupiec(h, p)}
            r.update(christoffersen(h))
            rows.append(r)
    return pd.DataFrame(rows)
