"""Equity return model (GARCH family on daily log excess returns), premium, rate model.

Units: all stored parameters are in **decimal daily** units. `arch` is fitted on 100*x and converted:
  mu_dec = mu/100;  GARCH/GJR: omega_dec = omega/1e4 (alpha, gamma, beta unchanged);
  EGARCH: ln s2_dec = ln s2_pct - ln(1e4)  =>  omega_dec = omega + (beta-1)*ln(1e4).

Model specs (``kind``):  'garch' (p=1,q=1), 'gjr' (p=1,o=1,q=1), 'egarch' (p=1,o=1,q=1).
Distributions: 'normal' | 't' (standardised Student-t, unit variance).

Variance recursions (the same equations are used by ``filter_variance`` and the simulator):
  GARCH/GJR: s2_{t+1} = omega + (alpha + gamma*1{e_t<0}) e_t^2 + beta s2_t,     e_t = x_t - mu
  EGARCH:    ln s2_{t+1} = omega + alpha(|e_t/s_t| - sqrt(2/pi)) + gamma e_t/s_t + beta ln s2_t
"""
from __future__ import annotations

import math
import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd
from arch import arch_model

SQRT2_OV_PI = math.sqrt(2.0 / math.pi)
PARAM_ORDER = ["mu", "omega", "alpha", "gamma", "beta", "nu"]

MODEL_SPECS = {
    "garch_n": {"kind": "garch", "dist": "normal"},
    "garch_t": {"kind": "garch", "dist": "t"},
    "gjr_t": {"kind": "gjr", "dist": "t"},
    "egarch_t": {"kind": "egarch", "dist": "t"},
}


# --------------------------------------------------------------------------- fitting
def _arch(y: np.ndarray, kind: str, dist: str):
    vol = "EGARCH" if kind == "egarch" else "GARCH"
    o = 0 if kind == "garch" else 1
    return arch_model(y, mean="Constant", vol=vol, p=1, o=o, q=1,
                      dist="t" if dist == "t" else "normal", rescale=False)


def _to_decimal(p: dict, kind: str, scale: float) -> dict:
    s2 = scale ** 2
    out = {"mu": p["mu"] / scale, "alpha": p["alpha[1]"], "gamma": p.get("gamma[1]", 0.0),
           "beta": p["beta[1]"], "nu": p.get("nu", np.inf)}
    if kind == "egarch":
        out["omega"] = p["omega"] + (p["beta[1]"] - 1.0) * math.log(s2)
    else:
        out["omega"] = p["omega"] / s2
    return out


def _jacobian(names: list[str], est: dict, kind: str, scale: float) -> np.ndarray:
    """d(decimal params in PARAM_ORDER present)/d(arch params) for delta-method / draws."""
    idx = {n: i for i, n in enumerate(names)}
    k = len(names)
    J = np.zeros((k, k))
    for i, n in enumerate(names):
        J[i, i] = 1.0
    J[idx["mu"], idx["mu"]] = 1.0 / scale
    if kind == "egarch":
        J[idx["omega"], idx["beta[1]"]] = math.log(scale ** 2)
    else:
        J[idx["omega"], idx["omega"]] = 1.0 / scale ** 2
    return J


@dataclass
class Fit:
    name: str
    kind: str
    dist: str
    params: dict          # decimal daily units, keys PARAM_ORDER
    se: dict              # robust SEs, decimal units
    cov_arch: np.ndarray  # robust covariance in arch (scaled) units
    params_arch: dict
    loglik: float         # log-likelihood of the *decimal* data (scaled-data loglik + n*log(scale))
    aic: float
    bic: float
    nobs: int
    backcast: float       # decimal units (variance, or log-variance for EGARCH)
    converged: bool
    start: str | None = None
    end: str | None = None

    def summary(self) -> dict:
        return {"model": self.name, "nobs": self.nobs, "start": self.start, "end": self.end,
                "loglik": self.loglik, "aic": self.aic, "bic": self.bic,
                **{k: self.params[k] for k in PARAM_ORDER},
                **{f"se_{k}": self.se.get(k, np.nan) for k in PARAM_ORDER},
                "persistence": persistence(self.params, self.kind),
                "half_life_days": half_life(self.params, self.kind),
                "uncond_vol_ann": uncond_vol_ann(self.params, self.kind, self.dist),
                "converged": self.converged}


