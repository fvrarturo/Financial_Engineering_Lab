"""Dynamic-programming policy with a breach penalty (SPEC 04).

Problem (Lagrangian relaxation of the breach constraint, multiplier lam >= 0):
    max E[ U(W_T) - lam 1{W_T < K} ],   U(W) = -1/W  (W in $M; gamma = 2)

State (tau, y, h): tau = trading days remaining, y = W_t / F_t with F_t the SPEC 03 robust floor
K exp(-q(tau)) (q = lower alpha-quantile of log cash growth under the investor's Vasicek model, at a fixed
rate r0), h = the investor's next-day GJR conditional variance. Under the base model (GJR-t, constant
arithmetic premium, rate held at r0) (y, h) is a sufficient statistic: next-day returns depend only on h,
next-day h only on (h, z), and the objective is homogeneous of degree -1 in W, so the dollar level enters
only through y = W/F.

Dynamics per day (decision at tau, transition to tau - 1):
    x  = m(h) + sqrt(h) z                 m(h) = mu_e - h/2 (constant_arith premium; same as SimMarket)
    R  = exp(x + log(1+rf)) - 1           floored at MIN_SIMPLE_RETURN like the simulator
    y' = y (1 + rf + phi (R - rf)) / g_tau,    g_tau = F_{t+1} / F_t = exp(q(tau) - q(tau-1))
    h' = min(omega + (alpha + gamma_lev 1{z<0}) h z^2 + beta h, h_cap)

Numerics (lecture 3): y grid log-spaced with extra density near the floor, h grid log-spaced in sigma,
z on a probability-scale quadrature (conditional bin means, left tail refined), phi on a 41-point grid.
The value is stored and interpolated as Jt = y * J (exactly constant in y when lam = 0, by homogeneity), bilinear
in (log y, log h). Above y_max: J(y) = J(y_max) y_max / y (Jt flat). Below y_min: terminal-style
J(y) = (J(y_min) + lam) y_min / y - lam.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np
from scipy import stats
from scipy.special import gammaln

from .simulate import MIN_SIMPLE_RETURN
from .strategies import cash_growth_lower

N_YEAR = 252


# --------------------------------------------------------------------------- quadrature
def _t_partial_mean(a: np.ndarray, nu: float) -> np.ndarray:
    """int_{-inf}^{a} t f_nu(t) dt for the (unit-scale) Student t; -inf -> 0."""
    a = np.asarray(a, float)
    logc = gammaln((nu + 1) / 2) - gammaln(nu / 2) - 0.5 * math.log(nu * math.pi)
    with np.errstate(over="ignore", invalid="ignore"):
        f = np.exp(logc - (nu + 1) / 2 * np.log1p(a * a / nu))
        out = -(nu + a * a) / (nu - 1) * f
    return np.where(np.isfinite(a), out, 0.0)


def default_u_edges(n_nodes: int = 60) -> np.ndarray:
    """Bin edges on the probability scale: 19 log-spaced bins in (0, 0.01] (edges include 1e-4 and 1e-3
    exactly), uniform bins on [0.01, 0.99], 5 bins in the right tail."""
    left = np.r_[0.0, np.geomspace(1e-5, 1e-2, 19)]
    left[[7, 13]] = [1e-4, 1e-3]             # exact decades (geomspace rounding)
    right = np.array([0.99, 0.995, 0.998, 0.999, 0.9999, 1.0])
    n_mid = n_nodes - (len(left) - 1) - (len(right) - 1)
    mid = np.linspace(0.01, 0.99, n_mid + 1)
    return np.r_[left, mid[1:-1], right]


@dataclass
class Quadrature:
    z: np.ndarray
    w: np.ndarray
    info: dict = field(default_factory=dict)


def shock_quadrature(nu: float | None, n_nodes: int = 60, u_edges: np.ndarray | None = None) -> Quadrature:
    """Standardised t(nu) (unit variance) or normal (nu None/inf) shock quadrature.

    Weights = CDF differences between bin edges (normalised); nodes = conditional means within each bin
    (closed form), so E[z] = 0 exactly. The within-bin variance is lost, so nodes are rescaled by one common
    factor to restore E[z^2] = 1 (reported; it is ~1e-4). The u = 0.001 edge keeps P(z < q_0.001) exact
    unless the rescale moves a node across it (checked)."""
    ue = default_u_edges(n_nodes) if u_edges is None else np.asarray(u_edges, float)
    normal = nu is None or not np.isfinite(nu)
    if normal:
        a = stats.norm.ppf(ue)
        pm = -stats.norm.pdf(a)                       # int_{-inf}^a z phi(z) dz
        pm = np.where(np.isfinite(a), pm, 0.0)
        s = 1.0
    else:
        s = math.sqrt((nu - 2.0) / nu)                # standardisation: z = s t
        a = stats.t.ppf(ue, nu)
        pm = _t_partial_mean(a, nu)
    w = np.diff(ue)
    z = s * np.diff(pm) / w
    w = w / w.sum()
    m1 = float(w @ z)
    z = z - m1                                         # numerically zero already
    m2 = float(w @ z ** 2)
    c = 1.0 / math.sqrt(m2)
    z = z * c
    q001 = (stats.norm.ppf(1e-3) if normal else s * stats.t.ppf(1e-3, nu))
    info = {"n_nodes": len(z), "var_before_rescale": m2, "rescale": c, "mean": float(w @ z),
            "var": float(w @ z ** 2), "p_below_q001": float(w[z < q001].sum()), "q001": float(q001),
            "nu": None if normal else float(nu)}
    return Quadrature(z, w, info)


# --------------------------------------------------------------------------- model
@dataclass
class DPModel:
    omega: float
    alpha: float
    gamma_lev: float
    beta: float
    nu: float | None              # None/inf -> normal shocks
    mu_e_daily: float
    rf_daily: float
    r0_ann: float
    h_cap: float                  # daily variance cap (SPEC 03 sigma_cap_mult x historical max)
    K: float = 95e6
    premium_mode: str = "constant_arith"
    premium_coef: float | None = None
    rate_model: dict | None = None     # investor Vasicek (kappa, theta, s); None -> observable-rate PV
    floor_alpha: float = 0.001
    h_fixed: bool = False          # V1: constant variance (h' = omega)

    def mean_log(self, h):
        if self.premium_mode == "constant_arith":
            mu = self.mu_e_daily
        elif self.premium_mode == "prop_variance":
            mu = self.premium_coef * h
        else:
            mu = self.premium_coef * np.sqrt(h)
        return mu - 0.5 * h

    def next_h(self, h, z):
        if self.h_fixed:
            return np.full(np.broadcast(h, z).shape, self.omega)
        hn = self.omega + (self.alpha + self.gamma_lev * (z < 0)) * h * z * z + self.beta * h
        return np.minimum(hn, self.h_cap)

    def log_disc(self, tau_days):
        """q(tau): -log of the floor discount, F = K exp(-q). Reuses the SPEC 03 floor code."""
        z = float(stats.norm.ppf(1 - self.floor_alpha)) if self.rate_model else 0.0
        return float(cash_growth_lower(self.r0_ann, tau_days / N_YEAR, self.rate_model, z))


def model_from_context(ctx, cfg, r0_ann: float | None = None) -> DPModel:
    """Base-model DP inputs from the SPEC 02/03 asset context (investor's estimates)."""
    b = ctx.base
    if b["kind"] not in ("gjr", "garch"):
        raise ValueError(f"DP supports GARCH/GJR base models, got {b['kind']}")
    if r0_ann is None:
        r0_ann = float(ctx.state_rows("named").r0_ann.iloc[0])
    fl = cfg["strategies"]["floor"]
    prem = ctx.params["premium"]["resolved"]
    coef = {"constant_arith": None, "prop_variance": prem.get("lambda"), "constant_sharpe": prem.get("S")}[prem["mode"]]
    rp = ctx.params["rate"]["params"]
    cap = ctx.sigma_cap_daily
    return DPModel(omega=b["omega"], alpha=b["alpha"], gamma_lev=b.get("gamma", 0.0) or 0.0, beta=b["beta"],
                   nu=b["nu"], mu_e_daily=ctx.mu_e_daily, rf_daily=(1 + r0_ann) ** (1 / ctx.N) - 1,
                   r0_ann=r0_ann, h_cap=cap ** 2 if cap else np.inf, K=cfg["problem"]["K"],
                   premium_mode=prem["mode"], premium_coef=coef,
                   rate_model={"kappa": rp["kappa"], "theta": rp["theta"], "s": rp["s"]}
                   if fl.get("mode") == "robust" else None,
                   floor_alpha=fl.get("alpha", 0.001))


# --------------------------------------------------------------------------- grids
def y_grid(n: int = 120, lo: float = 0.3, hi: float = 4.0, dense=(0.9, 1.3), dense_share: float = 0.35) -> np.ndarray:
    """Log-spaced y grid on [lo, hi], piecewise uniform in log y with `dense_share` of the points on
    [dense[0], dense[1]] and y = 1 an exact node."""
    L = np.log([lo, dense[0], 1.0, dense[1], hi])
    nd = int(round(dense_share * (n - 1)))
    n_out = (n - 1) - nd
    w_lo, w_hi = L[1] - L[0], L[4] - L[3]
    n_lo = max(1, int(round(n_out * w_lo / (w_lo + w_hi))))
    n_hi = n_out - n_lo
    n_d1 = max(1, int(round(nd * (L[2] - L[1]) / (L[3] - L[1]))))
    n_d2 = nd - n_d1
    parts = [np.linspace(L[0], L[1], n_lo + 1)[:-1], np.linspace(L[1], L[2], n_d1 + 1)[:-1],
             np.linspace(L[2], L[3], n_d2 + 1)[:-1], np.linspace(L[3], L[4], n_hi + 1)]
    g = np.exp(np.concatenate(parts))
    g[np.argmin(np.abs(g - 1.0))] = 1.0
    return g


def y_grid_bands(edges, counts, mult: int = 1) -> np.ndarray:
    """y grid uniform in log y within each band [edges[i], edges[i+1]] with counts[i] intervals (x mult)."""
    L = np.log(np.asarray(edges, float))
    parts = [np.linspace(L[i], L[i + 1], counts[i] * mult + 1)[:-1] for i in range(len(counts))]
    g = np.exp(np.r_[np.concatenate(parts), L[-1]])
    if np.any(np.isclose(g, 1.0, rtol=0, atol=1e-12)):
        g[np.argmin(np.abs(g - 1.0))] = 1.0
    return g


def h_grid(sigma_lo_ann: float, sigma_hi_ann: float, n: int = 40) -> np.ndarray:
    """Daily variances, log-spaced in sigma between the two annualised vols."""
    s = np.exp(np.linspace(math.log(sigma_lo_ann), math.log(sigma_hi_ann), n))
    return s * s / N_YEAR


def phi_grid(n: int = 41) -> np.ndarray:
    return np.linspace(0.0, 1.0, n)


def _breaks(xg):
    """Knots of a piecewise-uniform grid (indices where the spacing changes): exact fractional index via a
    short np.interp, ~2x faster than interpolating over every node."""
    xg = np.asarray(xg, float)
    if len(xg) < 3:
        return np.arange(len(xg))
    d = np.diff(xg)
    br = [0] + [i for i in range(1, len(xg) - 1) if abs(d[i] - d[i - 1]) > 1e-9 * max(abs(d[i]), 1e-300)] + [len(xg) - 1]
    return np.array(br)


def _lin_weights(xq, xg, br=None):
    """Index i0 and weight w for linear interpolation of xq on grid xg (clamped to the ends)."""
    n = len(xg)
    if n == 1:
        return np.zeros(np.shape(xq), np.int64), np.zeros(np.shape(xq))
    if br is None:
        u = np.interp(xq, xg, np.arange(n, dtype=float))
    else:
        u = np.interp(xq, xg[br], br.astype(float))
    i0 = np.minimum(u.astype(np.int64), n - 2)
    return i0, u - i0


def last_step_breach_prob(model: DPModel, y: np.ndarray, phi: np.ndarray, h: np.ndarray, lg: float) -> np.ndarray:
    """P(y' < 1) one day before the horizon, exact via the shock CDF. Shape (len(y), len(phi), len(h)).
    y' < 1  <=>  R < R* = rf + (g/y - 1 - rf)/phi  (phi > 0); phi = 0: deterministic (1 + rf < g/y)."""
    rf = model.rf_daily
    g = math.exp(lg)
    y3, p3, h3 = y[:, None, None], phi[None, :, None], h[None, None, :]
    gap = g / y3 - 1.0 - rf                                       # (ny, 1, 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        Rs = rf + gap / p3                                        # threshold on R (phi > 0)
        lx = np.log1p(np.maximum(Rs, -1 + 1e-300)) - math.log1p(rf)
        zs = (lx - model.mean_log(h3)) / np.sqrt(h3)
    if model.nu is None or not np.isfinite(model.nu):
        cdf = stats.norm.cdf(zs)
    else:
        s = math.sqrt((model.nu - 2) / model.nu)
        cdf = stats.t.cdf(zs / s, model.nu)
    cdf = np.where(Rs <= MIN_SIMPLE_RETURN, 0.0, cdf)             # R is floored at MIN_SIMPLE_RETURN
    det = np.broadcast_to((gap > 0).astype(float), cdf.shape)     # phi = 0
    return np.where(p3 > 0, cdf, det)


# --------------------------------------------------------------------------- y interpolation
def pchip_slopes(F: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Fritsch-Carlson (PCHIP) node derivatives of F along axis 0 on the grid x (non-uniform allowed).
    Local extrema get slope 0 (no overshoot: a monotone column stays monotone, B stays in [0, 1])."""
    hx = np.diff(x)[:, None]
    dl = np.diff(F, axis=0) / hx                                     # secant slopes (n-1, M)
    d = np.zeros_like(F)
    h0, h1 = hx[:-1], hx[1:]
    s0, s1 = dl[:-1], dl[1:]
    w1, w2 = 2 * h1 + h0, h1 + 2 * h0
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        hm = (w1 + w2) / (w1 / s0 + w2 / s1)
    d[1:-1] = np.where(s0 * s1 > 0, hm, 0.0)
    d[0], d[-1] = dl[0], dl[-1]                                      # one-sided ends (flat extrapolation beyond)
    return d


def _hermite(Ff, Df, idx, stride, t, hc):
    """Cubic Hermite on a cell: values Ff[idx], Ff[idx+stride], slopes Df[...], cell width hc, position t in [0, 1]."""
    t2 = t * t
    t3 = t2 * t
    return (Ff[idx] * (2 * t3 - 3 * t2 + 1) + Ff[idx + stride] * (3 * t2 - 2 * t3)
            + hc * (Df[idx] * (t3 - 2 * t2 + t) + Df[idx + stride] * (t3 - t2)))


# --------------------------------------------------------------------------- solver
@dataclass
class DPSolution:
    lam: float
    y: np.ndarray
    h: np.ndarray
    phi: np.ndarray
    tau_max: int
    phi_idx: np.ndarray          # (tau_max + 1, ny, nh) uint8; row 0 unused (terminal)
    J: np.ndarray                # (tau_max + 1, ny, nh) float32 value in $M utility units
    B: np.ndarray                # (tau_max + 1, ny, nh) float32 breach probability under phi*
    q: np.ndarray                # floor log discount q(tau), tau = 0..tau_max
    seconds: float
    below_ymin_share: float      # share of quadrature transitions (all steps) landing below y_min (at phi*)
    info: dict = field(default_factory=dict)

    def policy(self, tau: int) -> np.ndarray:
        return self.phi[self.phi_idx[tau]]


def solve(model: DPModel, lam: float, tau_max: int, y: np.ndarray, h: np.ndarray, quad: Quadrature,
          phi: np.ndarray | None = None, chunk_h: int = 4, store_J: bool = True, verbose: bool = False,
          interp_y: str = "linear", interp_h: str = "linear") -> DPSolution:
    """Backward induction from tau = 0 to tau_max (one solve covers every horizon T <= tau_max).

    interp_y: 'linear' (bilinear in (log y, log h), the spec's default) or 'pchip' (monotone cubic in log y, linear
    in log h). Linear interpolation repeated every day acts as numerical diffusion in y (extra variance ~ dy^2/6
    per step, comparable to the true daily variance on this grid); see DECISIONS 2026-11-21."""
    if interp_y not in ("linear", "pchip"):
        raise ValueError(interp_y)
    cubic = interp_y == "pchip"
    cubic_h = interp_h == "pchip"
    t0 = time.time()
    phi = phi_grid() if phi is None else np.asarray(phi, float)
    ny, nh, nz, nphi = len(y), len(h), len(quad.z), len(phi)
    ly, lh = np.log(y), np.log(h)
    wz = quad.w
    Kunit = model.K / 1e6
    rf = model.rf_daily
    lrf = math.log1p(rf)
    # time-invariant pieces -------------------------------------------------------------------
    x = model.mean_log(h)[:, None] + np.sqrt(h)[:, None] * quad.z[None, :]           # (nh, nz)
    x = np.maximum(x, math.log1p(MIN_SIMPLE_RETURN) - lrf)
    R = np.expm1(x + lrf)
    A = np.log1p(rf + phi[:, None, None] * (R - rf)[None])                            # (nphi, nh, nz)
    hn = model.next_h(h[:, None], quad.z[None, :])                                     # (nh, nz)
    hn = np.clip(hn, h[0], h[-1])
    hi0, hw = _lin_weights(np.log(hn), lh)                                             # (nh, nz)
    hi1 = np.minimum(hi0 + 1, nh - 1)
    dlh = np.diff(lh)[np.minimum(hi0, nh - 2)] if nh > 1 else np.zeros_like(hw)

    def h_interp(T2, clip=False):
        """(ny, nh) -> (ny, nh, nz) at h'(h_k, z_m): linear or PCHIP in log h (time-invariant positions)."""
        if not cubic_h or nh < 3:
            return T2[:, hi0] * (1 - hw) + T2[:, hi1] * hw
        D = pchip_slopes(T2.T, lh).T
        t = hw
        t2, t3 = t * t, t * t * t
        v = (T2[:, hi0] * (2 * t3 - 3 * t2 + 1) + T2[:, hi1] * (3 * t2 - 2 * t3)
             + dlh * (D[:, hi0] * (t3 - 2 * t2 + t) + D[:, hi1] * (t3 - t2)))
        return np.clip(v, 0.0, 1.0) if clip else v
    ybr = _breaks(ly)
    dly = np.diff(ly)
    # sum_m w_m Jt(y')/y' = (1/y) sum_m [w_m exp(-A)] Jt(y') exp(lg): fold exp(-A) into the weights
    WA = wz[None, None, :] * np.exp(-A)                                                # (nphi, nh, nz)
    q = np.array([model.log_disc(t) for t in range(tau_max + 1)])
    # terminal ------------------------------------------------------------------------------------
    pen = (y < 1.0).astype(float)
    Jt = np.repeat((-1.0 / Kunit - lam * y * pen)[:, None], nh, axis=1)              # Jt = y J at tau = 0
    Bc = np.repeat(pen[:, None], nh, axis=1)
    phi_idx = np.zeros((tau_max + 1, ny, nh), np.uint8)
    Jall = np.zeros((tau_max + 1, ny, nh), np.float32) if store_J else None
    Ball = np.zeros((tau_max + 1, ny, nh), np.float32)
    Jall_0 = Jt / y[:, None]
    if store_J:
        Jall[0] = Jall_0
    Ball[0] = Bc
    koff = (np.arange(nh)[:, None] * nz + np.arange(nz)[None, :])                     # flat (k, m) offset
    n_below = 0
    n_trans = 0
    # tau = 1 (last day), exact: E[J] = E_quad[-1/(y' K)] - lam P(y' < 1), P from the shock CDF
    lg1 = q[1] - q[0] if tau_max >= 1 else 0.0
    if tau_max >= 1:
        EU = np.einsum("pkm,pkm->pk", np.broadcast_to(wz, A.shape), np.exp(-A)) * math.exp(lg1)   # E[1/(1+port)] g
        EU = -(1e6 / model.K) * EU[None] / y[:, None, None]                               # (ny, nphi, nh)
        Pb = last_step_breach_prob(model, y, phi, h, lg1)
        V1 = EU - lam * Pb
        a1 = np.argmax(V1, axis=1)
        best1 = np.take_along_axis(V1, a1[:, None, :], axis=1)[:, 0, :]
        B1 = np.take_along_axis(Pb, a1[:, None, :], axis=1)[:, 0, :]
    for tau in range(1, tau_max + 1):
        lg = q[tau] - q[tau - 1]                                                       # log g_tau
        if tau == 1:
            Jt, Bc = best1 * y[:, None], B1
            phi_idx[1] = a1
            if store_J:
                Jall[1] = best1
            Ball[1] = B1
            n_trans += ny * nh
            continue
        # h-interpolation (time-invariant weights): (ny, nh, nz)
        Jh = h_interp(Jt)
        Bh = h_interp(Bc, clip=True)
        Jhf = Jh.reshape(ny, nh * nz).ravel()
        stride = nh * nz
        if cubic:
            Jdf = pchip_slopes(Jh.reshape(ny, stride), ly).ravel()
            Bdf = pchip_slopes(Bh.reshape(ny, stride), ly).ravel()
        best = np.empty((ny, nh))
        arg = np.empty((ny, nh), np.int64)
        for k0 in range(0, nh, chunk_h):
            ks = slice(k0, min(k0 + chunk_h, nh))
            c = ks.stop - ks.start
            lyn = ly[:, None, None, None] + A[None, :, ks, :] - lg                     # (ny, nphi, c, nz)
            i0, w = _lin_weights(lyn, ly, ybr)
            idx = i0 * stride + koff[ks][None, None, :, :]
            if cubic:
                v = _hermite(Jhf, Jdf, idx, stride, w, dly[i0])
            else:
                v = Jhf[idx] * (1 - w) + Jhf[idx + stride] * w
            if lam:
                below = lyn < ly[0]
                if below.any():
                    v = np.where(below, v + lam * (y[0] - np.exp(lyn)), v)
            # E[J] = E[Jt(y') / y'],  1/y' = exp(-A + lg) / y
            EJ = np.einsum("ipcm,pcm->ipc", v, WA[:, ks, :], optimize=False) * (math.exp(lg) / y)[:, None, None]
            a = np.argmax(EJ, axis=1)
            arg[:, ks] = a
            best[:, ks] = np.take_along_axis(EJ, a[:, None, :], axis=1)[:, 0, :]
        # breach recursion under phi* (V5) and below-y_min diagnostics
        Aopt = A[arg, np.arange(nh)[None, :], :]                                         # (ny, nh, nz)
        lyn = ly[:, None, None] + Aopt - lg
        i0, w = _lin_weights(lyn, ly, ybr)
        idx = i0 * stride + koff[None]
        Bhf = Bh.reshape(ny, nh * nz).ravel()
        if cubic:
            Bv = np.clip(_hermite(Bhf, Bdf, idx, stride, w, dly[i0]), 0.0, 1.0)
        else:
            Bv = Bhf[idx] * (1 - w) + Bhf[idx + stride] * w
        below = lyn < ly[0]
        Bv = np.where(below, 1.0, Bv)
        n_below += float((below * wz).sum())
        n_trans += ny * nh
        Bc = Bv @ wz
        Jt = best * y[:, None]
        phi_idx[tau] = arg
        if store_J:
            Jall[tau] = best
        Ball[tau] = Bc
        if verbose and tau % 50 == 0:
            print(f"  tau {tau}: {time.time() - t0:.1f}s", flush=True)
    secs = time.time() - t0
    return DPSolution(lam, y, h, phi, tau_max, phi_idx, Jall, Ball, q, secs, n_below / max(n_trans, 1),
                      {"ny": ny, "nh": nh, "nz": nz, "nphi": nphi, "interp_y": interp_y,
                                                                    "interp_h": interp_h})


# --------------------------------------------------------------------------- lookups
def interp2(table: np.ndarray, y: np.ndarray, h: np.ndarray, yq, hq) -> np.ndarray:
    """Bilinear interpolation of table (ny, nh) at (yq, hq) in (log y, log h), flat beyond the grid."""
    iy, wy = _lin_weights(np.log(yq), np.log(y))
    ih, wh = _lin_weights(np.log(hq), np.log(h))
    nh = table.shape[1]
    ih1 = np.minimum(ih + 1, nh - 1)
    t00, t01 = table[iy, ih], table[iy, ih1]
    t10, t11 = table[iy + 1, ih], table[iy + 1, ih1]
    return (1 - wy) * ((1 - wh) * t00 + wh * t01) + wy * ((1 - wh) * t10 + wh * t11)


def save_policy(path, sols: list[DPSolution], model: DPModel, lam_star: dict | None = None, meta: dict | None = None,
                J_path=None):
    """results/policies/dp_{asset}.npz (format read by strategies.DPPolicy, 'dp_v2')."""
    s0 = sols[0]
    arrs = {"format": np.array("dp_v2"), "tau": np.arange(s0.tau_max + 1), "y": s0.y, "h": s0.h, "phi_grid": s0.phi,
            "lam": np.array([s.lam for s in sols]), "phi_idx": np.stack([s.phi_idx for s in sols]),
            "q": s0.q, "r0_ann": np.array(model.r0_ann), "floor_alpha": np.array(model.floor_alpha),
            "K": np.array(model.K), "B": np.stack([s.B for s in sols]).astype(np.float32),
            "solve_seconds": np.array([s.seconds for s in sols]),
            "below_ymin_share": np.array([s.below_ymin_share for s in sols])}
    if lam_star:
        arrs["lam_star_sigma"] = np.asarray(lam_star["sigma"], float)
        arrs["lam_star_T"] = np.asarray(lam_star["T_days"], int)
        arrs["lam_star_idx"] = np.asarray(lam_star["idx"], int)            # (len(T), len(sigma))
    if meta:
        import json
        arrs["meta_json"] = np.array(json.dumps(meta, default=str))
    np.savez_compressed(path, **arrs)
    if J_path is not None and sols[0].J is not None:
        np.savez_compressed(J_path, lam=arrs["lam"], J=np.stack([s.J for s in sols]))
