#!/usr/bin/env python
"""Stage 5 (SPEC 04): dynamic-programming policy with a breach penalty.

Usage (each stage is resumable; outputs are cached under results/policies/cache):
  python scripts/05_dp.py solve     --asset us_mkt     # backward solves on the lam grid -> results/policies/dp_{asset}.npz
  python scripts/05_dp.py calibrate --asset us_mkt     # robust-set MC on the tuning seed -> lam*(sigma0, T) lookup
  python scripts/05_dp.py validate  --asset us_mkt     # V1-V5 (V6 after evaluate) -> reports/dp/validation_{asset}.json
  python scripts/05_dp.py evaluate  --asset us_mkt     # evaluation seed, base + robust + stress -> results_dp/
  python scripts/05_dp.py figures   --asset us_mkt     # policy heatmaps -> reports/dp/
  python scripts/05_dp.py all       --asset us_mkt

Same machinery as SPEC 03: SimMarket + run_engine + row_metrics, seeds cell_seed(master, asset, 'tune'|'eval'),
tune.n_paths for calibration, mc.n_paths for evaluation, tune.robust_set / tune.target_breach for lam*.
At most dp.n_jobs (2) worker processes; BLAS threads are limited to 2 in this script.
"""
from __future__ import annotations

import os

for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "1")      # 2 worker processes x 1 thread = the 2-core budget

import argparse  # noqa: E402
import datetime as dt  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from joblib import Parallel, delayed  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gf import dp as D  # noqa: E402
from gf.data import load_config, resolve  # noqa: E402
from gf.engine import SimMarket, run_engine  # noqa: E402
from gf.metrics import row_metrics  # noqa: E402
from gf.scenarios import cell_seed, load_context  # noqa: E402
from gf.strategies import make_strategy  # noqa: E402
from gf.tune import logistic_fit  # noqa: E402


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def jsonable(o):
    if isinstance(o, dict):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    if isinstance(o, (np.floating, np.integer, np.bool_)):
        return o.item()
    if isinstance(o, np.ndarray):
        return jsonable(o.tolist())
    if isinstance(o, float) and not math.isfinite(o):
        return None
    return o


def wjson(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(jsonable(obj), indent=1, default=str))
    os.replace(tmp, path)


# --------------------------------------------------------------------------- setup
class Setup:
    def __init__(self, cfg, asset, y_mult: int = 1):
        self.cfg, self.asset = cfg, asset
        dc = cfg["dp"]
        self.dc = dc
        self.ctx = load_context(cfg, asset)
        self.model = D.model_from_context(self.ctx, cfg)
        yg = dc["y_grid"]
        if "bands" in yg:
            self.y = D.y_grid_bands(yg["bands"]["edges"], yg["bands"]["counts"], y_mult)
        else:
            n = (yg["n"] - 1) * y_mult + 1
            self.y = D.y_grid(n, yg["lo"], yg["hi"], tuple(yg["dense"]), yg["dense_share"])
        named = self.ctx.state_rows("named")
        calm = float(named[named.state_name == "calm"].sigma0_ann.iloc[0])
        hg = dc["h_grid"]
        self.h = D.h_grid(hg["lo_mult_calm"] * calm, math.sqrt(self.model.h_cap * self.ctx.N), hg["n"])
        self.quad = D.shock_quadrature(self.model.nu, dc["quad_nodes"])
        self.phi = D.phi_grid(dc["phi_points"])
        self.lams = [float(v) for v in dc["lam_grid"]]
        self.tau_max = int(dc["tau_max"])
        self.pol_dir = resolve(cfg, dc["policy_dir"])
        self.cache = self.pol_dir / "cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.tag = "" if y_mult == 1 else f"_y{y_mult}"
        self.interp_y = dc.get("interp_y", "linear")
        self.interp_h = dc.get("interp_h", "linear")
        self.policy_path = self.pol_dir / f"dp_{asset}{self.tag}.npz"
        self.named = named
        self.rep = resolve(cfg, dc.get("report_dir", "reports/dp"))
        self.rep.mkdir(parents=True, exist_ok=True)

    def sol_path(self, lam):
        it = "" if self.interp_y == "pchip" else f"_{self.interp_y}"
        return self.cache / f"dp_{self.asset}{self.tag}{it}_lam{lam:.6g}.npz"

    def meta(self):
        return {"asset": self.asset, "interp_y": self.interp_y, "interp_h": self.interp_h, "y": self.y.tolist(),
                "y_n": len(self.y), "h_n": len(self.h), "z_n": len(self.quad.z),
                "phi_n": len(self.phi), "tau_max": self.tau_max, "lam_grid": self.lams, "quad": self.quad.info,
                "model": {k: v for k, v in self.model.__dict__.items()}, "units": "J in $M utility units: -1/(W/1e6)",
                "generated": dt.datetime.now().isoformat(timespec="seconds")}


def _cache_ok(S: "Setup", p) -> bool:
    """A cached solve is reused only if it was made on this (y, h) grid. Files without stored grids
    (written before 2026-11-22 01:40) are accepted; all such files left in cache/ were checked by hand."""
    if not p.exists():
        return False
    with np.load(p) as z:
        if "y" not in z.files:
            return True
        return (z["y"].shape == S.y.shape and np.allclose(z["y"], S.y, rtol=0, atol=1e-12)
                and z["h"].shape == S.h.shape and np.allclose(z["h"], S.h, rtol=1e-12, atol=0))


def _solve_one(cfg, asset, lam, y_mult):
    S = Setup(cfg, asset, y_mult)
    p = S.sol_path(lam)
    if _cache_ok(S, p):
        return str(p)
    s = D.solve(S.model, lam, S.tau_max, S.y, S.h, S.quad, S.phi, chunk_h=cfg["dp"].get("chunk_h", 4),
                interp_y=S.interp_y, interp_h=S.interp_h)
    tmp = p.with_name(p.stem + ".tmp.npz")
    np.savez_compressed(tmp, lam=lam, phi_idx=s.phi_idx, J=s.J, B=s.B, q=s.q, seconds=s.seconds,
                        below=s.below_ymin_share, y=S.y, h=S.h)
    os.replace(tmp, p)
    return str(p)


def load_sol(S: Setup, lam) -> D.DPSolution:
    if not _cache_ok(S, S.sol_path(lam)):
        raise RuntimeError(f"cached solve {S.sol_path(lam)} is missing or on a different grid")
    z = np.load(S.sol_path(lam))
    return D.DPSolution(float(z["lam"]), S.y, S.h, S.phi, S.tau_max, z["phi_idx"], z["J"], z["B"], z["q"],
                        float(z["seconds"]), float(z["below"]))


def stage_solve(cfg, asset, lams=None, y_mult=1, jobs=2):
    S = Setup(cfg, asset, y_mult)
    lams = S.lams if lams is None else lams
    todo = [l for l in lams if not _cache_ok(S, S.sol_path(l))]
    log(f"solve {asset}{S.tag}: {len(lams)} lam values ({len(todo)} to do), y {len(S.y)} x h {len(S.h)} x "
        f"z {len(S.quad.z)} x phi {len(S.phi)}, tau_max {S.tau_max}")
    t0 = time.time()
    Parallel(n_jobs=min(jobs, max(len(todo), 1)))(delayed(_solve_one)(cfg, asset, l, y_mult) for l in todo)
    sols = [load_sol(S, l) for l in lams]
    log(f"  solves done in {time.time() - t0:.0f}s wall; per-solve seconds "
        f"{[round(s.seconds) for s in sols]}")
    if y_mult == 1 and lams == S.lams:
        lk = None
        cal = S.rep / f"calibration_{asset}.json"
        if cal.exists():
            lk = json.loads(cal.read_text())["lookup"]
        D.save_policy(S.policy_path, sols, S.model, lk, S.meta(), J_path=S.pol_dir / f"dp_{asset}_J.npz")
    else:
        D.save_policy(S.policy_path, sols, S.model, None, S.meta())
    return sols


