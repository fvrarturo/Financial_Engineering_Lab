"""Candidate strategies (Spec 03 §2).

A Strategy instance handles one horizon and k "rows" (knob values / tune modes) at once. All arrays are
vectorised over paths: W and F have shape (k, n_paths); sigma_d and r_ann have shape (n_paths,).
`weight` returns the *raw* equity weight; the engine clips it to [0, 1] and counts the clips, so a strategy
that needs clipping shows up in the results.

Knobs may be scalars, shape (k,) (one per row) or shape (k, n_paths) (per path, used by the backtest where
each "path" is a different historical start date).

Strategies see only: the day index t, current wealth W, floor PV F, the investor's conditional daily vol
sigma_d (base-model filter), the observable short rate r_ann, and a read-only History of past returns.
They never receive future market arrays (the engine generates or slices them day by day).
"""
from __future__ import annotations

import math
import warnings
from pathlib import Path

import numpy as np
from scipy.special import ndtr

N_YEAR = 252


def _col(a, k: int | None = None) -> np.ndarray:
    """Knob -> array broadcastable against (k, n)."""
    a = np.asarray(a, dtype=float)
    if a.ndim == 0:
        return a.reshape(1, 1)
    if a.ndim == 1:
        return a[:, None]
    return a


def cash_growth_lower(r_ann, tau, rate_model: dict | None, z: float):
    """Lower z-quantile of log cash growth over tau years, ln of prod(1+rf), under the investor's Vasicek
    model for y = ln(1 + r): I = integral of y ~ Normal(m, v) with
      m = th tau + (y - th)(1 - e^{-k tau}) / k
      v = s^2/k^2 [tau - 2(1 - e^{-k tau})/k + (1 - e^{-2 k tau})/(2k)]   (k <= 0: random walk, v = s^2 tau^3/3).
    Returns m - z sqrt(v). With rate_model None (or s = 0) this is y tau, i.e. the spec's observable-rate PV."""
    y = np.log1p(r_ann)
    if rate_model is None or not rate_model.get("s"):
        return y * tau
    k, th, sv = rate_model["kappa"], math.log1p(rate_model["theta"]), rate_model["s"]
    if k is None or k <= 1e-8:
        m, v = y * tau, sv * sv * tau ** 3 / 3.0
    else:
        e1, e2 = math.exp(-k * tau), math.exp(-2 * k * tau)
        m = th * tau + (y - th) * (1 - e1) / k
        v = sv * sv / (k * k) * (tau - 2 * (1 - e1) / k + (1 - e2) / (2 * k))
    return m - z * math.sqrt(max(v, 0.0))


_LD_CACHE = {"r": None, "d": {}}


