"""Draw every figure in the README, light and dark.   .venv/bin/python code/make_figures.py

Needs data/processed/results.pkl from run_analysis.py. Numbers quoted in the README are printed and saved to
data/processed/numbers.json."""
import json
import pickle
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from yc import data, models, fit  # noqa: E402
from yc.plot import THEMES, style, save, subtitle, panel_title, shade, maturity_axis, note, ramp  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
R = pickle.load(open(ROOT / "data" / "processed" / "results.pkl", "rb"))
NUM = {}
DATES = R["dates"]
SHORT = {"2006-12-29": "Dec 2006", "2021-06-30": "Jun 2021", "2023-06-30": "Jun 2023", "2024-09-30": "Sep 2024",
         "2026-06-30": "Jun 2026"}
REGIME = {"2006-12-29": "mild inversion", "2021-06-30": "steep, zero bound", "2023-06-30": "deep inversion",
          "2024-09-30": "U-shape", "2026-06-30": "normal"}
MODEL_COL = {"Svensson": 0, "Vasicek": 1, "G2++": 2, "CIR": 3, "Nelson-Siegel": 4, "Dynamic NS": 6}

# ------------------------------------------------------------------------------------------------ data
cmt = data.load_cmt()
gsw = data.load_gsw()
REC = [(pd.Timestamp(a), pd.Timestamp(b)) for a, b in data.NBER_RECESSIONS]
spread_m = (cmt["10y"] - cmt["3m"]).dropna().resample("ME").mean() * 100          # bp


def inversion_episodes(m, gap=7):
    inv = m < 0
    eps, start, last = [], None, None
    for d, v in inv.items():
        if not v:
            continue
        if start is None:
            start = d
        elif (d.to_period("M") - last.to_period("M")).n > gap:
            eps.append((start, last))
            start = d
        last = d
    eps.append((start, last))
    rows = []
    for a, b in eps:
        nxt = [r for r in REC if r[0] >= a]
        r = nxt[0] if nxt else None
        rows.append(dict(start=a, end=b, months=int(inv[a:b].sum()), deepest=float(m[a:b].min()),
                         recession=r[0] if r else None,
                         lead=(r[0].to_period("M") - a.to_period("M")).n if r else None))
    return pd.DataFrame(rows)


EP = inversion_episodes(spread_m)
INV = [(a, b + pd.offsets.MonthEnd(0)) for a, b in zip(EP.start, EP.end)]
NUM["episodes"] = len(EP)
NUM["episode_2022"] = dict(months=int(EP.months.iloc[-1]), deepest_bp=round(EP.deepest.iloc[-1]))


# ------------------------------------------------------------------------------- 1 hero: term spread
def fig_hero(T, theme):
    fig, ax = plt.subplots(figsize=(11.5, 4.8))
    shade(ax, REC, T["rec"])
    shade(ax, INV, T["inv"])
    s = spread_m.loc["1962":]
    ax.plot(s.index, s, color=T["s"][0], lw=1.5, zorder=3)
    ax.axhline(0, color=T["muted"], lw=0.9, zorder=2)
    ax.set_xlim(pd.Timestamp("1962-01-01"), s.index.max() + pd.Timedelta(days=200))
    ax.set_ylabel("10-year minus 3-month yield (bp)")
    ax.set_title("Sixty years of the Treasury term spread")
    subtitle(ax, "Monthly average. Orange bands: inversions (3-month above 10-year). Gray bands: NBER recessions.", T)
    k = s.loc["1980":"1981"].idxmin()
    note(ax, (k, s[k]), f"1980–81: {s[k]:.0f}bp under Volcker", T, dx=30, dy=-4, ha="left")
    k = s.loc["2006":"2007"].idxmin()
    ax.annotate("2006–07: inverted 10 months,\nrecession 16 months later", (k, s[k]), xytext=(pd.Timestamp("1996-06-01"), -150),
                textcoords="data", ha="center", fontsize=9, color=T["muted"], arrowprops=dict(arrowstyle="-", color=T["axis"], lw=0.8))
    k = s.loc["2022":"2025"].idxmin()
    ax.annotate(f"2022–25: {NUM['episode_2022']['months']} months inverted, down to {NUM['episode_2022']['deepest_bp']}bp.\n"
                "The longest inversion on record. No recession so far.", (k, s[k]),
                xytext=(pd.Timestamp("2013-01-01"), -255), textcoords="data", ha="center", fontsize=9, color=T["ink"],
                arrowprops=dict(arrowstyle="-", color=T["axis"], lw=0.8))
    ax.set_ylim(-330, 450)
    ax.legend(handles=[Patch(color=T["inv"], label="Inversion"), Patch(color=T["rec"], label="NBER recession")],
              loc="upper left", ncol=2)
    save(fig, "hero_spread", theme)


# -------------------------------------------------------------------------- 2 inversion episodes summary
def fig_episodes(T, theme):
    e = EP.iloc[::-1].reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(11, 4.6))
    lab = [f"{a:%Y}–{b:%y}" if a.year != b.year else f"{a:%Y}" for a, b in zip(e.start, e.end)]
    none = e.recession.isna()
    cols = [T["s"][1] if n else T["s"][0] for n in none]
    ax.barh(range(len(e)), e.months, color=cols, height=0.62, zorder=3)
    for i, r in e.iterrows():
        txt = (f"deepest {r.deepest:.0f}bp  ·  " +
               (f"recession {int(r.lead)} months after it began" if not none[i] else "no recession so far"))
        ax.text(r.months + 0.6, i, txt, va="center", fontsize=9.5, color=T["ink"] if none[i] else T["muted"],
                fontweight="bold" if none[i] else "normal")
    ax.set_yticks(range(len(e)))
    ax.set_yticklabels(lab)
    ax.set_xlim(0, 52)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("months with a negative 10y − 3m spread")
    ax.set_title("Every recession since 1969 followed an inversion. The longest inversion didn't.")
    subtitle(ax, "Inversion episodes 1962–2026 (gaps of up to 7 months merged). 1966–67's recession came only in 1969.", T)
    ax.legend(handles=[Patch(color=T["s"][0], label="followed by a recession"),
                       Patch(color=T["s"][1], label="no recession so far")], loc="upper right")
    save(fig, "inversion_episodes", theme)


# ---------------------------------------------------------------------------- 3 curve evolution 2021-26
MATS = {"3m": .25, "6m": .5, "1y": 1, "2y": 2, "3y": 3, "5y": 5, "7y": 7, "10y": 10, "20y": 20, "30y": 30}


def fig_evolution(T, theme):
    m = cmt[list(MATS)].resample("ME").last()
    phases = [("2021: steep at the zero bound", "2021-01", "2021-12", 1),
              ("2022: hikes flatten, then invert", "2022-01", "2022-12", 1),
              ("2023: deep inversion", "2023-01", "2023-12", 1),
              ("2024–26: the inversion unwinds", "2024-01", "2026-10", 3)]
    fig, axs = plt.subplots(1, 4, figsize=(15, 4.4), sharey=True)
    x = list(MATS.values())
    for ax, (title, a, b, step) in zip(axs, phases):
        sub = m.loc[a:b].iloc[::step]
        cs = ramp(T, len(sub))
        for i, (d, row) in enumerate(sub.iterrows()):
            ax.plot(x, row.values, color=cs[i], lw=1.6 if i in (0, len(sub) - 1) else 1.1)
        ax.annotate(f"{sub.index[0]:%b %y}", (0.25, sub.iloc[0]["3m"]), xytext=(-4, 0), textcoords="offset points",
                    fontsize=8.5, color=T["muted"], va="center", ha="right")
        ax.annotate(f"{sub.index[-1]:%b %y}", (30, sub.iloc[-1]["30y"]), xytext=(4, 0), textcoords="offset points",
                    fontsize=8.5, color=T["ink"], va="center")
        maturity_axis(ax)
        panel_title(ax, title, T)
        ax.set_xlim(0.1, 60)
    axs[0].set_ylabel("par yield (%)")
    fig.suptitle("How the curve turned, 2021 to 2026", x=0.065, y=1.06, ha="left", fontsize=12.5, fontweight="bold",
                 color=T["ink"])
    fig.text(0.065, 0.985, "Treasury par yield curves at each month-end (each quarter in the last panel). "
             + ("Darker = later." if theme == "light" else "Brighter = later."),
             color=T["muted"], fontsize=9.5)
    fig.tight_layout()
    save(fig, "curve_evolution", theme)


# ------------------------------------------------------------------------------------- 4 factors (PCA)
def fig_pca(T, theme):
    cols = ["y1", "y2", "y3", "y5", "y7", "y10", "y20"]
    dx = gsw.loc["1990":, cols + ["y30"]].dropna().diff().dropna() * 100
    w, v = np.linalg.eigh(np.cov(dx[cols].T))
    w, v = w[::-1], v[:, ::-1]
    share = w / w.sum()
    v[:, 0] *= np.sign(v[5, 0]); v[:, 1] *= np.sign(v[-1, 1]); v[:, 2] *= np.sign(v[0, 2])
    spr = (gsw["y10"] - gsw["y1"]).reindex(dx.index)
    inv, steep = dx[spr < 0], dx[spr > 1]
    big = (dx.y2.abs() > 0.5) & (dx.y30.abs() > 0.5)
    NUM["pca_share"] = [round(float(x), 3) for x in share[:3]]
    NUM["opposite_2y_30y"] = round(float((np.sign(dx.y2[big]) != np.sign(dx.y30[big])).mean()), 3)
    def sh(x):
        ww = np.linalg.eigvalsh(np.cov(x[cols].T))[::-1]
        return ww / ww.sum()
    NUM["pca_share_inverted"] = [round(float(x), 3) for x in sh(inv)[:3]]
    NUM["pca_share_steep"] = [round(float(x), 3) for x in sh(steep)[:3]]

    fig, (a, b) = plt.subplots(1, 2, figsize=(12, 4.4), gridspec_kw={"width_ratios": [1.25, 1]})
    mat = [1, 2, 3, 5, 7, 10, 20]
    for i, lab in enumerate(["Level", "Slope", "Curvature"]):
        a.plot(mat, v[:, i], color=T["s"][i], lw=2, marker="o", ms=5, mec=T["bg"], mew=1.2)
        a.annotate(f"{lab}  {share[i]:.0%}", (20, v[-1, i]), xytext=(6, [0, -7, 7][i]), textcoords="offset points", va="center",
                   fontsize=9.5, color=T["ink"])
    a.axhline(0, color=T["axis"], lw=0.9)
    a.set_xlim(0, 27)
    a.set_xlabel("maturity (years)"); a.set_ylabel("loading")
    a.set_title("Three factors move the curve")
    subtitle(a, "Principal components of daily changes in zero yields, 1–20y, 1990–2026. % = share of variance.", T)
    mv = mat + [30]
    b.plot(mv, steep.std().values, color=T["s"][0], lw=2, marker="o", ms=5, mec=T["bg"], mew=1.2)
    b.plot(mv, inv.std().values, color=T["s"][1], lw=2, marker="o", ms=5, mec=T["bg"], mew=1.2)
    b.annotate("inverted-curve days", (5, inv.std()["y5"]), xytext=(8, 4), textcoords="offset points", fontsize=9.5,
               color=T["ink"])
    b.annotate("steep-curve days", (7, steep.std()["y7"]), xytext=(8, 8), textcoords="offset points", fontsize=9.5,
               color=T["ink"])
    b.set_xlabel("maturity (years)"); b.set_ylabel("std of daily change (bp)")
    b.set_title("Inverted curves are most volatile at 2–3y")
    subtitle(b, "Steep: 10y − 1y above 1pp. Inverted: 10y below 1y.", T)
    fig.tight_layout(w_pad=3)
    save(fig, "factors", theme)


