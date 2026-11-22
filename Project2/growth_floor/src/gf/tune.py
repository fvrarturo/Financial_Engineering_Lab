"""Tune strategy knobs to the breach constraint (Spec 03 §3).

For each (state, horizon) cell, a grid search with refinement finds the most aggressive knob value whose
breach probability on the *tuning seed* is <= target (a buffer below 2%). All candidates are evaluated on
the same simulated paths (common random numbers), so breach is a step function of the knob; monotonicity is
checked, not assumed: we count candidate pairs where breach falls by more than 2 binomial SEs as the knob
becomes more aggressive.

Selection rule: candidates are sorted from least to most aggressive; the tuned value is the last candidate
before the first one whose breach exceeds the target (conservative under noise). Refinement searches
between that candidate and the first violator.

Modes:
  uniform      one value for every state (excluding 'extreme') and horizon = most conservative per-cell value
  uniform_all  same, including 'extreme' (reported, per the addendum)
  state        per-cell values, linearly interpolated in (log sigma_0, T) -> KnobFunction
"""
from __future__ import annotations

import math

import numpy as np

from .engine import SimMarket, run_engine
from .strategies import make_strategy


def candidates(kcfg: dict, lo: float, hi: float, n: int) -> np.ndarray:
    if kcfg["spacing"] == "log":
        return np.exp(np.linspace(math.log(lo), math.log(hi), n))
    return np.linspace(lo, hi, n)


def aggressive_order(vals: np.ndarray, direction: int) -> np.ndarray:
    """Indices sorting vals from least to most aggressive."""
    return np.argsort(vals) if direction > 0 else np.argsort(-vals)


def pick(vals, breach, n_paths, direction, target):
    """Return (value, idx_ok, idx_viol, flag, n_monotone_violations) for one strategy/horizon."""
    o = aggressive_order(vals, direction)
    v, b = vals[o], breach[o]
    se = np.sqrt(np.maximum(b * (1 - b), 1e-12) / n_paths)
    viol = int(np.sum(np.diff(b) < -2 * np.sqrt(se[:-1] ** 2 + se[1:] ** 2)))
    over = np.nonzero(b > target)[0]
    if len(over) == 0:
        return v[-1], len(v) - 1, None, "cap_most_aggressive", viol, v, b
    j = over[0]
    if j == 0:
        return v[0], None, 0, "infeasible_least_aggressive", viol, v, b
    return v[j - 1], j - 1, j, "ok", viol, v, b


def _knob_u(kcfg, v):
    return np.log(v) if kcfg["spacing"] == "log" else np.asarray(v, float)


def _knob_from_u(kcfg, u):
    return float(np.exp(u)) if kcfg["spacing"] == "log" else float(u)


def logistic_fit(u, k, n):
    """Binomial MLE of logit p = a + b u (IRLS, ridge-stabilised). Returns (a, b) or None."""
    u = np.asarray(u, float); k = np.asarray(k, float)
    if len(u) < 3 or np.ptp(u) <= 0:
        return None
    um, us = u.mean(), u.std()
    z = (u - um) / us
    X = np.column_stack([np.ones_like(z), z])
    p0 = np.clip((k.sum() + 0.5) / (n * len(u) + 1.0), 1e-6, 0.5)
    beta = np.array([math.log(p0 / (1 - p0)), 0.0])
    for _ in range(100):
        eta = np.clip(X @ beta, -30, 30)
        p = 1 / (1 + np.exp(-eta))
        w = n * p * (1 - p)
        g = X.T @ (k - n * p) - 1e-6 * beta
        H = (X * w[:, None]).T @ X + 1e-6 * np.eye(2)
        step = np.linalg.solve(H, g)
        beta = beta + step
        if np.max(np.abs(step)) < 1e-10:
            break
    a, b = beta
    return a - b * um / us, b / us


def smooth_crossing(kcfg, vals, breach, n_paths, target, window):
    """Knob value where a logistic-in-knob fit (log knob for log-spaced knobs) to the breach estimates in
    `window` (the refinement bracket) equals the target. Returns (value, info) or (None, info) when the fit
    is unusable (too few points, slope of the wrong sign); the caller then falls back to the grid pick.
    The result is clipped to the window; if the fitted curve stays below the target over the whole window
    the aggressive end is returned (no crossing inside)."""
    lo, hi = window
    m = (vals >= lo - 1e-12) & (vals <= hi + 1e-12)
    v, b = vals[m], breach[m]
    info = {"n_points": int(m.sum())}
    u = _knob_u(kcfg, v)
    ab = logistic_fit(u, np.round(b * n_paths), n_paths)
    if ab is None:
        return None, {**info, "fit": "too_few_points"}
    a, s = ab
    d = kcfg["direction"]
    if s * d <= 0:                      # breach must rise as the knob gets more aggressive
        return None, {**info, "fit": "wrong_slope", "a": a, "b": s}
    ustar = (math.log(target / (1 - target)) - a) / s
    ulo, uhi = _knob_u(kcfg, lo), _knob_u(kcfg, hi)
    uc = float(np.clip(ustar, min(ulo, uhi), max(ulo, uhi)))
    return _knob_from_u(kcfg, uc), {**info, "fit": "ok" if uc == ustar else "clipped", "a": a, "b": s,
                                     "u_star": float(ustar)}