class Strategy:
    name = "base"
    knob = None          # knob name, or None

    def __init__(self, T_days: int, knob_values=None, labels: list[str] | None = None, **params):
        self.T = int(T_days)
        self.params = dict(params)
        kv = np.array([np.nan]) if knob_values is None else np.asarray(knob_values, dtype=float)
        if kv.ndim == 0:
            kv = kv[None]
        self.knob_values = kv          # (k,) or (k, n_paths)
        self.k = kv.shape[0]
        self.labels = labels or [f"{self.name}"] * self.k

    # -- lifecycle -------------------------------------------------------------------------------
    def reset(self, n_paths: int, W0: float, K: float, gamma: float, state0: dict, model_info: dict):
        self.n, self.W0, self.K, self.gamma = n_paths, W0, K, gamma
        self.state0, self.info = state0, model_info
        fl = model_info.get("floor", {"mode": "spec"})
        self.floor_mode = fl.get("mode", "spec")
        self.floor_rm = model_info.get("rate_model") if self.floor_mode == "robust" else None
        from scipy.stats import norm
        self.floor_z = float(norm.ppf(1 - fl.get("alpha", 0.005))) if self.floor_mode == "robust" else 0.0

    def log_disc(self, t, r_ann):
        """-ln(protection discount factor) over the remaining horizon: robust (lower-quantile cash growth)
        or the spec's observable-rate PV. Shared across strategies within a day (same r_ann object)."""
        c = _LD_CACHE
        if c["r"] is not r_ann:
            c["r"], c["d"] = r_ann, {}
        key = (self.T - t, id(self.floor_rm), self.floor_z)
        v = c["d"].get(key)
        if v is None:
            v = cash_growth_lower(r_ann, (self.T - t) / N_YEAR, self.floor_rm, self.floor_z)
            c["d"][key] = v
        return v

    def protect_floor(self, t, F, r_ann):
        """Floor the strategy protects: robust F~_t = K exp(-lower cash growth), or the engine's F.
        Cached per day (the engine and weight() both ask for it)."""
        if self.floor_mode != "robust":
            return F
        c = getattr(self, "_fp_cache", None)
        if c is not None and c[0] == t and c[1] is r_ann:
            return c[2]
        fp = self.K * np.exp(-self.log_disc(t, r_ann))
        self._fp_cache = (t, r_ann, fp)
        return fp

    def weight(self, t, W, F, sigma_d, r_ann, history) -> np.ndarray:  # pragma: no cover
        raise NotImplementedError

    def on_returns(self, t, r, rf):
        """Called after day t's returns are revealed (for strategies with internal state)."""


class Cash(Strategy):
    name = "cash"

    def weight(self, t, W, F, sigma_d, r_ann, history):
        return np.zeros((self.k, self.n))


class MertonConst(Strategy):
    """phi = mu_e / (gamma sigma_bar^2): Merton with the unconditional vol, no floor logic."""
    name = "merton_const"

    def reset(self, *a, **kw):
        super().reset(*a, **kw)
        mu = np.asarray(self.info["mu_e_ann"], dtype=float)
        sb = np.asarray(self.info["sigma_bar_ann"], dtype=float)
        self.phi = np.broadcast_to(mu / (self.gamma * sb ** 2), (self.n,))

    def weight(self, t, W, F, sigma_d, r_ann, history):
        return np.broadcast_to(self.phi, (self.k, self.n)).copy()


class MertonVol(Strategy):
    """phi_t = mu_e / (gamma sigma_t^2) with the conditional annualised vol (vol timing)."""
    name = "merton_vol"

    def weight(self, t, W, F, sigma_d, r_ann, history):
        s2 = sigma_d ** 2 * N_YEAR
        return np.broadcast_to(np.asarray(self.info["mu_e_ann"]) / (self.gamma * s2), (self.k, self.n)).copy()


class CPPI(Strategy):
    """phi_t = m max(C_t, 0) / W_t."""
    name, knob = "cppi", "m"

    def weight(self, t, W, F, sigma_d, r_ann, history):
        m = _col(self.knob_values)
        Fp = self.protect_floor(t, F, r_ann)
        return m * np.maximum(W - Fp, 0.0) / W


class CPPIVol(Strategy):
    """Gap-aware CPPI: m_t = 1 / (z sigma_d,t), so the cushion survives a z-sigma one-day move."""
    name, knob = "cppi_vol", "z"

    def weight(self, t, W, F, sigma_d, r_ann, history):
        z = _col(self.knob_values)
        Fp = self.protect_floor(t, F, r_ann)
        return np.maximum(W - Fp, 0.0) / (z * sigma_d[None, :] * W)


def obpi_value(X, Keff, r_c, sx, tau):
    """BSM value of max(Keff, X_T): X N(d1) + Keff e^{-r tau} N(-d2). Returns (value, N(d1))."""
    disc = Keff * np.exp(-r_c * tau)
    vol = sx * np.sqrt(tau)
    with np.errstate(divide="ignore", invalid="ignore"):
        d1 = (np.log(X / Keff) + (r_c + 0.5 * sx * sx) * tau) / vol
        nd1 = np.where(vol > 1e-12, ndtr(d1), (X > disc).astype(float))
        nmd2 = np.where(vol > 1e-12, ndtr(-(d1 - vol)), (X <= disc).astype(float))
    return X * nd1 + disc * nmd2, nd1


