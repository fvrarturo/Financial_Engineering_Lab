"""Using the fitted curves: pricing bonds the model has not seen, factor risk and hedging, and a bond option.

- holdout(): k-fold out-of-sample pricing. Fit on 80% of the bonds on a date, price the other 20%.
- factor_exposure(): price sensitivity of a bond to each G2++ factor, dP/dx_k = -sum_j CF_j D(t_j) B_k(t_j).
- hedge_test(): hold a 10-year note for a year, hedge it daily with 2- and 30-year notes using either duration (one
  factor, a parallel shift) or the two G2++ factors, and compare daily P&L.
- g2_zbc(): closed-form price of a European call on a zero-coupon bond under G2++ (Brigo & Mercurio, 2006, ch. 4),
  checked against a Monte Carlo simulation of the two factors under Q (g2_zbc_mc)."""
import numpy as np
import pandas as pd
from scipy.stats import norm

from . import data, fit, models


# ------------------------------------------------------------------------------------- out-of-sample pricing
def subset(cs, idx):
    return {"date": cs["date"], "bonds": cs["bonds"].iloc[idx].reset_index(drop=True), "T": cs["T"][idx], "CF": cs["CF"][idx]}


def holdout(cs, model, k=5, seed=0):
    """k-fold: every bond is priced once by a curve fitted without it. Returns per-bond out-of-sample errors (bp)."""
    n = len(cs["bonds"])
    rng = np.random.default_rng(seed)
    fold = rng.permutation(n) % k
    out = []
    for f in range(k):
        tr, te = np.flatnonzero(fold != f), np.flatnonzero(fold == f)
        p, _ = fit.fit_date(model, subset(cs, tr))
        e = fit.errors(model, list(p.values()), subset(cs, te))
        out.append(e)
    return pd.concat(out, ignore_index=True)


