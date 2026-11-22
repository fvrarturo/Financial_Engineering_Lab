#!/usr/bin/env python
"""Stage 3: tune strategy knobs and run the Monte Carlo grid (SPEC 03 §3-4 + addendum).

Usage:
  python scripts/03_run_mc.py --asset us_mkt [--quick] [--jobs N]
  python scripts/03_run_mc.py --asset industries      # the 10 ind_* assets (reduced plan)
  python scripts/03_run_mc.py --asset all

Plan (config mc.plan): us_mkt gets every state x horizon for `base`, every state x T in {1,2} for
`dgp_garch_t`, and named states x T in {1,2} for the other scenarios; industries get named states x
T in {1,2} x {base, dgp_garch_t, fhs, jump}. Knobs are tuned on the base model with the tuning seed
(tune.n_paths paths) and evaluated on an independent seed (mc.n_paths paths).

Outputs (under results/ or results/quick/ with --quick):
  tuned/{asset}_base.json, mc/summary.parquet, mc/frontier.parquet, mc/worst_case.parquet,
  mc/terminal/{cell}.npy, mc/paths/{cell}.npz, mc/timings.parquet, run_manifest.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from joblib import Parallel, delayed

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gf.data import load_config, resolve  # noqa: E402
from gf.engine import SimMarket, run_engine  # noqa: E402
from gf.metrics import row_metrics  # noqa: E402
from gf.scenarios import cell_seed, load_context  # noqa: E402
from gf.strategies import make_strategy  # noqa: E402
from gf.tune import KnobFunction, aggregate, candidates, select, tune_state  # noqa: E402

KNOBBED = ["cppi", "cppi_vol", "obpi_merton"]
NAMED = ["calm", "normal", "elevated", "crisis", "extreme"]


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def out_root(cfg, quick: bool) -> Path:
    return resolve(cfg, "results/quick") if quick else resolve(cfg, "results")


def jsonable(o):
    if isinstance(o, dict):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, float) and not math.isfinite(o):
        return None
    return o


def cell_id(asset, scenario, state, T):
    return f"{asset}__{scenario}__{state}__T{T}".replace("@", "_at_").replace("/", "_")


# --------------------------------------------------------------------------- resumable task cache
def code_fingerprint() -> str:
    """Hash of the source that determines results (src/gf + this script)."""
    h = hashlib.sha256()
    for f in sorted((ROOT / "src" / "gf").glob("*.py")) + [Path(__file__)]:
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


def task_key(*parts) -> str:
    return hashlib.sha256(json.dumps(jsonable(list(parts)), sort_keys=True, default=str).encode()).hexdigest()[:24]


def cached(root: Path, kind: str, key: str, fn, *args):
    """Run fn(*args) unless root/cache/{kind}/{key}.pkl exists (resume after an interruption).
    Written atomically after the task (and its own output files) completed."""
    d = root / "cache" / kind
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{key}.pkl"
    if f.exists():
        try:
            with open(f, "rb") as fh:
                return pickle.load(fh)
        except Exception:
            pass
    out = fn(*args)
    tmp = f.with_suffix(f".tmp{os.getpid()}")
    with open(tmp, "wb") as fh:
        pickle.dump(out, fh)
    os.replace(tmp, f)
    return out


# --------------------------------------------------------------------------- tuning
def tune_task(cfg, asset, state_row, horizons, n_paths, seed):
    t0 = time.time()
    ctx = load_context(cfg, asset)
    s0 = {"sigma0_ann": float(state_row["sigma0_ann"]), "r0_ann": float(state_row["r0_ann"])}
    res = tune_state(ctx, s0, horizons, KNOBBED, cfg, n_paths, seed)
    return {"state": state_row["state_name"], "sigma0": s0["sigma0_ann"], "r0": s0["r0_ann"], "result": res,
            "seconds": time.time() - t0, "n_paths": n_paths, "horizons": horizons}


def knob_rows(tuned, s, sigma0, T, with_uniform_all=True):
    """Rows evaluated per knobbed strategy: robust-tuned uniform / state (primary), the base-only tuned state
    function as a comparison ('state_base'), uniform incl. extreme in base named cells, OBPI untuned."""
    a = tuned["strategies"][s]
    fn = KnobFunction.from_dict(a["state_fn"])
    vals = [a["uniform"]["value"], fn(sigma0, T)]
    modes = ["uniform", "state"]
    if "base_strategies" in tuned:
        vals.append(KnobFunction.from_dict(tuned["base_strategies"][s]["state_fn"])(sigma0, T))
        modes.append("state_base")
    if with_uniform_all:            # reported (addendum 6) in base named-state cells only (compute budget)
        vals.insert(1, a["uniform_all"]["value"])
        modes.insert(1, "uniform_all")
    if s == "obpi_merton":
        vals.append(1.0)
        modes.append("untuned")
    return np.array(vals), modes


# --------------------------------------------------------------------------- evaluation
def eval_task(cfg, asset, state_row, scenario, horizons, n_paths, seed, tuned, root, record, frontier):
    t0 = time.time()
    ctx = load_context(cfg, asset)
    P, mc = cfg["problem"], cfg["mc"]
    st = state_row["state_name"]
    s0 = {"sigma0_ann": float(state_row["sigma0_ann"]), "r0_ann": float(state_row["r0_ann"])}
    dgp, rate, sinfo = ctx.dgp(scenario, mc["scenarios"][scenario], n_paths)
    info = ctx.model_info(s0["sigma0_ann"], P["gamma"], cfg["strategies"]["floor"])
    vol_in = cfg["strategies"]["obpi_vol_input"]
    strats, meta = [], []
    for T in horizons:
        for s in ["cash", "merton_const", "merton_vol"]:
            strats.append(make_strategy(s, T))
            meta.append((T, s, ["none"], None))
        for s in KNOBBED:
            vals, modes = knob_rows(tuned, s, s0["sigma0_ann"], T,
                                    scenario == "base" and state_row["source"] == "named")
            extra = {"vol_input": vol_in} if s == "obpi_merton" else {}
            strats.append(make_strategy(s, T, vals, labels=modes, cfg=cfg, **extra))
            meta.append((T, s, modes, cfg["strategies"]["knobs"][s]["name"]))
    rec = {"n_paths": mc["sample_paths"], "stride": mc["path_stride"], "fan_n": mc["fan_subsample"],
           "quantiles": mc["fan_quantiles"]} if record else None
    mk = SimMarket(dgp, ctx.investor(), rate, s0, n_paths, seed)
    t_sim0 = time.time()
    out = run_engine(mk, strats, P["W0"], P["K"], P["gamma"], s0, info, cfg["engine"], record=rec)
    t_engine = time.time() - t_sim0
    rows = []
    term_dir, path_dir = root / "mc" / "terminal", root / "mc" / "paths"
    by_T = {}
    for (T, s, modes, kname), res in zip(meta, out):
        by_T.setdefault(T, []).append((s, modes, kname, res))
    sub = min(mc["terminal_subsample"], n_paths)
    for T, items in by_T.items():
        cid = cell_id(asset, scenario, st, T)
        term, prow = [], {}
        for s, modes, kname, res in items:
            for i, mode in enumerate(modes):
                m = row_metrics(res, i, P["K"], P["W0"], s0["r0_ann"], P["gamma"], mc,
                                boot_seed=seed % (2 ** 31) + i)
                kv = res["knob_values"][i]
                rows.append({"asset": asset, "strategy": s, "tune_mode": mode, "knob_name": kname,
                             "knob": float(kv) if kname else np.nan, "state": st,
                             "state_source": state_row["source"], "sigma0": s0["sigma0_ann"], "r0": s0["r0_ann"],
                             "T": T / ctx.N, "T_days": T, "scenario": scenario, "cell_id": cid, "seed": seed,
                             "terminal_file": f"mc/terminal/{cid}.npy", "terminal_row": len(term),
                             "paths_file": f"mc/paths/{cid}.npz" if record else None,
                             "n_sim_return_clips": mk.n_clip, "n_jumps": mk.n_jumps,
                             "sigma_capped_share": mk.n_cap / (n_paths * max(horizons)), **m})
                term.append(res["W_T"][i][:sub].astype(np.float32))
                if record:
                    key = f"{s}|{mode}"
                    prow[key] = {"W": res["rec_W"][i], "phi": res["rec_phi"][i], "Wq": res["rec_Wq"][i],
                                 "phiq": res["rec_phiq"][i]}
                    prow["_F"] = res["rec_Fq"]
                    prow["_days"] = res["rec_days"]
        np.save(term_dir / f"{cid}.npy", np.stack(term))
        if record:
            arrs = {"days": prow.pop("_days"), "Fq": prow.pop("_F"), "keys": np.array(list(prow.keys())),
                    "quantiles": np.array(mc["fan_quantiles"])}
            for j, (k, v) in enumerate(prow.items()):
                for a, arr in v.items():
                    arrs[f"{a}_{j}"] = arr
            np.savez_compressed(path_dir / f"{cid}.npz", **arrs)
    fr = []
    if frontier:
        nf = min(n_paths, cfg["mc"].get("frontier_n_paths", n_paths))
        fr = frontier_rows(cfg, ctx, asset, state_row, s0, horizons, nf, seed, dgp, rate, info)
    secs = time.time() - t0
    n_rows = sum(len(m[2]) for m in meta)
    timing = {"asset": asset, "state": st, "scenario": scenario, "horizons": len(horizons),
              "sum_T_days": int(sum(horizons)), "n_paths": n_paths, "strategy_rows": n_rows, "seconds": secs,
              "engine_seconds": t_engine, "frontier": frontier, "record": record}
    return rows, fr, timing


def frontier_rows(cfg, ctx, asset, state_row, s0, horizons, n_paths, seed, dgp, rate, info):
    P, mc, kc = cfg["problem"], cfg["mc"], cfg["strategies"]["knobs"]
    G = mc["frontier_points"]
    strats, meta = [], []
    for T in horizons:
        for s in KNOBBED:
            c = candidates(kc[s], kc[s]["lo"], kc[s]["hi"], G)
            variants = [cfg["strategies"]["obpi_vol_input"]] if s != "obpi_merton" else ["conditional", "sigma0_fixed"]
            for v in variants:
                extra = {"vol_input": v} if s == "obpi_merton" else {}
                strats.append(make_strategy(s, T, c, cfg=cfg, **extra))
                meta.append((T, s, v if s == "obpi_merton" else "", c))
    mk = SimMarket(dgp, ctx.investor(), rate, s0, n_paths, seed)
    out = run_engine(mk, strats, P["W0"], P["K"], P["gamma"], s0, info, cfg["engine"])
    rows = []
    for (T, s, v, c), res in zip(meta, out):
        for i, kv in enumerate(c):
            W = res["W_T"][i]
            br = (W < P["K"]).mean()
            ce = 1.0 / np.mean(1.0 / W) if P["gamma"] == 2 else np.mean(W ** (1 - P["gamma"])) ** (1 / (1 - P["gamma"]))
            se = math.sqrt(br * (1 - br) / n_paths)
            rows.append({"asset": asset, "state": state_row["state_name"], "sigma0": s0["sigma0_ann"],
                         "r0": s0["r0_ann"], "T": T / ctx.N, "T_days": T, "scenario": "base", "strategy": s,
                         "variant": v, "knob_name": kc[s]["name"], "knob": float(kv), "breach": br,
                         "breach_lo": max(br - 1.96 * se, 0), "breach_hi": br + 1.96 * se, "ce": ce,
                         "ce_ann": (ce / P["W0"]) ** (ctx.N / T) - 1, "phi_avg": float(res["phi_mean_path"][i].mean())})
    return rows


# --------------------------------------------------------------------------- plan
def plan_tasks(cfg, asset, ctx, quick):
    mc = cfg["mc"]
    H = cfg["problem"]["horizons_days"]
    tasks = []
    if asset == "us_mkt":
        p = mc["plan"]["us_mkt"]
        for scen, spec in [("base", p["base"]), ("dgp_garch_t", p["dgp_garch_t"])]:
            hz = H if spec["horizons"] == "all" else spec["horizons"]
            for r in ctx.state_rows(spec["states"]).to_dict("records"):
                tasks.append((scen, r, hz, r["source"] == "named",
                              bool(spec.get("frontier")) and r["source"] == "named"))
        for scen in p["other"]["scenarios"]:
            for r in ctx.state_rows(p["other"]["states"]).to_dict("records"):
                tasks.append((scen, r, p["other"]["horizons"], True, False))
    else:
        p = mc["plan"]["industries"]
        for scen in p["scenarios"]:
            for r in ctx.state_rows(p["states"]).to_dict("records"):
                tasks.append((scen, r, p["horizons"], scen == "base", False))
    return tasks


def worst_case(summary: pd.DataFrame, cfg) -> pd.DataFrame:
    keys = ["asset", "strategy", "tune_mode", "state", "T_days"]
    wc = summary[summary.scenario.isin(cfg["mc"]["worst_case_set"])]
    g = wc.groupby(keys)
    idx = g["breach"].idxmax()
    w = wc.loc[idx, keys + ["scenario", "breach", "breach_lo", "breach_hi", "ce", "ce_ann"]].rename(
        columns={"scenario": "worst_scenario", "breach": "breach_worst", "breach_lo": "breach_worst_lo",
                 "breach_hi": "breach_worst_hi", "ce": "ce_at_worst", "ce_ann": "ce_ann_at_worst"})
    w["n_scenarios"] = g.size().values
    w["scenarios"] = g["scenario"].agg(lambda s: ",".join(sorted(s))).values
    base = summary[summary.scenario == "base"][keys + ["breach", "ce", "ce_ann"]].rename(
        columns={"breach": "breach_base", "ce": "ce_base", "ce_ann": "ce_ann_base"})
    return base.merge(w, on=keys, how="left")


# --------------------------------------------------------------------------- main
def run_assets(cfg, assets, quick, jobs, root):
    """Tune all assets in one process pool, then evaluate all (state, scenario) tasks in one pool."""
    mc, tc = cfg["mc"], cfg["tune"]
    industries_only = all(a != "us_mkt" for a in assets)
    if quick:
        n_eval, n_tune = mc["n_paths_quick"], tc["n_paths_quick"]
    elif industries_only:
        n_eval, n_tune = mc.get("n_paths_industries", mc["n_paths"]), tc.get("n_paths_industries", tc["n_paths"])
    else:
        n_eval, n_tune = mc["n_paths"], tc["n_paths"]
    master = mc["master_seed"]
    ctxs = {a: load_context(cfg, a) for a in assets}
    seeds = {a: (cell_seed(master, a, "tune"), cell_seed(master, a, "eval")) for a in assets}
    H = {a: cfg["problem"]["horizons_days"] if a == "us_mkt" else mc["plan"]["industries"]["horizons"] for a in assets}
    # one task per (asset, state, horizon): small memory per worker; the asset's tuning seed is shared, so every
    # horizon sees the same paths (common random numbers are preserved)
    tjobs = [(a, r, T) for a in assets for r in ctxs[a].state_rows("tune" if a == "us_mkt" else "named").to_dict("records")
             for T in H[a]]
    log(f"tuning {len(tjobs)} (asset, state, horizon) cells at {n_tune} paths")
    t0 = time.time()
    tjobs.sort(key=lambda x: -x[2])
    tjobs_h = list(tjobs)
    cfg_key = {k: v for k, v in cfg.items() if k not in ("mc",)} | {"mc": {k: v for k, v in cfg["mc"].items()
                                                                          if k not in ("n_jobs", "plan")}}
    fp = code_fingerprint()
    tkeys = [task_key("tune", fp, cfg_key, a, r, T, n_tune, seeds[a][0]) for a, r, T in tjobs]
    n_done = sum((root / "cache" / "tune" / f"{k}.pkl").exists() for k in tkeys)
    log(f"  resume: {n_done}/{len(tjobs)} tuning cells already cached")
    parts = Parallel(n_jobs=jobs)(delayed(cached)(root, "tune", k, tune_task, cfg, a, r, [T], n_tune, seeds[a][0])
                                  for k, (a, r, T) in zip(tkeys, tjobs))
    t_tune = time.time() - t0
    merged = {}
    for (a, r, T), c in zip(tjobs, parts):
        m = merged.setdefault((a, c["state"]), {**c, "result": {}, "seconds": 0.0})
        m["result"].update(c["result"])
        m["seconds"] += c["seconds"]
    cells = list(merged.values())
    tjobs = [(a, st) for (a, st) in merged]
    tuned = {}
    for a in assets:
        ca = [c for (aa, _), c in zip(tjobs, cells) if aa == a]
        kc = cfg["strategies"]["knobs"]
        agg = aggregate(select(ca, "robust"), KNOBBED, H[a], kc)
        agg_base = aggregate(select(ca, "base"), KNOBBED, H[a], kc)
        common = {"asset": a, "seed": seeds[a][0], "n_paths": n_tune, "target_breach": tc["target_breach"],
                  "horizons": H[a], "floor": cfg["strategies"]["floor"],
                  "timings": [{"state": c["state"], "seconds": c["seconds"]} for c in ca],
                  "generated": dt.datetime.now().isoformat(timespec="seconds")}
        tuned[a] = {**common, "scenario": "robust", "robust_set": tc["robust_set"], "strategies": agg,
                    "base_strategies": agg_base}
        (root / "tuned" / f"{a}_robust.json").write_text(json.dumps(jsonable(tuned[a]), indent=1))
        (root / "tuned" / f"{a}_base.json").write_text(json.dumps(jsonable(
            {**common, "scenario": "base", "strategies": agg_base}), indent=1))
    log(f"tuning done in {t_tune:.0f}s")
    ejobs = [(a, scen, r, [T], rec, fr) for a in assets for scen, r, hz, rec, fr in plan_tasks(cfg, a, ctxs[a], quick)
             for T in hz]
    ejobs.sort(key=lambda t: -(sum(t[3]) * (3 if t[5] else 1)))
    log(f"evaluating {len(ejobs)} (asset, state, scenario) tasks at {n_eval} paths")
    t0 = time.time()
    tuned_key = {a: task_key({k: v for k, v in tuned[a].items() if k not in ("timings", "generated")}) for a in assets}
    ekeys = [task_key("eval", fp, cfg_key, a, scen, r, hz, n_eval, seeds[a][1], tuned_key[a], rec, fr)
             for a, scen, r, hz, rec, fr in ejobs]
    n_done = sum((root / "cache" / "eval" / f"{k}.pkl").exists() for k in ekeys)
    log(f"  resume: {n_done}/{len(ejobs)} evaluation tasks already cached")
    res = Parallel(n_jobs=jobs)(delayed(cached)(root, "eval", k, eval_task, cfg, a, r, scen, hz, n_eval, seeds[a][1],
                                                tuned[a], root, rec, fr)
                                for k, (a, scen, r, hz, rec, fr) in zip(ekeys, ejobs))
    t_eval = time.time() - t0
    log(f"evaluation done in {t_eval:.0f}s")
    tune_tim = [{"asset": a, "state": c["state"], "kind": "tune", "seconds": c["seconds"], "n_paths": n_tune,
                 "horizons": 1, "sum_T_days": int(T)} for (a, r, T), c in zip(tjobs_h, parts)]
    per = {a: {"n_eval": n_eval, "n_tune": n_tune, "seed_tune": seeds[a][0], "seed_eval": seeds[a][1],
               "draws": ctxs[a].draws_info, "fallback": ctxs[a].params.get("fallback"),
               "n_tune_cells": sum(1 for aa, _ in tjobs if aa == a)} for a in assets}
    return {"rows": [x for r in res for x in r[0]], "frontier": [x for r in res for x in r[1]],
            "timings": [r[2] | {"kind": "eval"} for r in res] + tune_tim, "t_tune": t_tune, "t_eval": t_eval,
            "per": per}


def merge_write(path: Path, df: pd.DataFrame, assets):
    if path.exists():
        old = pd.read_parquet(path)
        df = pd.concat([old[~old.asset.isin(assets)], df], ignore_index=True)
    df.to_parquet(path, index=False)
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="us_mkt", help="us_mkt | industries | all | <asset>")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--jobs", type=int, default=None)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    a = ap.parse_args()
    cfg = load_config(a.config)
    jobs = a.jobs or cfg["mc"]["n_jobs"]
    inds = [k for k in cfg["data"]["assets"] if k.startswith("ind_")]
    assets = {"all": ["us_mkt", *inds], "industries": inds}.get(a.asset, [a.asset])
    root = out_root(cfg, a.quick)
    for d in ("mc/terminal", "mc/paths", "tuned"):
        (root / d).mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    # us_mkt first (its own path counts), then the industries (n_paths_industries); each group is resumable
    groups = [g for g in ([x for x in assets if x == "us_mkt"], [x for x in assets if x != "us_mkt"]) if g]
    for grp in groups:
        log(f"=== group {grp}")
        run_group(cfg, grp, a.quick, jobs, root, a)
    log(f"done in {time.time() - t_start:.0f}s -> {root}")


def run_group(cfg, assets, quick, jobs, root, a):
    t_start = time.time()
    started = dt.datetime.now().isoformat(timespec="seconds")
    out = run_assets(cfg, assets, quick, jobs, root)
    summary = pd.DataFrame(out["rows"])
    frontier = pd.DataFrame(out["frontier"])
    timings = pd.DataFrame(out["timings"])
    summary = merge_write(root / "mc" / "summary.parquet", summary, assets)
    if len(frontier):
        merge_write(root / "mc" / "frontier.parquet", frontier, assets)
    merge_write(root / "mc" / "timings.parquet", timings, assets)
    worst_case(summary, cfg).to_parquet(root / "mc" / "worst_case.parquet", index=False)
    manifest_path = root / "run_manifest.json"
    man = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"assets": {}}
    dmf = json.loads(resolve(cfg, cfg["data"]["manifest"]).read_text())
    man.update({"git_commit": git_commit(), "master_seed": cfg["mc"]["master_seed"], "quick": a.quick,
                "config_snapshot": cfg, "data_sha256": {e["file"]: e["sha256"] for e in dmf["files"]},
                "last_run": {"started": started, "finished": dt.datetime.now().isoformat(timespec="seconds"),
                             "assets": assets, "runtime_s": time.time() - t_start, "jobs": jobs,
                             "cpu_count": os.cpu_count()}})
    man["last_run"].update({"tune_seconds": out["t_tune"], "eval_seconds": out["t_eval"]})
    for k, v in out["per"].items():
        man["assets"][k] = {"n_paths_eval": v["n_eval"], "n_paths_tune": v["n_tune"], "seed_tune": v["seed_tune"],
                            "seed_eval": v["seed_eval"], "n_tune_cells": v["n_tune_cells"],
                            "param_draws": v["draws"], "fallback": v["fallback"], "run_assets": assets,
                            "finished": dt.datetime.now().isoformat(timespec="seconds")}
    manifest_path.write_text(json.dumps(jsonable(man), indent=1, default=str))
    log(f"group {assets} done in {time.time() - t_start:.0f}s")


def git_commit():
    import subprocess
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return "not a git repository"


if __name__ == "__main__":
    main()