def tune_state(ctx, state0: dict, horizons: list[int], strat_names: list[str], cfg: dict, n_paths: int,
               seed: int, scenarios: list[str] | None = None) -> dict:
    """Tune every knobbed strategy at one starting state for all horizons.

    Robust tuning (2026-11-21): every candidate is simulated under each scenario in `scenarios` (default
    cfg.tune.robust_set) on the tuning seed (same seed per scenario), and the robust value is the most
    aggressive candidate whose *maximum* breach across scenarios is <= target. The base-only value (breach
    under `base` alone) comes from the same simulations and is kept as a comparison.
    Pass 1 is a grid over the knob bounds; pass 2 refines between the last-ok and first-violating candidate for
    both objectives (union of the two grids). Final picks use every candidate evaluated (all on the same paths).
    """
    P, tc, kc = cfg["problem"], cfg["tune"], cfg["strategies"]["knobs"]
    target = tc["target_breach"]
    G = tc["grid_points"]
    scenarios = list(scenarios or tc.get("robust_set") or ["base"])
    if "base" not in scenarios:
        scenarios = ["base", *scenarios]
    info = ctx.model_info(state0["sigma0_ann"], P["gamma"], cfg["strategies"]["floor"])
    grids = {(s, T): candidates(kc[s], kc[s]["lo"], kc[s]["hi"], G) for s in strat_names for T in horizons}
    hist = {(s, T): {} for s in strat_names for T in horizons}         # value -> {scenario: breach}
    brackets = {}
    result = {}
    for p in range(tc["passes"]):
        for scen in scenarios:
            dgp, rate, _ = ctx.dgp(scen, cfg["mc"]["scenarios"][scen], n_paths)
            strats, keys = [], []
            for s in strat_names:
                for T in horizons:
                    c = grids[(s, T)]
                    if len(c) == 0:
                        continue
                    extra = {"vol_input": cfg["strategies"]["obpi_vol_input"]} if s == "obpi_merton" else {}
                    strats.append(make_strategy(s, T, c, cfg=cfg, **extra))
                    keys.append((s, T, c))
            if not strats:
                continue
            mk = SimMarket(dgp, ctx.investor(), rate, state0, n_paths, seed)
            out = run_engine(mk, strats, P["W0"], P["K"], P["gamma"], state0, info, cfg["engine"])
            for (s, T, c), res in zip(keys, out):
                br = (res["W_T"] < P["K"]).mean(axis=1)
                for v, b_ in zip(c.tolist(), br.tolist()):
                    hist[(s, T)].setdefault(v, {})[scen] = b_
        # picks over everything evaluated so far; next pass refines both brackets
        for s in strat_names:
            for T in horizons:
                h = hist[(s, T)]
                vals = np.array(sorted(h))
                b_rob = np.array([max(h[v].values()) for v in vals])
                b_base = np.array([h[v]["base"] for v in vals])
                out_ = {}
                new = []
                for lab, bv in (("robust", b_rob), ("base", b_base)):
                    val, i_ok, i_v, flag, viol, vs, bs = pick(vals, bv, n_paths, kc[s]["direction"], target)
                    out_[lab] = {"value": float(val), "flag": flag, "monotone_violations": viol,
                                 "breach_at_value": float(bs[i_ok]) if i_ok is not None else float(bs[0])}
                    if flag == "ok":
                        lo_, hi_ = sorted([vs[i_ok], vs[i_v]])
                        if p == 0:
                            brackets[(s, T, lab)] = (lo_, hi_)       # refinement window (logistic fit)
                        new.append(candidates(kc[s], lo_, hi_, G + 2)[1:-1])
                vb = out_["robust"]["value"]
                by = h[vb]
                out_["robust"]["breach_by_scenario"] = dict(by)
                out_["robust"]["binding_scenario"] = max(by, key=by.get)
                out_["base"]["breach_by_scenario"] = dict(h[out_["base"]["value"]])
                result[(s, T)] = out_
                grids[(s, T)] = np.unique(np.concatenate(new)) if new else np.array([])
                grids[(s, T)] = np.array([v for v in grids[(s, T)] if v not in h])
    final = {}
    for (s, T), v in result.items():
        h = hist[(s, T)]
        grid = [(val, h[val]) for val in sorted(h)]
        if tc.get("selection", "grid") == "logistic":
            vals = np.array(sorted(h))
            for lab, scens in (("robust", scenarios), ("base", ["base"])):
                r = v[lab]
                r["grid_value"] = r["value"]
                win = brackets.get((s, T, lab))
                if r["flag"] != "ok" or win is None:
                    r["selection"] = "grid_" + r["flag"]
                    continue
                d = kc[s]["direction"]
                cross, infos = {}, {}
                for sc in scens:
                    bv = np.array([h[x].get(sc, np.nan) for x in vals])
                    c_, inf_ = smooth_crossing(kc[s], vals, bv, n_paths, target, win)
                    infos[sc] = inf_
                    if c_ is None:           # unusable fit: conservative grid pick on this scenario alone
                        c_ = pick(vals, bv, n_paths, d, target)[0]
                        inf_["fallback_grid"] = float(c_)
                    cross[sc] = c_
                # most conservative crossing across scenarios
                sc_bind = min(cross, key=lambda q: cross[q] * d)
                r.update({"value": float(cross[sc_bind]), "selection": "logistic", "binding_scenario": sc_bind,
                          "crossing_by_scenario": cross, "fit_by_scenario": infos})
        final[(s, T)] = {**v["robust"], "base": v["base"], "grid": grid, "scenarios": scenarios}
    return final