# --------------------------------------------------------------------------- MC with the harness
def mc_task(cfg, asset, state_row, scenario, horizons, n_paths, seed, policy_path, lam_rows=None,
            record=False, terminal_dir=None):
    """One SimMarket run with a dp_policy strategy per horizon. lam_rows: list of lam values (rows) or None
    (lam* lookup). Returns per-row metrics rows (+ the strategy diagnostics)."""
    t0 = time.time()
    ctx = load_context(cfg, asset)
    P, mc = cfg["problem"], cfg["mc"]
    s0 = {"sigma0_ann": float(state_row["sigma0_ann"]), "r0_ann": float(state_row["r0_ann"])}
    dgp, rate, _ = ctx.dgp(scenario, mc["scenarios"][scenario], n_paths)
    info = ctx.model_info(s0["sigma0_ann"], P["gamma"], cfg["strategies"]["floor"])
    strats = []
    for T in horizons:
        st = make_strategy("dp_policy", T, lam_rows, labels=None if lam_rows is None else
                           [f"lam={v:.3g}" for v in lam_rows], cfg=cfg, path=str(policy_path))
        strats.append(st)
    rec = {"n_paths": mc["sample_paths"], "stride": mc["path_stride"], "fan_n": mc["fan_subsample"],
           "quantiles": mc["fan_quantiles"]} if record else None
    mk = SimMarket(dgp, ctx.investor(), rate, s0, n_paths, seed)
    out = run_engine(mk, strats, P["W0"], P["K"], P["gamma"], s0, info, cfg["engine"], record=rec)
    rows, extras = [], {}
    for T, st, res in zip(horizons, strats, out):
        for i in range(st.k):
            m = row_metrics(res, i, P["K"], P["W0"], s0["r0_ann"], P["gamma"], mc, boot_seed=seed % (2 ** 31) + i)
            rows.append({"asset": asset, "strategy": "dp_policy",
                         "tune_mode": "lam_star" if lam_rows is None else "lam_grid",
                         "knob_name": "lam", "knob": float(st.knob_values[i]), "lam_idx": int(st.lam_idx[i]),
                         "state": state_row["state_name"], "state_source": state_row["source"],
                         "sigma0": s0["sigma0_ann"], "r0": s0["r0_ann"], "T": T / ctx.N, "T_days": T,
                         "scenario": scenario, "seed": seed,
                         "n_sim_return_clips": mk.n_clip, "n_jumps": mk.n_jumps,
                         "sigma_capped_share": mk.n_cap / (n_paths * max(horizons)),
                         "dp_below_ymin_share": float(st.n_below_ymin[i] / (n_paths * T)),
                         "dp_above_ymax_share": float(st.n_above_ymax[i] / (n_paths * T)),
                         "dp_h_above_grid_share": float(st.n_h_above / (n_paths * T)),
                         "dp_h_below_grid_share": float(st.n_h_below / (n_paths * T)), **m})
        extras[T] = res
    return rows, extras, time.time() - t0, mk


def _calib_task(cfg, asset, state_row, scenario, horizons, n_paths, seed, policy_path, lams):
    rows, _, secs, _ = mc_task(cfg, asset, state_row, scenario, horizons, n_paths, seed, policy_path, lams)
    return rows, secs


def run_mc_grid(cfg, asset, scenarios, horizons, n_paths, seed, policy_path, lams, cache_name, jobs=2):
    S = Setup(cfg, asset)
    cf = S.cache / f"{cache_name}.parquet"
    if cf.exists():
        return pd.read_parquet(cf)
    tasks = [(r, sc) for sc in scenarios for r in S.named.to_dict("records")]
    log(f"  MC grid {cache_name}: {len(tasks)} runs x {len(horizons)} horizons x {len(lams)} lam rows at {n_paths} paths")
    t0 = time.time()
    res = Parallel(n_jobs=jobs)(delayed(_calib_task)(cfg, asset, r, sc, horizons, n_paths, seed, policy_path, lams)
                                for r, sc in tasks)
    df = pd.DataFrame([x for r in res for x in r[0]])
    log(f"  done in {time.time() - t0:.0f}s wall (sum of task seconds {sum(r[1] for r in res):.0f})")
    df.to_parquet(cf, index=False)
    return df


def cash_ce(cfg, asset, seed_kind: str, n_paths: int, horizons, scenario: str = "base") -> dict:
    """{(state, T_days): ce_ann of the cash strategy} on the named states, same seed / n_paths / scenario as the
    runs it is subtracted from (CE convention: excess over the cash return, %/yr). Cached in the policy cache."""
    S = Setup(cfg, asset)
    seed = cell_seed(cfg["mc"]["master_seed"], asset, seed_kind)
    cf = S.cache / f"cash_ce_{asset}_{seed_kind}_{scenario}_{n_paths}.parquet"
    if cf.exists():
        df = pd.read_parquet(cf)
    else:
        P, mc = cfg["problem"], cfg["mc"]
        rows = []
        for r in S.named.itertuples():
            s0 = {"sigma0_ann": float(r.sigma0_ann), "r0_ann": float(r.r0_ann)}
            dgp, rate, _ = S.ctx.dgp(scenario, mc["scenarios"][scenario], n_paths)
            info = S.ctx.model_info(s0["sigma0_ann"], P["gamma"], cfg["strategies"]["floor"])
            strats = [make_strategy("cash", int(T)) for T in horizons]
            mk = SimMarket(dgp, S.ctx.investor(), rate, s0, n_paths, seed)
            out = run_engine(mk, strats, P["W0"], P["K"], P["gamma"], s0, info, cfg["engine"])
            for T, res in zip(horizons, out):
                m = row_metrics(res, 0, P["K"], P["W0"], s0["r0_ann"], P["gamma"], {**mc, "bootstrap_reps": 0}, 0)
                rows.append({"state": r.state_name, "T_days": int(T), "ce_ann_cash": m["ce_ann"]})
        df = pd.DataFrame(rows)
        df.to_parquet(cf, index=False)
    return {(r.state, int(r.T_days)): float(r.ce_ann_cash) for r in df.itertuples()}


