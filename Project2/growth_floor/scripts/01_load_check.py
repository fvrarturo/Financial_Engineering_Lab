#!/usr/bin/env python
"""Spec 01 entry point: parse raw French files, build asset frames, run data checks.

Outputs
-------
data/raw/MANIFEST.json                     file hashes, sizes, download dates, date ranges
data/processed/{asset}.parquet             asset frames (r, rf, x, rf_ann)
reports/data_check/data_check.json         machine-readable checks (PASS/WARN/FAIL)
reports/data_check/data_check.md           human-readable report
reports/data_check/figures/...             figures

Exit code is 1 if any check is FAIL, else 0.

Usage:  python scripts/01_load_check.py [--config config.yaml]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gf import checks as C  # noqa: E402
from gf.data import load_asset, load_config, resolve, save_asset_frame, source_path  # noqa: E402
from gf.io_french import file_manifest_entry, read_french  # noqa: E402


# --------------------------------------------------------------------------- helpers
def jsonable(o):
    if isinstance(o, dict):
        return {str(k): jsonable(v) for k, v in o.items() if not str(k).startswith("_")}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return o


def pct(v, nd=2):
    return "n/a" if v is None or (isinstance(v, float) and not np.isfinite(v)) else f"{v * 100:.{nd}f}%"


def md_table(rows: list[list], header: list[str]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


# --------------------------------------------------------------------------- manifest
def write_manifest(cfg: dict) -> dict:
    path = resolve(cfg, cfg["data"]["manifest"])
    previous = {}
    if path.exists():
        previous = {e["file"]: e for e in json.loads(path.read_text()).get("files", [])}
    entries, missing = [], []
    for sid, s in cfg["data"]["sources"].items():
        p = source_path(cfg, sid)
        if not p.exists():
            if s.get("required", True):
                raise FileNotFoundError(f"Required raw file missing: {p}")
            missing.append(s["file"])
            continue
        e = file_manifest_entry(p, previous.get(p.name))
        e["source_id"] = sid
        entries.append(e)
    manifest = {
        "note": "download_date defaults to the file mtime. Overwrite it with the real download date "
                "and set download_date_source to 'user'; it is preserved while the sha256 is unchanged.",
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "files": entries, "optional_files_missing": missing,
    }
    path.write_text(json.dumps(manifest, indent=2))
    return manifest


# --------------------------------------------------------------------------- figures
def make_figures(frame: pd.DataFrame, name: str, cfg: dict, figdir: Path) -> dict:
    fc, N = cfg["checks"]["figures"], cfg["trading_days_per_year"]
    figdir.mkdir(parents=True, exist_ok=True)
    out = {}

    def save(fig, key):
        p = figdir / f"{key}.png"
        fig.tight_layout()
        fig.savefig(p, dpi=fc["dpi"])
        plt.close(fig)
        out[key] = str(p.relative_to(resolve(cfg, cfg["checks"]["report_dir"])))

    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(np.log1p(frame["r"]).cumsum(), lw=0.8, label="asset (total return)")
    ax.plot(np.log1p(frame["rf"]).cumsum(), lw=0.8, label="cash (daily rf)")
    ax.set(title=f"{name}: cumulative log wealth", ylabel="log wealth")
    ax.legend()
    save(fig, "cum_log_wealth")

    w = fc["rolling_vol_days"]
    fig, ax = plt.subplots(figsize=(9, 3.5))
    ax.plot(frame["x"].rolling(w).std() * np.sqrt(N), lw=0.6)
    ax.set(title=f"{name}: rolling {w}-day annualised vol of x", ylabel="vol")
    save(fig, "rolling_vol")

    fig, ax = plt.subplots(figsize=(9, 3.5))
    ax.plot(frame["rf_ann"], lw=0.8, label="rf_ann (monthly file)")
    ax.plot(frame["rf"].rolling(21).mean() * N, lw=0.5, alpha=0.7, label="daily rf × N (21d avg)")
    ax.set(title=f"{name}: annualised short rate", ylabel="rate")
    ax.legend()
    save(fig, "rf_ann")

    x = frame["x"].dropna()
    fig, ax = plt.subplots(figsize=(7, 4))
    dens, _, _ = ax.hist(x, bins=fc["hist_bins"], density=True, alpha=0.6, label="daily x")
    grid = np.linspace(x.min(), x.max(), 600)
    mu, sd = x.mean(), x.std()
    ax.plot(grid, np.exp(-0.5 * ((grid - mu) / sd) ** 2) / (sd * np.sqrt(2 * np.pi)), "r-", lw=1,
            label=f"normal (μ={mu:.4f}, σ={sd:.4f})")
    ax.set_yscale("log")
    ax.set_ylim(1e-3, dens.max() * 3)
    ax.set(title=f"{name}: daily log excess returns", xlabel="x")
    ax.legend()
    save(fig, "hist_x")

    w = fc["rolling_return_days"]
    thr = cfg["checks"]["summary"]["loss_threshold"]
    fig, ax = plt.subplots(figsize=(9, 3.5))
    ax.plot(np.expm1(np.log1p(frame["r"]).rolling(w).sum()), lw=0.6)
    ax.axhline(thr, color="r", lw=0.8, ls="--", label=f"{thr:.0%}")
    ax.set(title=f"{name}: rolling {w}-day cumulative total return", ylabel="return")
    ax.legend()
    save(fig, "rolling_return")
    return out


def rf_figure(rf_res: dict, cfg: dict, figdir: Path) -> str:
    s = rf_res["_series"]
    figdir.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(2, 1, figsize=(9, 5), sharex=True)
    t = s.index.to_timestamp()
    axes[0].plot(t, s["monthly"], lw=0.7, label="monthly RF")
    axes[0].plot(t, s["compounded_daily"], lw=0.7, alpha=0.7, label="prod(1+daily RF)-1")
    axes[0].legend()
    axes[0].set_title("RF: monthly file vs daily compounded")
    axes[1].plot(t, s["diff"] * 1e4, lw=0.6)
    axes[1].set_ylabel("diff (bp/month)")
    fig.tight_layout()
    p = figdir / "rf_daily_vs_monthly.png"
    fig.savefig(p, dpi=cfg["checks"]["figures"]["dpi"])
    plt.close(fig)
    return str(p.relative_to(resolve(cfg, cfg["checks"]["report_dir"])))


def crosscheck_figure(diff: pd.Series, name: str, cfg: dict, figdir: Path) -> str:
    figdir.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(9, 3))
    ax.plot(diff.index.to_timestamp(), diff * 100, lw=0.6)
    thr = cfg["checks"]["monthly_crosscheck"]["flag_abs_diff"] * 100
    for y in (thr, -thr):
        ax.axhline(y, color="r", ls="--", lw=0.7)
    ax.set(title=f"{name}: compounded daily − monthly total return", ylabel="pp")
    fig.tight_layout()
    p = figdir / "monthly_crosscheck.png"
    fig.savefig(p, dpi=cfg["checks"]["figures"]["dpi"])
    plt.close(fig)
    return str(p.relative_to(resolve(cfg, cfg["checks"]["report_dir"])))


# --------------------------------------------------------------------------- per-asset checks
def run_asset(name: str, cfg: dict, figroot: Path) -> dict:
    res = {"asset": name, "checks": {}, "figures": {}}
    frame, info = load_asset(cfg, name)
    res["parse"] = info["parse"]
    res["spec"] = info["spec"]
    ch = res["checks"]
    try:
        ch["units"] = C.check_units(frame["r"], cfg)
    except C.UnitsError as e:
        print(f"\n!!!!!!!! {name}: {e}\n", file=sys.stderr)
        ch["units"] = {"status": C.FAIL, "error": str(e), "notes": [str(e)]}
        res["status"] = C.FAIL
        return res  # nothing downstream is meaningful with wrong units
    ch["coverage"] = C.check_coverage(frame, cfg)
    ch["dates"] = C.check_dates(frame, cfg)
    ch["missing"] = C.check_missing(frame, info, cfg)
    ch["extremes"] = C.check_extremes(frame, cfg)
    figdir = figroot / name
    mc = info["spec"].get("monthly_check")
    if mc:
        mdf, mmeta = read_french(source_path(cfg, mc["source"]), section=mc.get("section"))
        monthly_total = mdf[mc["column"]] + (mdf[cfg["data"]["rf"]["monthly"]["column"]] if mc.get("add_rf") else 0)
        ch["monthly_crosscheck"] = C.check_daily_vs_monthly(frame["r"], monthly_total, cfg)
        res["figures"]["monthly_crosscheck"] = crosscheck_figure(ch["monthly_crosscheck"]["_series"], name, cfg, figdir)
    ch["summary"] = C.summary_stats(frame, cfg)
    res["figures"].update(make_figures(frame, name, cfg, figdir))
    out = save_asset_frame(cfg, name, frame)
    res["parquet"] = str(out.relative_to(Path(cfg["_root"])))
    res["n_rows"] = int(len(frame))
    res["status"] = C._worst(*(c["status"] for c in ch.values()))
    return res


# --------------------------------------------------------------------------- report
def render_md(report: dict, cfg: dict) -> str:
    L = []
    L.append("# Data check report (Spec 01)\n")
    L.append(f"Generated {report['generated']} by `scripts/01_load_check.py`. "
             f"Overall status: **{report['overall_status']}**.\n")
    L.append("All returns are in decimal units. `r` = simple total return, `rf` = daily simple risk-free "
             "return, `x` = log(1+r) − log(1+rf), `rf_ann` = (1+rf_monthly)^12 − 1.\n")

    L.append("## Raw files (MANIFEST)\n")
    L.append(md_table([[e["file"], e["sha256"][:16] + "…", e["size_bytes"], e["download_date"],
                        e["download_date_source"], e["first_date"], e["last_date"],
                        "; ".join(f"{s['title']} ({s['rows']})" for s in e["sections"])]
                       for e in report["manifest"]["files"]],
                      ["file", "sha256", "bytes", "downloaded", "date source", "first", "last", "sections (rows)"]))
    if report["manifest"]["optional_files_missing"]:
        L.append(f"\nOptional files not present: {report['manifest']['optional_files_missing']}")

    L.append("\n## Status summary\n")
    names = list(report["assets"])
    keys = ["units", "coverage", "dates", "missing", "extremes", "monthly_crosscheck", "summary"]
    rows = [[n, report["assets"][n]["status"]] + [report["assets"][n]["checks"].get(k, {}).get("status", "–")
                                                 for k in keys] for n in names]
    rows.append(["(global) rf", report["rf"]["status"]] + ["–"] * len(keys))
    L.append(md_table(rows, ["asset", "overall"] + keys))
    L.append("\n`–` = not applicable (e.g. no monthly file exists for industry portfolios).\n")

    # RF
    rf = report["rf"]
    L.append(f"## 7. Risk-free rate checks — {rf['status']}\n")
    for n in rf["notes"]:
        L.append(f"- {n}")
    d = rf["compounded_vs_monthly_abs_diff"]
    L.append(f"- |prod(1+rf_d) − 1 − rf_m| over {rf['n_months']} months: mean {d['mean'] * 1e4:.2f} bp, "
             f"median {d['median'] * 1e4:.2f} bp, p95 {d['p95'] * 1e4:.2f} bp, max {d['max'] * 1e4:.2f} bp "
             f"({d['max_month']}); mean signed diff {rf['mean_signed_diff'] * 1e4:.3f} bp.")
    L.append(f"- Months by number of distinct daily RF values: {rf['months_by_n_unique_values']}")
    L.append(f"- Negative RF days: {rf['negative_days_total']}; zero RF days: {rf['zero_days_total']}.\n")
    L.append(md_table([[k, v["days"], v["zero_days"], pct(v["zero_share"], 1), v["negative_days"]]
                       for k, v in rf["by_decade"].items()],
                      ["decade", "days", "zero days", "zero share", "negative days"]))
    if rf["flagged_months"]:
        fm = list(rf["flagged_months"].items())
        L.append(f"\nFlagged months (diff, {len(fm)} total, first 30 shown): "
                 + ", ".join(f"{k}: {v * 1e4:.1f} bp" for k, v in fm[:30]))
    L.append(f"\n![rf]({rf['figure']})\n")

    for n in names:
        L.append(render_asset(report["assets"][n], cfg))
    return "\n".join(L) + "\n"


def render_asset(a: dict, cfg: dict) -> str:
    L = [f"\n---\n\n# Asset `{a['asset']}` — {a['status']}\n"]
    sp = a["spec"]
    L.append(f"Source `{a['parse']['source']['file']}`, section “{a['parse']['source']['section_title']}”, "
             f"column `{sp['column']}`" + (" + RF (excess → total return)" if sp.get("add_rf") else " (total return)")
             + (f"; saved to `{a['parquet']}` ({a['n_rows']} rows)." if "parquet" in a else "."))
    ch = a["checks"]
    u = ch["units"]
    if u["status"] == "FAIL":
        L.append(f"\n**UNITS FAIL**: {u['error']}\n")
        return "\n".join(L)

    c = ch["coverage"]
    L.append(f"\n## 1. Coverage — {c['status']}\n")
    L.append(f"{c['first_date']} to {c['last_date']}, {c['n_days']} trading days.\n")
    if c["flagged_years"]:
        L.append(md_table([[f["year"], f["days"], f["saturdays"], f["reason"]] for f in c["flagged_years"]],
                          ["year", "days", "Saturdays", "reason"]))
    dpy = c["days_per_year"]
    L.append("\n<details><summary>Trading days per year</summary>\n\n"
             + ", ".join(f"{y}: {n}" for y, n in dpy.items()) + "\n\n</details>\n")
    for n in c["notes"]:
        L.append(f"- {n}")

    d = ch["dates"]
    L.append(f"\n## 2. Dates — {d['status']}\n")
    L.append(f"Strictly increasing: {d['strictly_increasing']}; unique: {d['unique']}; weekend dates after "
             f"{cfg['checks']['coverage']['saturday_trading_last_year']}: {len(d['weekend_dates_after_saturday_era'])}.\n")
    L.append(f"Gaps > {cfg['checks']['dates']['gap_report_days']} calendar days:\n")
    L.append(md_table([[g["from"], g["to"], g["calendar_days"]] for g in d["gaps"]], ["last date before", "next date", "calendar days"]))
    for n in d["notes"]:
        L.append(f"- {n}")

    m = ch["missing"]
    L.append(f"\n## 3. Missing values — {m['status']}\n")
    L.append(f"- Sentinel (-99.99 / -999) counts: source {m['sentinel_counts']['source']}; "
             f"rf_daily {m['sentinel_counts']['rf_daily']}; rf_monthly {m['sentinel_counts']['rf_monthly']}")
    L.append(f"- Missing in the columns used: {m['missing_in_used_columns']}")
    L.append(f"- NaN in asset frame: {m['nan_counts_asset_frame']}")
    for k, v in m["rows_dropped"].items():
        L.append(f"- Rows dropped — {k}: {len(v)}" + (f" ({', '.join(v[:10])}{' …' if len(v) > 10 else ''})" if v else ""))
    L.append(f"- rf_ann days filled from an earlier month (monthly rate not yet published): "
             f"{m['rf_ann_days_forward_filled_from_earlier_month']}; days with no monthly rate: {m['rf_ann_days_without_monthly_rate']}")
    for n in m["notes"]:
        L.append(f"- {n}")

    L.append(f"\n## 4. Units sanity — {u['status']}\n")
    L.append(f"median daily r = {u['median']:.5f}, std = {u['std']:.5f} (bounds in config `checks.units`).")

    e = ch["extremes"]
    L.append(f"\n## 5. Extreme days — {e['status']}\n")
    L.append(md_table([[t["rank"], t["date"], pct(t["r"]), pct(t["x"])] for t in e["top"]], ["rank", "date", "r", "x"]))
    L.append("\nExpected dates: " + ", ".join(f"{k} (r = {pct(e['expected_date_returns'][k])}, in top: {v})"
                                             for k, v in e["expected_dates_in_top"].items()))
    L.append(f"\nSuspicious |r| > {cfg['checks']['extremes']['suspicious_abs_return']:.0%}: "
             + (", ".join(f"{s['date']} ({pct(s['r'])}, {s['calendar_days_since_previous_obs']} calendar days after the previous observation)"
                       for s in e["suspicious"]) or "none"))
    for n in e["notes"]:
        L.append(f"- {n}")

    if "monthly_crosscheck" in ch:
        x = ch["monthly_crosscheck"]
        ad = x["abs_diff"]
        L.append(f"\n## 6. Daily vs monthly cross-check — {x['status']}\n")
        L.append(f"{x['n_months']} months ({x['first_month']} – {x['last_month']}). |diff|: mean {pct(ad['mean'], 3)}, "
                 f"median {pct(ad['median'], 3)}, p90 {pct(ad['p90'], 3)}, p95 {pct(ad['p95'], 3)}, p99 {pct(ad['p99'], 3)}, "
                 f"max {pct(ad['max'], 3)} ({ad['max_month']}). Mean signed diff {pct(x['mean_signed_diff'], 4)}. "
                 f"Months with |diff| > {pct(x['flag_threshold'], 1)}: {x['n_flagged']}.\n")
        if x["flagged"]:
            L.append(md_table([[f["month"], pct(f["daily_compounded"]), pct(f["monthly"]), pct(f["diff"], 3)] for f in x["flagged"]],
                              ["month", "daily compounded", "monthly", "diff"]))
        L.append(f"\n![crosscheck]({a['figures']['monthly_crosscheck']})\n")

    s = ch["summary"]
    thr = s["loss_threshold"]
    w1, w2 = s["windows"]
    L.append(f"\n## 8. Summary statistics — {s['status']}\n")
    L.append(f"Annualised with N = {cfg['trading_days_per_year']}. Skew/kurtosis are of daily x. Worst-window and "
             f"share columns use overlapping {w1}- and {w2}-day cumulative total returns (window assigned to the decade it ends in).\n")
    hdr = ["period", "days", "mean r", "mean x", "vol x", "skew x", "exkurt x", "min r", "max r",
           f"worst {w1}d", f"worst {w2}d", f"P({w1}d<{thr:.0%})", f"P({w2}d<{thr:.0%})", "mean rf_ann"]
    rows = []
    for k, r in s["table"].items():
        rows.append([k, r["n_days"], pct(r["ann_mean_r"]), pct(r["ann_mean_x"]), pct(r["ann_vol_x"]),
                     f"{r['skew_x']:.2f}", f"{r['exkurt_x']:.1f}", pct(r["min_r"]), pct(r["max_r"]),
                     pct(r[f"worst_{w1}d"], 1), pct(r[f"worst_{w2}d"], 1),
                     pct(r[f"share_{w1}d_below_{thr}"], 1), pct(r[f"share_{w2}d_below_{thr}"], 1),
                     pct(r["mean_rf_ann"])])
    L.append(md_table(rows, hdr))

    L.append("\n## 9. Figures\n")
    for k in ["cum_log_wealth", "rolling_vol", "rf_ann", "hist_x", "rolling_return"]:
        L.append(f"![{k}]({a['figures'][k]})")
    return "\n".join(L)


# --------------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    report_dir = resolve(cfg, cfg["checks"]["report_dir"])
    figroot = report_dir / "figures"
    report_dir.mkdir(parents=True, exist_ok=True)

    manifest = write_manifest(cfg)
    present = {e["source_id"] for e in manifest["files"]}
    report = {"generated": dt.datetime.now().isoformat(timespec="seconds"), "manifest": manifest, "assets": {}}

    rfd, rfm = cfg["data"]["rf"]["daily"], cfg["data"]["rf"]["monthly"]
    rf_d = read_french(source_path(cfg, rfd["source"]), section=rfd.get("section"))[0][rfd["column"]]
    rf_m = read_french(source_path(cfg, rfm["source"]), section=rfm.get("section"))[0][rfm["column"]]
    rf_res = C.check_rf(rf_d, rf_m, cfg)
    rf_res["figure"] = rf_figure(rf_res, cfg, figroot)
    report["rf"] = rf_res

    for name, spec in cfg["data"]["assets"].items():
        if spec["source"] not in present:
            print(f"[skip] {name}: optional source {spec['source']} not present")
            continue
        print(f"[run ] {name}")
        report["assets"][name] = run_asset(name, cfg, figroot)

    statuses = [a["status"] for a in report["assets"].values()] + [rf_res["status"]]
    report["overall_status"] = C._worst(*statuses)
    (report_dir / "data_check.json").write_text(json.dumps(jsonable(report), indent=2))
    (report_dir / "data_check.md").write_text(render_md(report, cfg))

    print(f"\nWrote {report_dir / 'data_check.md'} and data_check.json")
    for n, a in report["assets"].items():
        print(f"  {n:10s} {a['status']}")
    print(f"  {'rf':10s} {rf_res['status']}")
    print(f"Overall: {report['overall_status']}")
    if report["overall_status"] == C.FAIL:
        print("FAIL: at least one check failed (see report).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