# ----------------------------------------------------------------------------------- 5 Vasicek shapes
def fig_vasicek_shapes(T, theme):
    tau = np.linspace(0.05, 30, 300)
    base = dict(kappa=0.3, theta=0.045, sigma=0.012, r0=0.03)

    def vz(**kw):
        p = {**base, **kw}
        return models.vasicek_zero(tau, [p["kappa"], p["theta"], p["sigma"], p["r0"]]) * 100

    fig, axs = plt.subplots(1, 4, figsize=(15, 4.0), sharey=True)
    panels = [("r0", [0.01, 0.025, 0.04, 0.055, 0.07], "Short rate r: flips the shape", lambda v: f"r = {v:.1%}"),
              ("theta", [0.03, 0.045, 0.06], "Long-run mean θ: lifts the long end", lambda v: f"θ = {v:.1%}"),
              ("kappa", [0.08, 0.3, 1.0], "Mean reversion κ: sets the reach", lambda v: f"κ = {v:g}"),
              ("sigma", [0.005, 0.02, 0.035], "Volatility σ: bends the long end", lambda v: f"σ = {v:.1%}")]
    for ax, (k, vals, title, f) in zip(axs, panels):
        cs = ramp(T, len(vals))
        for i, v in enumerate(vals):
            y = vz(**{k: v})
            ax.plot(tau, y, color=cs[i], lw=2, label=f(v))
        ax.legend(loc="upper right" if k == "r0" else "lower right", fontsize=8.5, handlelength=1.2)
        ax.set_xlim(0, 30)
        panel_title(ax, title, T)
        ax.set_xlabel("maturity (years)")
    axs[0].set_ylabel("zero yield (%)")
    fig.suptitle("What one-factor Vasicek can draw", x=0.055, y=1.08, ha="left", fontsize=12.5, fontweight="bold",
                 color=T["ink"])
    fig.text(0.055, 1.0, "Change one parameter, keep the others at κ = 0.3, θ = 4.5%, σ = 1.2%, r = 3%. "
             "Upward, humped or inverted, never a U.", color=T["muted"], fontsize=9.5)
    fig.tight_layout()
    save(fig, "vasicek_shapes", theme)


# --------------------------------------------------------------------------------- helpers for bond fits
def ytick_mat(ax):
    maturity_axis(ax, (0.25, 1, 2, 5, 10, 30), ("3m", "1y", "2y", "5y", "10y", "30y"))
    ax.set_xlim(0.07, 40)


def bonds_scatter(ax, b, T, s=9):
    for typ, mk in [("bill", "o"), ("note", "o"), ("bond", "o")]:
        x = b[b.type == typ]
        ax.scatter(x.tau, x.ytm, s=s, color=T["gray"], lw=0, zorder=2)


# ----------------------------------------------------------------------------- 6 one date up close
def fig_june2023(T, theme):
    d = "2023-06-30"
    b = R["bonds"][d]
    fig, (a, r) = plt.subplots(2, 1, figsize=(11.5, 7.2), sharex=True, gridspec_kw={"height_ratios": [2.3, 1], "hspace": 0.1})
    marks = {"bill": ("o", "bills"), "note": ("s", "notes"), "bond": ("^", "bonds")}
    for typ, (mk, lab) in marks.items():
        x = b[b.type == typ]
        a.scatter(x.tau, x.ytm, s=14, marker=mk, color=T["gray"], lw=0, zorder=2, label=f"{lab} ({len(x)})")
    for m in ["Svensson", "Vasicek", "G2++"]:
        e = R["per_date"][(d, m)][1].sort_values("tau")
        a.plot(e.tau, e.model_ytm, color=T["s"][MODEL_COL[m]], lw=2, zorder=3, label=m)
        r.scatter(e.tau, e.err_bp, s=10, color=T["s"][MODEL_COL[m]], lw=0, alpha=0.85, zorder=3)
    r.axhline(0, color=T["muted"], lw=0.9)
    ytick_mat(a); ytick_mat(r)
    a.set_ylabel("yield to maturity (%)")
    r.set_ylabel("model − market (bp)")
    r.set_xlabel("maturity (log scale)")
    a.set_title("One date up close: 30 June 2023, the deepest part of the inversion")
    subtitle(a, "Each gray mark is one Treasury's market yield. Lines: each model's yield for the same bonds, all "
                "parameters fitted to this date.", T)
    a.legend(loc="upper right", ncol=2)
    e = R["per_date"][(d, "Svensson")][1]
    old = e[(e.TMATDT.dt.year.isin([2036, 2037])) & (e.type == "bond")]
    k = old.err_bp.idxmax()
    note(r, (old.tau[k], old.err_bp[k]), "old 30-year bonds due 2036–37 trade rich:\nevery model's yield is above theirs",
         T, dx=-150, dy=8, ha="right")
    e["orig"] = ((e.TMATDT - e.issued).dt.days / 365.25).round()
    twenty = e[(e.orig == 20) & (e.tau > 15)]
    NUM["jun23_old_bonds_rich_bp"] = [round(float(old.err_bp.min())), round(float(old.err_bp.max()))]
    NUM["jun23_20y_cheap_bp"] = round(float(twenty.err_bp.mean()), 1)
    k = twenty.err_bp.idxmin()
    note(r, (twenty.tau[k], twenty.err_bp[k]), f"20-year issues trade cheap (≈{-NUM['jun23_20y_cheap_bp']:.0f}bp)",
         T, dx=-60, dy=-22, ha="right")
    v = R["per_date"][(d, "Vasicek")][1].sort_values("tau")
    k = v.index[np.searchsorted(v.tau.values, 20)]
    note(a, (v.tau[k], v.model_ytm[k]), "Vasicek can't turn back up\nafter 10 years", T, dx=-10, dy=-50, ha="center")
    for m in ["Svensson", "Vasicek", "G2++"]:
        NUM[f"jun23_rmse_{m}"] = round(float(fit.summary(R['per_date'][(d, m)][1])["RMSE (bp)"]), 1)
    save(fig, "june2023_closeup", theme)


# ------------------------------------------------------------------------------- 7 five dates, per-date
def five_panels(T, theme, getter, name, title, sub, models_, elim=60):
    fig, axs = plt.subplots(2, 5, figsize=(16, 6.2), sharex=True,
                            gridspec_kw={"height_ratios": [2.2, 1], "hspace": 0.08, "wspace": 0.25})
    for j, d in enumerate(DATES):
        a, r = axs[0, j], axs[1, j]
        b = R["bonds"][d]
        a.scatter(b.tau, b.ytm, s=6, color=T["gray"], lw=0, zorder=2)
        for m in models_:
            e = getter(d, m).sort_values("tau")
            a.plot(e.tau, e.model_ytm, color=T["s"][MODEL_COL[m]], lw=1.7, zorder=3)
            r.plot(e.tau, e.err_bp, color=T["s"][MODEL_COL[m]], lw=0.9, zorder=3, alpha=0.9)
        r.axhline(0, color=T["muted"], lw=0.8)
        r.set_ylim(-elim, elim)
        ytick_mat(a); ytick_mat(r)
        panel_title(a, f"{SHORT[d]} · {REGIME[d]}", T)
        rm = "   ".join(f"{m} {fit.summary(getter(d, m))['RMSE (bp)']:.0f}" for m in models_)
        r.text(0.02, 0.04, rm, transform=r.transAxes, fontsize=8, color=T["muted"])
    axs[0, 0].set_ylabel("yield (%)")
    axs[1, 0].set_ylabel("error (bp)")
    fig.legend(handles=[Line2D([], [], marker="o", ls="", color=T["gray"], label="bonds (market yield)")] +
               [Line2D([], [], color=T["s"][MODEL_COL[m]], lw=2, label=m) for m in models_],
               loc="upper right", bbox_to_anchor=(0.9, 1.01), ncol=len(models_) + 1, fontsize=9.5)
    fig.suptitle(title, x=0.07, y=1.0, ha="left", fontsize=12.5, fontweight="bold", color=T["ink"])
    fig.text(0.07, 0.945, sub, color=T["muted"], fontsize=9.5)
    save(fig, name, theme)


def fig_five_perdate(T, theme):
    five_panels(T, theme, lambda d, m: R["per_date"][(d, m)][1], "five_dates_perdate",
                "Five regimes, each fitted on its own",
                "All parameters re-fitted on every date. Bottom row: model − market yield per bond (bp); "
                "numbers are RMSE (bp).", ["Svensson", "Vasicek", "G2++"])


def fig_five_shared(T, theme):
    sh = {m: dict(zip(DATES, R["shared"][m][1])) for m in ["Vasicek", "G2++"]}
    five_panels(T, theme, lambda d, m: sh[m][d], "five_dates_shared",
                "Five regimes, one parameter set",
                "Structural parameters common to all five dates; only the state (r, or the two factors) changes "
                "from date to date.", ["Vasicek", "G2++"], elim=90)


