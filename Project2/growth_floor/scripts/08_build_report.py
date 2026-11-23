#!/usr/bin/env python
"""SPEC 05 §1: figures + report and deck rendering.

Inputs (read only): results/report/numbers.json, results/report/tables/*, results/report/inputs/* (all written by
scripts/07_report_numbers.py). Nothing here computes a number that is quoted in prose: figures plot result rows, and
the templates (report/templates/*.j2) pull every number from numbers.json or a table.

Pipeline: Jinja2 template → deliverables/report.md (editable source) → HTML (python-markdown) → PDF (headless Chromium via
Playwright). Deck: Jinja2 → deliverables/deck.html → PDF. Figures: deliverables/figures/*.png, shared by report and deck.

Usage:  python scripts/08_build_report.py [--no-pdf] [--no-figures]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
REP = ROOT / "results" / "report"
TAB = REP / "tables"
INP = REP / "inputs"
DEL = ROOT / "deliverables"
FIG = DEL / "figures"
TPL = ROOT / "report" / "templates"
S07 = "scripts/07_report_numbers.py"
S08 = "scripts/08_build_report.py"

# the app's strategy colours (app/app.py COLORS)
COL = {"dp": "#e377c2", "deployed": "#e377c2", "dp_base": "#9467bd", "cppi_gap": "#ff7f0e", "obpi": "#2ca02c",
       "merton": "#1f77b4", "cppi": "#d62728", "cash": "#7f7f7f"}
LAB = {"dp": "Premium-robust DP", "dp_base": "DP (base λ*)", "cppi_gap": "Gap-aware CPPI", "obpi": "OBPI",
       "merton": "No-floor Merton", "cppi": "CPPI", "cash": "Cash", "deployed": "Deployed"}
STATES = ["calm", "normal", "elevated", "crisis", "extreme"]


# --------------------------------------------------------------------------- figures
def style():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.titlesize": 11, "axes.labelsize": 10,
                         "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.alpha": 0.25,
                         "legend.frameon": False, "figure.dpi": 150, "savefig.bbox": "tight"})
    return plt


def figures(N):
    plt = style()
    from matplotlib.colors import LinearSegmentedColormap
    FIG.mkdir(parents=True, exist_ok=True)
    cells = pd.read_csv(TAB / "all_cells.csv")
    S, M = N["settings"], N["mandate"]
    u = cells[cells.asset == "us_mkt"]
    caps = {}

    # 1. frontier: CE - cash vs base breach, us_mkt normal, T = 1 and 2
    fr = pd.read_parquet(INP / "frontier_us_mkt.parquet")
    dl = pd.read_parquet(INP / "dp_lambda_frontier_us_mkt.parquet")
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.9), sharey=False)
    for ax, T in zip(axes, (252, 504)):
        cash = u[(u.state == "normal") & (u.T_days == T) & (u.source_strategy == "dp")].ce_ann_cash.iloc[0]
        f = fr[(fr.state == "normal") & (fr.T_days == T) & (fr.scenario == "base")]
        for s, k in [("cppi", "cppi"), ("cppi_vol", "cppi_gap"), ("obpi_merton", "obpi")]:
            g = f[(f.strategy == s) & (f.variant.isin(["", "conditional"]))].sort_values("breach")
            ax.plot(100 * g.breach, 100 * (g.ce_ann - cash), "-o", ms=3, color=COL[k], label=LAB[k] + " (knob sweep)")
        d = dl[(dl.state == "normal") & (dl.T_days == T)].sort_values("breach")
        ax.plot(100 * d.breach, 100 * (d.ce_ann - cash), "-s", ms=4, color=COL["dp"], label="DP (λ sweep)")
        for k in ["dp", "cppi_gap", "obpi"]:
            r = u[(u.state == "normal") & (u.T_days == T) & (u.source_strategy == k)].iloc[0]
            ax.plot(100 * r.breach_base, 100 * r.ce_xcash, "*", ms=14, color=COL[k], mec="k", mew=0.6)
        ax.axvline(2.0, color="k", lw=1, ls="--")
        ax.axvline(100 * N["settings"]["tune_target"], color="grey", lw=0.8, ls=":")
        ax.set_xlim(0, 6)
        ax.set_xlabel("floor breach, base scenario (%)")
        ax.set_title(f"normal start, T = {T // 252} year{'s' if T > 252 else ''}")
    axes[0].set_ylabel("CE − cash (%/yr, annualised)")
    axes[1].legend(loc="lower right", fontsize=8)
    fig.savefig(FIG / "frontier.png")
    plt.close(fig)
    caps["frontier"] = ("CE − cash against base-scenario breach for each strategy family (us_mkt, normal start). "
                        "Lines sweep each family's knob; stars are the robust-tuned versions actually evaluated. "
                        f"Dashed: the {100 * N['mandate']['breach_target']:.0f}% mandate; dotted: the "
                    f"{100 * N['settings']['tune_target']:.2f}% tuning target. "
                        f"Source: {S07} → results/report/inputs/frontier_us_mkt.parquet, dp_lambda_frontier_us_mkt.parquet")

    # 2. terminal wealth CDF
    tb = np.load(INP / "terminal_base.npz")
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.8), sharey=True)
    for ax, st in zip(axes, ("normal", "crisis")):
        for k in ["dp", "cppi_gap", "obpi", "merton", "cash"]:
            key = f"us_mkt|{k}|{st}|504"
            if key not in tb.files:
                continue
            W = np.sort(tb[key].astype(float)) / 1e6
            ax.plot(W, np.arange(1, len(W) + 1) / len(W), color=COL[k], lw=1.6 if k == "dp" else 1.1, label=LAB[k])
        ax.axvline(95, color="k", lw=1, ls="--")
        ax.axhline(0.02, color="grey", lw=0.8, ls=":")
        ax.set_xlim(80, 150)
        ax.set_xlabel("terminal wealth W_T ($M)")
        ax.set_title(f"{st} start, T = 2 years")
    axes[0].set_ylabel("P(W_T ≤ w)")
    axes[1].legend(loc="lower right", fontsize=8)
    fig.savefig(FIG / "cdf.png")
    plt.close(fig)
    S, M = N["settings"], N["mandate"]
    caps["cdf"] = (f"Distribution of terminal wealth, us_mkt, T = 2, base scenario ({S['terminal_subsample']:,}-path "
                   f"subsample of the {S['n_paths_eval']:,} evaluation paths). Dashed: the ${M['K'] / 1e6:.0f}M floor; "
                   f"dotted: {100 * M['breach_target']:.0f}% probability. "
                   f"Source: {S07} → results/report/inputs/terminal_base.npz")

    # 3. DP policy
    pz = np.load(INP / "policy_us_mkt.npz")
    y, h = pz["y"], pz["h"]
    sig = np.sqrt(h * 252)
    m = (y >= 0.85) & (y <= 2.0)
    st_sig = {s: N["model"]["states"][s]["sigma0"] for s in STATES}
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.9), sharey=True)
    for ax, tau in zip(axes, (252, 21)):
        P = pz[f"phi_T504_tau{tau}"][m]
        im = ax.pcolormesh(100 * sig, y[m], P, shading="nearest", cmap="viridis", vmin=0, vmax=1)
        ax.set_xscale("log")
        ax.axhline(1.0, color="w", lw=1, ls="--")
        for s, v in st_sig.items():
            if s != "extreme":
                ax.axvline(100 * v, color="w", lw=0.6, ls=":")
                ax.text(100 * v, 1.97, s, color="w", fontsize=7, rotation=90, va="top", ha="right")
        ax.set_xlabel("conditional volatility σ (%/yr, annualised; log scale)")
        ax.set_title(f"{tau} trading days to the horizon")
        ax.grid(False)
    axes[0].set_ylabel("cushion ratio y = W / floor PV")
    fig.colorbar(im, ax=axes, label="equity weight φ", fraction=0.04)
    fig.savefig(FIG / "policy.png")
    plt.close(fig)
    caps["policy"] = (f"Premium-robust DP policy for us_mkt at the λ* of the normal state, T = 2 (λ = {pz['lam_T504']:g}). "
                      "y = 1 (dashed) is the conservative floor PV. Dotted lines mark the named starting states' σ0. "
                      f"Source: {S07} → results/report/inputs/policy_us_mkt.npz (from results/policies/dp_premrobust_us_mkt.npz)")

    # 4. worst-case breach heatmap (state x T): deployed DP and gap-aware CPPI
    cmap = LinearSegmentedColormap.from_list("b", ["#f7fbff", "#c6dbef", "#fdd49e", "#ef6548", "#990000"])
    fig, axes = plt.subplots(1, 2, figsize=(8.6, 3.4))
    for ax, k in zip(axes, ("dp", "cppi_gap")):
        g = u[u.source_strategy == k].pivot_table(index="state", columns="T", values="breach_worst").reindex(STATES)
        Mx = 100 * g.values
        im = ax.imshow(Mx, cmap=cmap, vmin=0, vmax=4, aspect="auto")
        for i in range(Mx.shape[0]):
            for j in range(Mx.shape[1]):
                ax.text(j, i, f"{Mx[i, j]:.2f}%", ha="center", va="center", fontsize=9,
                        color="k" if Mx[i, j] < 2 else "w", fontweight="bold" if Mx[i, j] >= 2 else None)
        ax.set_xticks(range(Mx.shape[1]), [f"T = {c}" for c in g.columns])
        ax.set_yticks(range(len(STATES)),
                      [f"{s} ({100 * st_sig[s]:.0f}%)" for s in STATES] if k == "dp" else [""] * len(STATES))
        ax.set_title(LAB[k])
        ax.grid(False)
    cb = fig.colorbar(im, ax=axes, label="worst-case breach (%)", fraction=0.04)
    cb.ax.axhline(2.0, color="k", lw=2)
    fig.savefig(FIG / "heatmap.png")
    plt.close(fig)
    caps["heatmap"] = ("Worst-case breach over the robust model set (base, GARCH-t DGP, filtered historical simulation, "
                       f"parameter uncertainty, persistent vol), us_mkt, by starting σ0 and horizon; {S['n_paths_eval']:,} "
                       f"evaluation paths per cell. The colour bar's black line is the {100 * M['breach_target']:.0f}% mandate. "
                       f"Source: {S07} → results/report/tables/all_cells.csv")

    # 5. stress grouped bars (max over starting states)
    scen = [("breach_base", "base"), ("breach_worst", "robust\nworst"), ("breach_mu_low", "μ × 0.5"),
            ("breach_mu_zero", "μ = 0"), ("breach_fat_tails", "fat tails\n(ν = 3)"), ("breach_jump", "−20%\njumps")]
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6), sharey=True)
    for ax, T in zip(axes, (1, 2)):
        x = np.arange(len(scen))
        for i, k in enumerate(["dp", "cppi_gap", "obpi"]):
            g = u[(u.source_strategy == k) & (u["T"] == T)]
            vals = [100 * g[c].max() for c, _ in scen]
            ax.bar(x + (i - 1) * 0.27, vals, 0.27, color=COL[k], label=LAB[k])
        ax.axhline(2.0, color="k", lw=1, ls="--")
        ax.set_xticks(x, [s for _, s in scen], fontsize=8)
        ax.set_title(f"T = {T} year{'s' if T > 1 else ''}: max over the five starting states")
    axes[0].set_ylabel("floor breach (%)")
    axes[0].legend(fontsize=8)
    fig.savefig(FIG / "stress.png")
    plt.close(fig)
    caps["stress"] = ("Breach under the tuning scenarios and under stress scenarios held out of tuning, maximum over the "
                      f"five named starting states, us_mkt. Dashed: {100 * M['breach_target']:.0f}%. "
                      f"Source: {S07} → results/report/tables/all_cells.csv")

    # 6. industries: deployed strategy per asset
    ind = pd.read_csv(TAB / "industries.csv")
    d = ind[ind.strategy.str.startswith("Deployed")].copy()
    d["col"] = np.where(d.strategy.str.contains("DP"), COL["dp"], COL["cppi_gap"])
    order = list(d.asset)
    fig, ax = plt.subplots(figsize=(10, 3.4))
    x = np.arange(len(d))
    ax.bar(x - 0.2, 100 * d.worst_max, 0.4, color=d.col, label="worst case over robust set (max over cells)")
    ax.bar(x + 0.2, 100 * d.mu_zero_max.fillna(0), 0.4, color=d.col, alpha=0.45, hatch="//",
           label="μ = 0 stress (max over cells)")
    ax.axhline(2.0, color="k", lw=1, ls="--")
    ax.set_xticks(x, [a.replace("ind_", "") for a in order])
    ax.set_ylabel("floor breach (%)")
    from matplotlib.patches import Patch
    h1, l1 = ax.get_legend_handles_labels()
    ax.legend(h1 + [Patch(color=COL["dp"]), Patch(color=COL["cppi_gap"])],
              l1 + ["deployed: premium-robust DP", "deployed: gap-aware CPPI (rule fallback)"], fontsize=8, ncol=2,
              loc="upper left")
    ax.set_ylim(0, 3.2)
    fig.savefig(FIG / "industries.png")
    plt.close(fig)
    caps["industries"] = ("Deployed strategy on every asset (the rule's choice). Bars: maximum over the named-state × "
                          "horizon cells; industries at " + f"{S['n_paths_eval_ind']:,}" + " evaluation paths. "
                          "Assets without a μ = 0 bar were not evaluated under that stress. "
                          f"Source: {S07} → results/report/tables/industries.csv, results/deployment.csv")
    (FIG / "captions.json").write_text(json.dumps(caps, indent=1, ensure_ascii=False))
    return caps


# --------------------------------------------------------------------------- templates
def jinja_env():
    import jinja2
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TPL)), undefined=jinja2.StrictUndefined,
                             trim_blocks=True, lstrip_blocks=True)

    def pct(v, d=2, sign=False):
        if v is None:
            return "n/a"
        return f"{100 * v:{'+' if sign else ''}.{d}f}%"

    env.filters["pct"] = pct
    env.filters["pp"] = lambda v, d=2: f"{100 * v:.{d}f}"
    env.filters["bp"] = lambda v, sign=True: f"{1e4 * v:{'+' if sign else ''}.0f}"
    env.filters["musd"] = lambda v, d=1: f"${v / 1e6:,.{d}f}M"
    env.filters["f"] = lambda v, d=2: f"{v:,.{d}f}"
    env.filters["i"] = lambda v: f"{int(round(v)):,}"
    env.filters["sci"] = lambda v: f"{v:.3g}"
    env.filters["name"] = lambda a: a.replace("ind_", "")
    env.filters["names"] = lambda xs: ", ".join(a.replace("ind_", "") for a in xs)
    return env


def table(name):
    md = (TAB / f"{name}.md").read_text().rstrip()
    src = (TAB / f"{name}.source").read_text().strip()
    return md + f"\n\n<p class='src'>Source: {src}</p>"


def md_to_html(md: str, title: str, css: str) -> str:
    import markdown
    body = markdown.markdown(md, extensions=["tables", "toc", "attr_list", "md_in_html", "sane_lists"])
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title><style>{css}</style></head>"
            f"<body>{body}</body></html>")


def to_pdf(html_path: Path, pdf_path: Path, landscape=False):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto(html_path.resolve().as_uri(), wait_until="load")
        if landscape:
            pg.pdf(path=str(pdf_path), width="13.333in", height="7.5in", print_background=True,
                   margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        else:
            pg.pdf(path=str(pdf_path), format="A4", print_background=True, display_header_footer=True,
                   header_template="<span></span>",
                   footer_template="<div style='font-size:8px;width:100%;text-align:center;color:#666'>"
                                   "<span class='pageNumber'></span> / <span class='totalPages'></span></div>",
                   margin={"top": "16mm", "bottom": "16mm", "left": "16mm", "right": "16mm"})
        b.close()


def build(do_pdf=True, do_figs=True):
    N = json.loads((REP / "numbers.json").read_text())
    caps = figures(N) if do_figs else json.loads((FIG / "captions.json").read_text())
    env = jinja_env()
    tables = {p.stem: table(p.stem) for p in TAB.glob("*.md")}
    import markdown
    ctx = {"N": N, "T": tables, "cap": caps, "fig": lambda n: f"figures/{n}.png",
           "T_exec_html": markdown.markdown((TAB / "exec_compact_us_mkt.md").read_text(), extensions=["tables"])}
    DEL.mkdir(parents=True, exist_ok=True)
    # report
    md = env.get_template("report.md.j2").render(**ctx)
    (DEL / "report.md").write_text(md)
    css = (TPL / "report.css").read_text()
    (DEL / "report.html").write_text(md_to_html(md, "Growth with a floor", css))
    # deck
    deck = env.get_template("deck.html.j2").render(**ctx)
    (DEL / "deck.html").write_text(deck)
    if do_pdf:
        to_pdf(DEL / "report.html", DEL / "report.pdf")
        to_pdf(DEL / "deck.html", DEL / "deck.pdf", landscape=True)
    # guard: no unformatted numbers left from template slips (e.g. raw floats with many digits)
    raw = re.findall(r"\d\.\d{6,}", md)
    if raw:
        print(f"WARNING: raw floats in report.md: {raw[:5]}")
    print(f"wrote {DEL / 'report.md'}, report.html, deck.html" + (", report.pdf, deck.pdf" if do_pdf else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--no-figures", action="store_true")
    a = ap.parse_args()
    build(not a.no_pdf, not a.no_figures)


if __name__ == "__main__":
    main()