# --------------------------------------------------------------------------- calibration
def select_lam(lams, sub: pd.DataFrame, target: float, n_paths: int):
    """lam* for one (state, T) cell from per-(lam, scenario) breach estimates.

    Grid rule: smallest grid lam whose worst-case (max over the robust set) breach <= target. If the worst-case
    curve is noisy (a rise by more than 2 binomial SEs as lam grows), a logistic fit in log lam (lam > 0)
    per scenario (tune.logistic_fit, as SPEC 03) locates each crossing; the most conservative crossing is
    rounded UP to the next grid lam (no interpolation between grid values)."""
    lams = np.asarray(lams, float)
    piv = sub.pivot_table(index="knob", columns="scenario", values="breach").reindex(lams)
    worst = piv.max(axis=1).to_numpy()
    se = np.sqrt(np.maximum(worst * (1 - worst), 1e-12) / n_paths)
    rises = int(np.sum(np.diff(worst) > 2 * np.sqrt(se[:-1] ** 2 + se[1:] ** 2)))
    ok = np.nonzero(worst <= target)[0]
    grid_idx = int(ok[0]) if len(ok) else None
    out = {"worst_by_lam": worst.tolist(), "binding_scenario_by_lam": piv.idxmax(axis=1).tolist(),
           "noisy_rises": rises, "grid_idx": grid_idx}
    if len(ok) and ok[0] == 0:
        out.update({"idx": 0, "rule": "lam0_feasible"})
        return out
    if rises == 0 or len(ok) == 0:
        idx = grid_idx if grid_idx is not None else len(lams) - 1
        out.update({"idx": idx, "rule": "grid" if grid_idx is not None else "infeasible_largest_lam"})
        return out
    pos = lams > 0
    cross = {}
    for sc in piv.columns:
        b = piv[sc].to_numpy()[pos]
        ab = logistic_fit(np.log(lams[pos]), np.round(b * n_paths), n_paths)
        if ab is None or ab[1] >= 0:          # breach must fall as lam rises
            continue
        cross[sc] = math.exp((math.log(target / (1 - target)) - ab[0]) / ab[1])
    if not cross:
        out.update({"idx": grid_idx, "rule": "grid_fit_unusable"})
        return out
    lc = max(cross.values())
    above = np.nonzero(lams >= lc)[0]
    idx = int(above[0]) if len(above) else len(lams) - 1
    out.update({"idx": idx, "rule": "logistic_next_grid_up", "crossing": lc, "crossing_by_scenario": cross})
    return out


def stage_calibrate(cfg, asset, jobs=2):
    S = Setup(cfg, asset)
    dc, tc = cfg["dp"], cfg["tune"]
    if not S.policy_path.exists():
        raise SystemExit("run `solve` first")
    n = dc["calib"]["n_paths"] if asset == "us_mkt" else dc["calib"].get("n_paths_industries", dc["calib"]["n_paths"])
    seed = cell_seed(cfg["mc"]["master_seed"], asset, "tune")
    H = dc["calib"]["horizons"]
    scen = tc["robust_set"] if asset == "us_mkt" else dc["calib"].get("robust_set_industries", tc["robust_set"])
    log(f"calibrate {asset}: robust set {scen}, {n} paths, tuning seed {seed}")
    df = run_mc_grid(cfg, asset, scen, H, n, seed, S.policy_path, S.lams, f"calib_{asset}", jobs)
    target = tc["target_breach"]
    cells, idx = [], np.zeros((len(H), len(S.named)), int)
    sig = S.named.sigma0_ann.to_numpy()
    for j, r in enumerate(S.named.itertuples()):
        for i, T in enumerate(H):
            sub = df[(df.state == r.state_name) & (df.T_days == T)]
            sel = select_lam(S.lams, sub, target, n)
            idx[i, j] = sel["idx"]
            cells.append({"state": r.state_name, "sigma0": r.sigma0_ann, "T_days": T, "lam_star": S.lams[sel["idx"]], **sel})
    lookup = {"sigma": sig.tolist(), "T_days": H, "idx": idx.tolist()}
    wjson(S.rep / f"calibration_{asset}.json", {"asset": asset, "lam_grid": S.lams, "target": target,
                                                "robust_set": scen, "n_paths": n, "seed": seed, "cells": cells,
                                                "lookup": lookup})
    # rewrite the policy file with the lookup
    sols = [load_sol(S, l) for l in S.lams]
    D.save_policy(S.policy_path, sols, S.model, lookup, S.meta())
    log(f"  lam* table written: {[(c['state'], c['T_days'], c['lam_star'], c['rule']) for c in cells]}")
    return df, cells


# --------------------------------------------------------------------------- validation
def v1_merton(sig_list=(0.08, 0.12, 0.15, 0.2, 0.3, 0.5), tau_max=60, mu=0.0391323 / 252 * 252 / 252, rf=1.0465e-4):
    """Constant variance, normal shocks, lam = 0: phi* must equal clip(mu_e/(gamma sigma^2), 0, 1) everywhere."""
    y = D.y_grid()
    out = []
    for sg in sig_list:
        hh = sg * sg / 252
        m = D.DPModel(omega=hh, alpha=0, gamma_lev=0, beta=0, nu=None, mu_e_daily=mu, rf_daily=rf, r0_ann=0.0267,
                      h_cap=np.inf, h_fixed=True, rate_model={"kappa": 0.338, "theta": 0.043, "s": 0.0257})
        s = D.solve(m, 0.0, tau_max, y, np.array([hh]), D.shock_quadrature(None), store_J=False)
        merton = float(np.clip(mu * 252 / (2 * sg * sg), 0, 1))
        P = np.stack([s.policy(t) for t in range(1, tau_max + 1)])
        out.append({"sigma": sg, "merton": merton, "phi_values": np.unique(P).tolist(),
                    "max_abs_dev": float(np.abs(P - merton).max())})
    step = 1 / 40
    return {"cells": out, "tolerance": step / 2 + 1e-3, "pass": all(c["max_abs_dev"] <= step / 2 + 1e-3 for c in out)}


def v3_shape(S: Setup, sols, taus=(1, 5, 21, 63, 126, 252, 378, 504)):
    """J increasing in y; phi in [0, 1]; phi non-decreasing in y (cushion) at fixed (tau, h); phi non-increasing
    in h at fixed (tau, y). Counts violations larger than one control step and where they occur."""
    step = S.phi[1] - S.phi[0]
    res = []
    for s in sols:
        jv = phi_y = phi_h = 0
        tot_y = tot_h = 0
        locs_y, locs_h = [], []
        for t in taus:
            J = s.J[t].astype(float)
            jv += int(np.sum(np.diff(J, axis=0) < -1e-9 * np.abs(J[1:]).max()))
            P = s.policy(t)
            dy = np.diff(P, axis=0)
            dh = np.diff(P, axis=1)
            vy = dy < -step - 1e-9
            vh = dh > step + 1e-9
            phi_y += int(vy.sum())
            phi_h += int(vh.sum())
            tot_y += dy.size
            tot_h += dh.size
            for (i, k) in zip(*np.nonzero(vy)):
                if len(locs_y) < 12:
                    locs_y.append({"tau": t, "y": float(S.y[i]), "sigma": float(math.sqrt(S.h[k] * 252)),
                                   "phi": float(P[i, k]), "phi_next_y": float(P[i + 1, k])})
            for (i, k) in zip(*np.nonzero(vh)):
                if len(locs_h) < 12:
                    locs_h.append({"tau": t, "y": float(S.y[i]), "sigma": float(math.sqrt(S.h[k] * 252)),
                                   "phi": float(P[i, k]), "phi_next_h": float(P[i, k + 1])})
        res.append({"lam": s.lam, "J_decreasing_in_y": jv, "phi_falls_with_cushion": phi_y, "n_y_pairs": tot_y,
                    "phi_rises_with_h": phi_h, "n_h_pairs": tot_h, "examples_y": locs_y, "examples_h": locs_h,
                    "phi_range": [float(min(s.policy(t).min() for t in taus)), float(max(s.policy(t).max() for t in taus))]})
    return res


