#!/usr/bin/env python
"""Premium-robust recalibration of the DP policy (branch dp_mu, 2026-11-22).

Same 8 backward solves as SPEC 04 (results/policies/dp_us_mkt.npz, base estimates, base premium); only lam* is
re-chosen. Calibration rule = SPEC 04 Section 4 (scripts/05_dp.select_lam): 1.25% target on the worst case,
logistic crossing in log lam if noisy, next grid lam at or above; tuning seed, 20k paths, named states x
T in {1, 2}, over the robust set PLUS mu_low (premium x 0.5). The result is saved as a separate strategy
`dp_premrobust` (policy file dp_premrobust_us_mkt.npz = same phi tables, new lookup); dp_us_mkt.npz is untouched.

Industries (branch dp_ind): `solve --asset ind_telcm,ind_enrgy,ind_hlth` runs the 8 SPEC 04 solves per asset in one
pool (assets in the given order), then each asset gets its own dp_{asset}.npz. Industries use tune.n_paths_industries
(10k) for calibration and mc.n_paths_industries (20k) for evaluation, as in the SPEC 03 industry runs; seeds are
cell_seed(master, asset, 'tune'|'eval'), so the paths are paired with those runs. `compare` evaluates the SPEC 03
tuned cppi_vol / obpi_merton (state-dependent, robust) knobs under the scenarios the SPEC 03 industry plan did not
run, on the same eval seed (no retuning) -> results_ind_extra/.

Usage:
  python scripts/06_dp_premrobust.py solve     --asset ind_telcm,ind_enrgy,ind_hlth [--jobs 6]
  python scripts/06_dp_premrobust.py calibrate [--asset us_mkt] [--jobs 6]
  python scripts/06_dp_premrobust.py evaluate  [--jobs 6] [--scenarios base,mu_zero]
  python scripts/06_dp_premrobust.py report
"""
from __future__ import annotations

import os

for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

import argparse  # noqa: E402
import datetime as dt  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from joblib import Parallel, delayed  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_spec = importlib.util.spec_from_file_location("dp05", ROOT / "scripts" / "05_dp.py")
dp05 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dp05)

from gf.data import load_config, resolve  # noqa: E402
from gf.metrics import add_ce_xcash  # noqa: E402
from gf.scenarios import cell_seed  # noqa: E402

STRAT = "dp_premrobust"
log = dp05.log


def paths(cfg, asset):
    pc = cfg["dp_premrobust"]
    pol = resolve(cfg, cfg["dp"]["policy_dir"])
    cache = pol / "cache_premrobust"
    cache.mkdir(parents=True, exist_ok=True)
    return {"src": pol / f"dp_{asset}.npz", "dst": pol / f"{STRAT}_{asset}.npz", "cache": cache,
            "rep": resolve(cfg, pc["report_dir"]), "res": resolve(cfg, pc["results_dir"])}


def policy_lams(path) -> list[float]:
    with np.load(path) as z:
        return [float(v) for v in z["lam"]]


def write_policy(src: Path, dst: Path, lookup: dict, note: dict):
    """Copy every array of the base policy file (phi tables etc.) and replace only the lam* lookup."""
    with np.load(src, allow_pickle=False) as z:
        arrs = {k: z[k] for k in z.files}
    arrs["lam_star_sigma"] = np.asarray(lookup["sigma"], float)
    arrs["lam_star_T"] = np.asarray(lookup["T_days"], int)
    arrs["lam_star_idx"] = np.asarray(lookup["idx"], int)
    arrs["premrobust_json"] = np.array(json.dumps(note, default=str))
    tmp = dst.with_name(dst.stem + ".tmp.npz")
    np.savez_compressed(tmp, **arrs)
    os.replace(tmp, dst)


