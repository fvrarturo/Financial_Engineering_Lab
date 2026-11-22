"""Vectorised path simulator for the equity (GARCH family) + short-rate (Vasicek) model.

Timing convention (day t = 1..n_days after the start date):
  sigma[:, t]  conditional daily vol for day t, known at the start of day t (from data up to t-1)
  r_ann[:, t]  annualised short rate known at the start of day t
  rf[:, t]     = (1 + r_ann[:, t])**(1/N) - 1         cash return earned on day t
  x[:, t]      = m_t + sigma_t z_t,  m_t = mu_t - sigma_t^2/2   (mu_t: arithmetic premium per day)
  r[:, t]      = exp(x_t + log(1+rf_t)) - 1            simple total equity return
State0: {"sigma0_ann": ..., "r0_ann": ...}; sigma[:,0] = sigma0_ann/sqrt(N), r_ann[:,0] = r0_ann.

Rates:  r_{t+1} = r_t + kappa (theta - r_t) dt + s sqrt(dt) eta_t,  dt = 1/N,
        eta_t = rho z_t + sqrt(1-rho^2) xi_t   (z_t unit-variance equity shock; corr(eta, z) = rho).
        rate mode 'constant' holds r at r0.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

import numpy as np

from .model import PARAM_ORDER, SQRT2_OV_PI

MIN_SIMPLE_RETURN = -0.99


@dataclass
class SimPaths:
    x: np.ndarray
    rf: np.ndarray
    r: np.ndarray
    sigma: np.ndarray
    r_ann: np.ndarray
    meta: dict = field(default_factory=dict)

    @property
    def cum_log_total(self) -> np.ndarray:
        return np.cumsum(np.log1p(self.r), axis=1)


def params_hash(*objs) -> str:
    def clean(o):
        if isinstance(o, dict):
            return {k: clean(v) for k, v in sorted(o.items()) if not str(k).startswith("_")}
        if isinstance(o, np.ndarray):
            return hashlib.sha256(np.ascontiguousarray(o).tobytes()).hexdigest()
        if isinstance(o, (np.floating, float)):
            return float(o)
        return o
    return hashlib.sha256(json.dumps([clean(o) for o in objs], default=str).encode()).hexdigest()[:16]


def common_random_numbers(n_paths: int, n_days: int, seed: int, innovations: str = "t",
                          nu: float | np.ndarray = 8.0, zpool: np.ndarray | None = None) -> dict:
    """Pre-draw the equity shocks z (unit variance) and the rate noise xi.

    Pass the returned dict as ``crn`` to ``simulate_paths`` to reuse identical draws across
    strategies or starting states. ``nu`` may be a per-path array (parameter draws).
    """
    rng = np.random.Generator(np.random.PCG64(seed))
    if innovations == "t":
        nu_arr = np.broadcast_to(np.asarray(nu, dtype=float), (n_paths,))
        if np.all(np.isinf(nu_arr)):
            z = rng.standard_normal((n_paths, n_days))
        else:
            z = rng.standard_t(nu_arr[:, None], size=(n_paths, n_days))
            z *= np.sqrt((nu_arr - 2.0) / nu_arr)[:, None]
    elif innovations == "normal":
        z = rng.standard_normal((n_paths, n_days))
    elif innovations == "fhs":
        if zpool is None:
            raise ValueError("fhs innovations need zpool (standardised residuals)")
        pool = np.asarray(zpool, dtype=float)
        pool = pool[np.isfinite(pool)]
        pool = (pool - pool.mean()) / pool.std()
        z = pool[rng.integers(0, len(pool), size=(n_paths, n_days))]
    else:
        raise ValueError(innovations)
    xi = rng.standard_normal((n_paths, n_days))
    return {"z": z, "xi": xi, "seed": seed, "innovations": innovations}


def _param_matrix(model_params: dict, param_draws: np.ndarray | None, n_paths: int) -> dict:
    """Per-path parameter arrays. With param_draws (J x len(PARAM_ORDER)), paths are split into J
    equal contiguous blocks, one parameter vector per block."""
    if param_draws is None:
        return {k: np.full(n_paths, float(model_params.get(k, 0.0 if k != "nu" else np.inf)))
                for k in PARAM_ORDER}
    J = len(param_draws)
    if n_paths % J:
        raise ValueError(f"n_paths ({n_paths}) must be a multiple of the number of param draws ({J})")
    rep = n_paths // J
    return {k: np.repeat(param_draws[:, i], rep) for i, k in enumerate(PARAM_ORDER)}


def simulate_paths(model_params: dict, rate_params: dict, state0: dict, n_days: int, n_paths: int,
                   seed: int, innovations: str = "t", premium: dict | None = None,
                   param_draws: np.ndarray | None = None, zpool: np.ndarray | None = None,
                   crn: dict | None = None, N: int = 252, store: bool = True) -> SimPaths:
    """Simulate equity and rate paths.

    model_params: {"kind": 'garch'|'gjr'|'egarch', "mu","omega","alpha","gamma","beta","nu"} (decimal).
    rate_params:  {"mode": 'vasicek'|'constant', "kappa","theta","s","rho","floor_at_zero"}.
    premium:      output of gf.model.premium_params (mode + coefficient). Required.
    """
    if premium is None:
        raise ValueError("premium must be given explicitly (see gf.model.premium_params)")
    kind = model_params["kind"]
    P = _param_matrix(model_params, param_draws, n_paths)
    if crn is None:
        crn = common_random_numbers(n_paths, n_days, seed, innovations, nu=P["nu"], zpool=zpool)
    z_all, xi_all = crn["z"], crn["xi"]
    if z_all.shape != (n_paths, n_days):
        raise ValueError(f"crn shape {z_all.shape} != {(n_paths, n_days)}")

    om, al, ga, be = P["omega"], P["alpha"], P["gamma"], P["beta"]
    rmode = rate_params.get("mode", "vasicek")
    kappa, theta, s = (rate_params.get(k, 0.0) for k in ("kappa", "theta", "s"))
    rho = float(rate_params.get("rho", 0.0))
    floor0 = bool(rate_params.get("floor_at_zero", False))
    dt = 1.0 / N
    sq_rho = math.sqrt(max(1.0 - rho * rho, 0.0))

    pm = premium["mode"]
    shape = (n_paths, n_days)
    X = np.empty(shape) if store else None
    RF = np.empty(shape) if store else None
    SIG = np.empty(shape) if store else None
    RA = np.empty(shape) if store else None
    cum = np.zeros(n_paths)

    s2 = np.full(n_paths, (state0["sigma0_ann"] ** 2) / N)
    lns2 = np.log(s2)
    ra = np.full(n_paths, float(state0["r0_ann"]))
    n_clip, n_neg_rate = 0, 0
    x_floor_base = math.log1p(MIN_SIMPLE_RETURN)

    for t in range(n_days):
        sig = np.sqrt(s2)
        z = z_all[:, t]
        rf = (1.0 + ra) ** dt - 1.0
        if pm == "constant_arith":
            mu = premium["mu_e_daily"]
        elif pm == "prop_variance":
            mu = premium["lambda"] * s2
        else:
            mu = premium["S"] * sig
        eps = sig * z
        x = mu - 0.5 * s2 + eps
        x_floor = x_floor_base - np.log1p(rf)
        clipped = x < x_floor
        if clipped.any():
            n_clip += int(clipped.sum())
            x = np.where(clipped, x_floor, x)
        if store:
            X[:, t], RF[:, t], SIG[:, t], RA[:, t] = x, rf, sig, ra
        cum += x + np.log1p(rf)
        # variance update with the shock e = sigma*z (deviation from the conditional mean)
        if kind == "egarch":
            lns2 = om + al * (np.abs(z) - SQRT2_OV_PI) + ga * z + be * lns2
            s2 = np.exp(lns2)
        else:
            s2 = om + (al + ga * (eps < 0)) * eps * eps + be * s2
        # rate update
        if rmode == "vasicek":
            eta = rho * z + sq_rho * xi_all[:, t]
            ra = ra + kappa * (theta - ra) * dt + s * math.sqrt(dt) * eta
            neg = ra < 0
            n_neg_rate += int(neg.sum())
            if floor0:
                ra = np.where(neg, 0.0, ra)

    meta = {"seed": seed, "innovations": crn.get("innovations", innovations), "n_paths": n_paths,
            "n_days": n_days, "state0": dict(state0), "kind": kind, "rate_mode": rmode,
            "premium": {k: v for k, v in premium.items()},
            "params_hash": params_hash(model_params, rate_params, premium,
                                       param_draws if param_draws is not None else 0),
            "n_param_draws": 0 if param_draws is None else len(param_draws),
            "n_clipped": n_clip, "share_negative_rate_path_days": n_neg_rate / (n_paths * n_days),
            "floor_at_zero": floor0, "cum_log_total_final": None}
    if not store:
        meta["cum_log_total_final"] = cum
        empty = np.empty((0, 0))
        return SimPaths(empty, empty, empty, empty, empty, meta)
    R = np.expm1(X + np.log1p(RF))
    return SimPaths(x=X, rf=RF, r=R, sigma=SIG, r_ann=RA, meta=meta)


def cum_log_total_at(n_days_list, model_params, rate_params, state0, n_paths, seed, **kw) -> dict:
    """Cumulative log total return at several horizons from one simulation (memory-light)."""
    H = max(n_days_list)
    sp = simulate_paths(model_params, rate_params, state0, H, n_paths, seed, **kw)
    c = sp.cum_log_total
    return {h: c[:, h - 1] for h in n_days_list}, sp.meta