def dp_breach_at_start(S: Setup, sol: D.DPSolution, sigma0_ann: float, T: int):
    F0 = S.model.K * math.exp(-sol.q[T])
    y0 = 100e6 / F0
    h0 = sigma0_ann ** 2 / 252
    b = float(D.interp2(sol.B[T].astype(float), S.y, S.h, np.array([y0]), np.array([h0]))[0])
    J = float(D.interp2(sol.J[T].astype(float), S.y, S.h, np.array([y0]), np.array([h0]))[0]) if sol.J is not None else None
    EU = None if J is None else J + sol.lam * b
    ce = None if EU is None else -1.0 / EU * 1e6
    return {"y0": y0, "breach_dp": b, "ce_dp": ce}


def stage_validate(cfg, asset, jobs=2):
    S = Setup(cfg, asset)
    out_p = S.rep / f"validation_{asset}.json"
    V = json.loads(out_p.read_text()) if out_p.exists() else {}
    sols = [load_sol(S, l) for l in S.lams]
    cal = json.loads((S.rep / f"calibration_{asset}.json").read_text())
    n = cal["n_paths"]
    df = pd.read_parquet(S.cache / f"calib_{asset}.parquet")
    # V1 ------------------------------------------------------------------------------------------
    if "V1" not in V:
        V["V1"] = v1_merton(mu=S.model.mu_e_daily, rf=S.model.rf_daily)
        log(f"V1 pass={V['V1']['pass']} max dev {max(c['max_abs_dev'] for c in V['V1']['cells']):.4f}")
    # V2 ------------------------------------------------------------------------------------------
    base = df[df.scenario == "base"]
    v2 = []
    for (st, T), g in base.groupby(["state", "T_days"]):
        g = g.sort_values("knob")
        b, c = g.breach.to_numpy(), g.ce.to_numpy()
        se = np.sqrt(np.maximum(b * (1 - b), 1e-12) / n)
        rises = int(np.sum(np.diff(b) > 2 * np.sqrt(se[:-1] ** 2 + se[1:] ** 2)))
        v2.append({"state": st, "T_days": int(T), "breach_by_lam": b.tolist(), "ce_by_lam": c.tolist(),
                   "lam0_highest_breach": bool(b[0] >= b.max() - 2 * se.max()),
                   "lam0_highest_ce": bool(c[0] >= c.max() - 1e-9 * c[0]),
                   "breach_rises_beyond_2se": rises,
                   "gap_share_largest_lam": float(g.breach_gap_share.iloc[-1]) if g.n_breach.iloc[-1] else None,
                   "n_breach_largest_lam": int(g.n_breach.iloc[-1]),
                   "max_jump_pp": float(np.max(-np.diff(b)) * 100)})
    big = sols[-1]
    near = (S.y >= 1.0) & (S.y <= 1.02)
    phi_near = {int(t): float(big.policy(t)[near].mean()) for t in (21, 126, 252, 504)}
    V["V2"] = {"cells": v2, "phi_near_y1_largest_lam": phi_near,
               "pass": all(c["lam0_highest_breach"] and c["lam0_highest_ce"] and c["breach_rises_beyond_2se"] == 0
                           for c in v2)}
    log(f"V2 pass={V['V2']['pass']} phi near y=1 at lam_max {phi_near}")
    # V3 ------------------------------------------------------------------------------------------
    V["V3"] = {"by_lam": v3_shape(S, sols)}
    log("V3 " + str([(r["lam"], r["J_decreasing_in_y"], r["phi_falls_with_cushion"], r["phi_rises_with_h"])
                     for r in V["V3"]["by_lam"]]))
    # V5: DP backward-recursion breach vs MC (base, tuning seed) and vs MC with the rate held constant ----------
    if "V5" not in V:
        rc = run_mc_grid(cfg, asset, ["rate_const"], cal["lookup"]["T_days"], n, cell_seed(cfg["mc"]["master_seed"], asset, "tune"),
                         S.policy_path, S.lams, f"v5_rateconst_{asset}", jobs)
        cells = []
        tol_grid = cfg["dp"]["validation"]["v5_grid_tol"]
        for s in sols:
            for r in S.named.itertuples():
                for T in cal["lookup"]["T_days"]:
                    d = dp_breach_at_start(S, s, r.sigma0_ann, T)
                    mb = base[(base.state == r.state_name) & (base.T_days == T) & np.isclose(base.knob, s.lam)].iloc[0]
                    mr = rc[(rc.state == r.state_name) & (rc.T_days == T) & np.isclose(rc.knob, s.lam)].iloc[0]
                    se_r = math.sqrt(max(mr.breach * (1 - mr.breach), 1e-12) / n)
                    se_b = math.sqrt(max(mb.breach * (1 - mb.breach), 1e-12) / n)
                    cells.append({"lam": s.lam, "state": r.state_name, "T_days": T, **d,
                                  "mc_rate_const": mr.breach, "mc_base": mb.breach,
                                  "ok_rate_const": abs(d["breach_dp"] - mr.breach) <= 2.58 * se_r + tol_grid,
                                  "ok_base": abs(d["breach_dp"] - mb.breach) <= 2.58 * se_b + tol_grid,
                                  "ce_dp": d["ce_dp"], "ce_mc_rate_const": mr.ce, "ce_mc_base": mb.ce})
        V["V5"] = {"cells": cells, "grid_tol": tol_grid, "z": 2.58,
                   "pass_rate_const": all(c["ok_rate_const"] for c in cells),
                   "pass_base": all(c["ok_base"] for c in cells),
                   "n_fail_rate_const": sum(not c["ok_rate_const"] for c in cells),
                   "n_fail_base": sum(not c["ok_base"] for c in cells), "n": len(cells)}
        log(f"V5 pass(rate_const)={V['V5']['pass_rate_const']} ({V['V5']['n_fail_rate_const']}/{len(cells)} fail), "
            f"pass(base)={V['V5']['pass_base']} ({V['V5']['n_fail_base']}/{len(cells)} fail)")
    V["below_ymin_share_quadrature"] = {str(s.lam): s.below_ymin_share for s in sols}
    V["below_ymin_share_mc_max"] = float(df.dp_below_ymin_share.max())
    V["h_above_grid_share_mc_max"] = float(df.dp_h_above_grid_share.max())
    V["solve_seconds"] = {str(s.lam): s.seconds for s in sols}
    V["quadrature"] = S.quad.info
    wjson(out_p, V)
    return V