# -------------------------------------------------------------------------------- 8 RMSE matrix summary
def fig_rmse_matrix(T, theme):
    rows = [("Svensson", "per date"), ("Nelson-Siegel", "per date"), ("G2++", "per date"), ("CIR", "per date"),
            ("Vasicek", "per date"), ("G2++", "shared"), ("CIR", "shared"), ("Vasicek", "shared")]
    M = np.zeros((len(rows), len(DATES)))
    for i, (m, k) in enumerate(rows):
        for j, d in enumerate(DATES):
            e = R["per_date"][(d, m)][1] if k == "per date" else R["shared"][m][1][j]
            M[i, j] = fit.summary(e)["RMSE (bp)"]
    NUM["rmse"] = {f"{m} ({k})": [round(float(x), 1) for x in M[i]] for i, (m, k) in enumerate(rows)}
    fig, ax = plt.subplots(figsize=(10.5, 5.2))
    from matplotlib.colors import LinearSegmentedColormap, LogNorm
    seq = THEMES["light"]["seq"] if theme == "light" else THEMES["dark"]["seq"][::-1]
    cmap = LinearSegmentedColormap.from_list("q", [T["bg"]] + seq[1:] if theme == "light" else [T["bg"]] + seq[:-1][::-1])
    cmap = LinearSegmentedColormap.from_list("q", THEMES["light"]["seq"]) if theme == "light" else \
        LinearSegmentedColormap.from_list("q", THEMES["dark"]["seq"][:5])
    im = ax.imshow(M, cmap=cmap, norm=LogNorm(2, 55), aspect="auto")
    for i in range(M.shape[0]):
        for j in range(M.shape[1]):
            dark_cell = (np.log(M[i, j]) - np.log(2)) / (np.log(55) - np.log(2)) > 0.55
            c = ("#ffffff" if dark_cell else T["ink"]) if theme == "light" else ("#0d1117" if dark_cell else T["ink"])
            ax.text(j, i, f"{M[i, j]:.1f}", ha="center", va="center", fontsize=10.5, color=c,
                    fontweight="bold" if M[i, j] > 25 else "normal")
    ax.set_xticks(range(len(DATES)))
    ax.set_xticklabels([f"{SHORT[d]}\n{REGIME[d]}" for d in DATES])
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([f"{m}, {k}" for m, k in rows])
    ax.axhline(4.5, color=T["bg"], lw=6)
    ax.grid(False)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.xaxis.tick_top()
    fig.text(0.02, 1.04, "Fit error on every date (RMSE of bond yields, bp)", fontsize=12.5, fontweight="bold", color=T["ink"])
    fig.text(0.02, 0.995, "Top: every parameter fitted per date. Bottom: one parameter set for all five dates. "
                          + ("Darker = worse." if theme == "light" else "Brighter = worse."), color=T["muted"], fontsize=9.5)
    save(fig, "rmse_matrix", theme)


# ----------------------------------------------------------------------------- 9 through time: RMSE
P = R["panel"]
PF = R["panel_fits"]


def monthly_rmse(m):
    F = PF[m][2]
    return np.sqrt((((F - P) * 100) ** 2).mean(1))


def fig_through_time(T, theme):
    fig, ax = plt.subplots(figsize=(11.5, 4.8))
    shade(ax, [(a, b) for a, b in INV if b >= P.index[0]], T["inv"])
    shade(ax, [(a, b) for a, b in REC if b >= P.index[0]], T["rec"])
    for m, lab in [("Vasicek", "Vasicek (1 factor)"), ("G2++", "G2++ (2 factors)"),
                   ("Dynamic NS", "Dynamic NS (3 factors,\nnot arbitrage-free)")]:
        s = monthly_rmse(m)
        sm = s.rolling(12, center=True, min_periods=6).median()
        ax.plot(s.index, s, color=T["s"][MODEL_COL[m]], lw=0.8, alpha=0.35, zorder=3)
        ax.plot(sm.index, sm, color=T["s"][MODEL_COL[m]], lw=2.2, zorder=4)
        NUM[f"panel_rmse_{m}"] = dict(all=round(float(np.sqrt((s**2).mean())), 1), median=round(float(s.median()), 1),
                                      inverted=round(float(s[spread_m.reindex(s.index) < 0].median()), 1),
                                      normal=round(float(s[spread_m.reindex(s.index) >= 0].median()), 1))
        ax.text(1.01, {"Vasicek": 0.78, "G2++": 0.52, "Dynamic NS": 0.3}[m], f"{lab}\nmedian {s.median():.0f}bp",
                transform=ax.transAxes, va="center", fontsize=9.5, color=T["s"][MODEL_COL[m]], fontweight="bold")
    ax.set_xlim(P.index[0], P.index[-1])
    ax.set_yscale("log")
    ax.set_yticks([2, 5, 10, 20, 50, 100, 200])
    ax.set_yticklabels(["2", "5", "10", "20", "50", "100", "200"])
    ax.set_ylabel("RMSE across 11 maturities (bp, log scale)")
    ax.set_title("One parameter set for 37 years", pad=40)
    subtitle(ax, "Each model fitted to all 442 month-end zero curves (3m–30y, Jan 1990 – Oct 2026) with ONE parameter "
                 "set; only the factors move.\nThin: each month. Thick: 12-month rolling median. Orange: inversions. "
                 "Gray: recessions.", T)
    save(fig, "through_time_rmse", theme)


# ------------------------------------------------------------------------ 10 G2++ factors through time
def fig_g2_factors(T, theme):
    th, X, F = PF["G2++"]
    level = th["delta"] * 100 + X.x1
    slope = X.x2
    y10 = P[10.0]
    sp = P[0.25] - P[10.0]
    NUM["g2_panel_params"] = {k: round(float(v), 4) for k, v in th.items()}
    NUM["g2_halflife_years"] = [round(float(np.log(2) / th["a1"]), 1), round(float(np.log(2) / th["a2"]), 1)]
    NUM["corr_slope_vs_3m10y"] = round(float(np.corrcoef(slope, sp)[0, 1]), 3)
    NUM["corr_level_vs_10y"] = round(float(np.corrcoef(level, y10)[0, 1]), 3)
    fig, (a, b) = plt.subplots(2, 1, figsize=(11.5, 6.6), sharex=True, gridspec_kw={"hspace": 0.32})
    for ax in (a, b):
        shade(ax, [(x, y) for x, y in INV if y >= P.index[0]], T["inv"])
    a.plot(y10.index, y10, color=T["gray"], lw=1.4, label="10-year zero yield (data)")
    a.plot(level.index, level, color=T["s"][0], lw=1.8, label=r"level factor  $\delta + x_1$  (model)")
    a.legend(loc="upper right")
    a.set_ylabel("%")
    a.set_title("What the two G2++ factors are")
    subtitle(a, f"Level factor: half-life {NUM['g2_halflife_years'][0]:.0f} years, so almost a random walk. "
                f"It tracks the 10-year yield (correlation {NUM['corr_level_vs_10y']:.2f}).", T)
    b.axhline(0, color=T["muted"], lw=0.9)
    b.plot(sp.index, sp, color=T["gray"], lw=1.4, label="3-month minus 10-year yield (data)")
    b.plot(slope.index, slope, color=T["s"][2], lw=1.8, label=r"slope factor  $x_2$  (model)")
    b.legend(loc="lower right")
    b.set_ylabel("%")
    b.set_title("The slope factor is the inversion", pad=24)
    subtitle(b, f"Half-life {NUM['g2_halflife_years'][1]:.1f} years. Positive = front end above where the level says "
                f"it should settle: an inversion (correlation with 3m − 10y: {NUM['corr_slope_vs_3m10y']:.2f}).", T)
    k = slope.loc["2023"].idxmax()
    note(b, (k, slope[k]), f"{k:%b %Y}: slope factor {slope[k]:+.1f}pp", T, dx=-70, dy=-10, ha="right")
    save(fig, "g2_factors", theme)


# --------------------------------------------------------------- 11 Vasicek refitted each month: parameters
def fig_vasicek_params(T, theme):
    V = R["vasicek_monthly"]
    inv_mask = spread_m.reindex(V.index) < 0
    NUM["vas_monthly"] = dict(
        median_rmse=round(float(V.rmse.median()), 1),
        sigma_median=round(float(V.sigma.median()), 4),
        sigma_cap_share=round(float((V.sigma > 0.099).mean()), 3),
        sigma_cap_share_inverted=round(float((V.sigma[inv_mask] > 0.099).mean()), 3),
        sigma_cap_share_normal=round(float((V.sigma[~inv_mask] > 0.099).mean()), 3),
        theta_cap_share=round(float((V.theta > 0.249).mean()), 3))
    # realised normal vol of the 3m zero yield, rolling 1y, from daily data
    r3 = (2 * np.log1p(cmt["3m"] / 200) * 100).dropna()
    rv = r3.diff().rolling(250).std() * np.sqrt(250)
    rv = rv.resample("ME").last().reindex(V.index)
    NUM["realised_short_vol_median_pct"] = round(float(rv.median()), 2)
    fig, (a, b) = plt.subplots(2, 1, figsize=(11.5, 6.6), sharex=True, gridspec_kw={"hspace": 0.32})
    for ax in (a, b):
        shade(ax, [(x, y) for x, y in INV if y >= P.index[0]], T["inv"])
    a.plot(V.index, V.sigma * 100, color=T["s"][1], lw=1.6, label="σ fitted to that month's curve (Q)")
    a.plot(rv.index, rv, color=T["gray"], lw=1.4, label="realised volatility of the 3-month yield, past year (P)")
    a.axhline(10, color=T["muted"], lw=0.8, ls=(0, (3, 3)))
    a.text(V.index[-1], 9.35, "upper bound 10%", fontsize=8.5, color=T["muted"], ha="right")
    a.set_ylabel("% per year")
    a.set_ylim(-0.3, 12.8)
    a.legend(loc="upper left", ncol=2, bbox_to_anchor=(0, 1.0))
    a.set_title("Vasicek refitted every month: the parameters won't sit still")
    subtitle(a, f"Volatility. The fitted σ (median {NUM['vas_monthly']['sigma_median']:.1%}) is set by the curve's "
                f"shape, not by how much rates move (median {NUM['realised_short_vol_median_pct']:.1f}%).", T)
    b.plot(V.index, V.kappa, color=T["s"][0], lw=1.6)
    b.set_yscale("log")
    b.set_ylim(8e-4, 8)
    b.set_yticks([0.001, 0.01, 0.1, 1, 5])
    b.set_yticklabels(["0.001", "0.01", "0.1", "1", "5"])
    b.yaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    b.set_ylabel("κ (log scale)")
    b.set_title("Mean reversion κ jumps between 'random walk' and 'reverts within months'", pad=24, fontsize=11)
    subtitle(b, "Half-life ln2/κ ranges from about 2 months to decades as the curve's shape changes.", T)
    save(fig, "vasicek_params_monthly", theme)