# ------------------------------------------------------------------------------------------ factor risk
def bond_flows(row, date):
    """Cash-flow times (years) and amounts per 100 face for one CRSP issue row (needs TMATDT, TCOUPRT, TNIPPY)."""
    date = pd.Timestamp(date)
    n = int(row.TNIPPY) if row.TNIPPY and row.TNIPPY > 0 else 2
    ts, k = [], 0
    while True:
        d = row.TMATDT - pd.DateOffset(months=12 // n * k)
        if d <= date:
            break
        ts.append((d - date).days / 365.25)
        k += 1
    ts = np.array(ts[::-1])
    cf = np.full(len(ts), row.TCOUPRT / n)
    cf[-1] += 100
    return ts, cf


def exposures(t, cf, y, a1, a2):
    """Discounting at the bond's own yield y: dollar duration (parallel shift) and G2++ factor exposures."""
    D = cf * np.exp(-y * t)
    B1 = (1 - np.exp(-a1 * t)) / a1
    B2 = (1 - np.exp(-a2 * t)) / a2
    return {"parallel": -(D * t).sum(), "x1": -(D * B1).sum(), "x2": -(D * B2).sum()}


def hedge_test(start, ids, a1, a2, days=252):
    """ids = {'target': KYTREASNO of the 10y, 'h2': 2y, 'h30': 30y}. Daily rebalanced hedges held close to close.
    Returns daily P&L per 100 face of the target for each strategy."""
    dly, iss = data._crsp()
    start = pd.Timestamp(start)
    px = {}
    for k, kid in ids.items():
        x = dly[(dly.KYTREASNO == kid) & (dly.CALDT >= start)].sort_values("CALDT").set_index("CALDT")
        px[k] = x[["TDNOMPRC", "TDACCINT"]].copy()
    dates = px["target"].index.intersection(px["h2"].index).intersection(px["h30"].index)[: days + 1]
    rows = {k: iss[iss.KYTREASNO == kid].iloc[0] for k, kid in ids.items()}
    out = []
    for d0, d1 in zip(dates[:-1], dates[1:]):
        E, dP = {}, {}
        for k in ids:
            t, cf = bond_flows(rows[k], d0)
            dirty = px[k].loc[d0, "TDNOMPRC"] + px[k].loc[d0, "TDACCINT"]
            y = data.ytm_cont(np.array([dirty]), t[None, :], cf[None, :])[0]
            E[k] = exposures(t, cf, y, a1, a2)
            dP[k] = px[k].loc[d1, "TDNOMPRC"] - px[k].loc[d0, "TDNOMPRC"]          # clean price change
        h2 = -E["target"]["parallel"] / E["h2"]["parallel"]
        h30 = -E["target"]["parallel"] / E["h30"]["parallel"]
        A = np.array([[E["h2"]["x1"], E["h30"]["x1"]], [E["h2"]["x2"], E["h30"]["x2"]]])
        g = np.linalg.solve(A, -np.array([E["target"]["x1"], E["target"]["x2"]]))
        out.append(dict(date=d1, unhedged=dP["target"], dur_2y=dP["target"] + h2 * dP["h2"],
                        dur_30y=dP["target"] + h30 * dP["h30"], g2_2y_30y=dP["target"] + g[0] * dP["h2"] + g[1] * dP["h30"],
                        hedge_2y=g[0], hedge_30y=g[1], dur_hedge_2y=h2))
    return pd.DataFrame(out).set_index("date")


# ------------------------------------------------------------------------------------------- bond option
def g2_P(tau, xt, th):
    """G2++ zero-coupon price for maturities tau given state xt=(x1,x2) and th dict."""
    p = [th["a1"], th["a2"], th["s1"], th["s2"], th["rho"], th["delta"], xt[0], xt[1]]
    return np.exp(-models.g2_zero(np.asarray(tau, float), p) * np.asarray(tau, float))


def g2_sigma_p(T, S, th, extra_var=0.0):
    """Std of ln P(T,S) seen from 0 (Brigo-Mercurio Sigma). extra_var adds variance (e.g. scheduled jumps)."""
    a, b, s1, s2, r = th["a1"], th["a2"], th["s1"], th["s2"], th["rho"]
    v = (s1**2 / (2 * a**3) * (1 - np.exp(-a * (S - T)))**2 * (1 - np.exp(-2 * a * T))
         + s2**2 / (2 * b**3) * (1 - np.exp(-b * (S - T)))**2 * (1 - np.exp(-2 * b * T))
         + 2 * r * s1 * s2 / (a * b * (a + b)) * (1 - np.exp(-a * (S - T))) * (1 - np.exp(-b * (S - T))) * (1 - np.exp(-(a + b) * T)))
    return np.sqrt(v + extra_var)


def g2_zbc(K, T, S, xt, th, extra_var=0.0):
    """European call, expiry T, on a zero-coupon bond maturing at S > T, strike K (per 1 face)."""
    PT, PS = g2_P(T, xt, th), g2_P(S, xt, th)
    sig = g2_sigma_p(T, S, th, extra_var)
    h = np.log(PS / (K * PT)) / sig + sig / 2
    return PS * norm.cdf(h) - K * PT * norm.cdf(h - sig)


def g2_zbc_mc(K, T, S, xt, th, n=200_000, steps=250, seed=1):
    """Monte Carlo check: simulate x1, x2 under Q (exact OU steps), discount with the integrated short rate."""
    rng = np.random.default_rng(seed)
    a, b, s1, s2, r, d = th["a1"], th["a2"], th["s1"], th["s2"], th["rho"], th["delta"]
    dt = T / steps
    x1 = np.full(n, xt[0])
    x2 = np.full(n, xt[1])
    integ = np.zeros(n)
    for _ in range(steps):
        z1 = rng.standard_normal(n)
        z2 = r * z1 + np.sqrt(1 - r**2) * rng.standard_normal(n)
        r0 = d + x1 + x2
        x1 = x1 * np.exp(-a * dt) + s1 * np.sqrt((1 - np.exp(-2 * a * dt)) / (2 * a)) * z1
        x2 = x2 * np.exp(-b * dt) + s2 * np.sqrt((1 - np.exp(-2 * b * dt)) / (2 * b)) * z2
        integ += 0.5 * (r0 + d + x1 + x2) * dt
    tau = S - T
    p = [a, b, s1, s2, r, d, 0, 0]
    yz = models.g2_zero(np.array([tau]), p)[0]          # zero yield at state 0 ...
    B1, B2 = (1 - np.exp(-a * tau)) / a, (1 - np.exp(-b * tau)) / b
    PTS = np.exp(-yz * tau - B1 * x1 - B2 * x2)          # ... then shift by the simulated state
    pay = np.maximum(PTS - K, 0) * np.exp(-integ)
    return pay.mean(), pay.std() / np.sqrt(n)


def normal_vol_from_sigma(sig, T, S):
    """Approximate normal (bp/yr) vol of the forward zero yield for maturity S-T implied by Sigma."""
    return sig / ((S - T) * np.sqrt(T)) * 1e4