def solve_x0(W0, Keff, r_c, sx, tau, iters: int = 80):
    """Vectorised bisection for X0 with obpi_value(X0) = W0. NaN where infeasible (Keff e^{-r tau} >= W0)."""
    Keff, r_c, sx, tau = np.broadcast_arrays(*(np.asarray(v, dtype=float) for v in (Keff, r_c, sx, tau)))
    lo = np.full(Keff.shape, 1e-9 * W0)
    hi = np.full(Keff.shape, float(W0))
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        v, _ = obpi_value(mid, Keff, r_c, sx, tau)
        up = v < W0
        lo = np.where(up, mid, lo)
        hi = np.where(up, hi, mid)
    x0 = 0.5 * (lo + hi)
    feasible = Keff * np.exp(-r_c * tau) < W0
    return np.where(feasible, x0, np.nan)


def obpi_nd1(X, Keff, r_c, sx, tau):
    """N(d1) only (what the equity weight needs); degenerate sigma_X -> indicator."""
    vol = sx * math.sqrt(tau)
    with np.errstate(divide="ignore", invalid="ignore"):
        d1 = (np.log(X / Keff) + (r_c + 0.5 * sx * sx) * tau) / vol
        d1 = np.where(vol > 1e-12, d1, np.where(X > Keff * np.exp(-r_c * tau), np.inf, -np.inf))
    return ndtr(d1)


def solve_x_call(c, Kd, r_c, sx, tau, X0=None, W=None, tol=1e-9, max_iter=40):
    """Re-anchoring: solve Call(X) = c for X, where Call = X N(d1) - Kd N(d2) is the BSM call on the model
    portfolio X with strike Kd e^{r tau} (Kd = discounted floor), i.e. V(X) = Kd + Call(X) = W.
    Bracket: c <= X <= c + Kd (Call(X) <= X and Call(X) >= X - Kd). Safeguarded Newton (dCall/dX = N(d1)),
    warm-started at X0 (yesterday's model portfolio grown by today's return), bisection only when a step leaves
    the bracket; converged elements are frozen. Returns (X, N(d1) at X). c must be > 0."""
    lo, hi = c.copy(), c + Kd
    X = np.clip(X0, lo, hi) if X0 is not None else 0.5 * (lo + hi)
    vol = np.broadcast_to(sx * math.sqrt(tau), X.shape)
    nd1 = np.empty_like(X)
    act = np.arange(X.size)
    for _ in range(max_iter):
        x, k, v = X[act], Kd[act], vol[act]
        small = v <= 1e-12
        with np.errstate(divide="ignore", invalid="ignore"):
            d1 = (np.log(x / k) + 0.5 * v * v) / np.where(small, 1.0, v)
        n1 = np.where(small, (x > k).astype(float), ndtr(d1))
        n2 = np.where(small, (x > k).astype(float), ndtr(d1 - v))
        nd1[act] = n1
        f = x * n1 - k * n2 - c[act]
        done = np.abs(f) <= tol * (c[act] + k)
        l_, h_ = lo[act], hi[act]
        l_ = np.where(f < 0, x, l_)
        h_ = np.where(f > 0, x, h_)
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            xn = x - f / n1
        bad = ~np.isfinite(xn) | (xn < l_) | (xn > h_)
        xn = np.where(bad, 0.5 * (l_ + h_), xn)
        lo[act], hi[act] = l_, h_
        X[act] = np.where(done, x, xn)
        act = act[~done]
        if act.size == 0:
            break
    if act.size:      # not converged within max_iter (rare): N(d1) at the final X
        x, k, v = X[act], Kd[act], vol[act]
        with np.errstate(divide="ignore", invalid="ignore"):
            d1 = (np.log(x / k) + 0.5 * v * v) / np.where(v <= 1e-12, 1.0, v)
        nd1[act] = np.where(v <= 1e-12, (x > k).astype(float), ndtr(d1))
    return X, nd1


