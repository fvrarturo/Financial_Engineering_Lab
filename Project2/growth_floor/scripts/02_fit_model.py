#!/usr/bin/env python
"""Spec 02: fit the equity/rate model, build starting states, validate (PIT, VaR, stylised facts).

Usage:
  python scripts/02_fit_model.py --asset us_mkt
  python scripts/02_fit_model.py --asset all
Options: --jobs N (process pool size), --base-only-pit (skip comparison-model PIT runs)

Outputs (per asset):
  results/model/params_{asset}.json, model_comparison_{asset}.csv, states_{asset}.csv,
  filtered_{asset}.parquet, param_draws_{asset}.npy, pit_{asset}.csv
  reports/model/{asset}/model_report.md, model_report.json, figures/*.png
Expanding-window refits are cached in results/model/cache/ (keyed by data hash and settings).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gf import model as M  # noqa: E402
from gf import states as S  # noqa: E402
from gf import validation as V  # noqa: E402
from gf.data import load_config, read_asset_frame, resolve  # noqa: E402
from gf.simulate import simulate_paths  # noqa: E402


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def jsonable(o):
    if isinstance(o, dict):
        return {str(k): jsonable(v) for k, v in o.items() if not str(k).startswith("_")}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    if isinstance(o, np.ndarray):
        return jsonable(o.tolist())
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, pd.Timestamp):
        return str(o.date())
    return o


def pct(v, nd=1):
    return "n/a" if v is None or not np.isfinite(v) else f"{100 * v:.{nd}f}%"


def md_table(df: pd.DataFrame, floatfmt: dict | None = None) -> str:
    floatfmt = floatfmt or {}
    cols = list(df.columns)
    out = ["| " + " | ".join(map(str, cols)) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, r in df.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            f = floatfmt.get(c)
            if f and isinstance(v, (float, np.floating)) and np.isfinite(v):
                cells.append(f(v) if callable(f) else format(v, f))
            elif isinstance(v, (float, np.floating)):
                cells.append("n/a" if not np.isfinite(v) else f"{v:.4g}")
            else:
                cells.append(str(v))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def window(frame: pd.DataFrame, start, end) -> pd.DataFrame:
    return frame.loc[start:end]


# --------------------------------------------------------------------------- per asset

def alt_entry(f, gap_variant: str, N: int) -> dict:
    """Everything Spec 03 needs to simulate from a non-base fitted model."""
    return {"model": f.name, "kind": f.kind, "dist": f.dist, "gap_variant": gap_variant,
            "params": f.params, "se": f.se, "backcast": f.backcast, "n_obs": f.nobs,
            "loglik": f.loglik, "bic": f.bic, "converged": f.converged,
            "persistence": M.persistence(f.params, f.kind), "half_life_days": M.half_life(f.params, f.kind),
            "uncond_vol_ann": M.uncond_vol_ann(f.params, f.kind, f.dist, N)}

def fallback_check(asset: str, full_all: pd.DataFrame, cfg: dict) -> dict:
    """Fallback-window rule: fit the base model (base gap variant) on the full estimation window; trigger the
    fallback start if persistence >= max_persistence or the valid-draw share < min_draw_valid_share."""
    mc, est = cfg["model"], cfg["estimation"]
    rule = est["fallback_rule"]
    dc = json.loads((resolve(cfg, cfg["checks"]["report_dir"]) / "data_check.json").read_text())
    fl = M.gap_flags(full_all.index, dc["assets"][asset]["checks"]["dates"]["gaps"], mc["gaps"]["flag_calendar_days"])
    x = M.adjust_for_gaps(full_all["x"], fl, mc["gaps"]["base_variant"])
    x = window(x.to_frame("x"), est.get("start_date"), est.get("end_date"))["x"]
    f = M.fit_garch(x, mc["base"], mc["scale"])
    pers = M.persistence(f.params, f.kind)
    _, info = M.draw_params(f, mc["n_param_draws"], mc["seed"] + 2, mc["nu_min"], mc["scale"])
    share = info["acceptance_rate"]
    reasons = []
    if not np.isfinite(pers) or pers >= rule["max_persistence"]:
        reasons.append(f"persistence {pers:.5f} >= {rule['max_persistence']}")
    if share < rule["min_draw_valid_share"]:
        reasons.append(f"valid draws {share:.2f} < {rule['min_draw_valid_share']}")
    trig = bool(reasons)
    return {"triggered": trig, "reasons": reasons, "start_used": est["fallback_start"] if trig else None,
            "persistence_full": pers, "half_life_full": M.half_life(f.params, f.kind),
            "draw_valid_share_full": share, "converged_full": f.converged, "rule": rule,
            "fallback_start": est["fallback_start"]}


def run_asset(asset: str, cfg: dict, jobs: int, base_only_pit: bool) -> dict:
    t0 = time.time()
    N = cfg["trading_days_per_year"]
    mc = cfg["model"]
    res_dir = resolve(cfg, mc["results_dir"])
    rep_dir = resolve(cfg, mc["report_dir"]) / asset
    fig_dir = rep_dir / "figures"
    for d in (res_dir, fig_dir):
        d.mkdir(parents=True, exist_ok=True)
    cache_dir = res_dir / "cache" / asset
    frame_path = resolve(cfg, cfg["data"]["processed_dir"]) / f"{asset}.parquet"

    full_all = read_asset_frame(cfg, asset, apply_window=False)
    est = cfg["estimation"]
    fallback = fallback_check(asset, full_all, cfg)
    asset_start = fallback["start_used"]
    full = full_all.loc[asset_start:] if asset_start else full_all
    fw = window(full, est.get("start_date"), est.get("end_date"))
    R = {"asset": asset, "generated": dt.datetime.now().isoformat(timespec="seconds"),
         "estimation_window": {"start": str(fw.index[0].date()), "end": str(fw.index[-1].date()), "n_obs": len(fw),
                               "fallback_triggered": fallback["triggered"]},
         "fallback": fallback}
    log(f"{asset}: fallback rule: persistence {fallback['persistence_full']:.5f}, valid draws "
        f"{fallback['draw_valid_share_full']:.2f} -> {'FALLBACK to ' + str(asset_start) if asset_start else 'full history'}")

    # ---------------- closure gaps (from Spec 01 data check)
    dc = json.loads((resolve(cfg, cfg["checks"]["report_dir"]) / "data_check.json").read_text())
    gaps = dc["assets"][asset]["checks"]["dates"]["gaps"]
    flags = M.gap_flags(full.index, gaps, mc["gaps"]["flag_calendar_days"])
    flags["x"] = full["x"].reindex(flags.index)
    R["gap_flags"] = [{"date": str(d.date()), **{k: (v if not isinstance(v, (np.floating, np.integer)) else v.item())
                                                for k, v in r.items()}} for d, r in flags.iterrows()]
    xv_full = {v: M.adjust_for_gaps(full["x"], flags, v) for v in mc["gaps"]["variants"]}

    # ---------------- 1. full-window fits for all models x variants
    log(f"{asset}: full-window fits")
    fits_full, comp_rows = {}, []
    for v in mc["gaps"]["variants"]:
        xw = window(xv_full[v].to_frame("x"), est.get("start_date"), est.get("end_date"))["x"]
        for name in mc["comparison"]:
            f = M.fit_garch(xw, name, mc["scale"])
            fits_full[(name, v)] = f
            jac = M.gap_loglik_jacobian(flags.loc[xw.index[0]:xw.index[-1]], v)
            k = len(f.params_arch)
            ll = f.loglik + jac
            comp_rows.append({"model": name, "gap_variant": v, "nobs": f.nobs, "loglik": ll,
                              "aic": -2 * ll + 2 * k, "bic": -2 * ll + k * math.log(f.nobs),
                              "nu": f.params["nu"], "persistence": M.persistence(f.params, f.kind),
                              "half_life_days": M.half_life(f.params, f.kind),
                              "uncond_vol_ann": M.uncond_vol_ann(f.params, f.kind, f.dist, N),
                              "alpha": f.params["alpha"], "gamma": f.params["gamma"], "beta": f.params["beta"],
                              "converged": f.converged})
    comp = pd.DataFrame(comp_rows)

    base_name, base_var = mc["base"], mc["gaps"]["base_variant"]
    base = fits_full[(base_name, base_var)]
    kind = base.kind
    x_base_full = xv_full[base_var].to_numpy()

    # ---------------- 2. expanding (+ rolling) refits, in parallel
    log(f"{asset}: expanding refits ({len(mc['comparison']) * len(mc['gaps']['variants'])} model/variant combos + rolling)")
    ex = mc["expanding"]
    job_args = []
    for v in mc["gaps"]["variants"]:
        for name in mc["comparison"]:
            job_args.append({"x": xv_full[v].to_numpy(), "dates": [str(d.date()) for d in full.index], "name": name,
                             "min_obs": ex["min_obs"], "step": ex["refit_every"], "scale": mc["scale"],
                             "cache_dir": str(cache_dir), "tag": f"exp|{name}|{v}"})
    job_args.append({"x": x_base_full, "dates": [str(d.date()) for d in full.index], "name": base_name,
                     "min_obs": mc["rolling"]["window"], "step": mc["rolling"]["step"], "window": mc["rolling"]["window"],
                     "scale": mc["scale"], "cache_dir": str(cache_dir), "tag": f"roll|{base_name}|{base_var}"})
    with ProcessPoolExecutor(max_workers=jobs) as ppe:
        exp_out = {o["tag"]: o["fits"] for o in ppe.map(V.expanding_job, job_args)}
    exp_fits = {(t.split("|")[1], t.split("|")[2]): f for t, f in exp_out.items() if t.startswith("exp|")}
    roll_fits = exp_out[f"roll|{base_name}|{base_var}"]
    base_exp = exp_fits[(base_name, base_var)]

    # ---------------- 3. filtered history and states
    log(f"{asset}: filtered history and states")
    s2_full = M.filter_variance(x_base_full, base.params, kind, base.backcast)
    s2_exp = M.expanding_sigma2(x_base_full, base_exp, kind)
    filt = S.filtered_history(full, s2_full, s2_exp, flags, cfg)
    filt.to_parquet(res_dir / f"filtered_{asset}.parquet")
    r0_named = float(fw["rf_ann"].median())
    states = S.named_states(filt.loc[fw.index], cfg, r0_named)
    anchors = S.anchor_states(filt, cfg)
    states_all = pd.concat([states, anchors], ignore_index=True)
    states_all.to_csv(res_dir / f"states_{asset}.csv", index=False)
    edges = S.bucket_edges(filt.loc[fw.index], cfg)
    R["bucket_edges_sigma_ann"] = edges

    # ---------------- 4. premium
    prem = M.premium_estimate(fw, mc["premium"], N)
    prem_p = M.premium_params(prem, s2_full[:-1][full.index.isin(fw.index)], N)
    R["premium"] = {**prem, "resolved": prem_p}
    alt_modes = {}
    for mode in ["constant_arith", "prop_variance", "constant_sharpe"]:
        alt_modes[mode] = M.premium_params({**prem, "mode": mode}, s2_full[:-1][full.index.isin(fw.index)], N)
    R["premium"]["all_modes_resolved"] = alt_modes

    # ---------------- 5. rate model
    log(f"{asset}: rate model")
    rm = M.monthly_rate(full_all)   # rate model always uses the full rf history
    rc = cfg["rate"]
    rate_rows = {}
    rate_windows = {"base": (rc.get("start_date"), rc.get("end_date")), "full_sample": (None, None),
                    "from_1952-07": ("1952-07-01", None)}
    for lab, (a, b) in rate_windows.items():
        sub = rm.loc[pd.Period(a, "M") if a else None: pd.Period(b, "M") if b else None]
        vas = M.fit_vasicek(sub)
        cor = M.rate_equity_correlation(full, vas, rc.get("corr_alignment", "same_month"))
        rate_rows[lab] = {**{k: v for k, v in vas.items() if not k.startswith("_")}, **cor}
    rate_base = rate_rows["base"]
    neg_months = [str(p) for p in rm[rm < 0].index]
    rate_params = {"mode": rc["mode"], "kappa": rate_base["kappa"], "theta": rate_base["theta"], "s": rate_base["s"],
                   "rho": rate_base["rho"], "floor_at_zero": rc["floor_at_zero"]}
    R["rate"] = {"params": rate_params, "windows": rate_rows, "negative_months_all": neg_months,
                 "base_window_excludes_negative_months": all(pd.Period(p, "M") < pd.Period(rc["start_date"], "M")
                                                             for p in neg_months) if rc.get("start_date") else False}

    # ---------------- 6. FHS residuals
    zhat = (x_base_full - base.params["mu"]) / np.sqrt(s2_full[:-1])
    zh = zhat[np.isfinite(zhat)]
    R["fhs"] = {"n": len(zh), "mean": float(zh.mean()), "std": float(zh.std()), "skew": float(stats.skew(zh)),
                "exkurt": float(stats.kurtosis(zh)),
                "fitted_t_exkurt": float(6 / (base.params["nu"] - 4)) if base.params["nu"] > 4 else float("inf"),
                "min": float(zh.min()), "min_date": str(full.index[np.nanargmin(zhat)].date())}
    np.save(res_dir / f"zhat_{asset}.npy", zhat)

    # ---------------- 7. PIT (parallel over model x variant [+ FHS for base])
    log(f"{asset}: PIT calibration")
    pit_jobs = []
    combos = [(base_name, base_var, mc["innovations"] if mc["innovations"] != "fhs" else "t")]
    combos += [(base_name, base_var, "fhs")]
    if not base_only_pit:
        combos += [(n_, v_, "t" if M.MODEL_SPECS[n_]["dist"] == "t" else "normal")
                   for v_ in mc["gaps"]["variants"] for n_ in mc["comparison"]
                   if (n_, v_) != (base_name, base_var)]
    for n_, v_, inn in combos:
        pit_jobs.append({"frame_path": str(frame_path), "start_date": asset_start, "x_model": xv_full[v_].to_numpy(), "fits": exp_fits[(n_, v_)],
                         "name": n_, "innovations": inn, "cfg": cfg, "edges": edges, "tag": f"{n_}|{v_}|{inn}"})
    with ProcessPoolExecutor(max_workers=jobs) as ppe:
        pit_out = {o["tag"]: pd.DataFrame(o["pit"]) for o in ppe.map(V.pit_job, pit_jobs)}
    pit_tabs = {tag: V.pit_table(df, cfg) for tag, df in pit_out.items()}
    pit_all = pd.concat([df.assign(tag=tag) for tag, df in pit_out.items()], ignore_index=True)
    pit_all.to_csv(res_dir / f"pit_{asset}.csv", index=False)
    base_tag = f"{base_name}|{base_var}|{combos[0][2]}"

    # PIT summary into comparison table
    H1, H2 = cfg["validation"]["pit"]["horizons"]
    for tag, tab in pit_tabs.items():
        n_, v_, inn = tag.split("|")
        sel = (comp.model == n_) & (comp.gap_variant == v_)
        if inn == "fhs":
            row = comp[sel].iloc[0].to_dict()
            row["model"] = f"{n_} (FHS innovations)"
            comp = pd.concat([comp, pd.DataFrame([row])], ignore_index=True)
            sel = (comp.model == row["model"]) & (comp.gap_variant == v_)
        o = tab[(tab.group_type == "overall")]
        for h in (H1, H2):
            for lv in (0.02, 0.05):
                comp.loc[sel, f"PIT u<{lv:g} H{h}"] = float(o[(o.H == h) & (o.level == lv)]["freq"].iloc[0])
        comp.loc[sel, "PIT score"] = V.pit_score(tab)
        comp.loc[sel, "buckets thin/fat"] = ", ".join(
            f"H{h}: {int(((tab.group_type == 'bucket') & (tab.H == h) & (tab.verdict == 'too thin')).sum())}/"
            f"{int(((tab.group_type == 'bucket') & (tab.H == h) & (tab.verdict == 'too fat')).sum())}" for h in (H1, H2))
    comp.to_csv(res_dir / f"model_comparison_{asset}.csv", index=False)

    # ---------------- 8. VaR backtest + crash check
    log(f"{asset}: VaR backtest and crash check")
    buckets_daily = S.assign_bucket(np.sqrt(s2_exp[:-1] * N), edges)
    var_tabs = {}
    for (n_, v_), fl in exp_fits.items():
        s2e = s2_exp if (n_, v_) == (base_name, base_var) else M.expanding_sigma2(xv_full[v_].to_numpy(), fl, M.MODEL_SPECS[n_]["kind"])
        gs = None
        if v_ == "scale" and not flags.empty:
            gs = np.ones(len(full))
            gs[full.index.get_indexer(flags.index)] = np.sqrt(flags["busdays"].to_numpy())
        var_tabs[(n_, v_)] = V.var_backtest(full["x"].to_numpy(), s2e, fl, M.MODEL_SPECS[n_]["kind"],
                                            cfg["validation"]["var"]["levels"], buckets_daily, gs)
    for (n_, v_), vt in var_tabs.items():
        sel = (comp.model == n_) & (comp.gap_variant == v_)
        for lv in cfg["validation"]["var"]["levels"]:
            r = vt[(vt.level == lv) & (vt.bucket == "all")].iloc[0]
            comp.loc[sel, f"VaR{lv:g} hit rate"] = r["rate"]
            comp.loc[sel, f"VaR{lv:g} p_uc"] = r["p_uc"]
    comp.to_csv(res_dir / f"model_comparison_{asset}.csv", index=False)

    cc = cfg["validation"]["crash"]
    crash = {}
    sd_, kd_ = pd.Timestamp(cc["state_date"]), pd.Timestamp(cc["shock_date"])
    if sd_ in full.index and kd_ in full.index:
        i_s, i_k = full.index.get_loc(sd_), full.index.get_loc(kd_)
        rf_k = full["rf"].iloc[i_k]
        thr_x = math.log(1 - cc["one_day_loss"]) - math.log1p(rf_k)
        for lab, s2arr, fl in [("full_sample_params", s2_full, None), ("expanding_params", s2_exp, base_exp)]:
            if fl is not None and not np.isfinite(s2arr[i_k]):
                crash[lab] = {"skipped": "no expanding fit before the shock date"}
                continue
            p = base.params if fl is None else M.fits_at(fl, i_k)["params"]
            sig = math.sqrt(s2arr[i_k])
            zthr = (thr_x - p["mu"]) / sig
            prob = float(V.std_t_cdf(zthr, p["nu"]))
            zreal = (full["x"].iloc[i_k] - p["mu"]) / sig
            crash[lab] = {"sigma_ann_for_shock_day": sig * math.sqrt(N), "nu": p["nu"],
                          "prob_one_day_loss": prob, "one_in_years": (1 / prob / N) if prob > 0 else float("inf"),
                          "realised_standardised_shock": float(zreal),
                          "model_prob_of_shock_this_size_or_worse": float(V.std_t_cdf(zreal, p["nu"]))}
        # same loss probability from the named states (constant-sigma one-day view)
        crash["from_named_states"] = {r.state_name: float(V.std_t_cdf((thr_x - base.params["mu"]) / (r.sigma0_ann / math.sqrt(N)),
                                                                     base.params["nu"]))
                                      for r in states[states.source == "named"].itertuples()}
    R["crash"] = crash

    # ---------------- 9. stylised facts
    log(f"{asset}: stylised facts simulation")
    sc = cfg["validation"]["stylised"]
    n_days = len(fw)
    sim = simulate_paths({**base.params, "kind": kind}, rate_params,
                         {"sigma0_ann": math.sqrt(M.uncond_var_daily(base.params, kind, base.dist) * N),
                          "r0_ann": float(fw["rf_ann"].iloc[0])},
                         n_days, sc["n_paths"], mc["seed"] + 1, innovations="t", premium=prem_p, N=N)
    xd = fw["x"].to_numpy()
    sty = {"data": {"ann_vol": float(xd.std() * math.sqrt(N)), "exkurt": float(stats.kurtosis(xd)),
                    "skew": float(stats.skew(xd)), "ann_mean": float(xd.mean() * N)},
           "sim": {"ann_vol": float(np.mean(sim.x.std(axis=1)) * math.sqrt(N)),
                   "ann_vol_p05_p95": np.percentile(sim.x.std(axis=1) * math.sqrt(N), [5, 95]).tolist(),
                   "exkurt": float(np.median([stats.kurtosis(p) for p in sim.x])),
                   "exkurt_p05_p95": np.percentile([stats.kurtosis(p) for p in sim.x], [5, 95]).tolist(),
                   "skew": float(np.median([stats.skew(p) for p in sim.x])),
                   "ann_mean": float(sim.x.mean() * N)},
           "n_clipped": sim.meta["n_clipped"]}
    L = sc["acf_lags"]
    acf_d = {"abs": V.acf(np.abs(xd), L), "sq": V.acf(xd ** 2, L)}
    acf_s = {"abs": np.array([V.acf(np.abs(p), L) for p in sim.x]), "sq": np.array([V.acf(p ** 2, L) for p in sim.x])}
    win = {}
    for w in cfg["validation"]["pit"]["horizons"]:
        dsum = V.window_sums(xd, w, sc["window_step"])
        ssum = np.concatenate([V.window_sums(p, w, sc["window_step"]) for p in sim.x])
        ddd = V.max_drawdown_windows(xd, w, sc["window_step"])
        sdd = np.concatenate([V.max_drawdown_windows(p, w, sc["window_step"]) for p in sim.x[:10]])
        qs = [0.01, 0.02, 0.05, 0.10, 0.5]
        win[w] = {"cum_quantiles_data": dict(zip(map(str, qs), np.quantile(dsum, qs))),
                  "cum_quantiles_sim": dict(zip(map(str, qs), np.quantile(ssum, qs))),
                  "mdd_quantiles_data": dict(zip(map(str, qs), np.quantile(ddd, qs))),
                  "mdd_quantiles_sim": dict(zip(map(str, qs), np.quantile(sdd, qs))),
                  "_d": dsum, "_s": ssum, "_dd": ddd, "_sd": sdd}
    sty["windows"] = win
    sty["acf_abs_lag1_10_50_100"] = {"data": acf_d["abs"][[0, 9, 49, 99]].tolist(),
                                     "sim_median": np.median(acf_s["abs"], 0)[[0, 9, 49, 99]].tolist()}
    R["stylised"] = sty

    # ---------------- 10. stability
    log(f"{asset}: stability (sub-samples, robustness windows)")
    sub_rows = []
    min_sub = mc["expanding"]["min_obs"]
    for lab, a, b in mc["subsamples"]:
        xs = xv_full[base_var].loc[a:b]
        if len(xs) < min_sub:
            sub_rows.append({"sample": f"{lab} (skipped: {len(xs)} obs)"})
            continue
        f = M.fit_garch(xs, base_name, mc["scale"])
        sub_rows.append({"sample": lab, **{k: v for k, v in f.summary().items() if k != "model"}})
    for a in mc["robustness_start_dates"]:
        if asset_start and pd.Timestamp(a) <= pd.Timestamp(asset_start):
            continue
        f = M.fit_garch(xv_full[base_var].loc[a:], base_name, mc["scale"])
        sub_rows.append({"sample": f"from {a}", **{k: v for k, v in f.summary().items() if k != "model"}})
    f = base
    sub_rows.insert(0, {"sample": "estimation window (base)", **{k: v for k, v in f.summary().items() if k != "model"}})
    subs = pd.DataFrame(sub_rows)
    R["subsamples"] = subs.to_dict(orient="records")

    # ---------------- 11. parameter draws
    draws, dinfo = M.draw_params(base, mc["n_param_draws"], mc["seed"] + 2, mc["nu_min"], mc["scale"])
    np.save(res_dir / f"param_draws_{asset}.npy", draws)
    R["param_draws"] = {**dinfo, "mean": dict(zip(M.PARAM_ORDER, draws.mean(0))),
                        "p05": dict(zip(M.PARAM_ORDER, np.percentile(draws, 5, 0))),
                        "p95": dict(zip(M.PARAM_ORDER, np.percentile(draws, 95, 0))),
                        "uncond_vol_p05_p95": np.percentile(
                            [M.uncond_vol_ann(dict(zip(M.PARAM_ORDER, d)), kind, base.dist, N) for d in draws], [5, 95]).tolist()}

    # ---------------- params json
    manifest = json.loads(resolve(cfg, cfg["data"]["manifest"]).read_text())
    src_files = {cfg["data"]["sources"][cfg["data"]["assets"][asset]["source"]]["file"],
                 cfg["data"]["sources"][cfg["data"]["rf"]["daily"]["source"]]["file"],
                 cfg["data"]["sources"][cfg["data"]["rf"]["monthly"]["source"]]["file"]}
    params_json = {
        "asset": asset, "model": base_name, "kind": kind, "dist": base.dist, "gap_variant": base_var,
        "params": base.params, "se": base.se, "param_order": M.PARAM_ORDER,
        "estimation_window": R["estimation_window"], "fallback": R["fallback"], "n_obs": base.nobs, "loglik": base.loglik,
        "aic": base.aic, "bic": base.bic, "persistence": M.persistence(base.params, kind),
        "half_life_days": M.half_life(base.params, kind), "uncond_vol_ann": M.uncond_vol_ann(base.params, kind, base.dist, N),
        "backcast": base.backcast, "premium": R["premium"], "rate": {"params": rate_params, "fit": rate_base},
        "innovations": mc["innovations"], "fhs_residuals_file": f"results/model/zhat_{asset}.npy",
        "data_sha256": {e["file"]: e["sha256"] for e in manifest["files"] if e["file"] in src_files},
        "config_model": mc, "generated": R["generated"],
        # full-sample fits of the other comparison models (same gap variant as base). Spec 03 simulates
        # from garch_t and egarch_t as alternative models for strategy-level model-risk tests.
        "alternative_models": {n_: alt_entry(fits_full[(n_, base_var)], base_var, N)
                               for n_ in mc["comparison"] if n_ != base_name},
    }
    (res_dir / f"params_{asset}.json").write_text(json.dumps(jsonable(params_json), indent=2))

    # ---------------- figures + report
    log(f"{asset}: figures and report")
    figs = make_figures(asset, cfg, full, fw, filt, states, edges, zh, base, sim, acf_d, acf_s, win,
                        pit_out[base_tag], base_exp, roll_fits, kind, rm, rate_base, fig_dir)
    R.update({"comparison": comp, "pit_tables": pit_tabs, "base_tag": base_tag, "var": var_tabs,
              "states": states, "anchors": anchors, "subs": subs, "figures": figs, "params": params_json,
              "flags": flags, "edges": edges, "runtime_s": time.time() - t0})
    write_report(R, cfg, rep_dir)
    log(f"{asset}: done in {time.time() - t0:.0f}s")
    return R


# --------------------------------------------------------------------------- figures
def make_figures(asset, cfg, full, fw, filt, states, edges, zh, base, sim, acf_d, acf_s, win, pit_base,
                 base_exp, roll_fits, kind, rm, rate_base, fig_dir) -> dict:
    N = cfg["trading_days_per_year"]
    dpi = cfg["checks"]["figures"]["dpi"]
    out = {}

    def save(fig, key):
        fig.tight_layout()
        fig.savefig(fig_dir / f"{key}.png", dpi=dpi)
        plt.close(fig)
        out[key] = f"figures/{key}.png"

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(filt.index, filt["sigma_next_ann_full"], lw=0.5, label="full-sample params")
    ax.plot(filt.index, filt["sigma_next_ann_exp"], lw=0.5, alpha=0.7, label="expanding params (no look-ahead)")
    for r in states[states.source == "named"].itertuples():
        ax.axhline(r.sigma0_ann, ls=":", lw=0.7, color="k")
        ax.text(filt.index[0], r.sigma0_ann, f" {r.state_name}", fontsize=7, va="bottom")
    ax.set_yscale("log")
    ax.set(title=f"{asset}: filtered conditional vol σ_0 (annualised)", ylabel="σ (log scale)")
    ax.legend(fontsize=8)
    save(fig, "filtered_sigma")

    fig, ax = plt.subplots(figsize=(5, 5))
    q = np.linspace(0.0005, 0.9995, 400)
    nu = base.params["nu"]
    ax.plot(V.std_t_ppf(q, nu), np.quantile(zh, q), ".", ms=2)
    lim = [min(zh.min(), -8), max(zh.max(), 8)]
    ax.plot(lim, lim, "k-", lw=0.7)
    ax.set(xlabel=f"fitted standardised t(ν={nu:.2f}) quantiles", ylabel="ẑ quantiles", title=f"{asset}: QQ of ẑ vs fitted t")
    save(fig, "qq_zhat")

    fig, axes = plt.subplots(1, 2, figsize=(11, 3.5))
    for ax, k, lab in [(axes[0], "abs", "|x|"), (axes[1], "sq", "x²")]:
        lags = np.arange(1, len(acf_d[k]) + 1)
        ax.plot(lags, acf_d[k], "k-", lw=1, label="data")
        ax.fill_between(lags, *np.percentile(acf_s[k], [5, 95], 0), alpha=0.3, label="sim 5-95%")
        ax.plot(lags, np.median(acf_s[k], 0), lw=1, label="sim median")
        ax.set(title=f"ACF of {lab}", xlabel="lag")
        ax.legend(fontsize=8)
    save(fig, "acf")

    fig, axes = plt.subplots(2, 2, figsize=(11, 6))
    for i, (w, d) in enumerate(win.items()):
        a = axes[0, i]
        bins = np.linspace(min(d["_d"].min(), np.quantile(d["_s"], 0.001)), max(d["_d"].max(), np.quantile(d["_s"], 0.999)), 60)
        a.hist(d["_d"], bins=bins, density=True, alpha=0.5, label="data")
        a.hist(d["_s"], bins=bins, density=True, alpha=0.5, label="model")
        a.set(title=f"{w}-day cumulative log excess return", yscale="log")
        a.legend(fontsize=8)
        a = axes[1, i]
        bins = np.linspace(min(d["_dd"].min(), d["_sd"].min()), 0, 50)
        a.hist(d["_dd"], bins=bins, density=True, alpha=0.5, label="data")
        a.hist(d["_sd"], bins=bins, density=True, alpha=0.5, label="model")
        a.set(title=f"max drawdown within {w} days (excess)", yscale="log")
        a.legend(fontsize=8)
    save(fig, "stylised_windows")

    H = cfg["validation"]["pit"]["horizons"]
    bks = list(cfg["validation"]["pit"]["buckets"])
    fig, axes = plt.subplots(len(H), len(bks), figsize=(3 * len(bks), 2.6 * len(H)), squeeze=False)
    for i, h in enumerate(H):
        for j, b in enumerate(bks):
            u = pit_base.loc[pit_base.bucket == b, f"u{h}"].dropna()
            axes[i, j].hist(u, bins=np.linspace(0, 1, 21), density=True, alpha=0.7)
            axes[i, j].axhline(1, color="k", lw=0.7)
            axes[i, j].set(title=f"H={h}, {b} (n={len(u)})", xlim=(0, 1))
    fig.suptitle(f"{asset}: PIT histograms by σ_0 bucket (base model)")
    save(fig, "pit_hist_buckets")

    fig, axes = plt.subplots(1, len(H), figsize=(5 * len(H), 3.5))
    for ax, h in zip(np.atleast_1d(axes), H):
        d = pit_base.dropna(subset=[f"u{h}"])
        ax.plot(pd.DatetimeIndex(d["date"]), d[f"u{h}"], ".", ms=3)
        for lv in (0.02, 0.05):
            ax.axhline(lv, color="r", lw=0.6, ls="--")
        ax.set(yscale="log", ylim=(5e-4, 1), title=f"PIT u over time, H={h}")
    save(fig, "pit_time")

    keys = ["omega", "alpha", "gamma", "beta", "nu"]
    fig, axes = plt.subplots(2, 3, figsize=(12, 6))
    axes = axes.ravel()
    def series(fl, key):
        d = pd.DatetimeIndex([f["date_first_use"] for f in fl])
        if key == "uncond_vol":
            return d, [M.uncond_vol_ann(f["params"], kind, base.dist, N) for f in fl]
        if key == "persistence":
            return d, [M.persistence(f["params"], kind) for f in fl]
        return d, [f["params"][key] for f in fl]
    for ax, key in zip(axes, ["uncond_vol", "persistence", "alpha", "gamma", "beta", "nu"]):
        for fl, lab in [(base_exp, "expanding"), (roll_fits, "rolling 20y")]:
            d, v = series(fl, key)
            ax.plot(d, v, lw=1, label=lab)
        ax.set_title(key)
        if key == "uncond_vol":
            ax.set_ylim(0, 0.6)
        ax.legend(fontsize=7)
    fig.suptitle(f"{asset}: parameter stability ({base.name})")
    save(fig, "param_stability")

    fig, ax = plt.subplots(figsize=(10, 3.5))
    ax.plot(rm.index.to_timestamp(), rm.values, lw=0.8, label="rf_ann (monthly)")
    ax.axhline(rate_base["theta"], color="r", ls="--", lw=0.8, label=f"Vasicek θ (base window) = {rate_base['theta']:.3f}")
    ax.axvline(pd.Timestamp(cfg["rate"]["start_date"]), color="k", lw=0.6, ls=":")
    ax.axhline(0, color="k", lw=0.4)
    ax.legend(fontsize=8)
    ax.set_title(f"{asset}: short rate and Vasicek long-run mean")
    save(fig, "rate")
    return out


# --------------------------------------------------------------------------- report
def write_report(R: dict, cfg: dict, rep_dir: Path):
    asset = R["asset"]
    N = cfg["trading_days_per_year"]
    P = R["params"]
    pc = cfg["validation"]["pit"]
    H = pc["horizons"]
    L = [f"# Model report (Spec 02): `{asset}`\n",
         f"Generated {R['generated']} by `scripts/02_fit_model.py --asset {asset}` in {R['runtime_s']:.0f}s. "
         f"Estimation window {R['estimation_window']['start']} – {R['estimation_window']['end']} "
         f"({R['estimation_window']['n_obs']} days). Base model **{P['model']}**, gap variant **{P['gap_variant']}**, "
         f"innovations **{P['innovations']}**. Data hashes: " +
         ", ".join(f"`{k}` {v[:12]}…" for k, v in P["data_sha256"].items()) + ".\n"]

    fb = R["fallback"]
    L.append(f"**Fallback-window rule**: full-history base fit persistence {fb['persistence_full']:.5f}, valid "
             f"parameter draws {fb['draw_valid_share_full']:.0%} → "
             + (f"**triggered** ({'; '.join(fb['reasons'])}); all equity estimation uses data from "
                f"{fb['fallback_start']}.\n" if fb["triggered"] else "not triggered; full history used.\n"))

    # 1. comparison
    L.append("## 1. Model comparison\n")
    L.append("Log-likelihoods are for the *unscaled decimal* x; for the `scale` gap variant the Jacobian "
             "(−½ Σ log k) is added so log-likelihood/BIC are comparable across variants. PIT columns: observed "
             "share of historical starts with u below the level (nominal = the level), expanding-window parameters. "
             "PIT score = mean |obs − nominal|/nominal over σ_0 buckets at levels ≤ 5% (lower is better). "
             "'buckets thin/fat' counts bucket×level cells whose CI excludes the nominal.\n")
    comp = R["comparison"].copy()
    show = [c for c in comp.columns if c not in ("alpha", "gamma", "beta", "converged", "aic")]
    fmt = {"loglik": ".1f", "bic": ".1f", "nu": ".2f", "persistence": ".4f", "half_life_days": ".1f",
           "uncond_vol_ann": lambda v: pct(v), "PIT score": ".2f"}
    for c in comp.columns:
        if c.startswith("PIT u") or c.endswith("hit rate"):
            fmt[c] = lambda v: pct(v, 2)
        if c.endswith("p_uc"):
            fmt[c] = ".3f"
    L.append(md_table(comp[show], fmt))
    L.append("")

    # params
    L.append(f"\n### Base model parameters ({P['model']}, decimal daily units, robust SEs)\n")
    prow = pd.DataFrame([{"param": k, "estimate": P["params"][k], "se": P["se"].get(k, np.nan)} for k in M.PARAM_ORDER])
    L.append(md_table(prow, {"estimate": ".6g", "se": ".3g"}))
    L.append(f"\nlog-lik {P['loglik']:.1f}, AIC {P['aic']:.1f}, BIC {P['bic']:.1f}; persistence {P['persistence']:.4f}; "
             f"half-life {P['half_life_days']:.1f} days; unconditional vol {pct(P['uncond_vol_ann'])}; ν = {P['params']['nu']:.2f}.\n")

    # 2. premium
    pr = R["premium"]
    L.append("## 2. Expected excess return (premium)\n")
    L.append(f"Raw estimate (mean of r − rf × {N}): **{pct(pr['raw_annual'], 2)}** p.a., SE {pct(pr['se_annual'], 2)} "
             f"(t = {pr['t_stat_raw']:.2f}, {pr['years']:.1f} years). Shrunk {pr['shrink_weight']} toward {pr['shrink_to']}: "
             f"**{pct(pr['annual_excess_used'], 2)}** used (mode `{pr['mode']}`). Resolved coefficients for all modes "
             f"(same average premium): " + "; ".join(f"`{m}` " + ", ".join(f"{k}={v:.4g}" for k, v in d.items() if k != "mode")
                                                      for m, d in pr["all_modes_resolved"].items()) + "\n")

    # 3. FHS
    fh = R["fhs"]
    L.append("## 3. Standardised residuals (FHS pool)\n")
    L.append(f"n = {fh['n']}, mean {fh['mean']:.3f}, sd {fh['std']:.3f}, skew {fh['skew']:.2f}, excess kurtosis "
             f"{fh['exkurt']:.2f} (fitted t implies {fh['fitted_t_exkurt']:.2f}); min {fh['min']:.2f} on {fh['min_date']}. "
             f"The FHS pool is demeaned and scaled to unit variance before resampling.\n")
    L.append(f"![qq]({R['figures']['qq_zhat']})\n")

    # 4. rates
    rt = R["rate"]
    L.append("## 4. Interest-rate model (Vasicek on monthly rf_ann)\n")
    rr = pd.DataFrame([{"window": k, "start": v["start"], "end": v["end"], "months": v["n_months"], "kappa": v["kappa"],
                        "se_kappa": v["se_kappa"], "theta": v["theta"], "s": v["s"], "half-life (y)": v["half_life_years"],
                        "neg. months": v["n_negative_months"], "rho used": v["rho"], "rho same-month": v["rho_same_month_label"], "rho prev-month": v["rho_prev_month"]}
                       for k, v in rt["windows"].items()])
    L.append(md_table(rr, {"kappa": ".3f", "se_kappa": ".3f", "theta": lambda v: pct(v, 2), "s": lambda v: pct(v, 2),
                           "half-life (y)": ".1f", "rho used": ".3f", "rho same-month": ".3f", "rho prev-month": ".3f"}))
    L.append(f"\nNegative monthly rf months in the data: {', '.join(rt['negative_months_all']) or 'none'}. "
             f"Base window ({cfg['rate']['start_date']}+) excludes them: **{rt['base_window_excludes_negative_months']}**. "
             f"`floor_at_zero = {cfg['rate']['floor_at_zero']}`.\n")
    L.append(f"![rate]({R['figures']['rate']})\n")

    # 5. states
    L.append("## 5. Starting states (σ_0, r_0)\n")
    L.append("Under GJR-GARCH the one-step-ahead conditional variance σ_0² is the complete equity state (the sign of "
             "the last shock is already inside it); r_0 is the Vasicek state. Both are observable at t = 0. Named "
             "states are percentiles of the asset's own filtered σ history (full-sample params), so they transfer "
             f"across assets. Annualised with N = {N} (see note on pre-1952 data in DECISIONS).\n")
    st = R["states"]
    L.append(md_table(st[st.source == "named"][["state_name", "sigma0_ann", "r0_ann", "definition"]],
                      {"sigma0_ann": lambda v: pct(v), "r0_ann": lambda v: pct(v, 2)}))
    L.append("\nAnchor dates (σ_0 = next-day vol at the close of the date):\n")
    an = R["anchors"]
    cols = [c for c in an.columns if c not in ("definition", "source")]
    L.append(md_table(an[cols], {c: (lambda v: pct(v, 1)) for c in cols if c != "state_name"}))
    L.append(f"\nσ_0 bucket edges used in the PIT/VaR tests (annualised): " +
             ", ".join(f"{k}: [{pct(a)}, {pct(b)})" for k, (a, b) in R["edges"].items()) + "\n")
    L.append(f"![sigma]({R['figures']['filtered_sigma']})\n")

    # 6. PIT
    L.append("## 6. Conditional tail calibration (PIT test)\n")
    L.append(f"Quarterly start dates; at each start the model is re-estimated on data **before** the start "
             f"(expanding window, refit every {cfg['model']['expanding']['refit_every']} days; premium and rate model "
             f"also expanding). {pc['n_paths']} paths per start. u = model CDF of the realised cumulative log total "
             f"return over H days. Effective n = non-overlapping windows = n_starts × 63/H; CIs are Wilson "
             f"{int(pc['ci_level'] * 100)}% on the effective n. 'too thin' = more realised left-tail outcomes than the "
             f"model predicts (dangerous for the floor).\n")
    for tag, tab in R["pit_tables"].items():
        if tag != R["base_tag"] and not tag.endswith("|fhs"):
            continue
        L.append(f"\n### {tag.replace('|', ' / ')}\n")
        t = tab.copy()
        t["obs (CI)"] = [f"{pct(f, 1)} ({pct(a, 1)}–{pct(b, 1)})" for f, a, b in zip(t.freq, t.ci_lo, t.ci_hi)]
        piv = t.pivot_table(index=["group_type", "group", "H", "n_starts", "n_eff"], columns="level",
                            values="obs (CI)", aggfunc="first").reset_index()
        piv.columns = [f"u<{c:g}" if isinstance(c, float) else c for c in piv.columns]
        order = {"overall": 0, "bucket": 1, "bucket_group": 2, "era": 3}
        piv = piv.sort_values(by=["H", "group_type"], key=lambda s: s.map(order) if s.name == "group_type" else s, kind="stable")
        L.append(md_table(piv, {"n_eff": ".1f"}))
        L.append("\n**Verdicts:**\n")
        for h in H:
            bg = list(pc.get("bucket_groups") or {})
            for g in list(pc["buckets"]) + bg + [e[0] for e in pc["eras"]]:
                gt = "bucket" if g in pc["buckets"] else ("bucket_group" if g in bg else "era")
                L.append(f"- H={h}, {gt} `{g}`: {V.group_verdict(tab, gt, g, h)}")
    L.append(f"\n![pit]({R['figures']['pit_hist_buckets']})\n\n![pit_time]({R['figures']['pit_time']})\n")

    # 7. VaR + crash
    L.append("## 7. One-day tail check\n")
    vt = R["var"][(P["model"], P["gap_variant"])]
    L.append(md_table(vt[["level", "bucket", "n", "hits", "rate", "LR_uc", "p_uc", "LR_ind", "p_ind"]],
                      {"rate": lambda v: pct(v, 3), "LR_uc": ".2f", "p_uc": ".3f", "LR_ind": ".2f", "p_ind": ".3f"}))
    L.append("\n(Christoffersen independence within a bucket uses consecutive days of that bucket's sub-sequence.)\n")
    for lab, c in R["crash"].items():
        if lab == "from_named_states":
            L.append("- P(one-day loss ≥ {:.0%}) from named states: ".format(cfg["validation"]["crash"]["one_day_loss"]) +
                     ", ".join(f"{k}: {v:.2e}" for k, v in c.items()))
        elif "skipped" in c:
            L.append(f"- **{lab}**: skipped ({c['skipped']}).")
        else:
            L.append(f"- **{lab}**: σ for {cfg['validation']['crash']['shock_date']} = {pct(c['sigma_ann_for_shock_day'])} "
                     f"(ann.), ν = {c['nu']:.2f}. P(one-day loss ≥ {cfg['validation']['crash']['one_day_loss']:.0%}) = "
                     f"{c['prob_one_day_loss']:.2e} (once in {c['one_in_years']:.3g} years at that σ). Realised standardised "
                     f"shock = {c['realised_standardised_shock']:.1f}; model probability of a shock that size or worse = "
                     f"{c['model_prob_of_shock_this_size_or_worse']:.2e}.")
    L.append("")

    # 8. stylised
    sy = R["stylised"]
    L.append("## 8. Stylised facts: data vs simulated long paths\n")
    L.append(f"{cfg['validation']['stylised']['n_paths']} simulated paths of the estimation-window length from the base model "
             f"(t innovations, base premium and rate model).\n")
    srows = pd.DataFrame([
        {"statistic": "annualised vol of x", "data": pct(sy["data"]["ann_vol"]), "model": f"{pct(sy['sim']['ann_vol'])} (5–95%: {pct(sy['sim']['ann_vol_p05_p95'][0])}–{pct(sy['sim']['ann_vol_p05_p95'][1])})"},
        {"statistic": "excess kurtosis of daily x", "data": f"{sy['data']['exkurt']:.1f}", "model": f"{sy['sim']['exkurt']:.1f} (5–95%: {sy['sim']['exkurt_p05_p95'][0]:.1f}–{sy['sim']['exkurt_p05_p95'][1]:.1f})"},
        {"statistic": "skew of daily x", "data": f"{sy['data']['skew']:.2f}", "model": f"{sy['sim']['skew']:.2f}"},
        {"statistic": "ACF |x| lags 1/10/50/100", "data": " / ".join(f"{v:.2f}" for v in sy["acf_abs_lag1_10_50_100"]["data"]),
         "model": " / ".join(f"{v:.2f}" for v in sy["acf_abs_lag1_10_50_100"]["sim_median"])},
    ])
    L.append(md_table(srows))
    for w, d in sy["windows"].items():
        L.append(f"\n{w}-day windows (start every {cfg['validation']['stylised']['window_step']} days) — quantiles:\n")
        qrows = pd.DataFrame([{"quantile": q, "cum log excess, data": d["cum_quantiles_data"][q], "cum log excess, model": d["cum_quantiles_sim"][q],
                               "max DD, data": d["mdd_quantiles_data"][q], "max DD, model": d["mdd_quantiles_sim"][q]} for q in d["cum_quantiles_data"]])
        L.append(md_table(qrows, {c: (lambda v: pct(v)) for c in qrows.columns if c != "quantile"}))
    L.append(f"\nSimulated x clipped at −99% simple return: {sy['n_clipped']} times.\n")
    L.append(f"![acf]({R['figures']['acf']})\n\n![windows]({R['figures']['stylised_windows']})\n")

    # 9. stability
    L.append("## 9. Parameter stability\n")
    sb = R["subs"][["sample", "start", "end", "nobs", "mu", "omega", "alpha", "gamma", "beta", "nu", "persistence",
                    "half_life_days", "uncond_vol_ann", "loglik"]]
    L.append(md_table(sb, {"mu": ".2e", "omega": ".2e", "alpha": ".3f", "gamma": ".3f", "beta": ".3f", "nu": ".2f",
                           "persistence": ".4f", "half_life_days": ".0f", "uncond_vol_ann": lambda v: pct(v), "loglik": ".0f"}))
    L.append(f"\n![stab]({R['figures']['param_stability']})\n")

    # 10. draws + gaps
    pd_ = R["param_draws"]
    L.append("## 10. Parameter-uncertainty draws\n")
    L.append(f"{pd_['n_draws']} draws from the asymptotic normal of the robust covariance (acceptance {pct(pd_['acceptance_rate'])}; "
             f"rejected: non-stationary, ω ≤ 0, negative ARCH terms, ν ≤ {pd_['nu_min']}). Unconditional vol 5–95%: "
             f"{pct(pd_['uncond_vol_p05_p95'][0])}–{pct(pd_['uncond_vol_p05_p95'][1])}; ν 5–95%: "
             f"{pd_['p05']['nu']:.2f}–{pd_['p95']['nu']:.2f}. Saved to `results/model/param_draws_{asset}.npy` "
             f"(columns {pd_['columns']}).\n")
    L.append("## 11. Closure-gap returns\n")
    fl = R["flags"].reset_index()
    fl["date"] = fl["date"].dt.strftime("%Y-%m-%d")
    if len(fl):
        fl["x"] = fl["x"].map(lambda v: pct(v, 2))
        L.append(md_table(fl))
    L.append("\nVariant `scale` divides these returns by sqrt(busdays) before fitting; `none` uses them as is. "
             "Compare the rows of the model comparison table with the same model name.\n")

    (rep_dir / "model_report.md").write_text("\n".join(L) + "\n")
    js = {k: v for k, v in R.items() if k not in ("comparison", "pit_tables", "var", "states", "anchors", "subs", "flags", "params")}
    js["comparison"] = R["comparison"].to_dict(orient="records")
    js["pit_tables"] = {k: v.to_dict(orient="records") for k, v in R["pit_tables"].items()}
    js["var"] = {f"{k[0]}|{k[1]}": v.to_dict(orient="records") for k, v in R["var"].items()}
    js["states"] = R["states"].to_dict(orient="records")
    js["anchors"] = R["anchors"].to_dict(orient="records")
    js["subsamples"] = R["subs"].to_dict(orient="records")
    js["params_file"] = f"results/model/params_{asset}.json"
    (rep_dir / "model_report.json").write_text(json.dumps(jsonable(js), indent=2))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--asset", default="us_mkt")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--base-only-pit", action="store_true")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    proc = resolve(cfg, cfg["data"]["processed_dir"])
    assets = [n for n in cfg["data"]["assets"] if (proc / f"{n}.parquet").exists()] if a.asset == "all" else [a.asset]
    for asset in assets:
        run_asset(asset, cfg, a.jobs, a.base_only_pit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