def stage_v4(cfg, asset, jobs=2):
    """V4: double the y grid once; compare base CE / breach (tuning seed, MC) at lam* for normal & crisis, T = 1, 2."""
    S = Setup(cfg, asset)
    out_p = S.rep / f"validation_{asset}.json"
    V = json.loads(out_p.read_text()) if out_p.exists() else {}
    cal = json.loads((S.rep / f"calibration_{asset}.json").read_text())
    cells = [c for c in cal["cells"] if c["state"] in ("normal", "crisis")]
    lams = sorted({c["lam_star"] for c in cells})
    stage_solve(cfg, asset, lams=lams, y_mult=2, jobs=jobs)
    S2 = Setup(cfg, asset, 2)
    n = cal["n_paths"]
    seed = cell_seed(cfg["mc"]["master_seed"], asset, "tune")
    rows = []
    for st in ("normal", "crisis"):
        r = S.named[S.named.state_name == st].iloc[0].to_dict()
        a, _, _, _ = mc_task(cfg, asset, r, "base", cal["lookup"]["T_days"], n, seed, S.policy_path, lams)
        b, _, _, _ = mc_task(cfg, asset, r, "base", cal["lookup"]["T_days"], n, seed, S2.policy_path, lams)
        for c in [c for c in cells if c["state"] == st]:
            ra = [x for x in a if x["T_days"] == c["T_days"] and np.isclose(x["knob"], c["lam_star"])][0]
            rb = [x for x in b if x["T_days"] == c["T_days"] and np.isclose(x["knob"], c["lam_star"])][0]
            rows.append({"state": st, "T_days": c["T_days"], "lam": c["lam_star"],
                         "ce_ann_y1": ra["ce_ann"], "ce_ann_y2": rb["ce_ann"],
                         "d_ce_bp_per_year": (rb["ce_ann"] - ra["ce_ann"]) * 1e4,
                         "breach_y1": ra["breach"], "breach_y2": rb["breach"],
                         "d_breach_pp": (rb["breach"] - ra["breach"]) * 100})
    ok = all(abs(r["d_ce_bp_per_year"]) < 2 and abs(r["d_breach_pp"]) < 0.1 for r in rows)
    V["V4"] = {"cells": rows, "pass": ok, "y_n": [len(S.y), len(S2.y)],
               "note": "doubling the h grid and z nodes is further work (trimmed)"}
    wjson(out_p, V)
    log(f"V4 pass={ok}: {[(r['state'], r['T_days'], round(r['d_ce_bp_per_year'], 2), round(r['d_breach_pp'], 3)) for r in rows]}")
    return V["V4"]


# --------------------------------------------------------------------------- evaluation
def cell_id(asset, scenario, state, T):
    return f"{asset}__{scenario}__{state}__T{T}".replace("@", "_at_").replace("/", "_")


def _eval_task(cfg, asset, state_row, scenario, horizons, n_paths, seed, policy_path, root: str, record):
    root = Path(root)
    rows, extras, secs, mk = mc_task(cfg, asset, state_row, scenario, horizons, n_paths, seed, policy_path, None,
                                     record=record)
    mc = cfg["mc"]
    sub = min(mc["terminal_subsample"], n_paths)
    for r in rows:
        T = r["T_days"]
        res = extras[T]
        cid = cell_id(asset, scenario, state_row["state_name"], T)
        r.update({"cell_id": cid, "terminal_file": f"mc/terminal/{cid}.npy", "terminal_row": 0,
                  "paths_file": f"mc/paths/{cid}.npz" if record else None, "results_root": "results_dp"})
        np.save(root / "mc" / "terminal" / f"{cid}.npy", res["W_T"][:1, :sub].astype(np.float32))
        if record:
            np.savez_compressed(root / "mc" / "paths" / f"{cid}.npz", days=res["rec_days"], Fq=res["rec_Fq"],
                                keys=np.array(["dp_policy|lam_star"]), quantiles=np.array(mc["fan_quantiles"]),
                                W_0=res["rec_W"][0], phi_0=res["rec_phi"][0], Wq_0=res["rec_Wq"][0],
                                phiq_0=res["rec_phiq"][0])
    timing = {"asset": asset, "state": state_row["state_name"], "scenario": scenario, "horizons": len(horizons),
              "n_paths": n_paths, "seconds": secs, "kind": "eval", "strategy": "dp_policy"}
    return rows, timing


def stage_evaluate(cfg, asset, scenarios=None, n_paths=None, jobs=2):
    S = Setup(cfg, asset)
    dc, mc = cfg["dp"], cfg["mc"]
    root = resolve(cfg, dc["results_dir"])
    for d in ("mc/terminal", "mc/paths"):
        (root / d).mkdir(parents=True, exist_ok=True)
    ev = dc["eval"]
    if scenarios is None:
        scenarios = ev["scenarios_us_mkt"] if asset == "us_mkt" else ev["scenarios_industries"]
    if n_paths is None:
        n_paths = mc["n_paths"] if asset == "us_mkt" else mc["n_paths_industries"]
    seed = cell_seed(mc["master_seed"], asset, "eval")
    H = ev["horizons"]
    cf = S.cache / f"eval_{asset}.parquet"
    done = pd.read_parquet(cf) if cf.exists() else pd.DataFrame()
    have = set(done.scenario.unique()) if len(done) else set()
    tasks = [(r, sc) for sc in scenarios if sc not in have for r in S.named.to_dict("records")]
    log(f"evaluate {asset}: {len(tasks)} runs ({len(have)} scenarios cached) at {n_paths} paths, eval seed {seed}")
    t0 = time.time()
    # one scenario at a time so a partial run is resumable
    for sc in [s for s in scenarios if s not in have]:
        ts = [(r, s) for r, s in tasks if s == sc]
        res = Parallel(n_jobs=jobs)(delayed(_eval_task)(cfg, asset, r, s, H, n_paths, seed, S.policy_path, str(root),
                                                        s == "base" or asset == "us_mkt") for r, s in ts)
        new = pd.DataFrame([x for r in res for x in r[0]])
        tim = pd.DataFrame([r[1] for r in res])
        done = pd.concat([done, new], ignore_index=True)
        done.to_parquet(cf, index=False)
        tp = S.cache / f"eval_timing_{asset}.parquet"
        tim = pd.concat([pd.read_parquet(tp), tim]) if tp.exists() else tim
        tim.to_parquet(tp, index=False)
        log(f"  {sc}: {time.time() - t0:.0f}s elapsed")
    write_results(cfg, root)
    return done


def write_results(cfg, root: Path):
    """Merge every asset's cached evaluation rows into results_dp/mc/{summary,worst_case,timings}.parquet."""
    pol = resolve(cfg, cfg["dp"]["policy_dir"]) / "cache"
    parts = [pd.read_parquet(p) for p in sorted(pol.glob("eval_*.parquet")) if not p.name.startswith("eval_timing")]
    if not parts:
        return
    summ = pd.concat(parts, ignore_index=True)
    summ.to_parquet(root / "mc" / "summary.parquet", index=False)
    keys = ["asset", "strategy", "tune_mode", "state", "T_days"]
    wc = summ[summ.scenario.isin(cfg["mc"]["worst_case_set"])]
    g = wc.groupby(keys)
    idx = g["breach"].idxmax()
    w = wc.loc[idx, keys + ["scenario", "breach", "breach_lo", "breach_hi", "ce", "ce_ann"]].rename(
        columns={"scenario": "worst_scenario", "breach": "breach_worst", "breach_lo": "breach_worst_lo",
                 "breach_hi": "breach_worst_hi", "ce": "ce_at_worst", "ce_ann": "ce_ann_at_worst"})
    w["n_scenarios"] = g.size().values
    w["scenarios"] = g["scenario"].agg(lambda s: ",".join(sorted(s))).values
    base = summ[summ.scenario == "base"][keys + ["breach", "ce", "ce_ann"]].rename(
        columns={"breach": "breach_base", "ce": "ce_base", "ce_ann": "ce_ann_base"})
    base.merge(w, on=keys, how="left").to_parquet(root / "mc" / "worst_case.parquet", index=False)
    tparts = [pd.read_parquet(p) for p in sorted(pol.glob("eval_timing_*.parquet"))]
    if tparts:
        pd.concat(tparts, ignore_index=True).to_parquet(root / "mc" / "timings.parquet", index=False)
    man = {"generated": dt.datetime.now().isoformat(timespec="seconds"), "git_branch": "dp",
           "master_seed": cfg["mc"]["master_seed"], "config_snapshot": cfg,
           "assets": sorted(summ.asset.unique().tolist()), "note": "SPEC 04 dp_policy rows only; merge with results/"}
    wjson(root / "run_manifest.json", man)