class OBPIMerton(Strategy):
    """Option-based portfolio insurance on the Merton portfolio (lecture: wealth constraint / option analogy).

    Optimal payoff with a hard floor under BSM is max(K_eff, X_T); X is the unconstrained Merton wealth
    with constant weight phi_M = clip(mu_e/(gamma sigma^2), 0, 1) and vol sigma_X = phi_M sigma.
    phi_t = phi_M X_t N(d1) / W_t. phi_M uses the investor's sigma at t=0 (the starting state).
    vol_input 'sigma0_fixed': sigma_X uses sigma_0 every day; 'conditional': sigma_X,t = phi_M sigma_t.
    Knob K_eff is relative to K (1.0 = protect exactly K).

    Re-anchoring (2026-11-21): X_t is re-solved every day from the *actual* wealth, V(X_t; tau, sigma_X,t) = W_t,
    instead of being carried forward as the model portfolio. Off-model moves (gaps, vol changes, discrete
    rebalancing) otherwise make W fall below V(X) and phi = phi_M X N(d1) / W exceed phi_M (and 1).
    With re-anchoring phi_M X N(d1) <= W - Kd N(-d2) <= W, so phi <= phi_M <= 1, and phi = 0 once
    W <= Kd = K_eff x (protection discount) (insuring K_eff is no longer affordable).
    """
    name, knob = "obpi_merton", "K_eff"

    def reset(self, n_paths, W0, K, gamma, state0, model_info):
        super().reset(n_paths, W0, K, gamma, state0, model_info)
        self.vol_input = self.params.get("vol_input", "conditional")
        self.reanchor = self.params.get("reanchor", True)
        s0 = np.broadcast_to(np.asarray(model_info["sigma0_ann"], dtype=float), (n_paths,))
        mu = np.asarray(model_info["mu_e_ann"], dtype=float)
        self.phiM = np.clip(mu / (gamma * s0 ** 2), 0.0, 1.0)                     # (n,)
        self.s0 = s0
        self.Keff = _col(self.knob_values) * K                                      # (k,1) or (k,n)
        r0 = np.broadcast_to(np.asarray(state0["r0_ann"], dtype=float), (n_paths,))
        tau0 = self.T / N_YEAR
        sx0 = self.phiM * s0
        r_eff0 = self.log_disc(0, r0) / tau0          # protection discount expressed as a rate
        self.X = np.broadcast_to(solve_x0(W0, self.Keff, r_eff0[None, :], sx0[None, :], tau0),
                                 (self.k, n_paths)).copy()
        self.infeasible = ~np.isfinite(self.X)         # at t = 0 (reported)
        self.X = np.where(self.infeasible, 0.0, self.X)
        self.n_unaffordable = np.zeros(self.k, dtype=np.int64)   # path-days with W <= Kd (phi forced to 0)

    def weight(self, t, W, F, sigma_d, r_ann, history):
        tau = (self.T - t) / N_YEAR
        sig = self.s0 if self.vol_input == "sigma0_fixed" else sigma_d * math.sqrt(N_YEAR)
        sx = (self.phiM * sig)[None, :]
        ld = self.log_disc(t, r_ann)
        if not self.reanchor:
            r_eff = ld / tau
            nd1 = obpi_nd1(np.maximum(self.X, 1e-300), self.Keff, r_eff[None, :], sx, tau)
            phi = self.phiM[None, :] * self.X * nd1 / W
            return np.where(self.infeasible, 0.0, phi)
        Kd = np.broadcast_to(self.Keff * np.exp(-ld)[None, :], W.shape)
        c = W - Kd
        ok = c > 1e-9 * W
        self.n_unaffordable += np.count_nonzero(~ok, axis=1)
        phi = np.zeros(W.shape)
        if ok.any():
            sxb = np.broadcast_to(sx, W.shape)
            X, nd1 = solve_x_call(c[ok], Kd[ok], None, sxb[ok], tau, X0=self.X[ok])
            self.X[ok] = X
            phi[ok] = np.broadcast_to(self.phiM[None, :], W.shape)[ok] * X * nd1 / W[ok]
        self.X[~ok] = 0.0
        return phi

    def on_returns(self, t, r, rf):
        # model portfolio grown at phi_M: warm start for tomorrow's re-anchoring solve (or the state, if off)
        self.X = self.X * (1.0 + rf + self.phiM * (r - rf))[None, :]