# ------------------------------------------------------------------------- 12 P vs Q: how fast factors revert
PQ = R.get("pq_params")
DEC = R.get("decomp10")


def fig_pq_reversion(T, theme):
    h = np.linspace(0, 30, 361)
    fig, axs = plt.subplots(1, 2, figsize=(12, 4.2), sharey=True)
    for ax, i, name in [(axs[0], 0, "Level factor"), (axs[1], 1, "Slope factor")]:
        q = np.exp(-PQ["kappaQ"][i] * h)
        pp = np.exp(-PQ["kappaP"][i] * h)
        ax.plot(h, q, color=T["s"][0], lw=2.2)
        ax.plot(h, pp, color=T["s"][1], lw=2.2)
        spots = {0: ((14, 0.86), 9, (9, 0.42), 6), 1: ((7, 0.55), 1.5, (14, 0.33), 7)}[i]
        (qx, qy), qh, (px, py), ph = spots
        ax.annotate(f"priced by bonds (Q)\nhalf-life {PQ['halflifeQ'][i]:.1f} years", (qh, np.exp(-PQ['kappaQ'][i] * qh)),
                    xytext=(qx, qy), textcoords="data", fontsize=9.5, color=T["s"][0], fontweight="bold",
                    arrowprops=dict(arrowstyle="-", color=T["axis"], lw=0.8))
        ax.annotate(f"actual history (P)\nhalf-life {PQ['halflifeP'][i]:.1f} years", (ph, np.exp(-PQ['kappaP'][i] * ph)),
                    xytext=(px, py), textcoords="data", fontsize=9.5, color=T["s"][1], fontweight="bold",
                    arrowprops=dict(arrowstyle="-", color=T["axis"], lw=0.8))
        panel_title(ax, name, T)
        ax.set_xlabel("years after a shock")
        ax.set_xlim(0, 30); ax.set_ylim(0, 1.05)
    axs[0].set_ylabel("share of a shock still left")
    fig.suptitle("Bonds price the level of rates as nearly permanent; history says it reverts in about 4 years", x=0.06, y=1.08,
                 ha="left", fontsize=12.5, fontweight="bold", color=T["ink"])
    fig.text(0.06, 1.0, "Expected decay of a factor shock: under the risk-neutral parameters fitted to the curve (Q) "
             "vs a VAR on the factors' monthly history, 1990–2026 (P).", color=T["muted"], fontsize=9.5)
    fig.tight_layout()
    save(fig, "pq_reversion", theme)


# ------------------------------------------------------------------------------- 13 term premium vs ACM
def fig_term_premium(T, theme):
    acm = R["acm"].reindex(DEC.index)
    NUM["tp_corr_acm"] = round(float(np.corrcoef(DEC.tp, acm.ACMTP10)[0, 1]), 3)
    NUM["tp_mean"] = round(float(DEC.tp.mean()), 2)
    NUM["acm_tp_mean"] = round(float(acm.ACMTP10.mean()), 2)
    fig, (a, b) = plt.subplots(2, 1, figsize=(11.5, 7), sharex=True, gridspec_kw={"hspace": 0.32, "height_ratios": [1.2, 1]})
    for ax in (a, b):
        shade(ax, [(x, y) for x, y in INV if y >= DEC.index[0]], T["inv"])
    a.fill_between(DEC.index, DEC.expect, DEC.y, color=T["s"][0], alpha=0.18, lw=0)
    a.plot(DEC.index, DEC.y, color=T["s"][0], lw=1.8, label="10-year yield (model)")
    a.plot(DEC.index, DEC.expect, color=T["s"][1], lw=1.8, label="expected average short rate, next 10 years (P)")
    a.legend(loc="upper right")
    a.set_ylabel("%")
    a.set_title("The 10-year yield = expected short rates + a term premium")
    subtitle(a, "Shaded gap = term premium: the extra yield for holding a 10-year bond instead of rolling 3-month bills.", T)
    b.axhline(0, color=T["muted"], lw=0.9)
    b.plot(DEC.index, DEC.tp, color=T["s"][0], lw=1.8, label="ours (G2++ + VAR, 1990–2026)")
    b.plot(acm.index, acm.ACMTP10, color=T["gray"], lw=1.6, label="NY Fed, Adrian-Crump-Moench")
    b.legend(loc="upper right")
    b.set_ylabel("10-year term premium (%)")
    b.set_title(f"Same swings as the NY Fed's estimate (correlation {NUM['tp_corr_acm']:.2f})", pad=24, fontsize=11.5)
    subtitle(b, f"Ours is {NUM['tp_mean'] - NUM['acm_tp_mean']:.1f} points higher on average: our long-run short rate "
                f"is estimated on 1990–2026 ({PQ['longrun_rP']:.1%}), theirs on data from 1961.", T)
    k = DEC.tp.loc["2023"].idxmin()
    b.annotate(f"{k:%b %Y}: {DEC.tp[k]:+.1f}", (k, DEC.tp[k]), xytext=(pd.Timestamp("2013-06-30"), -1.1), textcoords="data",
               ha="center", fontsize=9, color=T["muted"], arrowprops=dict(arrowstyle="-", color=T["axis"], lw=0.8))
    b.set_ylim(-1.6, None)
    save(fig, "term_premium", theme)


# ----------------------------------------------------------------------- 14 inversion decomposition bars
def deepest_months():
    out = []
    for a, b in zip(EP.start, EP.end):
        if b < DEC.index[0]:
            continue
        k = spread_m.loc[a:b].idxmin()
        k = DEC.index[DEC.index.get_indexer([k], method="nearest")[0]]
        out.append(k)
    return out


def fig_inversion_decomp(T, theme):
    ms = deepest_months()
    rows = []
    for k in ms:
        d = DEC.loc[k]
        y3, y10 = P.loc[k, 0.25], P.loc[k, 10.0]                 # actual zero yields
        rows.append(dict(month=k, cuts=y3 - d.expect, tp=-(y10 - d.expect), total=y3 - y10))
    D = pd.DataFrame(rows)
    NUM["inversion_decomp"] = {f"{r.month:%b %Y}": dict(expected_fall=round(r.cuts, 2), neg_tp=round(r.tp, 2),
                                                        spread_3m_10y=round(r.total, 2)) for r in D.itertuples()}
    fig, ax = plt.subplots(figsize=(11, 4.8))
    y = np.arange(len(D))[::-1] * 1.25
    ax.barh(y + 0.2, D.cuts, height=0.36, color=T["s"][0], zorder=3, label="expected fall in short rates, next 10 years (P)")
    ax.barh(y - 0.2, -D.tp * -1, height=0.36, color=T["s"][1], zorder=3, label="minus the 10-year term premium")
    ax.scatter(D.total, y, marker="D", s=46, color=T["ink"], zorder=4, label="= 3-month minus 10-year yield")
    for yi, r in zip(y, D.itertuples()):
        tp = -r.tp
        ax.text(max(r.cuts, r.tp, r.total, 0) + 0.12, yi,
                f"{r.total:+.2f}  =  {r.cuts:+.2f} expected fall  −  ({tp:+.2f} term premium)",
                va="center", fontsize=9.5, color=T["ink"])
    ax.axvline(0, color=T["muted"], lw=0.9)
    ax.set_yticks(y)
    ax.set_yticklabels([f"{m:%b %Y}" for m in D.month])
    ax.set_xlim(-1.5, 4.6)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("percentage points")
    ax.set_title("What each inversion was made of", pad=40)
    subtitle(ax, "Deepest month of each inversion since 1990. Inversions before 2008 were expected cuts offset by a positive "
                 "term premium;\n2019 was all negative term premium; 2023 was both.", T)
    ax.legend(loc="upper center", bbox_to_anchor=(0.45, -0.14), ncol=3, fontsize=9)
    save(fig, "inversion_decomposition", theme)


# ----------------------------------------------------------------------------- 15 priced vs realised paths
def fig_paths(T, theme):
    keys = list(R["paths"])
    fig, axs = plt.subplots(1, 3, figsize=(15, 4.4), sharey=True)
    titles = {"2006-12-31": "Dec 2006: priced mild cuts, got the crisis", "2019-08-31": "Aug 2019: priced cuts, got COVID",
              "2023-06-30": "Jun 2023: priced cuts, got them a year later"}
    NUM["paths"] = {}
    for ax, k in zip(axs, keys):
        p = R["paths"][k]
        m = np.arange(len(p))
        ax.plot(m, p.realised, color=T["ink"], lw=2.2, label="what happened (3-month yield)")
        ax.plot(m, p.forward, color=T["s"][0], lw=2, label="priced by the curve (forward rates)")
        ax.plot(m, p.expected, color=T["s"][1], lw=2, ls=(0, (4, 2)), label="model expectation (P, data up to then)")
        ax.axhline(p.realised.iloc[0], color=T["axis"], lw=0.8)
        panel_title(ax, titles.get(k, k), T)
        ax.set_xlabel("months later")
        ax.set_xlim(0, 40)
        NUM["paths"][k] = {f"{h}m": dict(forward=round(float(p.forward.iloc[h]), 2), expected=round(float(p.expected.iloc[h]), 2),
                                         realised=None if np.isnan(p.realised.iloc[h]) else round(float(p.realised.iloc[h]), 2))
                           for h in (0, 12, 24, 36)}
    axs[0].set_ylabel("3-month yield (%)")
    h_, l_ = axs[0].get_legend_handles_labels()
    fig.legend(h_, l_, loc="upper center", bbox_to_anchor=(0.5, -0.0), ncol=3, fontsize=9.5)
    fig.suptitle("What the inverted curve priced, and what happened", x=0.055, y=1.08, ha="left", fontsize=12.5,
                 fontweight="bold", color=T["ink"])
    fig.text(0.055, 1.0, "From the deepest month of three inversions: the market's forward rates, the model's real-world "
             "expectation, and the realised 3-month yield over the next 40 months.", color=T["muted"], fontsize=9.5)
    fig.tight_layout()
    save(fig, "priced_vs_realised", theme)