def fit_garch(x: pd.Series | np.ndarray, name: str = "gjr_t", scale: float = 100.0,
              starting_values: np.ndarray | None = None) -> Fit:
    spec = MODEL_SPECS[name]
    kind, dist = spec["kind"], spec["dist"]
    xs = pd.Series(x).dropna()
    y = scale * xs.to_numpy(dtype=float)
    am = _arch(y, kind, dist)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = am.fit(disp="off", starting_values=starting_values, options={"maxiter": 1000})
        if res.convergence_flag != 0 and starting_values is not None:
            res = am.fit(disp="off", options={"maxiter": 1000})
    p_arch = dict(res.params)
    names = list(res.params.index)
    params = _to_decimal(p_arch, kind, scale)
    cov = np.asarray(res.param_cov)
    J = _jacobian(names, p_arch, kind, scale)
    cov_dec = J @ cov @ J.T
    se_dec = np.sqrt(np.clip(np.diag(cov_dec), 0, None))
    name_map = {"mu": "mu", "omega": "omega", "alpha[1]": "alpha", "gamma[1]": "gamma",
                "beta[1]": "beta", "nu": "nu"}
    se = {name_map[n]: float(se_dec[i]) for i, n in enumerate(names)}
    bc = float(am._backcast)  # arch computes it from residuals at the *starting* mean (y - mean(y))
    bc_dec = (bc - math.log(scale ** 2)) if kind == "egarch" else bc / scale ** 2
    ll = float(res.loglikelihood) + len(y) * math.log(scale)   # change of variables to decimal data
    k = len(names)
    idx = xs.index if isinstance(xs.index, pd.DatetimeIndex) else None
    return Fit(name=name, kind=kind, dist=dist, params=params, se=se, cov_arch=cov,
               params_arch=p_arch, loglik=ll, aic=-2 * ll + 2 * k, bic=-2 * ll + k * math.log(len(y)),
               nobs=len(y), backcast=float(bc_dec), converged=res.convergence_flag == 0,
               start=str(idx[0].date()) if idx is not None else None,
               end=str(idx[-1].date()) if idx is not None else None)


# --------------------------------------------------------------------------- derived quantities
def persistence(p: dict, kind: str) -> float:
    if kind == "egarch":
        return float(p["beta"])
    return float(p["alpha"] + 0.5 * p.get("gamma", 0.0) + p["beta"])


def half_life(p: dict, kind: str) -> float:
    ph = persistence(p, kind)
    return float(math.log(0.5) / math.log(ph)) if 0 < ph < 1 else float("inf")


def expected_abs_t(nu: float) -> float:
    """E|z| for a unit-variance standardised t (normal if nu=inf)."""
    if not np.isfinite(nu):
        return SQRT2_OV_PI
    from scipy.special import gammaln
    return float(math.sqrt(nu - 2) * math.exp(gammaln((nu + 1) / 2) - gammaln(nu / 2)) / math.sqrt(math.pi) * 2 / (nu - 1))


def uncond_var_daily(p: dict, kind: str, dist: str = "t") -> float:
    ph = persistence(p, kind)
    if not ph < 1:
        return float("nan")
    if kind == "egarch":
        e_abs = expected_abs_t(p.get("nu", np.inf)) if dist == "t" else SQRT2_OV_PI
        # E[ln s2] (Jensen gap ignored: approximate)
        return float(math.exp((p["omega"] + p["alpha"] * (e_abs - SQRT2_OV_PI)) / (1 - p["beta"])))
    return float(p["omega"] / (1 - ph))


def uncond_vol_ann(p: dict, kind: str, dist: str = "t", N: int = 252) -> float:
    return float(math.sqrt(uncond_var_daily(p, kind, dist) * N))


