"""Real-world (P) dynamics of the G2++ factors, expected short rates and the term premium.

Two-step approach in the spirit of Joslin-Singleton-Zhu (2011) and ACM (2013):
  1. Q: the cross-section of yields gives the G2++ loadings and the monthly factors x_t (timeseries.fit_shared_panel).
  2. P: a VAR(1) on the monthly factors, x_{t+1} = c + Phi x_t + e, gives how the factors actually evolve.
Expected short rate under P:   E_t[r_{t+h}] = delta + 1'E_t[x_{t+h}].
Expectations component of the n-year yield = average expected short rate over the next n years; term premium = model
yield - expectations component (convexity is ignored; it is a few bp at 10 years)."""
import numpy as np
import pandas as pd

from . import timeseries as ts

G2_KEYS = ["a1", "a2", "s1", "s2", "rho", "delta"]


def var1(x):
    Z = np.column_stack([np.ones(len(x) - 1), x[:-1]])
    B, *_ = np.linalg.lstsq(Z, x[1:], rcond=None)
    c, Phi = B[0], B[1:].T
    E = x[1:] - Z @ B
    return c, Phi, np.cov(E.T)


def iterate(c, Phi, x0, h):
    """E[x_{t+k}] for k = 0..h."""
    out = [x0]
    z = x0.copy()
    for _ in range(h):
        z = c + Phi @ z
        out.append(z)
    return np.array(out)


class G2PQ:
    def __init__(self, panel_fit, panel):
        th, X, F = panel_fit
        self.th = th
        self.thv = [th[k] for k in G2_KEYS]
        self.x = X[["x1", "x2"]].values / 100
        self.idx = X.index
        self.panel = panel
        self.fitted = F
        self.c, self.Phi, self.S = var1(self.x)

    # ---- yields / forwards under the fitted Q loadings
    def y(self, tau, xt):
        a, B = ts.loadings("G2++", self.thv, np.atleast_1d(np.asarray(tau, float)))
        return a + B @ xt

    def fwd3m(self, H, xt):
        """Forward rate for the 3-month period starting in H years (decimal), from the model curve."""
        if H <= 0:
            return self.y(0.25, xt)
        return ((H + 0.25) * self.y(H + 0.25, xt) - H * self.y(H, xt)) / 0.25

    # ---- P quantities
    def params(self):
        eig = np.linalg.eigvals(self.Phi)
        mu = np.linalg.solve(np.eye(2) - self.Phi, self.c)
        volP = np.sqrt(np.diag(self.S) * 12)
        corrP = self.S[0, 1] / np.sqrt(self.S[0, 0] * self.S[1, 1])
        one = np.ones(2)
        th = self.th
        return dict(
            kappaQ=[th["a1"], th["a2"]], kappaP=list(-12 * np.log(np.abs(eig))),
            halflifeQ=[np.log(2) / th["a1"], np.log(2) / th["a2"]],
            halflifeP=list(np.log(2) / (-12 * np.log(np.abs(eig)))),
            volQ=[th["s1"], th["s2"]], volP=list(volP), corrQ=th["rho"], corrP=corrP,
            vol_rQ=np.sqrt(th["s1"] ** 2 + th["s2"] ** 2 + 2 * th["rho"] * th["s1"] * th["s2"]),
            vol_rP=np.sqrt(one @ self.S @ one * 12),
            longrun_rP=th["delta"] + mu.sum(), longrun_level=th["delta"] + mu[0])

    def decomposition(self, n_years=10):
        """Monthly: model n-year yield, expectations component, term premium (all %)."""
        n = 12 * n_years
        d = self.th["delta"]
        eh = np.array([(d + iterate(self.c, self.Phi, xt, n - 1).sum(1)).mean() for xt in self.x])
        yn = self.fitted[float(n_years)].values / 100
        r = d + self.x.sum(1)
        return pd.DataFrame({"y": yn * 100, "expect": eh * 100, "tp": (yn - eh) * 100, "r": r * 100,
                             "y3m": self.fitted[0.25].values}, index=self.idx)

    def paths(self, date, months, expanding=True):
        """From `date`: market forward path (Q), model expected path (P, VAR estimated on data up to `date` if
        expanding), both for the 3-month yield, plus the realised 3-month yield (all %)."""
        i = self.idx.get_loc(pd.Timestamp(date))
        c, Phi, _ = var1(self.x[: i + 1]) if expanding else (self.c, self.Phi, None)
        Ex = iterate(c, Phi, self.x[i], months)
        a, B = ts.loadings("G2++", self.thv, np.array([0.25]))
        h = np.arange(months + 1)
        exp_path = (a + Ex @ B.T)[:, 0] * 100
        fwd_path = np.array([self.fwd3m(k / 12, self.x[i])[0] for k in h]) * 100
        dates = pd.date_range(self.idx[i], periods=months + 1, freq="ME")
        real = self.panel[0.25].reindex(dates)
        return pd.DataFrame({"forward": fwd_path, "expected": exp_path, "realised": real.values}, index=dates)

    def forecasts(self, horizons=(12, 24), start="2000-01-01"):
        """Out-of-sample forecasts of the 3-month yield h months ahead; VAR re-estimated each month on data to t."""
        y3 = self.panel[0.25].values / 100
        a, B = ts.loadings("G2++", self.thv, np.array([0.25]))
        rows = []
        for i, d in enumerate(self.idx):
            if d < pd.Timestamp(start):
                continue
            c, Phi, _ = var1(self.x[: i + 1])
            for h in horizons:
                if i + h >= len(self.idx):
                    continue
                z = iterate(c, Phi, self.x[i], h)[-1]
                rows.append(dict(date=d, h=h, actual=y3[i + h] * 100, no_change=y3[i] * 100,
                                 forward=self.fwd3m(h / 12, self.x[i])[0] * 100, model=(a + B @ z)[0] * 100))
        return pd.DataFrame(rows)