def select(cells: list[dict], which: str) -> list[dict]:
    """Cells with each result replaced by the 'base' (base-only tuned) sub-result; 'robust' returns as is."""
    if which == "robust":
        return cells
    return [{**c, "result": {k: {**v["base"], "grid": v["grid"]} for k, v in c["result"].items()}} for c in cells]


class KnobFunction:
    """Knob as a function of (sigma_0, T): linear in log sigma_0 and in T, flat beyond the tuned range."""

    def __init__(self, sigmas, Ts, values):
        self.sig = np.asarray(sigmas, float)
        self.T = np.asarray(Ts, float)
        self.V = np.asarray(values, float)        # shape (len(T), len(sig))
        o = np.argsort(self.sig)
        self.sig, self.V = self.sig[o], self.V[:, o]

    def __call__(self, sigma0, T_days):
        s = np.log(np.atleast_1d(np.asarray(sigma0, float)))
        rows = np.array([np.interp(s, np.log(self.sig), self.V[i]) for i in range(len(self.T))])  # (nT, m)
        if len(self.T) == 1:
            out = rows[0]
        else:
            out = np.array([np.interp(T_days, self.T, rows[:, j]) for j in range(rows.shape[1])])
        return out if np.ndim(sigma0) else float(out[0])

    def to_dict(self):
        return {"sigma": self.sig.tolist(), "T_days": self.T.tolist(), "values": self.V.tolist()}

    @classmethod
    def from_dict(cls, d):
        return cls(d["sigma"], d["T_days"], d["values"])


def aggregate(cells: list[dict], strat_names: list[str], horizons: list[int], kcfg: dict) -> dict:
    """cells: [{"state", "sigma0", "r0", "result": tune_state output}]. Returns per-strategy summaries."""
    out = {}
    for s in strat_names:
        d = kcfg[s]["direction"]
        rows = [{"state": c["state"], "sigma0": c["sigma0"], "r0": c["r0"], "T_days": T, **c["result"][(s, T)]}
                for c in cells for T in horizons if (s, T) in c["result"]]
        cons = (lambda vals: min(vals)) if d > 0 else (lambda vals: max(vals))

        def uni(rs):
            vals = [r["value"] for r in rs]
            v = cons(vals)
            b = [r for r in rs if r["value"] == v][0]
            return {"value": v, "binding_state": b["state"], "binding_T_days": b["T_days"]}
        named = [r for r in rows if r["state"] in ("calm", "normal", "elevated", "crisis", "extreme")]
        excl = [r for r in named if r["state"] != "extreme"]
        sigs = sorted({c["sigma0"] for c in cells})
        V = [[next(r["value"] for r in rows if r["sigma0"] == sg and r["T_days"] == T) for sg in sigs] for T in horizons]
        out[s] = {"knob": kcfg[s]["name"], "direction": d, "cells": rows,
                  "uniform": uni(excl), "uniform_all": uni(named),
                  "state_fn": KnobFunction(sigs, horizons, V).to_dict(),
                  "flags": sorted({r["flag"] for r in rows}),
                  "monotone_violations_total": int(sum(r["monotone_violations"] for r in rows))}
    return out