# --------------------------------------------------------------------------- filtering
def filter_variance(x: np.ndarray, params: dict | list[dict], kind: str, backcast) -> np.ndarray:
    """Conditional variance path, vectorised over parameter sets.

    Returns array of shape (n+1,) [single set] or (n+1, J): element t is the variance for day t
    given data up to t-1; element n is the one-step-ahead variance after the last observation.
    NaN observations are skipped: the recursion carries the variance through as s2_{t+1} =
    omega + (alpha + gamma/2) s2_t + beta s2_t (expected update), or the EGARCH analogue.
    """
    single = isinstance(params, dict)
    plist = [params] if single else list(params)
    g = lambda k: np.array([q.get(k, 0.0) for q in plist], dtype=float)  # noqa: E731
    mu, om, al, ga, be = g("mu"), g("omega"), g("alpha"), g("gamma"), g("beta")
    bc = np.broadcast_to(np.asarray(backcast, dtype=float), mu.shape).copy()
    x = np.asarray(x, dtype=float)
    n = len(x)
    out = np.empty((n + 1, len(plist)))
    if kind == "egarch":
        e_abs_nan = np.array([expected_abs_t(q.get("nu", np.inf)) for q in plist])
        lns2 = om + be * bc   # t=0: arch adds no shock terms before the first observation
        for t in range(n):
            out[t] = np.exp(lns2)
            s = np.sqrt(out[t])
            if np.isnan(x[t]):
                lns2 = om + al * (e_abs_nan - SQRT2_OV_PI) + be * lns2
            else:
                z = (x[t] - mu) / s
                lns2 = om + al * (np.abs(z) - SQRT2_OV_PI) + ga * z + be * lns2
        out[n] = np.exp(lns2)
    else:
        s2 = om + (al + 0.5 * ga) * bc + be * bc
        for t in range(n):
            out[t] = s2
            if np.isnan(x[t]):
                s2 = om + (al + 0.5 * ga + be) * s2
            else:
                e = x[t] - mu
                e2 = e * e
                s2 = om + (al + ga * (e < 0)) * e2 + be * s2
        out[n] = s2
    return out[:, 0] if single else out


def backcast_value(x: np.ndarray, mu: float, kind: str) -> float:
    """arch's backcast (EWMA 0.94 over first 75 residuals), in decimal units. Pass mu = sample
    mean of x to reproduce arch exactly (arch uses residuals at its starting values)."""
    r = np.asarray(x, dtype=float)
    r = r[~np.isnan(r)] - mu
    tau = min(75, len(r))
    w = 0.94 ** np.arange(tau)
    w /= w.sum()
    bc = float(np.sum(r[:tau] ** 2 * w))
    return math.log(bc) if kind == "egarch" else bc


def std_residuals(x: np.ndarray, fit: Fit) -> np.ndarray:
    s2 = filter_variance(x, fit.params, fit.kind, fit.backcast)[:-1]
    return (np.asarray(x) - fit.params["mu"]) / np.sqrt(s2)


# --------------------------------------------------------------------------- closure gaps
def gap_flags(index: pd.DatetimeIndex, gaps: list[dict], min_calendar_days: int) -> pd.DataFrame:
    """Returns that follow a gap of more than ``min_calendar_days`` calendar days.

    ``gaps`` is the list from reports/data_check/data_check.json (``from``, ``to``, ``calendar_days``).
    ``busdays`` = Mon-Fri days from the previous observation to this one (1 for a normal day/weekend).
    """
    rows = []
    for g in gaps:
        if g["calendar_days"] > min_calendar_days and pd.Timestamp(g["to"]) in index:
            k = int(np.busday_count(g["from"], g["to"]))
            rows.append({"date": pd.Timestamp(g["to"]), "from": g["from"],
                         "calendar_days": g["calendar_days"], "busdays": max(k, 1)})
    return pd.DataFrame(rows, columns=["date", "from", "calendar_days", "busdays"]).set_index("date")


def adjust_for_gaps(x: pd.Series, flags: pd.DataFrame, method: str) -> pd.Series:
    """method: 'none' | 'scale' (divide by sqrt(busdays)) | 'exclude' (set NaN)."""
    y = x.copy()
    if method == "none" or flags.empty:
        return y
    if method == "scale":
        y.loc[flags.index] = y.loc[flags.index] / np.sqrt(flags["busdays"].to_numpy())
    elif method == "exclude":
        y.loc[flags.index] = np.nan
    else:
        raise ValueError(method)
    return y


def gap_loglik_jacobian(flags: pd.DataFrame, method: str) -> float:
    """Add to a 'scale'-variant loglik to express it as the density of the *unscaled* x."""
    if method == "scale" and not flags.empty:
        return float(-0.5 * np.log(flags["busdays"].to_numpy()).sum())
    return 0.0


# --------------------------------------------------------------------------- premium
def premium_estimate(frame: pd.DataFrame, cfg_premium: dict, N: int = 252) -> dict:
    """Arithmetic daily excess return (r - rf) mean x N, its SE, and the shrunk value used."""
    ex = (frame["r"] - frame["rf"]).dropna()
    raw = float(ex.mean() * N)
    se = float(ex.std() / math.sqrt(len(ex)) * N)
    years = len(ex) / N
    if cfg_premium.get("annual_excess", "estimate") == "estimate":
        w, tgt = float(cfg_premium["shrink_weight"]), float(cfg_premium["shrink_to"])
        used = (1 - w) * raw + w * tgt
        source = "estimate (shrunk)"
    else:
        used = float(cfg_premium["annual_excess"])
        source = "config value"
    return {"mode": cfg_premium["mode"], "raw_annual": raw, "se_annual": se, "years": years,
            "shrink_to": cfg_premium.get("shrink_to"), "shrink_weight": cfg_premium.get("shrink_weight"),
            "annual_excess_used": used, "source": source,
            "t_stat_raw": raw / se if se > 0 else float("nan")}


