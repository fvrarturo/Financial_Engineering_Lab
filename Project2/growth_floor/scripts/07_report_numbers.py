#!/usr/bin/env python
"""SPEC 05 §1: single source of every number in the report and the deck.

Reads only results files and writes
  results/report/numbers.json              every number quoted in prose (raw values; templates format them)
  results/report/tables/*.csv, *.md        every table
  results/deployment.csv                   deployment rule output (gf.deployment)
  results/report/inputs/*                  small extracts of large result files needed by the figures
                                           (policy slices, terminal-wealth subsamples), so the fast path works from a
                                           clone that does not contain the 66 MB policy files or the terminal arrays.

Two modes, chosen automatically:
  extract   when the large files exist (results/policies/*.npz, results/mc/terminal/): rebuild results/report/inputs/
  fast      otherwise: use the committed results/report/inputs/
numbers.json is a deterministic function of the committed results files (sorted keys, fixed float rounding).

Usage:  python scripts/07_report_numbers.py [--no-extract]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gf.data import load_config  # noqa: E402
from gf.deployment import deployment_table  # noqa: E402
from gf.metrics import add_ce_xcash  # noqa: E402

OUT = ROOT / "results" / "report"
TAB = OUT / "tables"
INP = OUT / "inputs"
SCRIPT = "scripts/07_report_numbers.py"

ASSETS = ["us_mkt", "ind_nodur", "ind_durbl", "ind_manuf", "ind_enrgy", "ind_hitec", "ind_telcm", "ind_shops",
          "ind_hlth", "ind_utils", "ind_other"]
STATES = ["calm", "normal", "elevated", "crisis", "extreme"]
HZ = [252, 504]
NEAR = 0.02   # "near the floor": K <= W_T < (1 + NEAR) K
ROBUST = ["base", "dgp_garch_t", "fhs", "param_unc", "persistent"]
STRESS = ["mu_low", "mu_zero", "fat_tails", "jump"]
# strategy keys used in the report -> (source strategy, tune_mode)
STRATS = {"dp": ("dp_premrobust", None), "cppi_gap": ("cppi_vol", "state"), "obpi": ("obpi_merton", "state"),
          "merton": ("merton_const", "none"), "cppi": ("cppi", "state"), "dp_base": ("dp_policy", None),
          "cash": ("cash", "none")}
LABEL = {"dp": "Premium-robust DP", "cppi_gap": "Gap-aware CPPI", "obpi": "OBPI", "merton": "No-floor Merton",
         "cppi": "CPPI", "dp_base": "DP (base λ*)", "cash": "Cash", "deployed": "Deployed"}


# --------------------------------------------------------------------------- helpers
def r6(x):
    """Deterministic float rounding for numbers.json."""
    if x is None:
        return None
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    if isinstance(x, (float, np.floating)):
        if not np.isfinite(x):
            return None
        return float(f"{float(x):.6g}")
    return x


def clean(o):
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return r6(o)


def pq(rel):
    return pd.read_parquet(ROOT / rel)


def write_table(name: str, df: pd.DataFrame, source: str, fmt: dict | None = None):
    """CSV (raw) + Markdown (formatted) table; the md ends with its source line."""
    TAB.mkdir(parents=True, exist_ok=True)
    df.to_csv(TAB / f"{name}.csv", index=False, float_format="%.6g")
    d = df.copy()
    for c, f in (fmt or {}).items():
        if c in d:
            d[c] = d[c].map(lambda v, f=f: "" if v is None or (isinstance(v, float) and not np.isfinite(v)) else f(v))
    cols = list(d.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for _, r in d.iterrows():
        lines.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
    (TAB / f"{name}.md").write_text("\n".join(lines) + "\n")
    (TAB / f"{name}.source").write_text(f"{SCRIPT} → results/report/tables/{name}.csv (from {source})")


P2 = lambda v: f"{100 * v:.2f}%"  # noqa: E731
P1 = lambda v: f"{100 * v:.1f}%"  # noqa: E731
BP = lambda v: f"{1e4 * v:+.0f}"  # noqa: E731
F2 = lambda v: f"{v:.2f}"  # noqa: E731
F3 = lambda v: f"{v:.3f}"  # noqa: E731


# --------------------------------------------------------------------------- load
def load_all(cfg):
    S = pq("results/mc/summary.parquet")
    parts = [S]
    for rel in ("results_dp/mc/summary.parquet", "results_dp_mu/mc/summary.parquet",
                "results_ind_extra/mc/summary.parquet"):
        p = ROOT / rel
        if p.exists():
            parts.append(pd.read_parquet(p).assign(src=rel))
    A = pd.concat(parts, ignore_index=True)
    # keep one row per (asset, strategy, tune_mode, state, T, scenario): SPEC 03 rows first (pairing check showed equal)
    A["tune_mode"] = A["tune_mode"].fillna("none")
    A = A.drop_duplicates(["asset", "strategy", "tune_mode", "state", "T_days", "scenario"], keep="first")
    cash = A[A.strategy == "cash"]
    A = add_ce_xcash(A, cash)
    return A


def pick(A, key, asset=None):
    s, tm = STRATS[key]
    x = A[A.strategy == s]
    if tm is not None:
        x = x[x.tune_mode == tm]
    if asset is not None:
        x = x[x.asset == asset]
    return x


def cell_table(A, key, asset, deployed_key=None):
    """One row per named state x T: base / worst over robust set / stress breaches, CE - cash, diagnostics."""
    x = pick(A, key, asset)
    x = x[x.state.isin(STATES) & x.T_days.isin(HZ)]
    out = []
    for (st, T), g in x.groupby(["state", "T_days"]):
        g = g.set_index("scenario")
        if "base" not in g.index:
            continue
        b = g.loc["base"]
        rob = g.loc[[s for s in ROBUST if s in g.index]]
        row = {"asset": asset, "strategy": deployed_key or key, "source_strategy": key, "state": st, "T": T // 252,
               "T_days": T, "sigma0": b.sigma0, "n_paths": int(b.n_paths),
               "breach_base": b.breach, "breach_base_lo": b.breach_lo, "breach_base_hi": b.breach_hi,
               "n_robust": len(rob), "breach_worst": rob.breach.max(), "worst_scen": rob.breach.idxmax(),
               "breach_worst_hi": rob.loc[rob.breach.idxmax(), "breach_hi"],
               "ce_xcash": b.ce_xcash, "ce_ann": b.ce_ann, "ce_ann_cash": b.ce_ann_cash,
               "phi_avg": b.phi_avg, "turnover": b.turnover, "locked_cash_share": b.locked_cash_share,
               "q1": b["q1%"], "q5": b["q5%"], "q25": b["q25%"], "q50": b["q50%"], "q75": b["q75%"], "q95": b["q95%"],
               "mean_W": b["mean"], "gap_share": b.breach_gap_share, "gradual_share": b.breach_gradual_share,
               "rate_share": b.breach_rate_share, "n_breach_base": b.n_breach, "knob": b.knob}
        for s in STRESS:
            row[f"breach_{s}"] = g.loc[s, "breach"] if s in g.index else np.nan
            row[f"ce_xcash_{s}"] = g.loc[s, "ce_xcash"] if s in g.index else np.nan
        out.append(row)
    df = pd.DataFrame(out)
    if len(df):
        df["state"] = pd.Categorical(df.state, STATES, ordered=True)
        df = df.sort_values(["state", "T"]).reset_index(drop=True)
        df["state"] = df.state.astype(str)
    return df


# --------------------------------------------------------------------------- figure inputs (extract mode)
def extract_inputs(cfg, A, dep):
    INP.mkdir(parents=True, exist_ok=True)
    # 1. policy slices: premium-robust DP, us_mkt, lam* of normal T=1 and T=2, tau in {252, 126, 21}
    pol = ROOT / "results" / "policies" / "dp_premrobust_us_mkt.npz"
    z = np.load(pol, allow_pickle=True)
    lam, idx = z["lam"], z["lam_star_idx"]
    sig = z["lam_star_sigma"]
    j_norm = int(np.argmin(np.abs(sig - sig[1])))
    taus = [252, 126, 21]
    slices = {}
    for ti, T in enumerate(z["lam_star_T"]):
        li = int(idx[ti, j_norm])
        for t in taus:
            if t <= T:
                slices[f"phi_T{int(T)}_tau{t}"] = z["phi_grid"][z["phi_idx"][li, t]].astype(np.float32)
                slices[f"lam_T{int(T)}"] = np.float64(lam[li])
    np.savez_compressed(INP / "policy_us_mkt.npz", y=z["y"], h=z["h"], **slices,
                        lam_star_idx=idx, lam_star_sigma=sig, lam_star_T=z["lam_star_T"], lam=lam, q=z["q"],
                        r0_ann=z["r0_ann"])
    # 2. terminal wealth subsamples for CDF figures and near-floor probabilities (all assets, named states, base)
    term = {}
    for a in ASSETS:
        keys = ["dp", "cppi_gap", "obpi", "merton", "cash"]
        for k in keys:
            x = pick(A, k, a)
            x = x[x.state.isin(STATES) & x.T_days.isin(HZ) & (x.scenario == "base")]
            for _, r in x.iterrows():
                root = ROOT / (r.src.split("/mc/")[0] if isinstance(r.get("src"), str) else "results")
                f = root / r.terminal_file
                if not f.exists():
                    continue
                W = np.load(f, mmap_mode="r")
                W = np.asarray(W[int(r.terminal_row)] if W.ndim == 2 else W, dtype=np.float32)
                term[f"{a}|{k}|{r.state}|{int(r.T_days)}"] = W
    np.savez_compressed(INP / "terminal_base.npz", **term)
    # 3. frontier sweep + DP lambda frontier (us_mkt)
    fr = pq("results/mc/frontier.parquet")
    fr.to_parquet(INP / "frontier_us_mkt.parquet", index=False)
    v6 = ROOT / "results" / "policies" / "cache" / "v6frontier_us_mkt.parquet"
    if v6.exists():
        cols = ["asset", "state", "T_days", "scenario", "knob", "lam_idx", "breach", "breach_lo", "breach_hi",
                "ce_ann", "phi_avg", "n_paths"]
        pd.read_parquet(v6)[cols].to_parquet(INP / "dp_lambda_frontier_us_mkt.parquet", index=False)
    # 4. CPPI soft lock-in diagnostic inputs: already in terminal_base


# --------------------------------------------------------------------------- main build
def build(cfg, do_extract: bool):
    N: dict = {}
    A = load_all(cfg)
    pr = cfg["problem"]
    N["mandate"] = {"W0": pr["W0"], "K": pr["K"], "gamma": pr["gamma"], "breach_target": pr["breach_target"],
                    "T_min_years": min(pr["horizons_days"]) / 252, "T_max_years": max(pr["horizons_days"]) / 252,
                    "horizons_days": pr["horizons_days"], "K_over_W0": pr["K"] / pr["W0"]}
    N["settings"] = {"tune_target": cfg["tune"]["target_breach"], "floor_alpha": cfg["strategies"]["floor"]["alpha"],
                     "n_paths_eval": cfg["mc"]["n_paths"], "n_paths_eval_ind": cfg["mc"]["n_paths_industries"],
                     "n_paths_tune": cfg["tune"]["n_paths"], "n_paths_tune_ind": cfg["tune"]["n_paths_industries"],
                     "master_seed": cfg["mc"]["master_seed"], "model_seed": cfg["model"]["seed"],
                     "robust_set": ROBUST, "dp_calib_set": cfg["dp_premrobust"]["calib_set"],
                     "lam_grid": cfg["dp"]["lam_grid"], "sigma_cap_mult": cfg["engine"]["sigma_cap_mult"],
                     "fallback_start": cfg["estimation"]["fallback_start"],
                     "fallback_max_persistence": cfg["estimation"]["fallback_rule"]["max_persistence"],
                     "fallback_min_draw_valid": cfg["estimation"]["fallback_rule"]["min_draw_valid_share"],
                     "locked_phi": cfg["engine"]["locked_phi"], "locked_days": cfg["engine"]["locked_days"],
                     "premium_shrink_weight": cfg["model"]["premium"]["shrink_weight"]}
    sc = cfg["mc"]["scenarios"]
    N["settings"]["jump_prob"] = sc["jump"]["jump_prob_per_year"]
    N["settings"]["jump_return"] = sc["jump"]["jump_return"]
    N["settings"]["fat_nu_min"] = sc["fat_tails"]["nu_min"]
    N["settings"]["mu_low_mult"] = sc["mu_low"]["premium_mult"]
    N["settings"]["gap_report_days"] = cfg["checks"]["coverage"]["gap_report_days"] if "gap_report_days" in cfg["checks"].get("coverage", {}) else cfg["checks"]["dates"]["gap_report_days"]
    N["settings"]["terminal_subsample"] = cfg["mc"]["terminal_subsample"]
    N["settings"]["near_floor_band"] = NEAR
    N["settings"]["bt_refit_years"] = cfg["backtest"]["refit_every_days"] / 252
    n = cfg["mc"]["n_paths"]
    N["settings"]["mc_se_2pct_50k"] = float(np.sqrt(0.02 * 0.98 / n))
    N["settings"]["mc_se_2pct_20k"] = float(np.sqrt(0.02 * 0.98 / cfg["mc"]["n_paths_industries"]))

    # ---- deployment rule
    dep = deployment_table(ASSETS, ROOT)
    dep.to_csv(ROOT / "results" / "deployment.csv", index=False)
    write_table("deployment", dep[["asset", "flag_fallback_window", "fallback_reasons", "valid_draw_share",
                                   "flag_valid_draws", "flag_pit_reject", "pit_rejections", "chosen_strategy"]],
                "gf.deployment.deployment_table → results/deployment.csv", {"valid_draw_share": P1})
    N["deploy"] = {"choice": dict(zip(dep.asset, dep.chosen_strategy)),
                   "to_cppi": [a for a, c in zip(dep.asset, dep.chosen_strategy) if c == "cppi_vol"],
                   "to_dp": [a for a, c in zip(dep.asset, dep.chosen_strategy) if c == "dp_premrobust"],
                   "flags": dep.set_index("asset").to_dict(orient="index")}
    N["deploy"]["n_to_cppi"] = len(N["deploy"]["to_cppi"])
    N["deploy"]["n_assets"] = len(ASSETS)

    if do_extract:
        extract_inputs(cfg, A, dep)

    # ---- per-asset cell tables for each strategy + deployed
    cells = {}
    for a in ASSETS:
        for k in ["dp", "cppi_gap", "obpi", "merton", "cppi", "dp_base"]:
            t = cell_table(A, k, a)
            if len(t):
                cells[(a, k)] = t
        dk = "dp" if N["deploy"]["choice"][a] == "dp_premrobust" else "cppi_gap"
        if (a, dk) in cells:
            cells[(a, "deployed")] = cells[(a, dk)].assign(strategy="deployed")
    allcells = pd.concat(cells.values(), ignore_index=True)
    allcells.to_csv(TAB / "all_cells.csv", index=False, float_format="%.6g") if TAB.exists() else None
    N["dp_available"] = sorted({a for (a, k) in cells if k == "dp"})
    N["dp_missing"] = [a for a in ASSETS if a not in N["dp_available"]]

    def cellnums(a, k):
        t = cells.get((a, k))
        if t is None:
            return None
        d = {}
        for _, r in t.iterrows():
            d.setdefault(r.state, {})[f"T{int(r['T'])}"] = {c: r[c] for c in t.columns
                                                          if c not in ("asset", "strategy", "source_strategy", "state",
                                                                       "T", "worst_scen")} | {
                "worst_scen": r.worst_scen}
        d["max"] = {c: float(t[c].max()) for c in ["breach_base", "breach_worst"] + [f"breach_{s}" for s in STRESS]
                    if c in t and t[c].notna().any()}
        d["min"] = {c: float(t[c].min()) for c in ["ce_xcash", "breach_base", "breach_worst"]}
        d["max"]["ce_xcash"] = float(t.ce_xcash.max())
        d["n_cells"] = len(t)
        d["n_mu_zero_over_2"] = int((t["breach_mu_zero"] > 0.02).sum()) if "breach_mu_zero" in t else None
        d["n_fat_tails_over_2"] = int((t["breach_fat_tails"] > 0.02).sum()) if "breach_fat_tails" in t else None
        return d

    N["cells"] = {a: {k: cellnums(a, k) for k in ["dp", "cppi_gap", "obpi", "merton", "cppi", "dp_base", "deployed"]
                      if (a, k) in cells} for a in ASSETS}

    # ---- executive summary table (us_mkt) + headline
    u = "us_mkt"
    rows = []
    for st in STATES:
        for T in HZ:
            for k in ["deployed", "cppi_gap", "obpi", "merton"]:
                t = cells[(u, k)]
                r = t[(t.state == st) & (t.T_days == T)].iloc[0]
                rows.append({"state": st, "T": T // 252, "strategy": LABEL[k], "breach_base": r.breach_base,
                             "breach_worst": r.breach_worst, "breach_mu_zero": r.breach_mu_zero,
                             "ce_xcash": r.ce_xcash})
    ex = pd.DataFrame(rows)
    write_table("exec_summary_us_mkt", ex, "results/mc, results_dp_mu/mc summary.parquet",
                {"breach_base": P2, "breach_worst": P2, "breach_mu_zero": P2, "ce_xcash": P2})
    cmp_ = []
    for st in STATES:
        for T in HZ:
            row = {"start (σ0)": f"{st} ({100 * cells[(u, 'dp')].query('state == @st').sigma0.iloc[0]:.0f}%)", "T": T // 252}
            for k in ["deployed", "cppi_gap", "obpi", "merton"]:
                r = ex[(ex.state == st) & (ex["T"] == T // 252) & (ex.strategy == LABEL[k])].iloc[0]
                row[LABEL[k]] = (f"{100 * r.breach_base:.2f} / {100 * r.breach_worst:.2f} / {100 * r.breach_mu_zero:.2f}"
                                 f" · **{100 * r.ce_xcash:+.2f}**")
            cmp_.append(row)
    write_table("exec_compact_us_mkt", pd.DataFrame(cmp_), "results/mc, results_dp_mu/mc summary.parquet")
    dp, cg, ob = cells[(u, "dp")], cells[(u, "cppi_gap")], cells[(u, "obpi")]
    best_simple = np.maximum(cg.ce_xcash.values, ob.ce_xcash.values)
    adv = dp.ce_xcash.values - best_simple
    N["headline"] = {"adv_best_simple_min": float(adv.min()), "adv_best_simple_max": float(adv.max()),
                     "adv_cppi_gap_min": float((dp.ce_xcash - cg.ce_xcash).min()),
                     "adv_cppi_gap_max": float((dp.ce_xcash - cg.ce_xcash).max()),
                     "adv_obpi_min": float((dp.ce_xcash - ob.ce_xcash).min()),
                     "adv_obpi_max": float((dp.ce_xcash - ob.ce_xcash).max()),
                     "dp_worst_max": float(dp.breach_worst.max()), "dp_base_max": float(dp.breach_base.max()),
                     "dp_ce_min": float(dp.ce_xcash.min()), "dp_ce_max": float(dp.ce_xcash.max()),
                     "dp_mu_zero_max": float(dp.breach_mu_zero.max()),
                     "dp_fat_max": float(dp.breach_fat_tails.max()),
                     "cg_worst_max": float(cg.breach_worst.max()), "ob_worst_max": float(ob.breach_worst.max()),
                     "n_cells": len(dp)}
    # deployed across all assets
    dep_cells = pd.concat([cells[(a, "deployed")] for a in ASSETS if (a, "deployed") in cells], ignore_index=True)
    N["headline"]["deployed_assets_covered"] = sorted(dep_cells.asset.unique().tolist())
    N["headline"]["deployed_worst_max_all"] = float(dep_cells.breach_worst.max())
    N["headline"]["deployed_worst_max_all_where"] = dep_cells.loc[dep_cells.breach_worst.idxmax(),
                                                                  ["asset", "state", "T"]].astype(str).tolist()
    N["headline"]["deployed_mu_zero_max_all"] = float(dep_cells.breach_mu_zero.max())
    N["headline"]["deployed_mu_zero_max_all_where"] = dep_cells.loc[dep_cells.breach_mu_zero.idxmax(),
                                                                    ["asset", "state", "T"]].astype(str).tolist()

    # ---- all states x horizons (appendix B), us_mkt, each strategy
    bt = pd.concat([cells[(u, k)].assign(strategy=LABEL[k]) for k in ["dp", "dp_base", "cppi_gap", "cppi", "obpi",
                                                                       "merton"]])
    write_table("us_mkt_all_cells", bt[["strategy", "state", "T", "breach_base", "breach_worst", "worst_scen",
                                        "breach_mu_low", "breach_mu_zero", "breach_fat_tails", "breach_jump",
                                        "ce_xcash", "phi_avg", "turnover"]],
                "results/mc, results_dp/mc, results_dp_mu/mc summary.parquet",
                {c: P2 for c in ["breach_base", "breach_worst", "breach_mu_low", "breach_mu_zero", "breach_fat_tails",
                                 "breach_jump", "ce_xcash"]} | {"phi_avg": F2, "turnover": F2})

    # ---- performance (Q2): DP us_mkt by state, from terminal subsamples
    tb = np.load(INP / "terminal_base.npz")
    K = pr["K"]
    perf = []
    for a in ASSETS:
        for k in ["dp", "cppi_gap", "obpi", "merton", "cash"]:
            for st in STATES:
                for T in HZ:
                    key = f"{a}|{k}|{st}|{T}"
                    if key not in tb.files:
                        continue
                    W = tb[key].astype(np.float64)
                    perf.append({"asset": a, "strategy": k, "state": st, "T": T // 252, "n_sub": len(W),
                                 "p_near_floor": float(np.mean((W >= K) & (W < (1 + NEAR) * K))),
                                 "p_below_floor_sub": float(np.mean(W < K)),
                                 "p_below_100": float(np.mean(W < pr["W0"])),
                                 "median": float(np.median(W)), "p10": float(np.quantile(W, 0.10)),
                                 "p90": float(np.quantile(W, 0.90))})
    perf = pd.DataFrame(perf)
    cdp = cells[(u, "dp")]
    pf = perf[(perf.asset == u) & (perf.strategy == "dp")].merge(
        cdp[["state", "T", "ce_xcash", "phi_avg", "turnover", "q5", "q50", "q95", "mean_W"]], on=["state", "T"])
    pf["state"] = pd.Categorical(pf.state, STATES, ordered=True)
    pf = pf.sort_values(["state", "T"])
    write_table("performance_dp_us_mkt",
                pf[["state", "T", "ce_xcash", "q5", "q50", "q95", "p_near_floor", "p_below_100", "phi_avg",
                    "turnover"]].assign(q5=lambda d: d.q5 / 1e6, q50=lambda d: d.q50 / 1e6, q95=lambda d: d.q95 / 1e6),
                "results_dp_mu/mc/summary.parquet + terminal subsamples (5k paths per cell)",
                {"ce_xcash": P2, "q5": F2, "q50": F2, "q95": F2, "p_near_floor": P1, "p_below_100": P1, "phi_avg": F2,
                 "turnover": F2})
    N["perf"] = {r.state: {} for r in pf.itertuples()}
    for r in pf.itertuples():
        N["perf"][r.state][f"T{r.T}"] = {"ce_xcash": r.ce_xcash, "q5": r.q5, "q50": r.q50, "q95": r.q95,
                                         "p_near_floor": r.p_near_floor, "p_below_100": r.p_below_100,
                                         "phi_avg": r.phi_avg, "turnover": r.turnover, "mean_W": r.mean_W}
    N["perf"]["phi_avg_min"] = float(pf.phi_avg.min())
    N["perf"]["phi_avg_max"] = float(pf.phi_avg.max())
    N["perf"]["turnover_min"] = float(pf.turnover.min())
    N["perf"]["turnover_max"] = float(pf.turnover.max())
    N["perf"]["near_floor_min"] = float(pf.p_near_floor.min())
    N["perf"]["near_floor_max"] = float(pf.p_near_floor.max())
    pz = np.load(INP / "policy_us_mkt.npz")
    N["policy"] = {"lam_T1_normal": float(pz["lam_T252"]), "lam_T2_normal": float(pz["lam_T504"]),
                   "r0": float(pz["r0_ann"]),
                   "lam_star_T1": [float(pz["lam"][i]) for i in pz["lam_star_idx"][0]],
                   "lam_star_T2": [float(pz["lam"][i]) for i in pz["lam_star_idx"][1]],
                   "floor_premium_T1": float(np.exp(-pz["q"][252]) - 1), "floor_premium_T2": float(np.exp(-pz["q"][504]) - 1)}
    # policy readings: phi at y=1.5 for normal / crisis sigma; phi just above the floor
    y, h = pz["y"], pz["h"]
    sg = np.sqrt(h * 252)
    P = pz["phi_T504_tau252"]
    def phi_at(yv, sv):
        i = int(np.argmin(np.abs(y - yv)))
        j = int(np.argmin(np.abs(np.log(sg) - np.log(sv))))
        return float(P[i, j])
    sts = {s["state_name"]: s["sigma0_ann"] for s in json.loads(
        (ROOT / "reports/model/us_mkt/model_report.json").read_text())["states"]}
    N["policy"]["phi_y15_normal"] = phi_at(1.5, sts["normal"])
    N["policy"]["phi_y15_crisis"] = phi_at(1.5, sts["crisis"])
    N["policy"]["phi_y105_normal"] = phi_at(1.05, sts["normal"])
    N["policy"]["phi_y12_normal"] = phi_at(1.2, sts["normal"])
    N["policy"]["phi_y12_elevated"] = phi_at(1.2, sts["elevated"])
    mu = json.loads((ROOT / "results/model/params_us_mkt.json").read_text())["premium"]["annual_excess_used"]
    N["policy"]["merton_normal"] = float(min(1.0, mu / (pr["gamma"] * sts["normal"] ** 2)))

    # ---- stress table (us_mkt) and failures
    st_rows = []
    for k in ["dp", "dp_base", "cppi_gap", "obpi", "cppi", "merton"]:
        t = cells[(u, k)]
        for T in HZ:
            g = t[t.T_days == T]
            row = {"strategy": LABEL[k], "T": T // 252, "worst_robust_max": g.breach_worst.max()}
            for s in STRESS:
                row[f"{s}_max"] = g[f"breach_{s}"].max()
                row[f"{s}_argmax"] = g.loc[g[f"breach_{s}"].idxmax(), "state"] if g[f"breach_{s}"].notna().any() else ""
            st_rows.append(row)
    stt = pd.DataFrame(st_rows)
    write_table("stress_us_mkt", stt, "results/mc, results_dp/mc, results_dp_mu/mc summary.parquet",
                {c: P2 for c in stt.columns if c.endswith("_max")})
    fails = []
    for k in ["dp", "cppi_gap", "obpi"]:
        t = cells[(u, k)]
        for s in STRESS + ["worst"]:
            col = f"breach_{s}"
            for _, r in t[t[col] > 0.02].iterrows():
                fails.append({"strategy": LABEL[k], "scenario": s, "state": r.state, "T": int(r["T"]), "breach": r[col]})
    fails = pd.DataFrame(fails, columns=["strategy", "scenario", "state", "T", "breach"])
    write_table("stress_failures_us_mkt", fails, "results/mc, results_dp_mu/mc summary.parquet", {"breach": P2})
    N["stress"] = {"n_fail": {LABEL[k]: int((fails.strategy == LABEL[k]).sum()) for k in ["dp", "cppi_gap", "obpi"]},
                   "dp_fail_cells": fails[fails.strategy == LABEL["dp"]].to_dict(orient="records"),
                   "cg_fail_cells": fails[fails.strategy == LABEL["cppi_gap"]].to_dict(orient="records")}
    for k in ["dp", "dp_base", "cppi_gap", "obpi", "merton"]:
        t = cells[(u, k)]
        N["stress"][k] = {s: float(t[f"breach_{s}"].max()) for s in STRESS}
        N["stress"][k]["worst"] = float(t.breach_worst.max())
    # mu_zero margin
    N["stress"]["dp_mu_zero_min_margin"] = float(0.02 - cells[(u, "dp")].breach_mu_zero.max())
    db = cells[(u, "dp_base")]
    N["stress"]["dp_base_mu_zero_max"] = float(db.breach_mu_zero.max())
    N["stress"]["dp_base_mu_zero_argmax"] = db.loc[db.breach_mu_zero.idxmax(), ["state", "T"]].astype(str).tolist()

    # ---- breach decomposition (pooled over robust set, named states, per T), DP + gap-aware CPPI, us_mkt
    dec = []
    for k in ["dp", "cppi_gap", "obpi"]:
        x = pick(A, k, u)
        x = x[x.state.isin(STATES) & x.T_days.isin(HZ) & x.scenario.isin(ROBUST)]
        for T, g in x.groupby("T_days"):
            nb = g.n_breach.sum()
            dec.append({"strategy": LABEL[k], "T": T // 252, "n_breaches": int(nb),
                        "gap": g.n_breach_gap.sum() / nb, "gradual": g.n_breach_gradual.sum() / nb,
                        "rate": g.n_breach_rate.sum() / nb})
    dec = pd.DataFrame(dec)
    write_table("breach_decomposition_us_mkt", dec, "results/mc, results_dp_mu/mc summary.parquet (robust set, named states)",
                {"gap": P1, "gradual": P1, "rate": P1})
    N["decomp"] = {f"{r.strategy}|T{r.T}": {"gap": r.gap, "gradual": r.gradual, "rate": r.rate, "n": r.n_breaches}
                   for r in dec.itertuples()}
    N["decomp_dp"] = {f"T{r.T}": {"gap": r.gap, "gradual": r.gradual, "rate": r.rate}
                      for r in dec[dec.strategy == LABEL["dp"]].itertuples()}

    # ---- worst-case heatmap data (deployed us_mkt: DP named states; CPPI-gap dense grid x all T for context)
    wc = pq("results/mc/worst_case.parquet")
    wcg = wc[(wc.asset == u) & (wc.strategy == "cppi_vol") & (wc.tune_mode == "state")]
    wcg = wcg[wcg.n_scenarios == len(ROBUST)]
    N["heatmap"] = {"cppi_gap_cells": int(len(wcg)), "cppi_gap_max": float(wcg.breach_worst.max()),
                    "cppi_gap_n_over_2": int((wcg.breach_worst > 0.02).sum()),
                    "cppi_gap_n_over_target": int((wcg.breach_worst > cfg["tune"]["target_breach"]).sum())}
    wcg.to_csv(INP / "worst_case_cppi_gap_us_mkt.csv", index=False, float_format="%.6g")

    # ---- industries (external validity)
    ind_rows = []
    for a in ASSETS:
        for k in ["deployed", "dp", "cppi_gap", "obpi"]:
            t = cells.get((a, k))
            if t is None:
                continue
            ind_rows.append({"asset": a, "strategy": LABEL[k] if k != "deployed" else
                             f"Deployed ({LABEL['dp'] if N['deploy']['choice'][a] == 'dp_premrobust' else LABEL['cppi_gap']})",
                             "n_cells": len(t), "worst_max": t.breach_worst.max(), "base_max": t.breach_base.max(),
                             "mu_zero_max": t.breach_mu_zero.max() if t.breach_mu_zero.notna().any() else np.nan,
                             "fat_tails_max": t.breach_fat_tails.max() if t.breach_fat_tails.notna().any() else np.nan,
                             "jump_max": t.breach_jump.max() if t.breach_jump.notna().any() else np.nan,
                             "ce_min": t.ce_xcash.min(), "ce_median": t.ce_xcash.median(), "ce_max": t.ce_xcash.max(),
                             "n_robust_scen": int(t.n_robust.min())})
    ind = pd.DataFrame(ind_rows)
    write_table("industries", ind, "results/mc, results_dp_mu/mc, results_ind_extra/mc summary.parquet",
                {c: P2 for c in ["worst_max", "base_max", "mu_zero_max", "fat_tails_max", "jump_max", "ce_min",
                                 "ce_median", "ce_max"]})
    dep_only = ind[ind.strategy.str.startswith("Deployed")].reset_index(drop=True)
    N["ind"] = {"deployed_worst_max": float(dep_only.worst_max.max()),
                "deployed_worst_argmax": dep_only.loc[dep_only.worst_max.idxmax(), "asset"],
                "deployed_mu_zero_max": float(dep_only.mu_zero_max.max()),
                "deployed_mu_zero_argmax": dep_only.loc[dep_only.mu_zero_max.idxmax(), "asset"],
                "deployed_fat_max": float(dep_only.fat_tails_max.max()),
                "deployed_fat_argmax": dep_only.loc[dep_only.fat_tails_max.idxmax(), "asset"],
                "deployed_ce_min": float(dep_only.ce_min.min()),
                "deployed_ce_min_where": dep_only.loc[dep_only.ce_min.idxmin(), "asset"],
                "n_assets_with_deployed": int(len(dep_only))}
    # DP misses across industries (mu_zero > 2% or worst > 2%) and whether the rule catches them
    misses = []
    for a in ASSETS:
        t = cells.get((a, "dp"))
        if t is None:
            continue
        for _, r in t.iterrows():
            for s in ["worst", "mu_zero"]:
                if r[f"breach_{s}"] > 0.02:
                    misses.append({"asset": a, "state": r.state, "T": int(r["T"]), "scenario": s,
                                   "breach": r[f"breach_{s}"], "rule_routes_to": N["deploy"]["choice"][a]})
    misses = pd.DataFrame(misses, columns=["asset", "state", "T", "scenario", "breach", "rule_routes_to"])
    write_table("dp_misses", misses, "results_dp_mu/mc summary.parquet + results/deployment.csv", {"breach": P2})
    # deployed-DP cells: thin zero-premium margins (within 2 SE of the limit) and fat-tail failures (stress, not robust set)
    thin, fat = [], []
    for a in N["deploy"]["to_dp"]:
        t_ = cells.get((a, "dp"))
        if t_ is None:
            continue
        se = np.sqrt(0.02 * 0.98 / t_.n_paths.iloc[0])
        for _, r in t_.iterrows():
            if 0.02 - 2 * se <= r.breach_mu_zero <= 0.02:
                thin.append({"asset": a, "state": r.state, "T": int(r["T"]), "breach": r.breach_mu_zero, "se": se})
            if r.breach_fat_tails > 0.02:
                fat.append({"asset": a, "state": r.state, "T": int(r["T"]), "breach": r.breach_fat_tails})
    N["ind"]["dp_thin_mu_zero"] = thin
    fat = pd.DataFrame(fat, columns=["asset", "state", "T", "breach"])
    write_table("dp_fat_tails_failures", fat, "results_dp_mu/mc/summary.parquet (deployed-DP assets)", {"breach": P2})
    N["ind"]["dp_fat_n"] = int(len(fat))
    N["ind"]["dp_fat_assets"] = sorted(fat.asset.unique().tolist())
    N["ind"]["dp_fat_max"] = float(fat.breach.max()) if len(fat) else None
    N["ind"]["dp_misses"] = misses.to_dict(orient="records")
    N["ind"]["n_dp_misses"] = len(misses)
    N["ind"]["n_dp_misses_caught"] = int((misses.rule_routes_to == "cppi_vol").sum())
    N["ind"]["n_dp_assets"] = len([a for a in ASSETS if (a, "dp") in cells])
    # CPPI/OBPI worst over all industries (SPEC 03 robust set)
    for k in ["cppi_gap", "obpi", "cppi"]:
        tt = pd.concat([cells[(a, k)] for a in ASSETS if a != u and (a, k) in cells])
        N["ind"][f"{k}_worst_max"] = float(tt.breach_worst.max())
        N["ind"][f"{k}_n_robust_min"] = int(tt.n_robust.min())
    # DP vs comparators CE in industries where both exist
    dif = []
    for a in ASSETS:
        if a == u or (a, "dp") not in cells:
            continue
        d, c, o = cells[(a, "dp")], cells[(a, "cppi_gap")], cells[(a, "obpi")]
        dif.append({"asset": a, "dp_minus_cg_min": (d.ce_xcash - c.ce_xcash).min(),
                    "dp_minus_cg_max": (d.ce_xcash - c.ce_xcash).max(),
                    "dp_minus_ob_min": (d.ce_xcash - o.ce_xcash).min(), "dp_minus_ob_max": (d.ce_xcash - o.ce_xcash).max(),
                    "n_dp_above_cg": int((d.ce_xcash.values > c.ce_xcash.values).sum()), "n": len(d)})
    dif = pd.DataFrame(dif)
    write_table("industries_dp_vs_comparators", dif, "results/mc, results_dp_mu/mc, results_ind_extra/mc",
                {c: BP for c in dif.columns if c.startswith("dp_minus")})
    N["ind"]["dp_vs_cg_min"] = float(dif.dp_minus_cg_min.min()) if len(dif) else None
    N["ind"]["dp_vs_cg_max"] = float(dif.dp_minus_cg_max.max()) if len(dif) else None
    N["ind"]["dp_above_cg_cells"] = int(dif.n_dp_above_cg.sum()) if len(dif) else None
    N["ind"]["dp_cells"] = int(dif.n.sum()) if len(dif) else None

    # ---- gap-aware CPPI negative CE - cash (industries): soft lock-in diagnostic
    cn = []
    for a in ASSETS:
        t = cells[(a, "cppi_gap")]
        for _, r in t.iterrows():
            key = f"{a}|cppi_gap|{r.state}|{int(r.T_days)}"
            W = tb[key].astype(np.float64) if key in tb.files else None
            cn.append({"asset": a, "state": r.state, "T": int(r["T"]), "sigma0": r.sigma0, "z": r.knob,
                       "cushion_daily_vol": 1.0 / r.knob, "ce_xcash": r.ce_xcash,
                       "locked_cash_share": r.locked_cash_share, "phi_avg": r.phi_avg,
                       "p_near_floor": float(np.mean((W >= K) & (W < (1 + NEAR) * K))) if W is not None else np.nan,
                       "median_W": r.q50, "mean_W": r.mean_W, "breach_base": r.breach_base})
    cn = pd.DataFrame(cn)
    neg = cn[cn.ce_xcash < 0].sort_values("ce_xcash")
    write_table("cppi_gap_negative_ce", neg.assign(median_W=lambda d: d.median_W / 1e6, mean_W=lambda d: d.mean_W / 1e6),
                "results/mc/summary.parquet + terminal subsamples",
                {"sigma0": P1, "z": F2, "cushion_daily_vol": P1, "ce_xcash": P2, "locked_cash_share": P1, "phi_avg": F2, "p_near_floor": P1,
                 "median_W": F2, "mean_W": F2, "breach_base": P2})
    pos = cn[cn.ce_xcash >= 0]
    N["cppi_neg"] = {"n_neg": int(len(neg)), "n_cells": int(len(cn)),
                     "worst": neg.iloc[0][["asset", "state", "T", "ce_xcash", "locked_cash_share", "p_near_floor",
                                           "median_W", "mean_W", "phi_avg"]].to_dict() if len(neg) else None,
                     "neg_locked_max": float(neg.locked_cash_share.max()) if len(neg) else None,
                     "neg_locked_min": float(neg.locked_cash_share.min()) if len(neg) else None,
                     "neg_near_min": float(neg.p_near_floor.min()) if len(neg) else None,
                     "neg_near_max": float(neg.p_near_floor.max()) if len(neg) else None,
                     "neg_states": sorted(neg.state.unique().tolist()),
                     "neg_z_min": float(neg.z.min()) if len(neg) else None, "neg_z_max": float(neg.z.max()) if len(neg) else None,
                     "neg_phi_max": float(neg.phi_avg.max()) if len(neg) else None,
                     "neg_mean_W_min": float(neg.mean_W.min()) if len(neg) else None,
                     "pos_near_median": float(pos.p_near_floor.median()),
                     "corr_ce_near": float(np.corrcoef(cn.ce_xcash, cn.p_near_floor)[0, 1]),
                     "corr_ce_locked": float(np.corrcoef(cn.ce_xcash, cn.locked_cash_share)[0, 1]),
                     "neg_deployed": sorted(set(neg.asset) & set(N["deploy"]["to_cppi"])),
                     "neg_cells_deployed": neg[neg.asset.isin(N["deploy"]["to_cppi"])][["asset", "state", "T", "ce_xcash"]]
                     .to_dict(orient="records")}
    N["cppi_neg"]["cushion_daily_vol_note"] = "m = 1/(z sigma_d): cushion daily vol = 1/z when phi < 1"

    # ---- model (us_mkt) and fallback
    pj = json.loads((ROOT / "results/model/params_us_mkt.json").read_text())
    mr = json.loads((ROOT / "reports/model/us_mkt/model_report.json").read_text())
    mc_ = pd.read_csv(ROOT / "results/model/model_comparison_us_mkt.csv")
    keep = mc_[mc_.gap_variant.isin(["scale"])] if "scale" in set(mc_.gap_variant) else mc_
    cols = [c for c in ["model", "gap_variant", "loglik", "bic", "nu", "persistence", "half_life_days", "uncond_vol_ann",
                        "PIT u<0.02 H252", "PIT u<0.02 H504", "VaR0.01 hit rate", "VaR0.001 hit rate"] if c in mc_]
    write_table("model_comparison_us_mkt", mc_[cols], "results/model/model_comparison_us_mkt.csv",
                {"loglik": lambda v: f"{v:,.0f}", "bic": lambda v: f"{v:,.0f}", "nu": F2, "persistence": lambda v: f"{v:.4f}",
                 "half_life_days": lambda v: f"{v:.0f}", "uncond_vol_ann": P1, "PIT u<0.02 H252": P1,
                 "PIT u<0.02 H504": P1, "VaR0.01 hit rate": P2, "VaR0.001 hit rate": P2})
    bic = {f"{r.model}|{r.gap_variant}": r.bic for r in mc_.itertuples()}
    prm = pj["params"]
    sty = mr["stylised"]
    N["model"] = {"params": prm, "nu": prm.get("nu"), "persistence": pj["persistence"], "half_life": pj["half_life_days"],
                  "uncond_vol": pj["uncond_vol_ann"], "bic": bic,
                  "premium": {k: mr["premium"][k] for k in ["raw_annual", "se_annual", "years", "annual_excess_used",
                                                            "shrink_weight", "t_stat_raw"]},
                  "rate": {k: mr["rate"]["windows"]["base"][k] for k in ["kappa", "theta", "s", "n_months", "start", "end",
                                                                         "half_life_years", "rho", "rho_prev_month",
                                                                         "se_kappa"]},
                  "states": {s["state_name"]: {"sigma0": s["sigma0_ann"], "r0": s["r0_ann"]} for s in mr["states"]},
                  "anchors": {s["state_name"].replace("anchor_", ""): s["sigma0_ann"] for s in mr["anchors"]},
                  "stylised": {"data_vol": sty["data"]["ann_vol"], "sim_vol": sty["sim"]["ann_vol"],
                               "data_exkurt": sty["data"]["exkurt"], "data_skew": sty["data"]["skew"],
                               "q2_252_data": sty["windows"]["252"]["cum_quantiles_data"]["0.02"],
                               "q2_252_sim": sty["windows"]["252"]["cum_quantiles_sim"]["0.02"],
                               "q2_504_data": sty["windows"]["504"]["cum_quantiles_data"]["0.02"],
                               "q2_504_sim": sty["windows"]["504"]["cum_quantiles_sim"]["0.02"]},
                  "fhs": mr["fhs"], "crash": mr["crash"]["full_sample_params"],
                  "window": mr["estimation_window"]}
    sub = pd.DataFrame(mr["subsamples"])
    write_table("param_stability_us_mkt", sub[["sample", "start", "end", "nobs", "nu", "persistence", "half_life_days",
                                               "uncond_vol_ann"]],
                "reports/model/us_mkt/model_report.json → subsamples",
                {"nu": F2, "persistence": lambda v: f"{v:.4f}", "half_life_days": lambda v: f"{v:.0f}", "uncond_vol_ann": P1})
    N["model"]["subsample_persistence_min"] = float(sub.persistence.min())
    N["model"]["subsample_persistence_max"] = float(sub.persistence.max())
    N["model"]["subsample_nu_min"] = float(sub.nu.min())
    N["model"]["subsample_nu_max"] = float(sub.nu.max())
    # PIT (base model) overall + buckets, us_mkt
    pit = pd.DataFrame(mr["pit_tables"]["gjr_t|scale|t"])
    pit2 = pit[(pit.level == 0.02)]
    write_table("pit_us_mkt", pit2[["group_type", "group", "H", "n_starts", "n_eff", "count", "freq", "ci_lo", "ci_hi",
                                    "verdict"]],
                "reports/model/us_mkt/model_report.json → pit_tables (base GJR-t, u < 2%)",
                {"n_eff": lambda v: f"{v:.0f}", "freq": P1, "ci_lo": P1, "ci_hi": P1})
    ov = pit2[pit2.group_type == "overall"].set_index("H")
    N["model"]["pit"] = {f"H{h}": {"freq": ov.loc[h, "freq"], "ci_lo": ov.loc[h, "ci_lo"], "ci_hi": ov.loc[h, "ci_hi"],
                                   "n_eff": ov.loc[h, "n_eff"]} for h in HZ}
    bk = pit2[pit2.group_type == "bucket"]
    N["model"]["pit_bucket_ci_width_min"] = float((bk.ci_hi - bk.ci_lo).min())
    N["model"]["pit_bucket_ci_width_max"] = float((bk.ci_hi - bk.ci_lo).max())
    N["model"]["pit_n_reject_us_mkt"] = int(pit[pit.verdict.isin(["too thin", "too fat"])].shape[0])
    el = pit2[(pit2.group == "elevated") & (pit2.H == 252)]
    N["model"]["pit_elevated_252"] = float(el.freq.iloc[0]) if len(el) else None
    # VaR backtest of t tail
    vb = json.loads((ROOT / "results/model/var_backtest.json").read_text())
    N["model"]["var_backtest"] = vb.get("us_mkt", vb) if isinstance(vb, dict) else None
    # fallback table
    fb = []
    for a in ASSETS:
        p = json.loads((ROOT / f"results/model/params_{a}.json").read_text())
        r = json.loads((ROOT / f"reports/model/{a}/model_report.json").read_text())
        f = p["fallback"]
        ovr = [x for x in r["pit_tables"]["gjr_t|scale|t"] if x["group_type"] == "overall" and x["level"] == 0.02]
        fb.append({"asset": a, "persistence_full": f["persistence_full"], "valid_draws_full": f["draw_valid_share_full"],
                   "fallback": f["triggered"], "start_used": r["estimation_window"]["start"],
                   "persistence_used": p["persistence"], "half_life_used": p["half_life_days"],
                   "valid_draws_used": r["param_draws"]["acceptance_rate"], "nu": p["params"].get("nu"),
                   "premium_used": r["premium"]["annual_excess_used"], "uncond_vol": p["uncond_vol_ann"],
                   "pit2_252": next((x["freq"] for x in ovr if x["H"] == 252), np.nan),
                   "pit2_504": next((x["freq"] for x in ovr if x["H"] == 504), np.nan),
                   "sigma_crisis": next(s["sigma0_ann"] for s in r["states"] if s["state_name"] == "crisis"),
                   "sigma_extreme": next(s["sigma0_ann"] for s in r["states"] if s["state_name"] == "extreme")})
    fb = pd.DataFrame(fb)
    write_table("assets_model", fb, "results/model/params_{asset}.json, reports/model/{asset}/model_report.json",
                {"persistence_full": lambda v: f"{v:.5f}", "valid_draws_full": P1, "persistence_used": lambda v: f"{v:.4f}",
                 "half_life_used": lambda v: f"{v:.0f}", "valid_draws_used": P1, "nu": F2, "premium_used": P2,
                 "uncond_vol": P1, "pit2_252": P1, "pit2_504": P1, "sigma_crisis": P1, "sigma_extreme": P1})
    N["fallback"] = {"n_triggered": int(fb.fallback.sum()), "assets": fb[fb.fallback].asset.tolist(),
                     "premium_min": float(fb.premium_used.min()), "premium_max": float(fb.premium_used.max())}

    # ---- data (SPEC 01)
    dc = json.loads((ROOT / "reports/data_check/data_check.json").read_text())
    rf = dc["rf"]
    um = dc["assets"]["us_mkt"]["checks"]
    cov = um["coverage"]
    N["data"] = {"first": cov["first_date"], "last": cov["last_date"], "n_days": cov["n_days"],
                 "rf_resolution": rf["daily_resolution"], "rf_zero_days": rf["zero_days_total"],
                 "rf_n_negative_months": len(rf["negative_months_monthly_file"]),
                 "rf_negative_months_first": sorted(rf["negative_months_monthly_file"])[0],
                 "rf_negative_months_last": sorted(rf["negative_months_monthly_file"])[-1],
                 "rf_months_all_zero_daily": len(rf["months_all_zero_daily_but_positive_monthly"]),
                 "rf_max_comp_diff": rf["compounded_vs_monthly_abs_diff"]["max"],
                 "rf_n_flagged": rf["n_flagged"], "n_months": rf["n_months"],
                 "xcheck_mean": um["monthly_crosscheck"]["abs_diff"]["mean"],
                 "xcheck_max": um["monthly_crosscheck"]["abs_diff"]["max"],
                 "xcheck_max_month": um["monthly_crosscheck"]["abs_diff"]["max_month"],
                 "days_1968": cov["days_per_year"]["1968"],
                 "max_saturdays": max(cov["saturdays_per_year"].values()),
                 "sat_last_year": max(cov["saturdays_per_year"].keys()),
                 "n_gaps": len(um["dates"]["gaps"]),
                 "gap_max_days": max(g["calendar_days"] for g in um["dates"]["gaps"]),
                 "extreme_day": um["extremes"]["top"][0]["date"], "extreme_r": um["extremes"]["top"][0]["r"],
                 "full_ann_mean_x": um["summary"]["table"]["full"]["ann_mean_x"],
                 "full_ann_vol_x": um["summary"]["table"]["full"]["ann_vol_x"],
                 "full_exkurt": um["summary"]["table"]["full"]["exkurt_x"],
                 "worst_252": um["summary"]["table"]["full"]["worst_252d"],
                 "worst_252_end": um["summary"]["table"]["full"]["worst_252d_end"],
                 "share_252_below_5": um["summary"]["table"]["full"]["share_252d_below_-0.05"],
                 "manifest": [{k: f[k] for k in ("file", "sha256", "size_bytes", "first_date", "last_date",
                                                 "download_date")} for f in dc["manifest"]["files"]]}
    win = pd.DataFrame([
        {"estimate": "Equity model (GJR-GARCH-t), premium, σ states, PIT", "data": "us_mkt daily x = log(1+r) − log(1+rf)",
         "dates": f"{N['model']['window']['start']} to {N['model']['window']['end']}", "source": "params_us_mkt.json"},
        {"estimate": "Equity model, industries without fallback", "data": "ind_* daily (value-weighted)",
         "dates": f"{N['data']['first']} to {N['data']['last']}", "source": "params_{asset}.json"},
        {"estimate": "Equity model, fallback industries", "data": "ind_* daily (value-weighted)",
         "dates": f"{N['settings']['fallback_start']} to {N['data']['last']}", "source": "params_{asset}.json → fallback"},
        {"estimate": "Vasicek short-rate model, equity–rate ρ", "data": "monthly FF RF → rf_ann",
         "dates": f"{N['model']['rate']['start']} to {N['model']['rate']['end']} ({N['model']['rate']['n_months']} months)",
         "source": "model_report.json → rate"},
        {"estimate": "Cash accounting in backtests", "data": "daily FF RF", "dates": f"{N['data']['first']} to {N['data']['last']}",
         "source": "data/processed/{asset}.parquet"},
        {"estimate": "PIT test (expanding refits)", "data": "us_mkt daily, quarterly starts",
         "dates": "first fit after 10 years; refit every 252 days", "source": "results/model/pit_us_mkt.csv"},
        {"estimate": "Historical backtest", "data": "daily r, rf; investor refit every 5 years",
         "dates": "first refit after 10 years of data", "source": "results/backtest/summary.parquet"},
    ])
    write_table("data_windows", win, "results/model/*, reports/model/*, config.yaml")

    # ---- backtest (us_mkt), by bucket and decade, with n_eff
    b = pq("results/backtest/summary.parquet")
    bu = b[(b.asset == u) & b.T_days.isin(HZ) & (((b.strategy == "cppi_vol") & (b.tune_mode == "state")) |
                             ((b.strategy == "obpi_merton") & (b.tune_mode == "state")) |
                             (b.strategy == "merton_const") | (b.strategy == "cash"))].copy()
    cash_b = bu[bu.strategy == "cash"][["T_days", "group_type", "group", "ce_ann"]].rename(columns={"ce_ann": "ce_cash"})
    bu = bu[bu.strategy != "cash"].merge(cash_b, on=["T_days", "group_type", "group"], how="left")
    bu["ce_xcash"] = bu.ce_ann - bu.ce_cash
    bu["strategy"] = bu.strategy.map({"cppi_vol": LABEL["cppi_gap"], "obpi_merton": LABEL["obpi"],
                                      "merton_const": LABEL["merton"]})
    bt_tab = bu[["strategy", "T_days", "group_type", "group", "n_starts", "n_eff", "breach", "breach_lo", "breach_hi",
                 "ce_xcash"]].assign(T=lambda d: d.T_days // 252).drop(columns="T_days")
    bt_tab = bt_tab[bt_tab.group_type.isin(["overall", "bucket"])]
    write_table("backtest_us_mkt", bt_tab, "results/backtest/summary.parquet",
                {"n_eff": lambda v: f"{v:.0f}", "breach": P1, "breach_lo": P1, "breach_hi": P1, "ce_xcash": P2})
    btd = bu[bu.group_type == "decade"][["strategy", "T_days", "group", "n_eff", "breach", "breach_hi"]]
    write_table("backtest_decade_us_mkt", btd.assign(T=lambda d: d.T_days // 252).drop(columns="T_days"),
                "results/backtest/summary.parquet", {"n_eff": lambda v: f"{v:.0f}", "breach": P1, "breach_hi": P1})
    o = bu[bu.group_type == "overall"].set_index(["strategy", "T_days"])
    N["backtest"] = {f"{s}|T{T // 252}": {"breach": o.loc[(s, T), "breach"], "hi": o.loc[(s, T), "breach_hi"],
                                          "n_eff": o.loc[(s, T), "n_eff"], "n": o.loc[(s, T), "n_starts"],
                                          "ce_xcash": o.loc[(s, T), "ce_xcash"]} for s, T in o.index}
    cr = bu[(bu.group == "crisis")].set_index(["strategy", "T_days"])
    N["backtest"]["crisis_n_eff_T1"] = float(cr.loc[(LABEL["cppi_gap"], 252), "n_eff"])
    N["backtest"]["crisis_hi_T1"] = float(cr.loc[(LABEL["cppi_gap"], 252), "breach_hi"])
    N["backtest"]["dp_backtested"] = bool(((b.strategy == "dp_premrobust") | (b.strategy == "dp_policy")).any())

    # ---- validation (DP, us_mkt)
    v = json.loads((ROOT / "reports/dp/validation_us_mkt.json").read_text())
    N["validation"] = {"V1_pass": v["V1"]["pass"], "V4_pass": v["V4"]["pass"],
                       "V5_pass_rate_const": v["V5"]["pass_rate_const"], "V5_n_fail_base": v["V5"]["n_fail_base"],
                       "V5_n": v["V5"]["n"], "V6f_n_comparable": v["V6_frontier"]["n_comparable"],
                       "V6f_n_ge": v["V6_frontier"]["n_dp_ce_ge"],
                       "V6f_dce_min": min(c["d_ce_bp_per_year"] for c in v["V6_frontier"]["cells"] if c["comparable"]),
                       "V6f_dce_max": max(c["d_ce_bp_per_year"] for c in v["V6_frontier"]["cells"] if c["comparable"]),
                       "V4_dce": [c.get("d_ce_bp") for c in v["V4"]["cells"]] if "cells" in v["V4"] else None}

    # ---- dead ends that are in the results files: base-only tuning, untuned OBPI (us_mkt, named states, T in {1, 2})
    de = {}
    for s, tm in [("cppi", "state_base"), ("cppi_vol", "state_base"), ("obpi_merton", "state_base"),
                  ("obpi_merton", "untuned"), ("cppi", "state"), ("cppi_vol", "state"), ("obpi_merton", "state")]:
        x = A[(A.asset == u) & (A.strategy == s) & (A.tune_mode == tm) & A.state.isin(STATES) & A.T_days.isin(HZ)]
        rob = x[x.scenario.isin(ROBUST)].groupby(["state", "T_days"]).breach.max()
        base = x[x.scenario == "base"]
        de[f"{s}|{tm}"] = {"worst_max": float(rob.max()), "worst_min": float(rob.min()),
                           "base_max": float(base.breach.max()), "base_min": float(base.breach.min()),
                           "ce_mean": float(base.ce_xcash.mean())}
    N["dead_ends"] = de
    N["dead_ends"]["base_only_worst_min"] = min(de[f"{s}|state_base"]["worst_max"] for s in ["cppi", "cppi_vol", "obpi_merton"])
    N["dead_ends"]["base_only_worst_max"] = max(de[f"{s}|state_base"]["worst_max"] for s in ["cppi", "cppi_vol", "obpi_merton"])

    # ---- run sizes / reproducibility
    rm = json.loads((ROOT / "results/run_manifest.json").read_text())
    N["repro"] = {"git_commit_spec03": rm.get("git_commit"), "data_sha256": rm.get("data_sha256"),
                  "seeds": {a: {"tune": rm["assets"][a]["seed_tune"], "eval": rm["assets"][a]["seed_eval"]}
                            for a in ASSETS if a in rm["assets"]}}
    N["source"] = {"script": SCRIPT}
    return N


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-extract", action="store_true", help="never rebuild results/report/inputs/ (fast path)")
    a = ap.parse_args()
    cfg = load_config()
    can = (ROOT / "results/policies/dp_premrobust_us_mkt.npz").exists() and (ROOT / "results/mc/terminal").exists()
    do_extract = can and not a.no_extract
    TAB.mkdir(parents=True, exist_ok=True)
    N = build(cfg, do_extract)
    N = clean(N)
    (OUT / "numbers.json").write_text(json.dumps(N, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"wrote {OUT / 'numbers.json'} ({'extract' if do_extract else 'fast'} mode); tables in {TAB}")


if __name__ == "__main__":
    main()