# --------------------------------------------------------------------------- calibration
def stage_calibrate(cfg, asset, jobs):
    P = paths(cfg, asset)
    S = dp05.Setup(cfg, asset)
    dc, tc, pc = cfg["dp"], cfg["tune"], cfg["dp_premrobust"]
    lams = policy_lams(P["src"])
    n = dc["calib"]["n_paths"] if asset == "us_mkt" else dc["calib"]["n_paths_industries"]
    seed = cell_seed(cfg["mc"]["master_seed"], asset, "tune")
    H = dc["calib"]["horizons"]
    scen = list(pc["calib_set"])
    target = tc["target_breach"]
    log(f"calibrate {STRAT}: set {scen}, {n} paths, tuning seed {seed}, lam grid {lams}")
    cf = P["cache"] / f"calib_{asset}.parquet"
    done = pd.read_parquet(cf) if cf.exists() else pd.DataFrame()
    # a cached scenario is reused only if it covers the current lam grid
    have = {sc for sc in (done.scenario.unique() if len(done) else [])
            if set(np.round(lams, 12)) <= set(np.round(done[done.scenario == sc].knob.unique(), 12))}
    todo = [sc for sc in scen if sc not in have]
    if todo:
        tasks = [(r, sc) for sc in todo for r in S.named.to_dict("records")]
        log(f"  {len(tasks)} runs x {len(H)} horizons x {len(lams)} lam rows")
        t0 = time.time()
        res = Parallel(n_jobs=jobs)(delayed(dp05._calib_task)(cfg, asset, r, sc, H, n, seed, P["src"], lams)
                                    for r, sc in tasks)
        new = pd.DataFrame([x for r in res for x in r[0]])
        done = pd.concat([done[~done.scenario.isin(todo)] if len(done) else done, new], ignore_index=True)
        done.to_parquet(cf, index=False)
        log(f"  done in {time.time() - t0:.0f}s wall")
    df = done[done.scenario.isin(scen) & done.knob.isin(lams)]
    cells, idx = [], np.zeros((len(H), len(S.named)), int)
    for j, r in enumerate(S.named.itertuples()):
        for i, T in enumerate(H):
            sub = df[(df.state == r.state_name) & (df.T_days == T)]
            sel = dp05.select_lam(lams, sub, target, n)
            # "not achievable on grid": even the largest lam misses the target on the worst case
            achievable = sel["rule"] != "infeasible_largest_lam"
            idx[i, j] = sel["idx"]
            cells.append({"state": r.state_name, "sigma0": r.sigma0_ann, "T_days": T, "lam_star": lams[sel["idx"]],
                          "achievable_on_grid": achievable, **sel})
    lookup = {"sigma": S.named.sigma0_ann.tolist(), "T_days": H, "idx": idx.tolist()}
    note = {"strategy": STRAT, "asset": asset, "calib_set": scen, "target": target, "n_paths": n, "seed": seed, "lam_grid": lams,
            "generated": dt.datetime.now().isoformat(timespec="seconds")}
    dp05.wjson(P["rep"] / f"calibration_{STRAT}_{asset}.json", {**note, "cells": cells, "lookup": lookup})
    write_policy(P["src"], P["dst"], lookup, note)
    for c in cells:
        log(f"  {asset} {c['state']:9s} T={c['T_days']}: lam*={c['lam_star']:.3g} ({c['rule']}), worst by lam "
            f"{[round(w, 4) for w in c['worst_by_lam']]}, binding {c['binding_scenario_by_lam'][c['idx']]}"
            + ("" if c["achievable_on_grid"] else "  NOT ACHIEVABLE ON GRID"))
    return cells


# --------------------------------------------------------------------------- evaluation
def _eval_task(cfg, asset, state_row, scenario, horizons, n_paths, seed, policy_path, root: str):
    root = Path(root)
    rows, extras, secs, mk = dp05.mc_task(cfg, asset, state_row, scenario, horizons, n_paths, seed, policy_path,
                                          None, record=True)
    mc = cfg["mc"]
    sub = min(mc["terminal_subsample"], n_paths)
    for r in rows:
        T = r["T_days"]
        res = extras[T]
        cid = f"{STRAT}__" + dp05.cell_id(asset, scenario, state_row["state_name"], T)
        r.update({"strategy": STRAT, "cell_id": cid, "terminal_file": f"mc/terminal/{cid}.npy", "terminal_row": 0,
                  "paths_file": f"mc/paths/{cid}.npz", "results_root": cfg["dp_premrobust"]["results_dir"]})
        np.save(root / "mc" / "terminal" / f"{cid}.npy", res["W_T"][:1, :sub].astype(np.float32))
        np.savez_compressed(root / "mc" / "paths" / f"{cid}.npz", days=res["rec_days"], Fq=res["rec_Fq"],
                            keys=np.array([f"{STRAT}|lam_star"]), quantiles=np.array(mc["fan_quantiles"]),
                            W_0=res["rec_W"][0], phi_0=res["rec_phi"][0], Wq_0=res["rec_Wq"][0],
                            phiq_0=res["rec_phiq"][0])
    timing = {"asset": asset, "state": state_row["state_name"], "scenario": scenario, "horizons": len(horizons),
              "n_paths": n_paths, "seconds": secs, "kind": "eval", "strategy": STRAT}
    return rows, timing