def premium_params(prem: dict, sigma2_daily: np.ndarray, N: int = 252) -> dict:
    """Resolve mode-specific coefficient so the *average* premium over the window equals mu_e."""
    mu_d = prem["annual_excess_used"] / N
    mode = prem["mode"]
    out = {"mode": mode, "mu_e_daily": mu_d}
    if mode == "prop_variance":
        out["lambda"] = mu_d / float(np.nanmean(sigma2_daily))
    elif mode == "constant_sharpe":
        out["S"] = mu_d / float(np.nanmean(np.sqrt(sigma2_daily)))
    elif mode != "constant_arith":
        raise ValueError(mode)
    return out


def premium_mean(prem: dict, s2: np.ndarray) -> np.ndarray:
    """Conditional *arithmetic* expected excess return per day, given variance s2."""
    m = prem["mode"]
    if m == "constant_arith":
        return np.full_like(s2, prem["mu_e_daily"])
    if m == "prop_variance":
        return prem["lambda"] * s2
    return prem["S"] * np.sqrt(s2)


# --------------------------------------------------------------------------- rate model
def monthly_rate(frame: pd.DataFrame) -> pd.Series:
    """rf_ann sampled monthly (it is constant within a month by construction)."""
    s = frame["rf_ann"].groupby(frame.index.to_period("M")).first()
    return s


def fit_vasicek(r_m: pd.Series, dt: float = 1 / 12) -> dict:
    """OLS on r_{m+1} - r_m = a + b r_m + e. kappa=-b/dt, theta=-a/b, s=sd(e)/sqrt(dt).
    If kappa <= 0 the series is treated as a random walk (kappa=0, theta=last rate)."""
    r = r_m.dropna().to_numpy()
    dr, lag = np.diff(r), r[:-1]
    X = np.column_stack([np.ones_like(lag), lag])
    coef, *_ = np.linalg.lstsq(X, dr, rcond=None)
    a, b = coef
    resid = dr - X @ coef
    n = len(dr)
    s_e = float(np.sqrt(resid @ resid / (n - 2)))
    covb = s_e ** 2 * np.linalg.inv(X.T @ X)
    kappa = -b / dt
    out = {"kappa": float(kappa), "theta": float(-a / b) if b != 0 else float(r[-1]),
           "s": s_e / math.sqrt(dt), "dt_fit": dt, "n_months": int(n + 1),
           "start": str(r_m.dropna().index[0]), "end": str(r_m.dropna().index[-1]),
           "se_kappa": float(math.sqrt(covb[1, 1]) / dt), "mean_rate": float(r.mean()),
           "share_negative_months": float((r < 0).mean()), "n_negative_months": int((r < 0).sum()),
           "random_walk_fallback": False}
    if kappa <= 0:
        out.update({"kappa": 0.0, "theta": float(r[-1]), "random_walk_fallback": True})
    out["half_life_years"] = math.log(2) / out["kappa"] if out["kappa"] > 0 else float("inf")
    out["stationary_sd"] = out["s"] / math.sqrt(2 * out["kappa"]) if out["kappa"] > 0 else float("inf")
    out["_resid"] = pd.Series(resid, index=r_m.dropna().index[1:])
    return out


def rate_equity_correlation(frame: pd.DataFrame, vas: dict, alignment: str = "same_month") -> dict:
    """Correlation of monthly rate innovations with monthly equity excess log returns.

    resid at label m is the innovation in rf_ann for month m (the 1-month bill rate set at the start of m)
    relative to the AR(1) forecast from m-1. Two alignments:
      same_month : (e_m, x summed over month m)        [base, decided 2026-11-20]
      prev_month : (e_m, x summed over month m-1), i.e. equity over the month in which the new bill
                   rate was being revealed.
    `rho` is the one selected by `alignment`; both are always reported.
    """
    resid = vas["_resid"]
    xm = frame["x"].groupby(frame.index.to_period("M")).sum()
    aligned = pd.concat([resid.rename("e"), xm.shift(1).rename("x_prev_month"),
                         xm.rename("x_same_month")], axis=1).dropna()
    rho_prev = float(aligned["e"].corr(aligned["x_prev_month"]))
    rho_same = float(aligned["e"].corr(aligned["x_same_month"]))
    if alignment not in ("same_month", "prev_month"):
        raise ValueError(f"unknown alignment {alignment!r}")
    rho = rho_same if alignment == "same_month" else rho_prev
    n = len(aligned)
    return {"rho": rho, "rho_same_month_label": rho_same, "rho_prev_month": rho_prev, "n_months": n,
            "se_approx": float((1 - rho ** 2) / math.sqrt(n - 1)), "alignment": alignment}