class DPPolicy(Strategy):
    """Hook for the SPEC 04 dynamic-programming policy (file format 'dp_v2', written by gf.dp.save_policy).

    The file holds phi*(tau, y, h) for a grid of breach multipliers lam: tau = trading days remaining,
    y = W / F~ with F~ the robust floor the other floor strategies protect (protect_floor), h = the investor's
    next-day conditional variance (sigma_d^2). phi is bilinear in (log y, log h) at the exact tau = T - t,
    flat beyond the grid (counted: below y_min / above y_max / h above the grid).

    Rows: knob_values are lam values (each must be on the file's lam grid), or, with knob_values None, the
    file's lam*(sigma_0, T) lookup is used (one row). Off-lookup sigma_0 / T take the larger (more conservative)
    lam of the bracketing lookup cells; lam is never interpolated between grid values.
    """
    name, knob = "dp_policy", "lam"

    def reset(self, n_paths, W0, K, gamma, state0, model_info):
        super().reset(n_paths, W0, K, gamma, state0, model_info)
        d = load_dp_policy(self.params["path"])
        if str(d["format"]) != "dp_v2":
            raise ValueError(f"dp_policy: unsupported format {d['format']!r}")
        if self.T > int(d["tau"][-1]):
            raise ValueError(f"dp_policy: horizon {self.T} > solved tau_max {int(d['tau'][-1])}")
        if self.floor_mode != "robust" or abs(float(d["floor_alpha"]) - float(model_info["floor"].get("alpha", -1))) > 1e-12:
            warnings.warn("dp_policy: the policy was solved on the robust floor; model_info floor differs")
        self.ly, self.lh = np.log(d["y"]), np.log(d["h"])
        self.phi_grid = d["phi_grid"]
        lam = d["lam"]
        if np.all(np.isnan(self.knob_values)):
            self.lam_idx = np.array([lookup_lam_idx(d, state0["sigma0_ann"], self.T)])
            self.k = 1
            self.knob_values = lam[self.lam_idx]
            self.labels = ["lookup"]
        else:
            kv = np.asarray(self.knob_values, float)
            idx = [int(np.argmin(np.abs(lam - v))) for v in kv]
            if not np.allclose(lam[idx], kv, rtol=1e-9, atol=0):
                raise ValueError(f"dp_policy: lam {kv} not on the file's grid {lam}")
            self.lam_idx = np.array(idx)
        self.tab = d["phi_idx"]                                    # (n_lam, n_tau, ny, nh) uint8
        self.n_below_ymin = np.zeros(self.k, np.int64)
        self.n_above_ymax = np.zeros(self.k, np.int64)
        self.n_h_above = 0
        self.n_h_below = 0

    def weight(self, t, W, F, sigma_d, r_ann, history):
        tau = self.T - t
        Fp = self.protect_floor(t, F, r_ann)
        lyq = np.log(W / Fp)                                        # (k, n)
        lhq = np.log(np.maximum(sigma_d, 1e-300) ** 2)              # (n,)
        self.n_below_ymin += np.count_nonzero(lyq < self.ly[0], axis=1)
        self.n_above_ymax += np.count_nonzero(lyq > self.ly[-1], axis=1)
        self.n_h_above += int(np.count_nonzero(lhq > self.lh[-1]))
        self.n_h_below += int(np.count_nonzero(lhq < self.lh[0]))
        ny, nh = len(self.ly), len(self.lh)
        uy = np.interp(lyq, self.ly, np.arange(ny, dtype=float))
        iy = np.minimum(uy.astype(np.int64), ny - 2)
        wy = uy - iy
        if self.params.get("phi_interp", "bilinear") == "nearest_y":     # diagnostic: no interpolation across y
            wy = np.round(wy)
        if nh > 1:
            uh = np.interp(lhq, self.lh, np.arange(nh, dtype=float))
            ih = np.minimum(uh.astype(np.int64), nh - 2)
            wh = uh - ih
            ih1 = ih + 1
        else:
            ih = ih1 = np.zeros(lhq.shape, np.int64)
            wh = np.zeros(lhq.shape)
        out = np.empty(W.shape)
        g = self.phi_grid
        for r, li in enumerate(self.lam_idx):
            P = self.tab[li, tau]
            a, b = iy[r], iy[r] + 1
            out[r] = ((1 - wy[r]) * ((1 - wh) * g[P[a, ih]] + wh * g[P[a, ih1]])
                      + wy[r] * ((1 - wh) * g[P[b, ih]] + wh * g[P[b, ih1]]))
        return out


