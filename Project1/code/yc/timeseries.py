"""Models through time: monthly zero curves 1990-2026, fitted with ONE parameter set for all months.

Every model here is affine: y(tau) = a(tau; Theta) + b(tau; Theta) . x_t, with Theta the structural parameters and
x_t the state on month t. For a given Theta the states are an OLS regression month by month, so we 'profile' them out
and optimise only Theta (variable projection)."""
import numpy as np
import pandas as pd
from scipy.optimize import least_squares

from . import data, models

TAUS = np.array([0.25, 0.5, 1, 2, 3, 5, 7, 10, 15, 20, 30], dtype=float)


def monthly_zero_panel(start="1990-01-01", long=False):
    """Month-end zero yields (continuously compounded, %).
    Default (balanced, 1990+): 3m and 6m from T-bill CMT (bond-equivalent -> continuous), 1-30y from the Fed's GSW curve.
    long=True: from Aug 1971 (first 10y GSW yield), unbalanced: 15y from Nov 1971, 20y from 1981, 30y from 1985.
    Before Sep 1981 the bill yields only exist as discount rates (dtb3, dtb6), converted exactly to zero yields."""
    g = data.load_gsw()
    gm = g[[f"y{m}" for m in (1, 2, 3, 5, 7, 10, 15, 20, 30)]].resample("ME").last()
    c = data.load_cmt()
    cm = c[["3m", "6m"]].resample("ME").last()
    cm = 200 * np.log1p(cm / 200)                       # semi-annual bond-equivalent -> continuous
    if long:
        h = pd.read_parquet(data.RAW / "frb_h15" / "frb_rates_daily.parquet").set_index("date").sort_index()
        disc = h[["dtb3", "dtb6"]].resample("ME").last()
        for col, days in [("dtb3", 91), ("dtb6", 182)]:
            zero = -np.log(1 - disc[col] / 100 * days / 360) / (days / 365) * 100
            tgt = "3m" if col == "dtb3" else "6m"
            early = cm.index < pd.Timestamp("1981-09-01")
            cm.loc[early, tgt] = zero.reindex(cm.index)[early]
        start = "1971-08-01"
    p = pd.concat([cm, gm], axis=1).loc[start:]
    p.columns = TAUS
    p = p.dropna(subset=[0.25, 0.5, 1.0, 2.0, 5.0, 10.0]) if long else p.dropna()
    return p


# ------------------------------------------------------------- affine loadings  y = a + B @ x   (decimal units)
def loadings(model, th, tau=TAUS):
    t = np.asarray(tau, float)
    if model == "Vasicek":
        k, mu, s = th
        B = (1 - np.exp(-k * t)) / k
        lnA = (mu - s**2 / (2 * k**2)) * (B - t) - s**2 * B**2 / (4 * k)
        return -lnA / t, (B / t)[:, None]
    if model == "G2++":
        a1, a2, s1, s2, rho, d = th
        B1, B2 = (1 - np.exp(-a1 * t)) / a1, (1 - np.exp(-a2 * t)) / a2

        def v(a, b, sa, sb):
            Ba, Bb = (1 - np.exp(-a * t)) / a, (1 - np.exp(-b * t)) / b
            return sa * sb / (a * b) * (t - Ba - Bb + (1 - np.exp(-(a + b) * t)) / (a + b))

        V = v(a1, a1, s1, s1) + v(a2, a2, s2, s2) + 2 * rho * v(a1, a2, s1, s2)
        return d - V / (2 * t), np.column_stack([B1 / t, B2 / t])
    if model == "Dynamic NS":                          # Diebold-Li: fixed decay, 3 free factors (not arbitrage-free)
        (lam,) = th
        x = t / lam
        f1 = (1 - np.exp(-x)) / x
        return np.zeros_like(t), np.column_stack([np.ones_like(t), f1, f1 - np.exp(-x)])
    raise ValueError(model)


SPEC = {
    "Vasicek": dict(names=["kappa", "theta", "sigma"], x0=[0.1, 0.05, 0.01], lb=[1e-3, -0.05, 1e-4], ub=[5, 0.25, 0.1]),
    "G2++": dict(names=["a1", "a2", "s1", "s2", "rho", "delta"], x0=[0.02, 0.5, 0.01, 0.015, -0.7, 0.05],
                 lb=[1e-3, 1e-2, 1e-4, 1e-4, -0.99, -0.05], ub=[3, 5, 0.1, 0.1, 0.99, 0.25]),
    "Dynamic NS": dict(names=["lambda"], x0=[2.0], lb=[0.1], ub=[15]),
}


def _states(a, B, Y):
    """OLS of (Y - a) on B for every month, using only the maturities observed that month. Y: (months, taus)."""
    X = np.full((Y.shape[0], B.shape[1]), np.nan)
    M = ~np.isnan(Y)
    pats, inv = np.unique(M, axis=0, return_inverse=True)
    for k, pat in enumerate(pats):
        rows = np.flatnonzero(inv.ravel() == k)
        sol, *_ = np.linalg.lstsq(B[pat], (Y[np.ix_(rows, pat)] - a[pat]).T, rcond=None)
        X[rows] = sol.T
    return X


def fit_shared_panel(model, panel, x0s=None, fit_index=None):
    """One Theta for every month; states by OLS. Returns (Theta dict, states DataFrame, fitted DataFrame in %)."""
    Y = panel.values / 100
    Yfit = Y if fit_index is None else panel.loc[fit_index].values / 100     # estimate Theta on a subsample only
    s = SPEC[model]

    def res(th):
        a, B = loadings(model, th)
        X = _states(a, B, Yfit)
        r = ((a + X @ B.T) - Yfit).ravel() * 1e4
        return r[~np.isnan(r)]

    best = None
    for x0 in (x0s or [s["x0"]]):
        r = least_squares(res, x0, bounds=(s["lb"], s["ub"]), x_scale="jac", max_nfev=5000)
        if best is None or r.cost < best.cost:
            best = r
    a, B = loadings(model, best.x)
    X = _states(a, B, Y)
    fitted = pd.DataFrame((a + X @ B.T) * 100, index=panel.index, columns=panel.columns)
    cols = {"Vasicek": ["r"], "G2++": ["x1", "x2"], "Dynamic NS": ["level", "slope", "curvature"]}[model]
    return dict(zip(s["names"], best.x)), pd.DataFrame(X * 100, index=panel.index, columns=cols), fitted


def fit_vasicek_each_month(panel):
    """Vasicek with ALL parameters free on each month separately (the per-date fit, through time)."""
    rows, prev = [], None
    for d, y in panel.iterrows():
        Y = y.values / 100
        best = None
        starts = [[0.2, 0.05, 0.01, Y[0]], [1.0, 0.04, 0.03, Y[0]], [0.05, 0.08, 0.005, Y[0]]] + ([prev] if prev is not None else [])
        for x0 in starts:
            r = least_squares(lambda p: (models.vasicek_zero(TAUS, p) - Y) * 1e4, x0,
                              bounds=([1e-3, -0.05, 1e-4, -0.02], [5, 0.25, 0.1, 0.25]), x_scale="jac")
            if best is None or r.cost < best.cost:
                best = r
        prev = list(best.x)
        rows.append(dict(date=d, kappa=best.x[0], theta=best.x[1], sigma=best.x[2], r=best.x[3],
                         rmse=np.sqrt(np.mean(best.fun**2))))
    return pd.DataFrame(rows).set_index("date")