# --------------------------------------------------------------------------- expanding / rolling refits
def refit_points(n: int, min_obs: int, step: int) -> list[int]:
    return list(range(min_obs, n, step))


def expanding_fits(x: pd.Series, name: str, min_obs: int, step: int, scale: float = 100.0,
                   window: int | None = None) -> list[dict]:
    """Fit on x[:k] (data strictly before index k) for k in refit_points; rolling if window is set.

    Returns list of {"k": k, "date_first_use": date at index k, "params": ..., "backcast": ...}.
    """
    out, sv = [], None
    for k in refit_points(len(x), min_obs, step):
        lo = max(0, k - window) if window else 0
        f = fit_garch(x.iloc[lo:k], name, scale, starting_values=sv)
        sv = np.array(list(f.params_arch.values())) if f.converged else None
        out.append({"k": k, "lo": lo, "date_first_use": str(x.index[k].date()),
                    "params": f.params, "se": f.se, "backcast": f.backcast,
                    "loglik": f.loglik, "converged": f.converged, "nobs": f.nobs})
    return out


def expanding_sigma2(x: np.ndarray, fits: list[dict], kind: str) -> np.ndarray:
    """Variance for day t (t=0..n) using the params of the latest refit with k <= t.

    Element t uses data x[:t] for the recursion and parameters fitted on x[:k], k <= t:
    strictly no look-ahead. Elements before the first refit are NaN.
    """
    n = len(x)
    if not fits:
        return np.full(n + 1, np.nan)
    all_s2 = filter_variance(x, [f["params"] for f in fits], kind, [f["backcast"] for f in fits])
    ks = np.array([f["k"] for f in fits])
    out = np.full(n + 1, np.nan)
    for j, k in enumerate(ks):
        k_end = ks[j + 1] if j + 1 < len(ks) else n + 1
        out[k:k_end] = all_s2[k:k_end, j]
    return out


def fits_at(fits: list[dict], t: int) -> dict | None:
    """Latest refit usable at day index t (k <= t)."""
    best = None
    for f in fits:
        if f["k"] <= t:
            best = f
    return best


# --------------------------------------------------------------------------- parameter draws
def valid_params(p: dict, kind: str, nu_min: float) -> bool:
    if kind == "egarch":
        ok = abs(p["beta"]) < 1
    else:
        ok = (p["omega"] > 0 and p["alpha"] >= 0 and p["beta"] >= 0 and p["alpha"] + p["gamma"] >= 0
              and persistence(p, kind) < 1)
    if np.isfinite(p.get("nu", np.inf)):
        ok = ok and p["nu"] > nu_min
    return bool(ok)


def draw_params(fit: Fit, n_draws: int, seed: int, nu_min: float = 2.5, scale: float = 100.0) -> tuple[np.ndarray, dict]:
    """Draws from the asymptotic normal of the arch estimates, converted to decimal units.
    Returns (array n_draws x len(PARAM_ORDER), info)."""
    rng = np.random.Generator(np.random.PCG64(seed))
    names = list(fit.params_arch.keys())
    mean = np.array(list(fit.params_arch.values()))
    kept, tried = [], 0
    while len(kept) < n_draws and tried < 200 * n_draws:
        block = rng.multivariate_normal(mean, fit.cov_arch, size=n_draws)
        tried += n_draws
        for row in block:
            p = _to_decimal(dict(zip(names, row)), fit.kind, scale)
            if valid_params(p, fit.kind, nu_min):
                kept.append([p.get(k, 0.0) for k in PARAM_ORDER])
                if len(kept) == n_draws:
                    break
    arr = np.array(kept)
    return arr, {"columns": PARAM_ORDER, "n_draws": len(arr), "n_tried": tried,
                 "acceptance_rate": len(arr) / tried, "seed": seed, "model": fit.name, "nu_min": nu_min}