def stage_evaluate(cfg, asset, jobs, scenarios=None):
    P = paths(cfg, asset)
    if not P["dst"].exists():
        raise SystemExit("run `calibrate` first")
    S = dp05.Setup(cfg, asset)
    pc, mc = cfg["dp_premrobust"], cfg["mc"]
    root = P["res"]
    for d in ("mc/terminal", "mc/paths"):
        (root / d).mkdir(parents=True, exist_ok=True)
    scenarios = scenarios or pc["eval_scenarios"]
    n = mc["n_paths"] if asset == "us_mkt" else mc["n_paths_industries"]
    seed = cell_seed(mc["master_seed"], asset, "eval")
    H = cfg["dp"]["eval"]["horizons"]
    cf = P["cache"] / f"eval_{asset}.parquet"
    tp = P["cache"] / f"eval_timing_{asset}.parquet"
    done = pd.read_parquet(cf) if cf.exists() else pd.DataFrame()
    have = set(done.scenario.unique()) if len(done) else set()
    todo = [s for s in scenarios if s not in have]
    log(f"evaluate {STRAT} {asset}: {len(todo)} scenarios to run ({sorted(have)} cached), {n} paths, eval seed {seed}")
    t0 = time.time()
    for sc in todo:          # one scenario at a time: resumable
        res = Parallel(n_jobs=jobs)(delayed(_eval_task)(cfg, asset, r, sc, H, n, seed, str(P["dst"]), str(root))
                                    for r in S.named.to_dict("records"))
        new = pd.DataFrame([x for r in res for x in r[0]])
        done = pd.concat([done, new], ignore_index=True)
        done.to_parquet(cf, index=False)
        tim = pd.DataFrame([r[1] for r in res])
        tim = pd.concat([pd.read_parquet(tp), tim]) if tp.exists() else tim
        tim.to_parquet(tp, index=False)
        log(f"  {sc}: {time.time() - t0:.0f}s elapsed")
    write_results(cfg)
    return done


def write_results(cfg):
    """results_dp_mu/mc/{summary,worst_case,timings}.parquet, same schema as results_dp/; merges every asset's
    cached evaluation rows."""
    P = paths(cfg, "us_mkt")
    root = P["res"]
    ev = sorted(p for p in P["cache"].glob("eval_*.parquet") if not p.name.startswith("eval_timing"))
    summ = pd.concat([pd.read_parquet(p) for p in ev], ignore_index=True)
    tpl = sorted(P["cache"].glob("eval_timing_*.parquet"))
    summ.to_parquet(root / "mc" / "summary.parquet", index=False)
    keys = ["asset", "strategy", "tune_mode", "state", "T_days"]
    wc = summ[summ.scenario.isin(cfg["mc"]["worst_case_set"])]
    if len(wc):
        g = wc.groupby(keys)
        w = wc.loc[g["breach"].idxmax(), keys + ["scenario", "breach", "breach_lo", "breach_hi", "ce", "ce_ann"]].rename(
            columns={"scenario": "worst_scenario", "breach": "breach_worst", "breach_lo": "breach_worst_lo",
                     "breach_hi": "breach_worst_hi", "ce": "ce_at_worst", "ce_ann": "ce_ann_at_worst"})
        w["n_scenarios"] = g.size().values
        w["scenarios"] = g["scenario"].agg(lambda s: ",".join(sorted(s))).values
        base = summ[summ.scenario == "base"][keys + ["breach", "ce", "ce_ann"]].rename(
            columns={"breach": "breach_base", "ce": "ce_base", "ce_ann": "ce_ann_base"})
        base.merge(w, on=keys, how="left").to_parquet(root / "mc" / "worst_case.parquet", index=False)
    if tpl:
        pd.concat([pd.read_parquet(p) for p in tpl], ignore_index=True).to_parquet(root / "mc" / "timings.parquet",
                                                                                    index=False)
    dp05.wjson(root / "run_manifest.json", {
        "generated": dt.datetime.now().isoformat(timespec="seconds"), "git_branch": "dp_ind",
        "master_seed": cfg["mc"]["master_seed"], "config_snapshot": cfg,
        "assets": sorted(summ.asset.unique().tolist()),
        "note": f"{STRAT} rows only (premium-robust lam*); merge with results/ and results_dp/"})