# --------------------------------------------------------------------------- comparison (V6 + scorecard)
def stage_compare(cfg, asset, modes=("state", "uniform"), jobs=2):
    """Scorecard vs OBPI-Merton and gap-aware CPPI from the SPEC 03 full run (paired: same evaluation seed and
    n_paths), and V6: under base, wherever DP's breach is <= the other strategy's, DP's CE should be >= it.
    Falls back to the quick run (unpaired, flagged) if the full run has not written its summary yet."""
    S = Setup(cfg, asset)
    root = resolve(cfg, cfg["dp"]["results_dir"])
    dp_ = pd.read_parquet(root / "mc" / "summary.parquet")
    dp_ = dp_[dp_.asset == asset]
    main = resolve(cfg, cfg["dp"]["compare_results_dir"])
    src, paired = main / "mc" / "summary.parquet", True
    if not src.exists():
        src, paired = main / "quick" / "mc" / "summary.parquet", False
    if not src.exists():
        log("compare: no SPEC 03 summary available yet")
        return None
    ref = pd.read_parquet(src)
    ref = ref[(ref.asset == asset) & ref.strategy.isin(["obpi_merton", "cppi_vol", "cppi"]) & ref.tune_mode.isin(modes)
              & (ref.state_source == "named")]
    keys = ["state", "T_days", "scenario"]
    wset = cfg["mc"]["worst_case_set"]
    rows = []
    for (st, T), g in dp_.groupby(["state", "T_days"]):
        d_b = g[g.scenario == "base"].iloc[0]
        d_w = g[g.scenario.isin(wset)]
        row = {"state": st, "T_days": int(T), "dp_lam": d_b.knob, "dp_breach_base": d_b.breach,
               "dp_breach_worst": d_w.breach.max(), "dp_worst_scen": d_w.loc[d_w.breach.idxmax(), "scenario"],
               "dp_ce_ann_base": d_b.ce_ann, "dp_ce_base": d_b.ce, "dp_ce_se": d_b.ce_se, "dp_phi_avg": d_b.phi_avg}
        for (s_, m_), r in ref[(ref.state == st) & (ref.T_days == T)].groupby(["strategy", "tune_mode"]):
            rb = r[r.scenario == "base"]
            if rb.empty:
                continue
            rb = rb.iloc[0]
            rw = r[r.scenario.isin(wset)]
            k = f"{s_}_{m_}"
            row.update({f"{k}_breach_base": rb.breach, f"{k}_breach_worst": rw.breach.max(),
                        f"{k}_ce_ann_base": rb.ce_ann, f"{k}_ce_base": rb.ce, f"{k}_ce_se": rb.ce_se,
                        f"{k}_knob": rb.knob})
        rows.append(row)
    sc = pd.DataFrame(rows)
    sc.to_csv(S.rep / f"scorecard_{asset}.csv", index=False)
    v6 = []
    n = int(dp_.n_paths.iloc[0])
    for r in rows:
        for k in ("obpi_merton_state", "cppi_vol_state", "obpi_merton_uniform", "cppi_vol_uniform"):
            if f"{k}_ce_base" not in r:
                continue
            se = math.sqrt(max(r[f"{k}_breach_base"] * (1 - r[f"{k}_breach_base"]), 1e-12) / n)
            comparable = r["dp_breach_base"] <= r[f"{k}_breach_base"] + (0 if paired else 2 * se)
            d_ce = (r["dp_ce_ann_base"] - r[f"{k}_ce_ann_base"]) * 1e4
            v6.append({"state": r["state"], "T_days": r["T_days"], "vs": k, "comparable": bool(comparable),
                       "dp_breach": r["dp_breach_base"], "other_breach": r[f"{k}_breach_base"],
                       "d_ce_bp_per_year": d_ce, "dp_ce_ge": bool(d_ce >= 0)})
    # Frontier version: DP is a one-parameter family; compare each comparator with the best-CE DP lam whose base
    # breach is <= the comparator's (same eval seed and n_paths as the lam* evaluation, all grid lam, base only).
    mc = cfg["mc"]
    n_ev = mc["n_paths"] if asset == "us_mkt" else mc["n_paths_industries"]
    fr = run_mc_grid(cfg, asset, ["base"], cfg["dp"]["eval"]["horizons"], n_ev,
                     cell_seed(mc["master_seed"], asset, "eval"), S.policy_path, S.lams, f"v6frontier_{asset}", jobs)
    fr = fr[fr.state_source == "named"]
    v6f = []
    for r in rows:
        g = fr[(fr.state == r["state"]) & (fr.T_days == r["T_days"])]
        for k in ("obpi_merton_state", "cppi_vol_state", "obpi_merton_uniform", "cppi_vol_uniform"):
            if f"{k}_ce_base" not in r:
                continue
            ob = r[f"{k}_breach_base"]
            se = math.sqrt(max(ob * (1 - ob), 1e-12) / n_ev)
            cand = g[g.breach <= ob + (0 if paired else 2 * se)]
            c = {"state": r["state"], "T_days": r["T_days"], "vs": k, "other_breach": ob,
                 "other_ce_ann": r[f"{k}_ce_ann_base"], "comparable": bool(len(cand))}
            if len(cand):
                b = cand.loc[cand.ce.idxmax()]
                d_ce = (b.ce_ann - r[f"{k}_ce_ann_base"]) * 1e4
                c.update({"dp_lam": float(b.knob), "dp_breach": float(b.breach), "d_ce_bp_per_year": d_ce,
                          "dp_ce_ge": bool(d_ce >= 0)})
            else:
                c.update({"dp_min_breach": float(g.breach.min())})
            v6f.append(c)
    out_p = S.rep / f"validation_{asset}.json"
    V = json.loads(out_p.read_text()) if out_p.exists() else {}
    compf = [c for c in v6f if c["comparable"]]
    V["V6_frontier"] = {"cells": v6f, "n": len(v6f), "n_comparable": len(compf),
                        "n_dp_ce_ge": sum(c["dp_ce_ge"] for c in compf),
                        "pass": all(c["dp_ce_ge"] for c in compf) and len(compf) > 0,
                        "failures": [c for c in compf if not c["dp_ce_ge"]]}
    log(f"V6 frontier: {V['V6_frontier']['n_dp_ce_ge']}/{len(compf)} comparable of {len(v6f)} with DP CE >= other; "
        f"pass={V['V6_frontier']['pass']}")
    comp = [c for c in v6 if c["comparable"]]
    V["V6"] = {"source": str(src), "paired_same_seed": paired, "cells": v6,
               "n_comparable": len(comp), "n_dp_ce_ge": sum(c["dp_ce_ge"] for c in comp),
               "pass": all(c["dp_ce_ge"] for c in comp) and len(comp) > 0,
               "failures": [c for c in comp if not c["dp_ce_ge"]]}
    wjson(out_p, V)
    log(f"V6 ({'paired' if paired else 'UNPAIRED quick run'}): {V['V6']['n_dp_ce_ge']}/{len(comp)} comparable cells "
        f"with DP CE >= other; pass={V['V6']['pass']}")
    return sc