# ------------------------------------------------------------------------ 16 forecast scorecard by episode
def fig_forecast_episodes(T, theme):
    f = R["forecasts"]
    f = f[f.h == 12].set_index("date")
    rows = []
    for a, b in zip(EP.start, EP.end):
        sub = f.loc[a:b + pd.offsets.MonthEnd(0)]
        if len(sub) == 0:
            continue
        rows.append(dict(ep=f"{a:%Y}–{b:%y}" if a.year != b.year else f"{a:%Y}", n=len(sub),
                         forward=(sub.forward - sub.no_change).mean(), model=(sub.model - sub.no_change).mean(),
                         realised=(sub.actual - sub.no_change).mean()))
    D = pd.DataFrame(rows)
    allm = f
    NUM["forecast_rmse_12m"] = {k: round(float(np.sqrt(((allm[k] - allm.actual) ** 2).mean())), 2) for k in ["no_change", "forward", "model"]}
    inv = allm[spread_m.reindex(allm.index).values < 0]
    NUM["forecast_rmse_12m_inverted"] = {k: round(float(np.sqrt(((inv[k] - inv.actual) ** 2).mean())), 2) for k in ["no_change", "forward", "model"]}
    NUM["forecast_by_episode"] = D.round(2).to_dict("records")
    fig, ax = plt.subplots(figsize=(11, 4.4))
    x = np.arange(len(D))
    w = 0.26
    for j, (k, col, lab) in enumerate([("forward", T["s"][0], "priced by the curve (forwards)"),
                                       ("model", T["s"][1], "model expectation (P)"),
                                       ("realised", T["ink"], "what happened")]):
        ax.bar(x + (j - 1) * w, D[k], width=w * 0.92, color=col, zorder=3, label=lab)
        for xi, v in zip(x + (j - 1) * w, D[k]):
            ax.text(xi, v - 0.08 if v < 0 else v + 0.05, f"{v:+.1f}", ha="center", va="top" if v < 0 else "bottom",
                    fontsize=8.5, color=T["muted"])
    ax.axhline(0, color=T["muted"], lw=0.9)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{r.ep} inversion\n({r.n} months)" for r in D.itertuples()])
    ax.set_ylabel("change in 3-month yield over 12 months (pp)")
    ax.set_title("Three inversions badly under-priced the cuts that came. 2022–25 priced them about right.")
    subtitle(ax, "Average over the months of each inversion: forecast vs realised 12-month change in the 3-month yield. "
                 "Model re-estimated each month on data up to then.", T)
    ax.legend(loc="lower right", fontsize=9)
    ax.grid(axis="x", visible=False)
    save(fig, "forecast_episodes", theme)


# ================================================================================ BONUS: intraday event study
INTRA_PATH = ROOT / "data" / "processed" / "intraday.pkl"
INTRA = pickle.load(open(INTRA_PATH, "rb")) if INTRA_PATH.exists() else None
from yc import intraday as it  # noqa: E402
from yc import timeseries as ts  # noqa: E402
CORE = it.CORE
MAT = np.array([it.ETF_MAT[s_] for s_ in CORE])
MAT_LAB = {"SHY": "2y (SHY)", "IEI": "5y (IEI)", "IEF": "8y (IEF)", "TLT": "25y (TLT)"}
EV_COL = {"CPI": 0, "NFP": 2, "FOMC": 1, "control": None}
EV_NAME = {"CPI": "CPI", "NFP": "jobs report", "FOMC": "FOMC statement", "control": "quiet control days"}


def intra_windows():
    W = INTRA["windows"].dropna(subset=CORE).copy()
    sp = (cmt["10y"] - cmt["3m"]).ffill()
    W["inverted"] = [bool(sp.loc[:d].iloc[-1] < 0) for d in W.date]
    return W


def day_type():
    ev = INTRA["events"]
    out = {}
    for d in INTRA["days"].date:
        t = ev[ev.date == d].event.tolist()
        out[d] = "control" if not t else ("FOMC" if "FOMC" in t else t[0])
    return out


def fig_intraday_example(T, theme):
    fig, (a, b) = plt.subplots(1, 2, figsize=(14, 4.6), sharey=False)
    for ax, d, t0, t1, marks, title in [
            (a, "2022-11-10", "07:30", "11:00", [("08:30", "CPI released")], "CPI, 10 Nov 2022: a soft print"),
            (b, "2022-11-02", "13:30", "16:00", [("14:00", "statement"), ("14:30", "press conference")],
             "FOMC, 2 Nov 2022: statement, then Powell")]:
        g = it.path_from(d, t0, t1).ffill()
        x = [pd.Timestamp(d) + pd.Timedelta(seconds=int(v)) for v in g.index]
        for j, s_ in enumerate(CORE):
            ax.plot(x, g[s_], color=ramp(T, 4)[j], lw=2, label=MAT_LAB[s_])
        for hhmm, lab in marks:
            tx = pd.Timestamp(d) + pd.Timedelta(seconds=it.hm(hhmm))
            ax.axvline(tx, color=T["muted"], lw=0.8, ls=(0, (3, 3)))
            ax.text(tx, 1.0, f" {lab}", transform=ax.get_xaxis_transform(), fontsize=9, color=T["muted"], va="top")
        ax.axhline(0, color=T["axis"], lw=0.8)
        ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%H:%M"))
        panel_title(ax, title, T)
        ax.set_ylabel("yield change since start (bp)")
    a.legend(loc="lower left", fontsize=9)
    fig.suptitle("Two announcements up close", x=0.055, y=1.08, ha="left", fontsize=12.5, fontweight="bold", color=T["ink"])
    fig.text(0.055, 1.0, "Treasury yields read from ETF quotes, minute by minute (ET). Darker = longer maturity.",
             color=T["muted"], fontsize=9.5)
    fig.tight_layout()
    save(fig, "intraday_examples", theme)


def fig_heartbeat(T, theme):
    G, typ = INTRA["grids"], day_type()
    fig, axs = plt.subplots(1, 2, figsize=(14, 4.4), sharey=True)
    NUM["heartbeat"] = {}
    for ax, s_ in zip(axs, ["SHY", "TLT"]):
        for k in ["control", "NFP", "CPI", "FOMC"]:
            ds = [d for d in G if typ.get(d) == k]
            A = pd.concat([G[d][s_].abs() for d in ds], axis=1).mean(1)
            A = A.loc[it.hm("07:00"):it.hm("17:00")]
            x = [pd.Timestamp("2026-01-01") + pd.Timedelta(seconds=int(v)) for v in A.index]
            col = T["gray"] if k == "control" else T["s"][EV_COL[k]]
            ax.plot(x, A.values, color=col, lw=2 if k != "control" else 1.6, label=f"{EV_NAME[k]} ({len(ds)} days)")
            NUM["heartbeat"][f"{s_}_{k}"] = dict(peak=round(float(A.max()), 2), median=round(float(A.median()), 2))
        ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%H:%M"))
        ax.axvspan(pd.Timestamp("2026-01-01 09:30"), pd.Timestamp("2026-01-01 16:00"), color=T["rec"], lw=0, zorder=0)
        panel_title(ax, f"{MAT_LAB[s_]}", T)
        ax.set_yscale("log")
        ax.set_yticks([0.1, 0.3, 1, 3])
        ax.set_yticklabels(["0.1", "0.3", "1", "3"])
        ax.yaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    axs[0].set_ylabel("average |5-minute yield move| (bp, log)")
    axs[1].legend(loc="upper right", fontsize=9)
    fig.suptitle("The heartbeat of the Treasury market: rates jump at 8:30 and 2:00", x=0.055, y=1.08, ha="left",
                 fontsize=12.5, fontweight="bold", color=T["ink"])
    fig.text(0.055, 1.0, "Average absolute 5-minute yield change by time of day (ET), 2018–2026. Shaded: regular "
             "stock-market hours, when the ETFs are most liquid.", color=T["muted"], fontsize=9.5)
    fig.tight_layout()
    save(fig, "heartbeat", theme)


def rms(x):
    return np.sqrt((x ** 2).mean())


def fig_event_size(T, theme):
    W = intra_windows()
    fig, ax = plt.subplots(figsize=(11, 4.4))
    groups = [("CPI", "release 8:30"), ("NFP", "release 8:30"), ("FOMC", "FOMC statement")]
    NUM["var_ratio"] = {}
    w = 0.26
    for j, (e, win) in enumerate(groups):
        x = W[(W.event == e) & (W.window == win) & W.scheduled]
        c = W[(W.event == "control") & (W.window == win)]
        ratio = (x[CORE].apply(rms) ** 2) / (c[CORE].apply(rms) ** 2)
        NUM["var_ratio"][e] = ratio.round(1).to_dict()
        NUM[f"rms_{e}"] = x[CORE].apply(rms).round(1).to_dict()
        NUM[f"rms_control_{win}"] = c[CORE].apply(rms).round(1).to_dict()
        xs = np.arange(4) + (j - 1) * w
        ax.bar(xs, ratio.values, width=w * 0.92, color=T["s"][EV_COL[e]], zorder=3,
               label=f"{EV_NAME[e]} ({len(x)})")
        for xi, v in zip(xs, ratio.values):
            ax.text(xi, v + 0.6, f"{v:.0f}×", ha="center", fontsize=8.5, color=T["muted"])
    ax.set_xticks(range(4))
    ax.set_xticklabels([MAT_LAB[s_] for s_ in CORE])
    ax.set_ylabel("variance vs the same window on quiet days")
    ax.grid(axis="x", visible=False)
    ax.set_title("One release is worth 10–35 normal half-hours at the front end")
    subtitle(ax, "Variance of the yield move in the 25–30 minutes around the release (8:25–8:50, 13:55–14:25), divided by "
                 "the same window on control days. 2018–2026.", T)
    ax.legend(loc="upper right")
    save(fig, "event_size", theme)


def implied_kappa(beta):
    from scipy.optimize import minimize_scalar
    def L(k):
        v = (1 - np.exp(-k * MAT)) / (k * MAT)
        return v / v[0]
    r = minimize_scalar(lambda k: ((L(k) - beta) ** 2).sum(), bounds=(1e-3, 5), method="bounded")
    return r.x