# --------------------------------------------------------------------------- solves (industries)
def stage_solve(cfg, assets: list[str], jobs):
    """SPEC 04 solves (unchanged method and lam grid) for several assets in one worker pool, in asset order; then
    each asset's dp_{asset}.npz (no lookup) via 05_dp.stage_solve, which finds every solve in the cache."""
    lams = [float(v) for v in cfg["dp"]["lam_grid"]]
    todo = []
    for a in assets:
        S = dp05.Setup(cfg, a)
        todo += [(a, l) for l in lams if not dp05._cache_ok(S, S.sol_path(l))]
    log(f"solve {assets}: {len(todo)} solves to do, {jobs} workers")
    t0 = time.time()
    Parallel(n_jobs=jobs)(delayed(_solve_logged)(cfg, a, l) for a, l in todo)
    log(f"  all solves done in {time.time() - t0:.0f}s wall")
    for a in assets:
        dp05.stage_solve(cfg, a, jobs=1)


def _solve_logged(cfg, asset, lam):
    t0 = time.time()
    p = dp05._solve_one(cfg, asset, lam, 1)
    print(f"[{dt.datetime.now():%H:%M:%S}]   solved {asset} lam={lam:.3g} in {time.time() - t0:.0f}s", flush=True)
    return p


# --------------------------------------------------------------------------- comparators (industries)
def stage_compare(cfg, asset, jobs, scenarios=None):
    """SPEC 03 tuned knobs (results/tuned/{asset}_robust.json, unchanged) for cppi / cppi_vol / obpi_merton, evaluated
    with 03_run_mc.eval_task on the SPEC 03 eval seed and n_paths_industries under the scenarios the SPEC 03 industry
    plan did not run (+ base, as a pairing check against results/mc). Output: results_ind_extra/ (same schema)."""
    spec = importlib.util.spec_from_file_location("mc03", ROOT / "scripts" / "03_run_mc.py")
    mc03 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mc03)
    pc, mc = cfg["dp_premrobust"], cfg["mc"]
    root = resolve(cfg, pc["compare_results_dir"])
    for d in ("mc/terminal", "mc/paths"):
        (root / d).mkdir(parents=True, exist_ok=True)
    tuned = json.loads((resolve(cfg, cfg["tune"]["results_dir"]) / f"{asset}_robust.json").read_text())
    n = mc["n_paths_industries"]
    seed = cell_seed(mc["master_seed"], asset, "eval")
    H = cfg["dp"]["eval"]["horizons"]
    S = dp05.Setup(cfg, asset)
    cf = paths(cfg, asset)["cache"] / f"compare_{asset}.parquet"
    done = pd.read_parquet(cf) if cf.exists() else pd.DataFrame()
    have = set(done.scenario.unique()) if len(done) else set()
    scenarios = [s for s in (scenarios or pc["compare_scenarios"]) if s not in have]
    log(f"compare {asset}: {scenarios}, {n} paths, eval seed {seed}")
    for sc in scenarios:
        res = Parallel(n_jobs=jobs)(delayed(mc03.eval_task)(cfg, asset, r, sc, H, n, seed, tuned, root, False, False)
                                    for r in S.named.to_dict("records"))
        new = pd.DataFrame([x for r in res for x in r[0]]).assign(results_root=pc["compare_results_dir"])
        done = pd.concat([done, new], ignore_index=True)
        done.to_parquet(cf, index=False)
        log(f"  {asset} {sc} done")
    parts = [pd.read_parquet(p) for p in sorted(cf.parent.glob("compare_*.parquet"))]
    pd.concat(parts, ignore_index=True).to_parquet(root / "mc" / "summary.parquet", index=False)
    return done


