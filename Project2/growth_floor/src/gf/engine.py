"""Wealth engine (Spec 03 §1) and market sources.

Timing for day t = 0..T-1 (per path):
  start of day t: the strategy sees W_t, F_t = K / (1 + r_ann_t)^((T - t)/252), the investor's conditional
                  daily vol sigma_d,t and r_ann_t, plus read-only past returns (History). It returns phi_t.
  during day t:   the market reveals the equity simple return r_t and the cash return rf_t.
  W_{t+1} = W_t (1 + rf_t + phi_t (r_t - rf_t)).
Market sources produce day t's returns only when asked (`realise(t)`), after every strategy has chosen
its weight, so no strategy can see future returns.

SimMarket simulates the scenario's data-generating process (DGP) day by day (no path storage) while the
investor's conditional vol is filtered with the investor's *base* model from the realised returns: under
a stress scenario the strategy does not know the DGP changed. ArrayMarket replays given arrays
(tests, historical backtest).

Breach decomposition: for each path, the last day on which the cushion C = W - F went from >= 0 to < 0
is classified as
  'gap'     : the day's equity excess move phi W (r - rf) alone exceeded the start-of-day cushion, and that
              cushion could absorb at least a gap_min_sigmas conditional-sd move (C >= k phi W sigma_d);
  'gradual' : the equity move crossed a cushion that had already eroded below k sigma (erosion, then a
              routine move);
  'rate'    : the equity move did not exceed the cushion: the floor PV rose / cash under-earned (rates).
"""
from __future__ import annotations

import math

import numpy as np

from .model import SQRT2_OV_PI
from .simulate import MIN_SIMPLE_RETURN

N_YEAR = 252
CROSS_CODES = {0: "none", 1: "gap", 2: "gradual", 3: "rate"}