# --------------------------------------------------------------------------- summary tables
def _md(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in df.itertuples(index=False):
        out.append("| " + " | ".join(str(v) for v in r) + " |")
    return "\n".join(out)


def stage_tables(cfg, asset):
    """Markdown tables for SUMMARY.md, generated from the stored outputs (no hand-copied numbers)."""
    S = Setup(cfg, asset)
    V = json.loads((S.rep / f"validation_{asset}.json").read_text())
    cal = json.loads((S.rep / f"calibration_{asset}.json").read_text())
    L = [f"## Tables for {asset} (generated by `scripts/05_dp.py tables`, {dt.datetime.now():%Y-%m-%d %H:%M})", ""]
    pct = lambda x: "" if x is None or (isinstance(x, float) and not math.isfinite(x)) else f"{100 * x:.2f}"
    # lam*
    t = pd.DataFrame([{"state": c["state"], "T": c["T_days"] // 252, "λ*": f"{c['lam_star']:g}", "rule": c["rule"],
                       "worst breach % at λ*": pct(c["worst_by_lam"][S.lams.index(c["lam_star"])]),
                       "binding scenario": c["binding_scenario_by_lam"][S.lams.index(c["lam_star"])],
                       "worst breach % by λ": " / ".join(pct(x) for x in c["worst_by_lam"])} for c in cal["cells"]])
    L += [f"### λ* (target {100 * cal['target']:.2f}% robust worst case over {', '.join(cal['robust_set'])}; "
          f"{cal['n_paths']} paths, tuning seed)", "", f"λ grid: {', '.join(f'{v:g}' for v in S.lams)}", "", _md(t), ""]
    # V1
    if "V1" in V:
        t = pd.DataFrame([{"σ": c["sigma"], "Merton φ": f"{c['merton']:.4f}", "DP φ values": c["phi_values"],
                           "max |dev|": f"{c['max_abs_dev']:.4f}"} for c in V["V1"]["cells"]])
        L += [f"### V1 Merton limit: pass = {V['V1']['pass']} (tolerance {V['V1']['tolerance']:.4f})", "", _md(t), ""]
    # CE convention: excess over the cash return (%/yr); cash on the same (tuning) seed / paths as the calibration MC
    cash_t = cash_ce(cfg, asset, "tune", cal["n_paths"], cal["lookup"]["T_days"])
    xc = lambda ce_level, st, T: 100 * ((ce_level / cfg["problem"]["W0"]) ** (252 / T) - 1 - cash_t[(st, T)])
    if "V2" in V:
        t = pd.DataFrame([{"state": c["state"], "T": c["T_days"] // 252,
                           "breach % by λ (base)": " / ".join(pct(x) for x in c["breach_by_lam"]),
                           "CE − cash %/yr λ=0 → λmax": f"{xc(c['ce_by_lam'][0], c['state'], c['T_days']):.2f} → "
                                                       f"{xc(c['ce_by_lam'][-1], c['state'], c['T_days']):.2f}",
                           "rises > 2SE": c["breach_rises_beyond_2se"], "max drop pp": f"{c['max_jump_pp']:.2f}",
                           "gap share @λmax": "" if c["gap_share_largest_lam"] is None else f"{c['gap_share_largest_lam']:.0%}",
                           "n breach @λmax": c["n_breach_largest_lam"]} for c in V["V2"]["cells"]])
        L += [f"### V2 limits in λ: pass = {V['V2']['pass']}", "",
              f"Mean φ* on y ∈ [1, 1.02] at λ = {S.lams[-1]:g}, by τ: {V['V2']['phi_near_y1_largest_lam']}", "", _md(t), ""]
    if "V3" in V:
        t = pd.DataFrame([{"λ": f"{r['lam']:g}", "J decreasing in y": r["J_decreasing_in_y"],
                           "φ falls with cushion (>1 step)": f"{r['phi_falls_with_cushion']} / {r['n_y_pairs']}",
                           "φ rises with h (>1 step)": f"{r['phi_rises_with_h']} / {r['n_h_pairs']}",
                           "φ range": r["phi_range"]} for r in V["V3"]["by_lam"]])
        L += ["### V3 shape (τ ∈ {1, 5, 21, 63, 126, 252, 378, 504})", "", _md(t), ""]
    if "V4" in V:
        t = pd.DataFrame([{"state": r["state"], "T": r["T_days"] // 252, "λ": f"{r['lam']:g}",
                           "CE − cash %/yr (120 → 239 y)":
                               f"{100 * (r['ce_ann_y1'] - cash_t[(r['state'], r['T_days'])]):.3f} → "
                               f"{100 * (r['ce_ann_y2'] - cash_t[(r['state'], r['T_days'])]):.3f}",
                           "ΔCE bp/yr": f"{r['d_ce_bp_per_year']:.2f}",
                           "breach % (120 → 239 y)": f"{pct(r['breach_y1'])} → {pct(r['breach_y2'])}",
                           "Δbreach pp": f"{r['d_breach_pp']:.3f}"} for r in V["V4"]["cells"]])
        L += [f"### V4 y-grid convergence: pass = {V['V4']['pass']}", "", _md(t), ""]
    if "V5" in V:
        c5 = pd.DataFrame(V["V5"]["cells"])
        t = c5.assign(**{"λ": c5.lam.map(lambda v: f"{v:g}"), "T": c5.T_days // 252,
                         "DP %": c5.breach_dp.map(pct), "MC rate const %": c5.mc_rate_const.map(pct),
                         "MC base %": c5.mc_base.map(pct), "ok (rate const)": c5.ok_rate_const,
                         "ok (base)": c5.ok_base})[["λ", "state", "T", "DP %", "MC rate const %", "MC base %",
                                                    "ok (rate const)", "ok (base)"]]
        L += [f"### V5 DP breach recursion vs MC: pass (rate const) = {V['V5']['pass_rate_const']} "
              f"({V['V5']['n_fail_rate_const']}/{V['V5']['n']} cells fail), pass (base) = {V['V5']['pass_base']} "
              f"({V['V5']['n_fail_base']}/{V['V5']['n']}); tolerance 2.58 SE + {100 * V['V5']['grid_tol']:.2f} pp", "",
              _md(t), ""]
    if "V6" in V:
        c6 = pd.DataFrame(V["V6"]["cells"])
        if len(c6):
            t = c6.assign(**{"T": c6.T_days // 252, "DP breach %": c6.dp_breach.map(pct),
                             "other breach %": c6.other_breach.map(pct),
                             "ΔCE bp/yr (DP − other)": c6.d_ce_bp_per_year.map(lambda v: f"{v:.1f}")})[
                ["state", "T", "vs", "comparable", "DP breach %", "other breach %", "ΔCE bp/yr (DP − other)"]]
            L += [f"### V6 optimality sanity (base): pass = {V['V6']['pass']}, "
                  f"{V['V6']['n_dp_ce_ge']}/{V['V6']['n_comparable']} comparable cells with DP CE ≥ other; "
                  f"paired = {V['V6']['paired_same_seed']}", "", _md(t), ""]
    if "V6_frontier" in V:
        f6 = pd.DataFrame(V["V6_frontier"]["cells"])
        if len(f6):
            for col in ("dp_lam", "dp_breach", "d_ce_bp_per_year", "dp_min_breach"):
                if col not in f6:
                    f6[col] = np.nan
            fmt = lambda v, f: "—" if pd.isna(v) else f(v)  # noqa: E731
            t = pd.DataFrame({"state": f6.state, "T": f6.T_days // 252, "vs": f6.vs,
                              "other breach %": f6.other_breach.map(pct),
                              "DP λ used": f6.dp_lam.map(lambda v: fmt(v, lambda x: f"{x:g}")),
                              "DP breach %": f6.dp_breach.map(lambda v: fmt(v, pct)),
                              "ΔCE bp/yr": f6.d_ce_bp_per_year.map(lambda v: fmt(v, lambda x: f"{x:.1f}")),
                              "DP min breach % (if none)": f6.dp_min_breach.map(lambda v: fmt(v, pct))})
            F = V["V6_frontier"]
            L += [f"### V6 frontier (base, eval seed, all grid λ): pass = {F['pass']}, {F['n_dp_ce_ge']}/"
                  f"{F['n_comparable']} comparable of {F['n']} with DP CE ≥ other", "",
                  "For each comparator, the DP λ with the highest base CE among those with base breach ≤ the "
                  "comparator's.", "", _md(t), ""]
    scp =S.rep / f"scorecard_{asset}.csv"
    if scp.exists():
        ref = pd.read_parquet(resolve(cfg, cfg["dp"]["compare_results_dir"]) / "mc" / "summary.parquet")
        ref = ref[(ref.asset == asset) & (ref.strategy == "cash") & (ref.scenario == "base")
                  & (ref.state_source == "named")]
        cash_e = {(r.state, int(r.T_days)): float(r.ce_ann) for r in ref.itertuples()}   # eval seed, paired
        sc = pd.read_csv(scp)
        sc = sc[sc.state.isin(["normal", "crisis"])]
        rows = []
        for r in sc.itertuples():
            for lab, k in (("DP (λ*)", "dp"), ("OBPI-Merton (state)", "obpi_merton_state"),
                           ("CPPI gap-aware (state)", "cppi_vol_state"), ("OBPI-Merton (uniform)", "obpi_merton_uniform"),
                           ("CPPI gap-aware (uniform)", "cppi_vol_uniform")):
                g = lambda f: getattr(r, f"{k}_{f}", float("nan"))
                rows.append({"state": r.state, "T": r.T_days // 252, "strategy": lab,
                             "base breach %": pct(g("breach_base")), "worst-case breach %": pct(g("breach_worst")),
                             "CE − cash %/yr (base)":
                                 f"{100 * (g('ce_ann_base') - cash_e[(r.state, int(r.T_days))]):.2f}"})
        L += ["### Scorecard (evaluation seed, us_mkt full-run paths)", "", _md(pd.DataFrame(rows)), ""]
    L += ["### Solve diagnostics", "", f"Solve seconds by λ: {V.get('solve_seconds')}", "",
          f"Quadrature: {V.get('quadrature')}", "",
          f"Share of quadrature transitions below y_min (all grid nodes, all τ): {V.get('below_ymin_share_quadrature')}", "",
          f"Max share of simulated path-days below y_min (calibration MC): {V.get('below_ymin_share_mc_max')}; "
          f"h above the grid: {V.get('h_above_grid_share_mc_max')}", ""]
    (S.rep / f"tables_{asset}.md").write_text("\n".join(L))
    log(f"tables -> {S.rep / f'tables_{asset}.md'}")


# --------------------------------------------------------------------------- figures
def stage_figures(cfg, asset):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    S = Setup(cfg, asset)
    cal = json.loads((S.rep / f"calibration_{asset}.json").read_text())
    c = [c for c in cal["cells"] if c["state"] == "normal" and c["T_days"] == 504][0]
    sol = load_sol(S, c["lam_star"])
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6), constrained_layout=True)
    sig = np.sqrt(S.h * 252)
    m = S.y <= 2.5
    for ax, t in zip(axes, (252, 21)):
        P = sol.policy(t)[m]
        im = ax.pcolormesh(sig, S.y[m], P, shading="nearest", cmap="viridis", vmin=0, vmax=1)
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.axhline(1.0, color="w", lw=0.8, ls="--")
        ax.set_xlabel("conditional vol σ (annualised)")
        ax.set_ylabel("y = W / F (robust floor)")
        ax.set_title(f"φ*(τ = {t}, y, σ), λ = {sol.lam:g}")
    fig.colorbar(im, ax=axes, label="equity weight φ*")
    fig.suptitle(f"{asset}: DP policy at λ* for the normal state, T = 2 (r0 = {S.model.r0_ann:.2%})")
    p = S.rep / f"policy_heatmap_{asset}.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    figure_lambda_curves(cfg, asset)