_DP_CACHE: dict = {}


def load_dp_policy(path) -> dict:
    """Load (and cache per process) a dp_v2 policy file."""
    key = (str(path), Path(path).stat().st_mtime_ns)
    d = _DP_CACHE.get(key)
    if d is None:
        with np.load(path, allow_pickle=False) as z:
            d = {k: z[k] for k in z.files if k not in ("B",)}
        _DP_CACHE.clear()
        _DP_CACHE[key] = d
    return d


def lookup_lam_idx(d: dict, sigma0_ann: float, T_days: int) -> int:
    """lam* index for (sigma_0, T): exact lookup cell if present, else the max over the bracketing cells
    (conservative: larger lam = fewer breaches). Flat beyond the lookup range."""
    if "lam_star_idx" not in d:
        raise ValueError("dp_policy: file has no lam* lookup; pass lam values as knob_values")
    S, Ts, I = np.asarray(d["lam_star_sigma"], float), np.asarray(d["lam_star_T"]), np.asarray(d["lam_star_idx"])

    def bracket(grid, v, log=False):
        g = np.log(grid) if log else grid
        x = math.log(v) if log else v
        o = np.argsort(g)
        g = g[o]
        if np.any(np.isclose(g, x, rtol=1e-9, atol=1e-12)):
            return [o[int(np.argmin(np.abs(g - x)))]]
        j = int(np.searchsorted(g, x))
        return [o[max(j - 1, 0)], o[min(j, len(g) - 1)]]
    ti, si = bracket(Ts.astype(float), float(T_days)), bracket(S, float(sigma0_ann), log=True)
    return int(max(I[a, b] for a in ti for b in si))


STRATEGIES = {
    "cash": Cash,
    "merton_const": MertonConst,
    "merton_vol": MertonVol,
    "cppi": CPPI,
    "cppi_vol": CPPIVol,
    "obpi_merton": OBPIMerton,
    "dp_policy": DPPolicy,
}


def make_strategy(name: str, T_days: int, knob_values=None, labels=None, cfg: dict | None = None, **params):
    """Factory. Returns None (with a warning) for dp_policy when its file is missing."""
    if name == "dp_policy":
        pol = params.get("policy", "dp_policy")
        d = Path(cfg["strategies"]["dp_policy_dir"]) if cfg else Path("results/policies")
        path = params.get("path") or str(d / f"{pol}.npz")
        if not Path(path).exists():
            warnings.warn(f"dp_policy: {path} not found; strategy skipped")
            return None
        params["path"] = path
    cls = STRATEGIES[name]
    return cls(T_days, knob_values, labels, **params)