def fig_event_shape(T, theme):
    W = intra_windows()
    th = R["panel_fits"]["G2++"][0]
    thv = [th[k] for k in ["a1", "a2", "s1", "s2", "rho", "delta"]]
    _, B = ts.loadings("G2++", thv, MAT)
    kv = R["panel_fits"]["Vasicek"][0]["kappa"]
    _, Bv = ts.loadings("Vasicek", [kv, 0.05, 0.01], MAT)
    fig, (a, b) = plt.subplots(1, 2, figsize=(14, 4.8), gridspec_kw={"width_ratios": [1, 1.1]})
    NUM["shape"] = {}
    for e, win in [("CPI", "release 8:30"), ("NFP", "release 8:30"), ("FOMC", "FOMC statement")]:
        X = W[(W.event == e) & (W.window == win) & W.scheduled][CORE].values
        beta = np.array([np.sum(X[:, j] * X[:, 0]) / np.sum(X[:, 0] ** 2) for j in range(4)])
        def r2(L):
            bb, *_ = np.linalg.lstsq(L, X.T, rcond=None)
            return 1 - ((X - (L @ bb).T) ** 2).sum() / (X ** 2).sum()
        k = implied_kappa(beta)
        NUM["shape"][e] = dict(beta=beta.round(2).tolist(), r2_vasicek=round(float(r2(Bv)), 3), r2_g2=round(float(r2(B)), 3),
                               implied_kappa=round(float(k), 3), n=len(X))
        a.plot(MAT, beta, color=T["s"][EV_COL[e]], lw=2.2, marker="o", ms=6, mec=T["bg"], mew=1.2)
        a.annotate(f"{EV_NAME[e]}: κ ≈ {k:.2f}", (25, beta[-1]), xytext=(6, 0), textcoords="offset points",
                   va="center", fontsize=9.5, color=T["s"][EV_COL[e]], fontweight="bold")
        # scatter for panel b
        lvl, slp = X.mean(1), X[:, 0] - X[:, 3]
        b.scatter(lvl, slp, s=22, color=T["s"][EV_COL[e]], alpha=0.8, lw=0, label=f"{EV_NAME[e]} ({len(X)})", zorder=3)
    tt = np.linspace(1.9, 25, 100)
    for kk, lab in [(th["a1"], "G2++ level factor"), (th["a2"], "G2++ slope factor")]:
        v = (1 - np.exp(-kk * tt)) / (kk * tt)
        a.plot(tt, v / v[0], color=T["gray"], lw=1.4, ls=(0, (4, 2)))
        a.annotate(lab, (tt[-1], v[-1] / v[0]), xytext=(6, 0), textcoords="offset points", va="center", fontsize=9,
                   color=T["muted"])
    a.set_xlim(0, 36); a.set_ylim(-0.05, 1.12)
    a.set_xlabel("maturity (years)"); a.set_ylabel("move per 1bp move in the 2-year")
    a.set_title("How far down the curve each announcement reaches", pad=40)
    subtitle(a, "Average response by maturity, scaled to the 2-year. κ = the mean reversion a one-factor model\nwould need "
                "to produce that shape. Dashed: the two G2++ factors fitted to bond prices.", T)
    # panel b: one-factor => every event on one ray
    lim = 32
    vl = Bv[:, 0]
    ray = (vl[0] - vl[3]) / vl.mean()
    xx = np.linspace(-lim, lim, 10)
    b.plot(xx, ray * xx, color=T["ink"], lw=1.2, ls=(0, (4, 2)), zorder=2)
    b.annotate("one-factor Vasicek: every\nannouncement on this line", (lim * 0.75, ray * lim * 0.75), xytext=(-10, 24),
               textcoords="offset points", ha="right", fontsize=9, color=T["ink"],
               arrowprops=dict(arrowstyle="-", color=T["axis"], lw=0.8))
    b.axhline(0, color=T["axis"], lw=0.8); b.axvline(0, color=T["axis"], lw=0.8)
    b.set_xlim(-lim, lim); b.set_ylim(-22, 22)
    b.set_xlabel("average move across the curve (bp)")
    b.set_ylabel("2-year minus 25-year move (bp)")
    b.set_title("Fed news twists the curve; data news shifts it", pad=40)
    subtitle(b, "Each dot is one announcement. One factor forces every response onto the dashed line.", T)
    b.legend(loc="lower right", fontsize=9)
    fig.tight_layout(w_pad=3)
    save(fig, "event_shape", theme)


def fig_jumps(T, theme):
    """Daily variance on event days vs other days, and what scheduled jumps do to option-implied volatility."""
    ev = INTRA["events"]
    c = cmt[["2y", "5y", "10y", "30y"]].loc["2018":"2026-11-09"].dropna()
    dy = c.diff().dropna() * 100
    evd = set(ev[ev.time_et.isin(["08:30", "14:00"])].date)
    base = dy[~dy.index.isin(evd)].var()
    jv = {}
    for e in ["CPI", "NFP", "FOMC"]:
        m = dy.index.isin(set(ev[(ev.event == e) & ev.scheduled].date))
        jv[e] = (dy[m].var() - base).clip(lower=0)
    NUM["daily_std_nonevent"] = np.sqrt(base).round(2).to_dict()
    NUM["jump_std"] = {e: np.sqrt(v).round(2).to_dict() for e, v in jv.items()}
    NUM["jump_days_equiv"] = {e: (v / base).round(1).to_dict() for e, v in jv.items()}
    n = {"CPI": 12, "NFP": 12, "FOMC": 8}
    tot = base * (252 - 32) + sum((base + jv[e]) * n[e] for e in n)
    NUM["jump_share_annual_var"] = (sum(jv[e] * n[e] for e in n) / tot).round(3).to_dict()
    # panel: 2y yield-option normal vol by expiry, Q1 2026 calendar
    cal = ev[(ev.date >= "2026-01-02") & (ev.date <= "2026-03-31") & ev.scheduled & ev.event.isin(["CPI", "NFP", "FOMC"])]
    bd = pd.bdate_range("2026-01-02", "2026-03-31")
    var_d = np.array([base["2y"]] * len(bd))
    jump = np.zeros(len(bd))
    for e in cal.itertuples():
        i = bd.searchsorted(e.date)
        if i < len(bd):
            jump[i] += jv[e.event]["2y"]
    cumd, cumj = np.cumsum(var_d), np.cumsum(var_d + jump)
    t = np.arange(1, len(bd) + 1)
    vol_d = np.sqrt(cumd / t * 252)
    vol_j = np.sqrt(cumj / t * 252)
    fig, (a, b) = plt.subplots(1, 2, figsize=(14, 4.4), gridspec_kw={"width_ratios": [1, 1.4]})
    mats = ["2y", "5y", "10y", "30y"]
    w = 0.2
    for j, e in enumerate(["CPI", "NFP", "FOMC"]):
        a.bar(np.arange(4) + (j - 1) * w, [NUM["jump_days_equiv"][e][m] for m in mats], width=w * 0.92,
              color=T["s"][EV_COL[e]], label=EV_NAME[e], zorder=3)
    a.set_xticks(range(4)); a.set_xticklabels(mats)
    a.set_ylabel("extra variance, in normal days")
    a.grid(axis="x", visible=False)
    a.legend(loc="upper right", fontsize=9)
    a.set_title("Each release adds 1–2 days of variance at the front", pad=40)
    subtitle(a, "Event-day minus normal-day variance of daily yield changes,\n2018–2026, in units of one normal day.", T)
    b.step(bd, vol_j, where="post", color=T["s"][1], lw=2, label="G2++ + scheduled jumps")
    b.plot(bd, vol_d, color=T["gray"], lw=2, label="diffusion only")
    for e in cal.itertuples():
        b.axvline(e.date, color=T["s"][EV_COL[e.event]], lw=0.8, alpha=0.6, ymax=0.08)
    b.set_ylabel("2-year yield vol to expiry (bp per year)")
    b.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%b %d"))
    b.legend(loc="upper right", fontsize=9)
    b.set_title("Adding the jumps to the model: volatility steps up on release dates", pad=40)
    subtitle(b, "Normal volatility of the 2-year yield from 2 Jan 2026 to each option expiry, Q1 2026 release calendar.\n"
                "Ticks: CPI (blue), jobs (green), FOMC (orange).", T)
    NUM["q1_vol_2y"] = dict(diffusion=round(float(vol_d[-1]), 1), with_jumps=round(float(vol_j[-1]), 1),
                             max_with_jumps=round(float(vol_j.max()), 1))
    fig.tight_layout(w_pad=3)
    save(fig, "jumps", theme)


# ======================================================================================== PRICING
PR_PATH = ROOT / "data" / "processed" / "pricing.pkl"
PR = pickle.load(open(PR_PATH, "rb")) if PR_PATH.exists() else None


def fig_bond_example(T, theme):
    ex = PR["example"]
    b, t, cf = ex["bond"], ex["t"], ex["cf"]
    cols = {"Svensson": T["s"][0], "Vasicek": T["s"][1], "G2++ (one parameter set)": T["s"][2]}
    fig, (a, c) = plt.subplots(1, 2, figsize=(14, 4.6), gridspec_kw={"width_ratios": [1, 1.25]})
    tt = np.linspace(0.08, 10.5, 200)
    for lab, cv in ex["curves"].items():
        z = models.MODELS[cv["model"]](tt, cv["params"]) * 100
        a.plot(tt, z, color=cols[lab], lw=2, label=lab)
        a.scatter(t, cv["zero"] * 100, s=18, color=cols[lab], zorder=4, lw=0)
    a.set_xlabel("years from 30 Jun 2023"); a.set_ylabel("zero-coupon yield (%)")
    a.legend(loc="upper right", fontsize=9)
    a.set_title("Step 1: read a discount rate for every payment date", pad=40)
    subtitle(a, "Fitted zero curves on 30 Jun 2023; dots mark the note's 20 payment dates.", T)
    sv = ex["curves"]["Svensson"]
    pv = sv["pv"]
    x = np.arange(len(t))
    c.bar(x, cf, color=T["grid"] if theme == "light" else T["gray"], width=0.75, zorder=2, label="cash flow")
    c.bar(x, pv, color=T["s"][0], width=0.75, zorder=3, label="present value (Svensson curve)")
    c.set_yscale("log")
    c.set_yticks([1, 2, 5, 10, 50, 100]); c.set_yticklabels(["1", "2", "5", "10", "50", "100"])
    c.yaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    c.set_xticks(x[::2]); c.set_xticklabels([f"{(pd.Timestamp(ex['date']) + pd.Timedelta(days=int(v * 365.25))):%b %y}" for v in t[::2]],
                                              rotation=0, fontsize=8.5)
    c.set_ylabel("per 100 face (log scale)")
    c.legend(loc="upper left", fontsize=9)
    mk = b["dirty"]
    model_px = {lab: cv["pv"].sum() for lab, cv in ex["curves"].items()}
    NUM["example_bond"] = dict(cusip=b["TCUSIP"], coupon=b["coupon"], maturity=str(b["TMATDT"].date()),
                               market_dirty=round(float(mk), 3), market_clean=round(float(b["clean"]), 3),
                               model=dict((k, round(float(v), 3)) for k, v in model_px.items()),
                               err_bp_yield={k: round(float((v - mk) / (mk * b["dur"]) * -1e4), 1) for k, v in model_px.items()})
    c.set_title(f"Step 2: discount each payment and add them up", pad=40)
    subtitle(c, f"3.375% note due 15 May 2033. Sum of present values: {model_px['Svensson']:.2f}; market price "
                f"(mid + accrued): {mk:.2f}.", T)
    fig.tight_layout(w_pad=3)
    save(fig, "bond_example", theme)