def figure_lambda_curves(cfg, asset):
    """Breach and CE (excess over the cash return, %/yr) vs lam: calibration MC, base, tuning seed."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    S = Setup(cfg, asset)
    cal = json.loads((S.rep / f"calibration_{asset}.json").read_text())
    cash_t = cash_ce(cfg, asset, "tune", cal["n_paths"], cal["lookup"]["T_days"])
    df = pd.read_parquet(S.cache / f"calib_{asset}.parquet")
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2), constrained_layout=True)
    for (st, T), g in df[df.scenario == "base"].groupby(["state", "T_days"]):
        g = g.sort_values("knob")
        x = np.arange(len(g))
        axes[0].plot(x, g.breach * 100, marker="o", label=f"{st} T={T // 252}")
        axes[1].plot(x, (g.ce_ann - cash_t[(st, int(T))]) * 100, marker="o", label=f"{st} T={T // 252}")
    for ax in axes:
        ax.set_xticks(range(len(S.lams)), [f"{v:g}" for v in S.lams], rotation=45)
        ax.set_xlabel("λ")
    axes[0].axhline(cfg["tune"]["target_breach"] * 100, color="r", ls="--")
    axes[0].set_ylabel("breach % (base, tuning seed)")
    axes[1].set_ylabel("CE − cash (%/yr, base, tuning seed)")
    axes[0].legend(fontsize=7, ncol=2)
    fig.savefig(S.rep / f"lambda_curves_{asset}.png", dpi=130)
    plt.close(fig)
    log(f"figures -> {S.rep}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["solve", "calibrate", "validate", "v4", "evaluate", "compare", "figures", "tables", "all"])
    ap.add_argument("--asset", default="us_mkt")
    ap.add_argument("--jobs", type=int, default=None)
    ap.add_argument("--scenarios", default=None, help="comma list (evaluate)")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    a = ap.parse_args()
    cfg = load_config(a.config)
    jobs = min(a.jobs or cfg["dp"]["n_jobs"], 2)
    sc = a.scenarios.split(",") if a.scenarios else None
    st = a.stage
    if st in ("solve", "all"):
        stage_solve(cfg, a.asset, jobs=jobs)
    if st in ("calibrate", "all"):
        stage_calibrate(cfg, a.asset, jobs)
    if st in ("validate", "all"):
        stage_validate(cfg, a.asset, jobs)
    if st in ("v4", "all"):
        stage_v4(cfg, a.asset, jobs)
    if st in ("evaluate", "all"):
        stage_evaluate(cfg, a.asset, sc, jobs=jobs)
    if st in ("compare", "all"):
        stage_compare(cfg, a.asset)
    if st in ("figures", "all"):
        stage_figures(cfg, a.asset)
    if st in ("tables", "all"):
        stage_tables(cfg, a.asset)


if __name__ == "__main__":
    main()
