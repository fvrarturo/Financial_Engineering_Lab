"""Fitting the ladder of models to CRSP bond prices.

Objective: duration-weighted price errors, (P_model - P_obs) / (P_obs * D_mac), which is (to first order) the
error in the bond's yield. After fitting we report exact yield errors: model yield minus market yield, in bp,
where the model yield is the yield-to-maturity of the model price."""
import numpy as np
import pandas as pd
from scipy.optimize import least_squares

from .models import MODELS
from .data import ytm_cont

# parameter names, bounds and a few starting points per model (decimal units)
SPEC = {
    "Nelson-Siegel": dict(names=["b0", "b1", "b2", "lam"],
                          lb=[-0.05, -0.2, -0.3, 0.05], ub=[0.25, 0.2, 0.3, 15.0],
                          x0=[[0.04, -0.01, 0.0, 2.0], [0.04, 0.01, -0.02, 1.0], [0.05, -0.03, 0.03, 5.0]]),
    "Svensson": dict(names=["b0", "b1", "b2", "b3", "lam1", "lam2"],
                     lb=[-0.05, -0.2, -0.3, -0.3, 0.05, 0.5], ub=[0.25, 0.2, 0.3, 0.3, 5.0, 30.0],
                     x0=[[0.04, -0.01, 0.0, 0.0, 1.0, 8.0], [0.04, 0.01, -0.02, 0.02, 0.5, 10.0],
                         [0.05, -0.02, 0.02, -0.02, 2.0, 15.0]]),
    "Vasicek": dict(names=["kappa", "theta", "sigma", "r0"],
                    lb=[1e-3, -0.05, 1e-4, -0.02], ub=[5.0, 0.25, 0.1, 0.25],
                    x0=[[0.2, 0.05, 0.01, 0.03], [0.6, 0.04, 0.02, 0.05], [0.05, 0.06, 0.005, 0.01]]),
    "CIR": dict(names=["kappa", "theta", "sigma", "r0"],
                lb=[1e-3, 1e-4, 1e-3, 1e-6], ub=[5.0, 0.3, 0.5, 0.25],
                x0=[[0.2, 0.05, 0.05, 0.03], [0.6, 0.04, 0.1, 0.05], [0.05, 0.06, 0.03, 0.01]]),
    "G2++": dict(names=["a1", "a2", "s1", "s2", "rho", "delta", "x1", "x2"],
                 lb=[1e-3, 1e-3, 1e-4, 1e-4, -0.99, -0.05, -0.3, -0.3], ub=[3.0, 5.0, 0.1, 0.1, 0.99, 0.25, 0.3, 0.3],
                 x0=[[0.05, 1.0, 0.01, 0.015, -0.5, 0.05, -0.01, 0.0],
                     [0.1, 2.0, 0.01, 0.02, -0.8, 0.04, 0.0, 0.01],
                     [0.02, 0.5, 0.005, 0.01, 0.0, 0.06, -0.02, -0.01]]),
}
# for "shared-parameter" fits: which parameters are state variables (free per date); the rest are common
STATE = {"Vasicek": ["r0"], "CIR": ["r0"], "G2++": ["x1", "x2"]}


def model_prices(model, p, T, CF):
    mask = CF != 0
    D = np.where(mask, np.exp(-MODELS[model](np.where(mask, T, 1.0), p) * T), 0.0)
    return (CF * D).sum(1)


def _resid(model, p, cs):
    b = cs["bonds"]
    P = model_prices(model, p, cs["T"], cs["CF"])
    return (P - b.dirty.values) / (b.dirty.values * b.dur.values) * 1e4   # ~ yield error in bp


def fit_date(model, cs):
    """Fit one model to one date's cross-section. Returns (params dict, result table)."""
    s = SPEC[model]
    best = None
    for x0 in s["x0"]:
        r = least_squares(lambda p: _resid(model, p, cs), x0, bounds=(s["lb"], s["ub"]), x_scale="jac",
                          max_nfev=4000)
        if best is None or r.cost < best.cost:
            best = r
    return dict(zip(s["names"], best.x)), errors(model, best.x, cs)


def errors(model, p, cs):
    b = cs["bonds"].copy()
    b["model_price"] = model_prices(model, p, cs["T"], cs["CF"])
    b["model_ytm"] = ytm_cont(b.model_price.values, cs["T"], cs["CF"]) * 100
    b["err_bp"] = (b.model_ytm - b.ytm) * 100
    b["price_err"] = b.model_price - b.dirty
    return b


def fit_shared(model, css):
    """Fit one model to several dates at once: structural parameters common to all dates, state variables
    (short rate / factors) free per date. This is the 'one model for every regime' test."""
    s = SPEC[model]
    st = STATE[model]
    common = [n for n in s["names"] if n not in st]
    ic = [s["names"].index(n) for n in common]
    isv = [s["names"].index(n) for n in st]
    nd = len(css)

    def unpack(z):
        c = z[:len(common)]
        out = []
        for j in range(nd):
            p = np.empty(len(s["names"]))
            p[ic] = c
            p[isv] = z[len(common) + j * len(st): len(common) + (j + 1) * len(st)]
            out.append(p)
        return out

    def res(z):
        return np.concatenate([_resid(model, p, cs) for p, cs in zip(unpack(z), css)])

    lb = [s["lb"][i] for i in ic] + [s["lb"][i] for i in isv] * nd
    ub = [s["ub"][i] for i in ic] + [s["ub"][i] for i in isv] * nd
    best = None
    for x0 in s["x0"]:
        z0 = [x0[i] for i in ic] + [x0[i] for i in isv] * nd
        r = least_squares(res, z0, bounds=(lb, ub), x_scale="jac", max_nfev=20000)
        if best is None or r.cost < best.cost:
            best = r
    ps = unpack(best.x)
    params = pd.DataFrame([dict(zip(s["names"], p)) for p in ps], index=[cs["date"] for cs in css])
    errs = [errors(model, p, cs) for p, cs in zip(ps, css)]
    return params, errs


def summary(err):
    e = err.err_bp
    return pd.Series({"RMSE (bp)": np.sqrt((e**2).mean()), "MAE (bp)": e.abs().mean(), "max |err| (bp)": e.abs().max(),
                      "n bonds": len(e)})