def fig_holdout(T, theme):
    H = PR["holdout"]
    ms = ["Svensson", "Nelson-Siegel", "G2++", "CIR", "Vasicek"]
    g = H.groupby("model")[["ins", "oos"]].mean().loc[ms]
    NUM["holdout"] = H.round(2).to_dict("records")
    NUM["holdout_avg"] = g.round(2).to_dict()
    fig, ax = plt.subplots(figsize=(11, 4.2))
    y = np.arange(len(ms))[::-1]
    for yi, m in zip(y, ms):
        ax.plot([g.loc[m, "ins"], g.loc[m, "oos"]], [yi, yi], color=T["axis"], lw=2, zorder=2)
    ax.scatter(g.ins, y, s=70, color=T["s"][0], zorder=3, label="bonds used in the fit")
    ax.scatter(g.oos, y, s=70, color=T["s"][1], zorder=4, marker="D", label="bonds left out of the fit")
    for yi, m in zip(y, ms):
        ax.text(g.loc[m, "oos"] + 0.5, yi, f"{g.loc[m, 'ins']:.1f} → {g.loc[m, 'oos']:.1f}bp", va="center", fontsize=9.5,
                color=T["muted"])
    ax.set_yticks(y); ax.set_yticklabels(ms)
    ax.set_xlim(0, 16)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("RMSE of bond yields, average of the five dates (bp)")
    ax.set_title("Pricing bonds the model has never seen")
    subtitle(ax, "5-fold hold-out: each bond is priced by a curve fitted on the other 80% of bonds that day. "
                 "Errors barely change, so no model is overfitting.", T)
    ax.legend(loc="upper right", fontsize=9)
    save(fig, "holdout", theme)


def fig_hedge(T, theme):
    Hs = PR["hedge"]
    labs = {"unhedged": "unhedged", "dur_2y": "duration hedge, 2-year", "dur_30y": "duration hedge, 30-year",
            "g2_2y_30y": "G2++ two-factor hedge, 2y + 30y"}
    cols = {"unhedged": T["gray"], "dur_2y": T["s"][6], "dur_30y": T["s"][1], "g2_2y_30y": T["s"][2]}
    NUM["hedge"] = {}
    fig, (a, b) = plt.subplots(1, 2, figsize=(14, 4.6), gridspec_kw={"width_ratios": [1, 1.3]})
    names = {"2019-06-28": "Jul 2019 – Jun 2020", "2021-06-30": "Jul 2021 – Jun 2022", "2023-06-30": "Jul 2023 – Jun 2024"}
    w = 0.26
    for j, k in enumerate(["dur_2y", "dur_30y", "g2_2y_30y"]):
        vals = []
        for s_, H in Hs.items():
            sd = H[list(labs)].std()
            vals.append(1 - sd[k] ** 2 / sd["unhedged"] ** 2)
            NUM["hedge"].setdefault(names[s_], {})[k] = dict(std=round(float(sd[k]), 3), var_removed=round(float(vals[-1]), 3),
                                                             unhedged_std=round(float(sd["unhedged"]), 3))
        xs = np.arange(3) + (j - 1) * w
        a.bar(xs, np.array(vals) * 100, width=w * 0.92, color=cols[k], label=labs[k], zorder=3)
        for xi, v in zip(xs, vals):
            a.text(xi, v * 100 + 1, f"{v:.0%}", ha="center", fontsize=8.5, color=T["muted"])
    a.set_xticks(range(3)); a.set_xticklabels(list(names.values()))
    a.set_ylim(0, 108); a.set_ylabel("share of daily P&L variance removed (%)")
    a.grid(axis="x", visible=False)
    a.legend(loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=3, fontsize=8.5)
    a.set_title("Hedging a 10-year note for a year", pad=40)
    subtitle(a, "Newest 10-year note, hedged every day with the newest 2- and 30-year.\nOne factor = match duration "
                "with one bond; two factors = neutralise both G2++ factors.", T)
    H = Hs["2023-06-30"]
    for k in labs:
        b.plot(H.index, H[k].cumsum(), color=cols[k], lw=2 if k != "unhedged" else 1.6, label=labs[k])
    b.axhline(0, color=T["axis"], lw=0.8)
    b.set_ylabel("cumulative P&L per 100 face")
    b.legend(loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=4, fontsize=8.5)
    b.set_title("Jul 2023 – Jun 2024, the deep inversion", pad=40)
    subtitle(b, "Price P&L of holding 100 face of the 10-year plus each hedge (carry excluded).", T)
    fig.tight_layout(w_pad=3)
    save(fig, "hedge", theme)


def fig_option(T, theme):
    o = PR["option"]
    NUM["option"] = dict(fwd_yield=round(float(o["fwd_y"] * 100), 3),
                         vols={k: round(float(v), 1) for k, v in o["vols"].items()},
                         atm={k: round(float(np.interp(o["fwd_y"], o["strikes_y"], v)), 3) for k, v in o["prices"].items()},
                         mc_vs_closed=[(round(float(m), 4), round(float(sd), 4),
                                        round(float(np.interp(k, o["strikes_y"], o["closed_q"])), 4))
                                       for k, (m, sd) in zip(o["mc_strikes"], o["mc"])])
    fig, ax = plt.subplots(figsize=(11, 4.6))
    xs = o["strikes_y"] * 100
    cols = [T["s"][0], T["s"][3], T["s"][1]]
    for (lab, v), c in zip(o["prices"].items(), cols):
        ax.plot(xs, v, color=c, lw=2.2, label=f"vol {o['vols'][lab]:.0f}bp/yr: {lab}")
    ax.errorbar(o["mc_strikes"] * 100, [m for m, _ in o["mc"]], yerr=[2 * s_ for _, s_ in o["mc"]], fmt="o", ms=6,
                color=T["ink"], mfc=T["bg"], lw=1.2, zorder=5, label="Monte Carlo, G2++ (100,000 paths)")
    ax.axvline(o["fwd_y"] * 100, color=T["muted"], lw=0.8, ls=(0, (3, 3)))
    ax.text(o["fwd_y"] * 100, 1.0, " forward", transform=ax.get_xaxis_transform(), fontsize=9, color=T["muted"], va="top")
    ax.set_xlabel("strike, as a 10-year zero yield in one year (%)")
    ax.set_ylabel("call price per 100 face")
    ax.set_title("A one-year call on a 10-year zero-coupon bond, 30 Jun 2023")
    subtitle(ax, "Closed-form G2++ price (Brigo-Mercurio), checked by simulation. The curve fixes the forward; the option "
                 "price depends on a volatility the curve doesn't pin down.", T)
    ax.legend(loc="upper left", fontsize=9)
    save(fig, "option", theme)


# ===================================================================================== ROBUSTNESS
RB_PATH = ROOT / "data" / "processed" / "robustness.pkl"
RB = pickle.load(open(RB_PATH, "rb")) if RB_PATH.exists() else None