# --------------------------------------------------------------------------- history
class History:
    """Ring buffer of the last L days of realised returns, read-only views for strategies."""

    def __init__(self, n_paths: int, L: int):
        self.L, self.filled, self.pos = L, 0, 0
        self._r = np.zeros((L, n_paths))
        self._rf = np.zeros((L, n_paths))

    def push(self, r, rf):
        self._r[self.pos], self._rf[self.pos] = r, rf
        self.pos = (self.pos + 1) % self.L
        self.filled = min(self.filled + 1, self.L)

    def returns(self, k: int | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Last k (<= L) days as (n_paths, k) arrays, oldest first; copies marked read-only."""
        k = self.filled if k is None else min(k, self.filled)
        idx = (self.pos - k + np.arange(k)) % self.L
        r, rf = self._r[idx].T.copy(), self._rf[idx].T.copy()
        r.flags.writeable = False
        rf.flags.writeable = False
        return r, rf


# --------------------------------------------------------------------------- markets
class ArrayMarket:
    """Replays given arrays of shape (n_paths, n_days): r, rf (realised), sigma_d, r_ann (start-of-day info)."""

    def __init__(self, r, rf, sigma_d, r_ann):
        self._r, self._rf = np.asarray(r, float), np.asarray(rf, float)
        self._s, self._ra = np.asarray(sigma_d, float), np.asarray(r_ann, float)
        self.n_paths, self.n_days = self._r.shape

    def start_of_day(self, t):
        return self._s[:, t], self._ra[:, t]

    def realise(self, t):
        return self._r[:, t], self._rf[:, t]


def _per_path(v, n):
    return np.broadcast_to(np.asarray(v, dtype=float), (n,)).copy()


class SimMarket:
    """Day-by-day simulation of a GARCH-family DGP + Vasicek rate, with the investor's base-model filter.

    dgp:      {"kind": garch|gjr|egarch, "omega","alpha","gamma","beta","nu" (scalars or per-path arrays),
               "innovations": t|normal|fhs, "zpool": array (fhs), "premium": {mode, mu_e_daily|lambda|S},
               "jump": None or {"p_day", "x_jump"} }
    investor: {"kind", "omega","alpha","gamma","beta", "mu_e_daily"}  (base estimates; constant premium)
    rate:     {"mode": vasicek|constant, "kappa","theta","s","rho","floor_at_zero"}
    state0:   {"sigma0_ann", "r0_ann"}  (same annualised sigma_0 for the DGP and the investor)
    """

    def __init__(self, dgp: dict, investor: dict, rate: dict, state0: dict, n_paths: int, seed: int,
                 N: int = N_YEAR):
        self.n_paths, self.N = n_paths, N
        self.rng = np.random.Generator(np.random.PCG64(seed))
        self.seed = seed
        self.dgp, self.inv, self.rate = dgp, investor, rate
        n = n_paths
        self.kind = dgp["kind"]
        self.om, self.al, self.ga, self.be = (_per_path(dgp.get(k, 0.0), n) for k in ("omega", "alpha", "gamma", "beta"))
        self.nu = _per_path(dgp.get("nu", np.inf), n)
        self.innov = dgp.get("innovations", "t")
        if self.innov == "fhs":
            pool = np.asarray(dgp["zpool"], float)
            pool = pool[np.isfinite(pool)]
            self.pool = (pool - pool.mean()) / pool.std()
        fin = np.isfinite(self.nu)
        nu_f = np.where(fin, self.nu, 3.0)
        self.t_scale = np.where(fin, np.sqrt(np.maximum(nu_f - 2.0, 1e-12) / nu_f), 1.0)
        self.all_normal = bool(np.all(~np.isfinite(self.nu)))
        self.same_nu = bool(np.all(self.nu == self.nu[0]))
        prem = dgp["premium"]
        self.pmode = prem["mode"]
        self.pcoef = {"constant_arith": prem.get("mu_e_daily", 0.0), "prop_variance": prem.get("lambda", 0.0),
                      "constant_sharpe": prem.get("S", 0.0)}[self.pmode]
        self.jump = dgp.get("jump")
        s2_0 = state0["sigma0_ann"] ** 2 / N
        self.s2 = np.full(n, s2_0)
        self.lns2 = np.log(self.s2)
        self.s2_inv = np.full(n, s2_0)
        self.ra = np.full(n, float(state0["r0_ann"]))
        self.ikind = investor["kind"]
        self.i_om, self.i_al, self.i_ga, self.i_be = (float(investor.get(k, 0.0)) for k in ("omega", "alpha", "gamma", "beta"))
        self.i_mu = float(investor["mu_e_daily"])
        self.rmode = rate.get("mode", "vasicek")
        self.kap, self.th, self.sr = (float(rate.get(k, 0.0)) for k in ("kappa", "theta", "s"))
        self.rho = float(rate.get("rho", 0.0))
        self.floor0 = bool(rate.get("floor_at_zero", False))
        self.n_clip = 0
        self.n_jumps = 0
        cap = dgp.get("sigma_cap_daily")
        self.s2_cap = float(cap) ** 2 if cap else np.inf      # cap on the DGP conditional variance
        self.n_cap = 0
        self._x_floor = math.log1p(MIN_SIMPLE_RETURN)

    def start_of_day(self, t):
        return np.sqrt(self.s2_inv), self.ra

    def _draw_z(self):
        n = self.n_paths
        if self.innov == "fhs":
            return self.pool[self.rng.integers(0, len(self.pool), size=n)]
        if self.innov == "normal" or self.all_normal:
            return self.rng.standard_normal(n)
        if self.same_nu:
            return self.rng.standard_t(self.nu[0], size=n) * self.t_scale
        nu = np.where(np.isfinite(self.nu), self.nu, 1e6)
        return self.rng.standard_t(nu) * self.t_scale

    def realise(self, t):
        n = self.n_paths
        z = self._draw_z()
        xi = self.rng.standard_normal(n)
        rf = (1.0 + self.ra) ** (1.0 / self.N) - 1.0
        s2 = self.s2
        sig = np.sqrt(s2)
        if self.pmode == "constant_arith":
            mu = self.pcoef
        elif self.pmode == "prop_variance":
            mu = self.pcoef * s2
        else:
            mu = self.pcoef * sig
        eps = sig * z
        if self.jump is not None:
            hit = self.rng.random(n) < self.jump["p_day"]
            if hit.any():
                self.n_jumps += int(hit.sum())
                eps = eps + np.where(hit, self.jump["x_jump"], 0.0)
        x = mu - 0.5 * s2 + eps
        lrf = np.log1p(rf)
        xf = self._x_floor - lrf
        cl = x < xf
        if cl.any():
            self.n_clip += int(cl.sum())
            x = np.where(cl, xf, x)
            eps = x - (mu - 0.5 * s2)
        # DGP variance update
        if self.kind == "egarch":
            zz = eps / sig
            self.lns2 = self.om + self.al * (np.abs(zz) - SQRT2_OV_PI) + self.ga * zz + self.be * self.lns2
            self.s2 = np.exp(self.lns2)
        else:
            self.s2 = self.om + (self.al + self.ga * (eps < 0)) * eps * eps + self.be * s2
        if self.s2_cap < np.inf:
            hi = self.s2 > self.s2_cap
            if hi.any():
                self.n_cap += int(hi.sum())
                self.s2 = np.where(hi, self.s2_cap, self.s2)
                if self.kind == "egarch":
                    self.lns2 = np.log(self.s2)
        # investor's base-model filter on the realised x
        si2 = self.s2_inv
        e = x - (self.i_mu - 0.5 * si2)
        if self.ikind == "egarch":
            zi = e / np.sqrt(si2)
            self.s2_inv = np.exp(self.i_om + self.i_al * (np.abs(zi) - SQRT2_OV_PI) + self.i_ga * zi + self.i_be * np.log(si2))
        else:
            self.s2_inv = self.i_om + (self.i_al + self.i_ga * (e < 0)) * e * e + self.i_be * si2
        # rates
        if self.rmode == "vasicek":
            eta = self.rho * z + math.sqrt(max(1 - self.rho ** 2, 0.0)) * xi
            self.ra = self.ra + self.kap * (self.th - self.ra) / self.N + self.sr * math.sqrt(1.0 / self.N) * eta
            if self.floor0:
                self.ra = np.maximum(self.ra, 0.0)
        r = np.expm1(x + lrf)
        return r, rf


# --------------------------------------------------------------------------- engine
class _Group:
    """State of one strategy instance (one horizon, k rows) during a run."""

    def __init__(self, s, n, keep_phi, record):
        self.s, self.T, self.k = s, s.T, s.k
        shp = (s.k, n)
        self.W = np.full(shp, np.nan)
        self.phi_last = np.zeros(shp)      # phi chosen at the start of the latest day
        self.phi_drift = np.zeros(shp)     # weight after that day's returns (used if no rebalance)
        self.sum_phi = np.zeros(shp)
        self.turn = np.zeros(shp)
        self.consec = np.zeros(shp, dtype=np.int32)
        self.cross = np.zeros(shp, dtype=np.int8)
        self.cross_own = np.zeros(shp, dtype=np.int8)
        self.C_last = np.zeros(shp)
        self.Co_last = np.zeros(shp)
        self.sig_last = np.zeros(n)
        self.own = hasattr(s, "protect_floor") and getattr(s, "floor_mode", "spec") != "spec"
        self.W_last = np.zeros(shp)
        self.exc_last = np.zeros(n)
        self.clip_lo = np.zeros(s.k, dtype=np.int64)
        self.clip_hi = np.zeros(s.k, dtype=np.int64)
        self.clip_nan = np.zeros(s.k, dtype=np.int64)
        self.done = False
        self.phi_full = np.zeros((s.k, n, s.T), dtype=np.float32) if keep_phi else None
        if record:
            nrec = len(range(0, s.T, record["stride"])) + 1
            ns, nq = min(record["n_paths"], n), len(record["quantiles"])
            self.rec_W = np.zeros((s.k, ns, nrec), dtype=np.float32)
            self.rec_phi = np.full((s.k, ns, nrec), np.nan, dtype=np.float32)
            self.rec_Wq = np.zeros((s.k, nq, nrec), dtype=np.float32)
            self.rec_phiq = np.full((s.k, nq, nrec), np.nan, dtype=np.float32)
            self.rec_Fq = np.zeros((nq, nrec), dtype=np.float32)

    def _mark(self, target, C_last, C_now, ecfg):
        """Write crossing codes into `target` at the (row, path) positions that crossed today."""
        crossed = (C_last >= 0) & (C_now < 0)
        if not crossed.any():
            return
        ii = np.nonzero(crossed)
        cl = C_last[ii]
        eq = self.phi_last[ii] * self.W_last[ii] * self.exc_last[ii[1]]   # day's equity excess move ($)
        equity_caused = eq <= -cl                      # the day's equity move alone exceeded the cushion
        expo = self.phi_last[ii] * self.W_last[ii] * self.sig_last[ii[1]]   # 1-sigma equity move in $
        with np.errstate(divide="ignore", invalid="ignore"):
            buffer_sigmas = np.where(expo > 0, cl / expo, np.inf)
        gap = equity_caused & (buffer_sigmas >= ecfg["gap_min_sigmas"])
        target[ii] = np.where(gap, 1, np.where(equity_caused, 2, 3))

    def classify(self, C_now, ecfg, Co_now=None):
        """Crossings on the latest day: C went from >= 0 (start of day) to < 0 (start of next day / T).
        `cross` uses the engine's F; `cross_own` uses the floor the strategy itself protects (if any)."""
        self._mark(self.cross, self.C_last, C_now, ecfg)
        if Co_now is not None and self.own:
            self._mark(self.cross_own, self.Co_last, Co_now, ecfg)


def run_engine(market, strategies: list, W0: float, K: float, gamma: float, state0: dict, model_info: dict,
               ecfg: dict, record: dict | None = None, keep_phi: bool = False) -> list[dict]:
    """Run all strategies on the same market paths (common random numbers across strategies).

    record: None, or {"n_paths": 200, "stride": 2, "fan_n": 5000, "quantiles": [...]} to store sample
    paths and pointwise quantiles. Returns one dict per strategy (input order) with per-row arrays.
    """
    n = market.n_paths
    reb = int(ecfg.get("rebalance_every", 1))
    lock_phi, lock_days = ecfg["locked_phi"], ecfg["locked_days"]
    groups = []
    for s in strategies:
        s.reset(n, W0, K, gamma, state0, model_info)
        g = _Group(s, n, keep_phi, record)
        g.W[:] = W0
        groups.append(g)
    Tmax = max(g.T for g in groups)
    hist = History(n, int(ecfg.get("history_days", 63)))
    qs = np.asarray(record["quantiles"]) * 100 if record else None
    fan_n = min(record["fan_n"], n) if record else 0

    for t in range(Tmax):
        sig, ra = market.start_of_day(t)
        lr = np.log1p(ra)
        active = [g for g in groups if not g.done]
        for g in active:
            F = K * np.exp(-((g.T - t) / N_YEAR) * lr)
            C = g.W - F
            Co = g.W - g.s.protect_floor(t, F, ra) if g.own else C
            if t > 0:
                g.classify(C, ecfg, Co)
            if t % reb == 0:
                raw = np.broadcast_to(np.asarray(g.s.weight(t, g.W, F, sig, ra, hist), dtype=float), g.W.shape)
                bad = ~np.isfinite(raw)
                if bad.any():
                    g.clip_nan += bad.sum(axis=1)
                    raw = np.where(bad, 0.0, raw)
                g.clip_lo += np.count_nonzero(raw < 0.0, axis=1)
                g.clip_hi += np.count_nonzero(raw > 1.0, axis=1)
                phi = np.clip(raw, 0.0, 1.0)
                if t > 0:
                    g.turn += np.abs(phi - g.phi_drift)
            else:
                phi = g.phi_drift
            g.C_last, g.Co_last, g.W_last, g.phi_last, g.sig_last = C, Co, g.W, phi, sig
            if keep_phi:
                g.phi_full[:, :, t] = phi
            if record and t % record["stride"] == 0:
                j, ns = t // record["stride"], g.rec_W.shape[1]
                g.rec_W[:, :, j] = g.W[:, :ns]
                g.rec_phi[:, :, j] = phi[:, :ns]
                g.rec_Wq[:, :, j] = np.percentile(g.W[:, :fan_n], qs, axis=1).T
                g.rec_phiq[:, :, j] = np.percentile(phi[:, :fan_n], qs, axis=1).T
                g.rec_Fq[:, j] = np.percentile(F[:fan_n], qs)
        r, rf = market.realise(t)
        exc = r - rf
        for g in active:
            phi = g.phi_last
            port = rf + phi * exc
            g.exc_last = exc
            g.W = g.W * (1.0 + port)
            g.sum_phi += phi
            if t >= g.T - lock_days:          # only the last locked_days days matter
                g.consec = np.where(phi < lock_phi, g.consec + 1, 0)
            g.phi_drift = phi * (1.0 + r) / (1.0 + port)
            g.s.on_returns(t, r, rf)
            if t == g.T - 1:
                g.classify(g.W - K, ecfg, g.W - K)          # F_T = K
                g.done = True
        hist.push(r, rf)

    out = []
    for g in groups:
        res = {"strategy": g.s.name, "labels": g.s.labels, "knob_values": g.s.knob_values, "T_days": g.T,
               "W_T": g.W, "phi_mean_path": g.sum_phi / g.T, "turnover": g.turn,
               "locked": g.consec >= lock_days, "cross": g.cross,
               "cross_own": g.cross_own if g.own else g.cross,
               "clip_lo": g.clip_lo, "clip_hi": g.clip_hi, "clip_nan": g.clip_nan, "path_days": g.T * n,
               "infeasible": getattr(g.s, "infeasible", None)}
        if record:
            g.rec_W[:, :, -1] = g.W[:, :g.rec_W.shape[1]]
            g.rec_Wq[:, :, -1] = np.percentile(g.W[:, :fan_n], qs, axis=1).T
            g.rec_Fq[:, -1] = K
            res.update({"rec_W": g.rec_W, "rec_phi": g.rec_phi, "rec_Wq": g.rec_Wq, "rec_phiq": g.rec_phiq,
                        "rec_Fq": g.rec_Fq, "rec_days": np.r_[np.arange(0, g.T, record["stride"]), g.T]})
        if keep_phi:
            res["phi_full"] = g.phi_full
        out.append(res)
    return out