# --------------------------------------------------------------------------- report
COMP = ["dp_premrobust", "dp_policy", "cppi_vol", "obpi_merton"]


def stage_report(cfg, asset):
    """Scorecard per asset: dp_premrobust vs (us_mkt: dp_policy) vs cppi_vol vs obpi_merton (state, robust).
    CE is quoted as excess over the cash return, %/yr (ce_ann_excess)."""
    P = paths(cfg, asset)
    parts = [pd.read_parquet(P["res"] / "mc" / "summary.parquet")]
    for rel in (cfg["dp"]["results_dir"], "results", pc_dir(cfg)):
        p = resolve(cfg, rel) / "mc" / "summary.parquet"
        if p.exists():
            parts.append(pd.read_parquet(p))
    allr = pd.concat(parts, ignore_index=True)
    allr = allr[allr.asset == asset]
    # CE convention: excess over the cash return (%/yr), cash strategy in the same cell (paired seeds). Cash rows come
    # from results/mc (base, dgp_garch_t, fhs, jump) and results_ind_extra (the scenarios SPEC 03 did not run).
    cash = allr[allr.strategy == "cash"].drop_duplicates(["asset", "state", "T_days", "scenario", "seed"])
    df = add_ce_xcash(allr[allr.strategy.isin(COMP)], cash)
    df = df[(df.asset == asset) & df.strategy.isin(COMP) & df.state.isin(["calm", "normal", "elevated", "crisis", "extreme"])
            & df.T_days.isin([252, 504]) & (~df.strategy.isin(["cppi_vol", "obpi_merton"]) | (df.tune_mode == "state"))]
    # comparator rows may come from results/mc and results_ind_extra (base in both: keep the SPEC 03 row)
    df = df.drop_duplicates(["strategy", "state", "T_days", "scenario"], keep="first")
    rob = cfg["mc"]["worst_case_set"]
    out = []
    for (s, st, T), x in df.groupby(["strategy", "state", "T_days"]):
        x = x.set_index("scenario")
        if "base" not in x.index:
            continue
        r = x[x.index.isin(rob)]
        full = len(r) == len(rob)
        out.append({"asset": asset, "strategy": s, "state": st, "T": T // 252, "breach_base": x.at["base", "breach"],
                    "breach_worst": r.breach.max() if full else np.nan,
                    "worst_scen": r.breach.idxmax() if full else None,
                    "breach_mu_low": x.breach.get("mu_low", np.nan), "breach_mu_zero": x.breach.get("mu_zero", np.nan),
                    "breach_fat_tails": x.breach.get("fat_tails", np.nan), "breach_jump": x.breach.get("jump", np.nan),
                    "ce_xcash_base": x.at["base", "ce_xcash"],
                    "lam": x.at["base", "knob"] if s.startswith("dp") else np.nan})
    t = pd.DataFrame(out)
    t.to_csv(P["rep"] / f"scorecard_{STRAT}_{asset}.csv", index=False)
    with pd.option_context("display.width", 250, "display.max_columns", 30, "display.max_rows", 200):
        print(t.sort_values(["T", "state", "strategy"]).to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    return t


def pc_dir(cfg):
    return cfg["dp_premrobust"].get("compare_results_dir", "results_ind_extra")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["solve", "calibrate", "evaluate", "compare", "report"])
    ap.add_argument("--asset", default="us_mkt", help="asset or comma list")
    ap.add_argument("--jobs", type=int, default=None)
    ap.add_argument("--scenarios", default=None)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    a = ap.parse_args()
    cfg = load_config(a.config)
    jobs = a.jobs or cfg["dp_premrobust"]["n_jobs"]
    assets = a.asset.split(",")
    sc = a.scenarios.split(",") if a.scenarios else None
    if a.stage == "solve":
        stage_solve(cfg, assets, jobs)
        return
    for asset in assets:
        if a.stage == "calibrate":
            stage_calibrate(cfg, asset, jobs)
        elif a.stage == "evaluate":
            stage_evaluate(cfg, asset, jobs, sc)
        elif a.stage == "compare":
            stage_compare(cfg, asset, jobs, sc)
        else:
            stage_report(cfg, asset)


if __name__ == "__main__":
    main()