def fig_out_of_sample(T, theme):
    Rf, Ro, inv = RB["rmse_full"], RB["rmse_frozen"], RB["inverted"]
    PL = RB["panel_long"]
    cut = pd.Timestamp("2016-01-01")
    NUM["oos"] = {}
    for m in Ro:
        o, f = Ro[m].loc["2016":], Rf[m].loc["2016":]
        ii = inv.loc["2016":]
        NUM["oos"][m] = dict(in_sample_2016_26=round(float(f.median()), 1), frozen_2016_26=round(float(o.median()), 1),
                             frozen_inverted_2022_25=round(float(o[ii].median()), 1),
                             in_sample_inverted_2022_25=round(float(f[ii].median()), 1),
                             full_median=round(float(Rf[m].median()), 1),
                             full_inverted=round(float(Rf[m][inv].median()), 1),
                             full_normal=round(float(Rf[m][~inv].median()), 1))
    NUM["halves"] = {m: dict(early={k: round(float(v), 4) for k, v in h["params_early"].items()},
                             late={k: round(float(v), 4) for k, v in h["params_late"].items()},
                             late_months_own=round(float(h["rmse_late_params"].loc["1999":].median()), 1),
                             late_months_other=round(float(h["rmse_early_params"].loc["1999":].median()), 1),
                             early_months_own=round(float(h["rmse_early_params"].loc[:"1998"].median()), 1),
                             early_months_other=round(float(h["rmse_late_params"].loc[:"1998"].median()), 1))
                     for m, h in RB["halves"].items()}
    fig, (a, b) = plt.subplots(1, 2, figsize=(14, 4.6), gridspec_kw={"width_ratios": [1.6, 1]})
    shade(a, [(x, y) for x, y in INV if y >= PL.index[0]], T["inv"])
    a.axvspan(cut, PL.index[-1], color=T["rec"], lw=0, zorder=0)
    for m, lab in [("Vasicek", "Vasicek"), ("G2++", "G2++"), ("Dynamic NS", "Dynamic NS")]:
        sm_ = Ro[m].rolling(12, center=True, min_periods=6).median()
        a.plot(sm_.index, sm_, color=T["s"][MODEL_COL[m]], lw=2, label=lab)
    a.axvline(cut, color=T["muted"], lw=0.9, ls=(0, (3, 3)))
    a.text(cut, 0.98, "  parameters frozen →\n  out of sample", transform=a.get_xaxis_transform(), fontsize=9,
           color=T["muted"], va="top")
    a.set_yscale("log"); a.set_yticks([5, 10, 20, 50, 100]); a.set_yticklabels(["5", "10", "20", "50", "100"])
    a.yaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    a.set_ylabel("RMSE across maturities (bp, log)")
    a.legend(loc="lower left", fontsize=9, ncol=3)
    a.set_title("Parameters estimated on 1971–2015, then frozen", pad=40)
    subtitle(a, "12-month median of monthly fit error, Aug 1971 – Oct 2026. Only the factors are\nre-read each month after 2015. "
                "Orange: inversions.", T)
    ms = ["G2++", "Dynamic NS", "Vasicek"]
    y = np.arange(3)[::-1]
    w = 0.26
    for j, (k, lab, col) in enumerate([("in_sample_2016_26", "fitted on all years, 2016–26", T["gray"]),
                                        ("frozen_2016_26", "frozen at 2015, 2016–26", T["s"][0]),
                                        ("frozen_inverted_2022_25", "frozen at 2015, inverted months 2022–25", T["s"][1])]):
        vals = [NUM["oos"][m][k] for m in ms]
        b.barh(y + (1 - j) * w, vals, height=w * 0.9, color=col, label=lab, zorder=3)
        for yi, v in zip(y + (1 - j) * w, vals):
            b.text(v + 1.5, yi, f"{v:.0f}", va="center", fontsize=8.5, color=T["muted"])
    b.set_yticks(y); b.set_yticklabels(ms)
    b.set_xlim(0, 140); b.grid(axis="y", visible=False)
    b.set_xlabel("median monthly RMSE (bp)")
    b.legend(loc="upper right", fontsize=8.5)
    b.set_title("Out of sample, two factors still fit the inversion", pad=40)
    subtitle(b, "G2++ frozen at 2015 prices the 2022–25 inversion\nas well as a fit that saw it.", T)
    fig.tight_layout(w_pad=3)
    save(fig, "out_of_sample", theme)


def fig_term_premium_long(T, theme):
    D = RB["decomp_long"]
    acm = R["acm"].reindex(D.index)
    PL = RB["panel_long"]
    NUM["tp_long"] = dict(corr=round(float(np.corrcoef(D.tp, acm.ACMTP10)[0, 1]), 3), mean_ours=round(float(D.tp.mean()), 2),
                          mean_acm=round(float(acm.ACMTP10.mean()), 2),
                          rmse_gap=round(float(np.sqrt(((D.tp - acm.ACMTP10) ** 2).mean())), 2),
                          params={k: (np.round(v, 4).tolist() if hasattr(v, "__len__") else round(float(v), 4))
                                  for k, v in RB["pq_params_long"].items()})
    k = pd.Timestamp("2023-05-31")
    spread = PL.loc[k, 0.25] - PL.loc[k, 10.0]
    short = {"ours, 1990–2026": -NUM["inversion_decomp"]["May 2023"]["neg_tp"] * -1,
             "ours, 1971–2026": -(PL.loc[k, 10.0] - D.loc[k, "expect"]),
             "NY Fed (ACM)": -acm.loc[k, "ACMTP10"]}
    shares = {lab: v / spread for lab, v in short.items()}
    NUM["may2023_tp_share"] = {lab: round(float(v), 3) for lab, v in shares.items()}
    fig, (a, b) = plt.subplots(1, 2, figsize=(14, 4.4), gridspec_kw={"width_ratios": [1.6, 1]})
    shade(a, [(x, y) for x, y in INV if y >= D.index[0]], T["inv"])
    a.axhline(0, color=T["muted"], lw=0.9)
    a.plot(acm.index, acm.ACMTP10, color=T["gray"], lw=1.6, label="NY Fed, Adrian-Crump-Moench")
    a.plot(D.index, D.tp, color=T["s"][0], lw=1.8, label="ours, G2++ + VAR on 1971–2026")
    a.set_ylabel("10-year term premium (%)")
    a.legend(loc="upper right", fontsize=9)
    a.set_title("With 55 years of data, our term premium matches the NY Fed's level", pad=40)
    subtitle(a, f"Average {NUM['tp_long']['mean_ours']:.2f} vs {NUM['tp_long']['mean_acm']:.2f} points, correlation "
                f"{NUM['tp_long']['corr']:.2f}. On 1990–2026 alone ours was 0.9 points too high.", T)
    labs = list(shares)
    yy = np.arange(len(labs))[::-1]
    b.barh(yy, [shares[l] * 100 for l in labs], color=[T["s"][0], T["s"][0], T["gray"]], height=0.55, zorder=3)
    for yi, l in zip(yy, labs):
        b.text(shares[l] * 100 + 1.5, yi, f"{shares[l]:.0%}", va="center", fontsize=9.5, color=T["ink"])
    b.set_yticks(yy); b.set_yticklabels(labs)
    b.set_xlim(0, 70); b.grid(axis="y", visible=False)
    b.set_xlabel("share of the May 2023 inversion from a negative term premium (%)")
    b.set_title("A third to a half of 2023's inversion", pad=40)
    subtitle(b, f"3-month minus 10-year spread: {spread:.2f} points. The rest\nis expected rate cuts.", T)
    fig.tight_layout(w_pad=3)
    save(fig, "term_premium_long", theme)


def fig_recession(T, theme):
    pr, rec = RB["probit"], RB["recession"]
    PL = RB["panel_long"]
    NUM["probit"] = {k: dict(params={kk: round(float(vv), 3) for kk, vv in v["params"].items()},
                             z={kk: round(float(vv), 1) for kk, vv in v["z"].items()},
                             pseudo_r2=round(float(v["pseudo_r2"]), 3), auc=round(float(v["auc"]), 3),
                             p_may2023=round(float(v["prob"].loc["2023-05-31"]), 2),
                             p_dec2006=round(float(v["prob"].loc["2006-12-31"]), 2),
                             p_aug2019=round(float(v["prob"].loc["2019-08-31"]), 2),
                             peak_2022_25=round(float(v["prob"].loc["2022":"2025"].max()), 2))
                     for k, v in pr.items()}
    NUM["probit_realtime"] = {k: dict(z={kk: round(float(vv), 1) for kk, vv in v["z"].items()},
                                      pseudo_r2=round(float(v["pseudo_r2"]), 3), auc=round(float(v["auc"]), 3),
                                      p_may2023=round(float(v["may2023"]), 2)) for k, v in RB["probit_realtime"].items()}
    recp = []
    on = None
    for t_, v in rec.items():
        if v and on is None:
            on = t_
        if not v and on is not None:
            recp.append((on, t_)); on = None
    fig, ax = plt.subplots(figsize=(11.5, 4.6))
    shade(ax, recp, T["rec"])
    for k, lab, col, ls in [("spread", "raw 10y − 3m spread", T["s"][0], "-"),
                            ("both parts", "expected-rates part + term premium", T["s"][1], "-"),
                            ("expected part", "expected-rates part only", T["s"][2], (0, (4, 2)))]:
        p_ = pr[k]["prob"]
        ax.plot(p_.index, p_ * 100, color=col, lw=1.8 if ls == "-" else 1.5, ls=ls,
                label=f"{lab} (AUC {pr[k]['auc']:.2f})")
    ax.axhline(50, color=T["muted"], lw=0.8, ls=(0, (3, 3)))
    k = pd.Timestamp("2023-05-31")
    note(ax, (k, pr["spread"]["prob"].loc[k] * 100), f"May 2023: {pr['spread']['prob'].loc[k]:.0%} (spread),\n"
         f"{pr['both parts']['prob'].loc[k]:.0%} (both parts). No recession followed.", T, dx=-120, dy=8, ha="right")
    ax.set_ylim(0, 100); ax.set_xlim(PL.index[0], PL.index[-1])
    ax.set_ylabel("probability of recession in 12 months (%)")
    ax.legend(loc="upper right", fontsize=9)
    ax.set_title("What the curve said about recession risk, 1971–2026")
    subtitle(ax, "Probit models of an NBER recession 12 months ahead, estimated on Aug 1971 – Oct 2025. Gray: recessions.", T)
    save(fig, "recession_probability", theme)


FIGS = [fig_hero, fig_episodes, fig_evolution, fig_pca, fig_vasicek_shapes, fig_june2023, fig_five_perdate,
        fig_five_shared, fig_rmse_matrix, fig_through_time, fig_g2_factors, fig_vasicek_params,
        fig_pq_reversion, fig_term_premium, fig_inversion_decomp, fig_paths, fig_forecast_episodes,
        fig_intraday_example, fig_heartbeat, fig_event_size, fig_event_shape, fig_jumps,
        fig_bond_example, fig_holdout, fig_hedge, fig_option,
        fig_out_of_sample, fig_term_premium_long, fig_recession]

if __name__ == "__main__":
    only = sys.argv[1:]
    for theme, T in THEMES.items():
        style(T)
        for f in FIGS:
            if not only or f.__name__ in only:
                f(T, theme)
    NUM["vasicek_shared_5dates"] = {k: round(float(v), 4) for k, v in R["shared"]["Vasicek"][0].iloc[0][["kappa", "theta", "sigma"]].items()}
    NUM["g2_shared_5dates"] = {k: round(float(v), 4) for k, v in R["shared"]["G2++"][0].iloc[0][["a1", "a2", "s1", "s2", "rho", "delta"]].items()}
    g2 = R["shared"]["G2++"][0]
    NUM["g2_shared_5dates_slope_pct"] = {SHORT[str(d.date())]: round(float(x * 100), 2) for d, x in g2.x2.items()}
    NUM["pq_params"] = {k: (np.round(v, 4).tolist() if hasattr(v, "__len__") else round(float(v), 4)) for k, v in PQ.items()}
    NUM["bonds_per_date"] = {SHORT[d]: int(len(b)) for d, b in R["bonds"].items()}
    json.dump(NUM, open(ROOT / "data" / "processed" / "numbers.json", "w"), indent=1, default=str)
    print(json.dumps(NUM, indent=1, default=str))
