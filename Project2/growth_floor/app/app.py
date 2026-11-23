#!/usr/bin/env python
"""Plotly Dash app for comparing strategies (SPEC 03 §6).

    python app/app.py            # then open http://127.0.0.1:8050
    python app/app.py --port 8051 --results results

The app only reads files under results/ (full run) and results/quick/ (quick run); it never re-runs the main
simulations, so its numbers match the report. The SPEC 04 dynamic-programming policy is evaluated separately
(scripts/05_dp.py -> results_dp/, config dp.results_dir); its rows are merged into the full run as strategy
`dp_policy`, and the "DP policy" tab shows the policy tables in results/policies/. The "Custom" tab runs a small *live* simulation and is
labelled as not part of the report.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import sys
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import dash  # noqa: E402
import dash_bootstrap_components as dbc  # noqa: E402
from dash import Input, Output, State, dash_table, dcc, html  # noqa: E402
from gf.metrics import add_ce_xcash, ce_xcash_se  # noqa: E402

FLOOR, TARGET = 95e6, 0.02
STRATEGY_KEYS = ["cash", "merton_const", "merton_vol", "cppi", "cppi_vol", "obpi_merton", "obpi_merton_untuned",
                 "dp_policy", "dp_premrobust"]
DEFAULT_STRATS = ["merton_const", "cppi", "cppi_vol", "obpi_merton"]
KNOBBED = {"cppi", "cppi_vol", "obpi_merton"}
COLORS = {"cash": "#7f7f7f", "merton_const": "#1f77b4", "merton_vol": "#17becf", "cppi": "#d62728",
          "cppi_vol": "#ff7f0e", "obpi_merton": "#2ca02c", "obpi_merton_untuned": "#98df8a", "dp_policy": "#9467bd",
          "dp_premrobust": "#e377c2"}
LABELS = {"cash": "Cash", "merton_const": "Merton (const σ)", "merton_vol": "Merton (cond. σ)", "cppi": "CPPI",
          "cppi_vol": "CPPI (gap-aware)", "obpi_merton": "OBPI-Merton", "obpi_merton_untuned": "OBPI-Merton (K_eff=K)",
          "dp_policy": "DP (λ*)", "dp_premrobust": "DP (premium-robust)"}
RUN_DIRS = {"full": "results", "quick": "results/quick"}
DP_RUNS = {"full"}          # runs that merge the dp_policy rows from config dp.results_dir (same seeds as the full run)


def _cfg():
    try:
        from gf.data import load_config
        return load_config(ROOT / "config.yaml")
    except Exception:
        return {}


def dp_results_dir() -> Path:
    return ROOT / (_cfg().get("dp", {}).get("results_dir") or "results_dp")


def dp_extra_results_dirs() -> list[Path]:
    """Every extra results directory merged into the full run: results_dp (dp_policy) and, when configured,
    results_dp_mu (dp_premrobust, branch dp_mu)."""
    c = _cfg()
    out = [dp_results_dir()]
    pr = c.get("dp_premrobust", {}).get("results_dir")
    if pr:
        out.append(ROOT / pr)
    return out


def dp_policy_dir() -> Path:
    return ROOT / (_cfg().get("dp", {}).get("policy_dir") or "results/policies")


# --------------------------------------------------------------------------- data
class Results:
    """Loads result files once; missing files -> None (the UI then names the script to run)."""

    def __init__(self, root: Path, extra_roots: list[Path] | None = None):
        self.root = Path(root)
        self.summary = self._pq("mc/summary.parquet")
        if self.summary is not None:
            self.summary = self.summary.assign(_root=str(self.root))
        self.frontier = self._pq("mc/frontier.parquet")
        self.backtest = self._pq("backtest/summary.parquet")
        mp = self.root / "run_manifest.json"
        self.manifest = json.loads(mp.read_text()) if mp.exists() else None
        # merge extra result directories (results_dp: dp_policy, results_dp_mu: dp_premrobust); each row
        # remembers its root; rows of a strategy an extra directory provides replace that strategy's rows
        for er in extra_roots or []:
            p = Path(er) / "mc" / "summary.parquet"
            if p.exists():
                x = pd.read_parquet(p).assign(_root=str(er))
                self.summary = x if self.summary is None else pd.concat(
                    [self.summary[~self.summary.strategy.isin(x.strategy.unique())], x], ignore_index=True)
                if self.manifest is None:
                    mp2 = Path(er) / "run_manifest.json"
                    self.manifest = json.loads(mp2.read_text()) if mp2.exists() else None

        # CE convention: excess over the cash return (%/yr), cash strategy in the same cell (gf.metrics.add_ce_xcash)
        if self.summary is not None:
            self.summary = add_ce_xcash(self.summary)
        if self.frontier is not None and self.summary is not None:   # frontier: same eval seed, cash from summary
            self.frontier = add_ce_xcash(self.frontier, self.summary, ["asset", "state", "T_days", "scenario"])
        if self.backtest is not None:
            self.backtest = add_ce_xcash(self.backtest, keys=["asset", "T_days", "group_type", "group"])

    def _pq(self, rel):
        p = self.root / rel
        return pd.read_parquet(p) if p.exists() else None

    @property
    def ok(self):
        return self.summary is not None

    def terminal(self, rel: str, root: str | None = None) -> np.ndarray | None:
        p = Path(root or self.root) / rel
        return np.load(p) if p.exists() else None

    def paths(self, rel: str | None, root: str | None = None):
        if not rel or (isinstance(rel, float) and math.isnan(rel)):
            return None
        p = Path(root or self.root) / rel
        return dict(np.load(p, allow_pickle=False)) if p.exists() else None


@lru_cache(maxsize=4)
def load_results(run: str) -> Results:
    return Results(ROOT / RUN_DIRS.get(run, run), dp_extra_results_dirs() if run in DP_RUNS else None)


def available_runs():
    return [r for r, d in RUN_DIRS.items() if (ROOT / d / "mc" / "summary.parquet").exists()
            or (r in DP_RUNS and (dp_results_dir() / "mc" / "summary.parquet").exists())]


def strat_rows(df: pd.DataFrame, strats: list[str], tune_mode: str) -> pd.DataFrame:
    parts = []
    for s in strats:
        if s == "obpi_merton_untuned":
            q = df[(df.strategy == "obpi_merton") & (df.tune_mode == "untuned")].assign(skey=s)
        elif s in KNOBBED:
            q = df[(df.strategy == s) & (df.tune_mode == tune_mode)].assign(skey=s)
        else:
            q = df[df.strategy == s].assign(skey=s)
        parts.append(q)
    return pd.concat(parts) if parts else df.iloc[:0]


def resolve_state(res: Results, asset, state_mode, named, sigma, r0, scenario):
    """Return the state name to use (named dropdown, or the dense-grid cell nearest to the sliders)."""
    if state_mode == "named" or sigma is None:
        return named
    d = res.summary[(res.summary.asset == asset) & (res.summary.state_source == "dense_grid")]
    if d.empty:
        return named
    st = d[["state", "sigma0", "r0"]].drop_duplicates()
    i = ((np.log(st.sigma0) - math.log(sigma)) ** 2 + ((st.r0 - r0) * 10) ** 2).idxmin()
    return st.loc[i, "state"]


def msg_fig(text):
    f = go.Figure()
    f.add_annotation(text=text, showarrow=False, x=0.5, y=0.5, xref="paper", yref="paper", font={"size": 14})
    f.update_xaxes(visible=False)
    f.update_yaxes(visible=False)
    f.update_layout(height=300, template="plotly_white")
    return f


MISSING = "No results found. Run: python scripts/03_run_mc.py --asset us_mkt [--quick]"
MISSING_BT = "No backtest results. Run: python scripts/04_backtest.py --asset us_mkt [--quick]"


# --------------------------------------------------------------------------- figures / tables (pure functions)
def scorecard(run, asset, strats, tune_mode, state, T_days, scenario):
    res = load_results(run)
    if not res.ok:
        return [], MISSING
    d = res.summary
    d = d[(d.asset == asset) & (d.state == state) & (d.T_days == T_days) & (d.scenario == scenario)]
    d = strat_rows(d, strats or [], tune_mode)
    if d.empty:
        return [], f"No cell for state={state}, T={T_days}d, scenario={scenario} (not in the run plan)."
    wc = res.summary[(res.summary.asset == asset) & (res.summary.state == state) & (res.summary.T_days == T_days)]
    wset = (res.manifest or {}).get("config_snapshot", {}).get("mc", {}).get("worst_case_set", [])
    rows = []
    for _, r in d.iterrows():
        w = strat_rows(wc[wc.scenario.isin(wset)], [r.skey], tune_mode)
        flag = "red" if r.breach_lo > TARGET else ("amber" if r.breach_hi > TARGET else "green")
        rows.append({"strategy": LABELS.get(r.skey, r.skey), "mode": r.tune_mode,
                     "knob": "" if pd.isna(r.knob) else f"{r.knob_name}={r.knob:.3g}",
                     "breach %": f"{100 * r.breach:.2f} [{100 * r.breach_lo:.2f}, {100 * r.breach_hi:.2f}]",
                     "worst-case breach %": (f"{100 * w.breach.max():.2f} ({w.loc[w.breach.idxmax(), 'scenario']}, "
                                             f"{len(w)} scen.)") if len(w) else "",
                     "gap share": "" if pd.isna(r.breach_gap_share) else f"{r.breach_gap_share:.0%}",
                     "CE − cash %/yr": f"{100 * r.ce_xcash:.2f} ± "
                                       f"{100 * ce_xcash_se(r.ce, r.ce_se, r.ce_ann, r.T_days):.2f}",
                     "mean W $M": f"{r['mean'] / 1e6:.2f}", "median W $M": f"{r['median'] / 1e6:.2f}",
                     "q1% $M": f"{r['q1%'] / 1e6:.2f}", "q5% $M": f"{r['q5%'] / 1e6:.2f}",
                     "ES|breach $M": f"{r.es_given_breach / 1e6:.2f}", "avg φ": f"{r.phi_avg:.2f}",
                     "turnover": f"{r.turnover:.1f}", "locked %": f"{100 * r.locked_cash_share:.1f}",
                     "flag": flag})
    return rows, ""


def heatmap_fig(run, asset, strategy, tune_mode, r0, scenario, worst):
    res = load_results(run)
    if not res.ok:
        return msg_fig(MISSING)
    d = res.summary[res.summary.asset == asset]
    d = strat_rows(d, [strategy], tune_mode)
    dense = d[d.state_source == "dense_grid"]
    if not dense.empty:
        rr = dense.r0.unique()
        r_pick = rr[np.argmin(np.abs(rr - (r0 if r0 is not None else rr[0])))]
        d = dense[np.isclose(dense.r0, r_pick)]
        title_r = f"r0 = {r_pick:.2%}"
    else:
        d = d[d.state_source == "named"]
        title_r = "named states, median r0"
    wset = (res.manifest or {}).get("config_snapshot", {}).get("mc", {}).get("worst_case_set", [])
    if worst:
        d = d[d.scenario.isin(wset)]
        agg = d.groupby(["sigma0", "T_days"]).agg(breach=("breach", "max"), ce_xcash=("ce_xcash", "min"),
                                                  n=("scenario", "nunique")).reset_index()
        title = f"Worst case over {', '.join(wset)} (scenarios available per cell shown)"
    else:
        agg = d[d.scenario == scenario].groupby(["sigma0", "T_days"]).agg(
            breach=("breach", "mean"), ce_xcash=("ce_xcash", "mean"), n=("scenario", "nunique")).reset_index()
        title = f"Scenario {scenario}"
    if agg.empty:
        return msg_fig("No cells for this selection (dense grid exists only for base and dgp_garch_t).")
    sig = sorted(agg.sigma0.unique())
    Ts = sorted(agg.T_days.unique())
    Z = agg.pivot(index="T_days", columns="sigma0", values="breach").reindex(index=Ts, columns=sig).to_numpy() * 100
    C = agg.pivot(index="T_days", columns="sigma0", values="ce_xcash").reindex(index=Ts, columns=sig).to_numpy() * 100
    Nn = agg.pivot(index="T_days", columns="sigma0", values="n").reindex(index=Ts, columns=sig).to_numpy()
    xs = [f"{s:.0%}" for s in sig]
    ys = [f"{t / 252:.2f}y" for t in Ts]
    fig = make_subplots(rows=1, cols=2, subplot_titles=["Breach probability (%)", "CE − cash (%/yr)"],
                        horizontal_spacing=0.12)
    txt = [[f"{z:.2f}" + (f" ({int(n)})" if worst else "") if np.isfinite(z) else "" for z, n in zip(rz, rn)]
           for rz, rn in zip(Z, Nn)]
    fig.add_trace(go.Heatmap(z=Z, x=xs, y=ys, text=txt, texttemplate="%{text}", colorscale="RdYlGn_r", zmin=0,
                             zmax=max(4.0, np.nanmax(Z) if np.isfinite(Z).any() else 4), colorbar={"x": 0.44, "title": "%"}),
                  1, 1)
    fig.add_trace(go.Contour(z=Z, x=xs, y=ys, contours={"start": 2, "end": 2, "size": 1, "coloring": "none"},
                             line={"color": "black", "width": 3}, showscale=False, name="2%"), 1, 1)
    fig.add_trace(go.Heatmap(z=C, x=xs, y=ys, text=np.round(C, 2), texttemplate="%{text}", colorscale="Viridis",
                             colorbar={"title": "%"}), 1, 2)
    fig.update_layout(title=f"{LABELS.get(strategy, strategy)} ({tune_mode}) — {title}; {title_r}",
                      height=520, template="plotly_white")
    fig.update_xaxes(title="σ0")
    fig.update_yaxes(title="T")
    return fig


def _cell_rows(res, asset, strats, tune_mode, state, T_days, scenario):
    d = res.summary
    d = d[(d.asset == asset) & (d.state == state) & (d.T_days == T_days) & (d.scenario == scenario)]
    return strat_rows(d, strats or [], tune_mode)


def distribution_fig(run, asset, strats, tune_mode, state, T_days, scenario, logy):
    res = load_results(run)
    if not res.ok:
        return msg_fig(MISSING)
    d = _cell_rows(res, asset, strats, tune_mode, state, T_days, scenario)
    if d.empty:
        return msg_fig("No cell for this selection.")
    fig = make_subplots(rows=1, cols=2, subplot_titles=["Empirical CDF of W_T", "Histogram of W_T"])
    term, n_sub = None, 0
    for _, r in d.iterrows():
        term = res.terminal(r.terminal_file, r.get("_root"))
        if term is None:
            continue
        n_sub = len(term[0])
        w = np.sort(term[int(r.terminal_row)]) / 1e6
        p = np.arange(1, len(w) + 1) / len(w)
        c = COLORS.get(r.skey)
        fig.add_trace(go.Scatter(x=w, y=p, mode="lines", name=LABELS.get(r.skey), line={"color": c}), 1, 1)
        fig.add_trace(go.Histogram(x=w, nbinsx=120, name=LABELS.get(r.skey), marker={"color": c}, opacity=0.45,
                                   showlegend=False), 1, 2)
    fig.add_vline(x=FLOOR / 1e6, line={"dash": "dash", "color": "black"})
    fig.add_hline(y=TARGET, line={"dash": "dot", "color": "red"}, row=1, col=1)
    fig.update_xaxes(title="W_T ($M)")
    fig.update_yaxes(type="log" if logy else "linear", row=1, col=1)
    fig.update_layout(barmode="overlay", height=480, template="plotly_white",
                      title=f"{asset} {state} T={T_days / 252:.2f}y {scenario} ({n_sub} subsampled paths)")
    if term is None:
        return msg_fig("Terminal-wealth file missing; rerun scripts/03_run_mc.py.")
    return fig


def paths_fig(run, asset, strats, tune_mode, state, T_days, scenario, show_paths):
    res = load_results(run)
    if not res.ok:
        return msg_fig(MISSING)
    d = _cell_rows(res, asset, strats, tune_mode, state, T_days, scenario)
    if d.empty:
        return msg_fig("No cell for this selection.")
    files = {}
    for _, r in d.iterrows():
        key = (r.get("_root"), r.paths_file)
        if key not in files:
            files[key] = res.paths(r.paths_file, r.get("_root"))
    if all(v is None for v in files.values()):
        return msg_fig("Sample paths are stored for named states only (see DECISIONS: storage budget).")
    pz = next(v for v in files.values() if v is not None)
    days = pz["days"] / 252
    q = list(pz["quantiles"])
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, subplot_titles=["Wealth W_t ($M)", "Equity weight φ_t"],
                        vertical_spacing=0.08)
    i50 = q.index(0.5)
    fig.add_trace(go.Scatter(x=days, y=pz["Fq"][i50] / 1e6, name="floor PV F_t (median)",
                             line={"color": "black", "dash": "dash"}), 1, 1)
    for _, r in d.iterrows():
        pz = files.get((r.get("_root"), r.paths_file))
        if pz is None:
            continue
        keys = list(pz["keys"])
        k = f"{r.strategy}|{r.tune_mode}"
        if k not in keys:
            continue
        j = keys.index(k)
        c = COLORS.get(r.skey)
        for a_, row in (("Wq", 1), ("phiq", 2)):
            Q = pz[f"{a_}_{j}"] / (1e6 if a_ == "Wq" else 1)
            fig.add_trace(go.Scatter(x=np.r_[days, days[::-1]], y=np.r_[Q[0], Q[-1][::-1]], fill="toself",
                                     fillcolor=c, opacity=0.12, line={"width": 0}, showlegend=False,
                                     hoverinfo="skip"), row, 1)
            fig.add_trace(go.Scatter(x=np.r_[days, days[::-1]], y=np.r_[Q[1], Q[-2][::-1]], fill="toself",
                                     fillcolor=c, opacity=0.25, line={"width": 0}, showlegend=False,
                                     hoverinfo="skip"), row, 1)
            fig.add_trace(go.Scatter(x=days, y=Q[i50], line={"color": c}, name=LABELS.get(r.skey),
                                     showlegend=(row == 1)), row, 1)
        if show_paths:
            W, P_ = pz[f"W_{j}"][:20] / 1e6, pz[f"phi_{j}"][:20]
            for i in range(len(W)):
                fig.add_trace(go.Scatter(x=days, y=W[i], line={"color": c, "width": 0.6}, opacity=0.5,
                                         showlegend=False, hoverinfo="skip"), 1, 1)
                fig.add_trace(go.Scatter(x=days[:-1], y=P_[i][:-1], line={"color": c, "width": 0.6}, opacity=0.5,
                                         showlegend=False, hoverinfo="skip"), 2, 1)
    fig.update_xaxes(title="years", row=2, col=1)
    fig.update_layout(height=650, template="plotly_white",
                      title="Fan charts: 5–95% and 25–75% bands, median line (pointwise over the first "
                            f"{(res.manifest or {}).get('config_snapshot', {}).get('mc', {}).get('fan_subsample', 5000)} paths)")
    return fig


def frontier_fig(run, asset, strats, tune_mode, state, T_days):
    res = load_results(run)
    if res.frontier is None:
        return msg_fig("No frontier file. Run scripts/03_run_mc.py (frontier: base scenario, named states).")
    f = res.frontier[(res.frontier.asset == asset) & (res.frontier.state == state) & (res.frontier.T_days == T_days)]
    if f.empty:
        return msg_fig("Frontier computed for us_mkt named states only (base scenario).")
    fig = go.Figure()
    for (s, v), g in f.groupby(["strategy", "variant"]):
        g = g.sort_values("breach")
        nm = LABELS.get(s, s) + (f" [{v}]" if v else "")
        fig.add_trace(go.Scatter(x=g.breach * 100, y=g.ce_xcash * 100, mode="lines+markers", name=nm,
                                 line={"color": COLORS.get(s), "dash": "dot" if v == "sigma0_fixed" else "solid"},
                                 text=[f"{g.knob_name.iloc[0]}={k:.3g}" for k in g.knob],
                                 hovertemplate="%{text}<br>breach %{x:.2f}%<br>CE − cash %{y:.2f}%/yr"))
    d = res.summary
    d = d[(d.asset == asset) & (d.state == state) & (d.T_days == T_days) & (d.scenario == "base")]
    d = strat_rows(d, strats or [], tune_mode)
    for _, r in d.iterrows():
        fig.add_trace(go.Scatter(x=[r.breach * 100], y=[r.ce_xcash * 100], mode="markers",
                                 marker={"symbol": "star", "size": 15, "color": COLORS.get(r.skey),
                                         "line": {"color": "black", "width": 1}},
                                 name=f"{LABELS.get(r.skey)} ({r.tune_mode}, eval seed)"))
    fig.add_vline(x=TARGET * 100, line={"dash": "dash", "color": "red"})
    fig.update_layout(xaxis_title="breach probability (%)", yaxis_title="CE − cash (%/yr)", height=520,
                      template="plotly_white", title=f"Frontier: {asset} {state} T={T_days / 252:.2f}y (base)")
    fig.update_xaxes(range=[0, min(10, max(4, f.breach.quantile(0.7) * 100))])
    return fig


def robustness_fig(run, asset, strats, tune_mode, state, T_days):
    res = load_results(run)
    if not res.ok:
        return msg_fig(MISSING)
    d = res.summary
    d = strat_rows(d[(d.asset == asset) & (d.state == state) & (d.T_days == T_days)], strats or [], tune_mode)
    if d.empty:
        return msg_fig("No cells for this selection.")
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, subplot_titles=["Breach (%)", "CE − cash (%/yr)"])
    scen = sorted(d.scenario.unique(), key=lambda s: (s != "base", s))
    for s, g in d.groupby("skey"):
        g = g.set_index("scenario").reindex(scen)
        fig.add_trace(go.Bar(x=scen, y=g.breach * 100, name=LABELS.get(s), marker={"color": COLORS.get(s)},
                             error_y={"type": "data", "symmetric": False, "array": (g.breach_hi - g.breach) * 100,
                                      "arrayminus": (g.breach - g.breach_lo) * 100}), 1, 1)
        fig.add_trace(go.Bar(x=scen, y=g.ce_xcash * 100, name=LABELS.get(s), marker={"color": COLORS.get(s)},
                             showlegend=False), 2, 1)
    fig.add_hline(y=2, line={"dash": "dash", "color": "red"}, row=1, col=1)
    fig.update_layout(barmode="group", height=620, template="plotly_white",
                      title=f"Robustness: {asset} {state} T={T_days / 252:.2f}y (strategy always uses base estimates)")
    return fig


def backtest_fig(run, asset, strats, tune_mode, T_days):
    res = load_results(run)
    if res.backtest is None:
        return msg_fig(MISSING_BT)
    b = res.backtest[(res.backtest.asset == asset) & (res.backtest.T_days == T_days)]
    bm = "state" if tune_mode in ("state", "uniform_all") else tune_mode
    b = strat_rows(b, strats or [], bm)
    if b.empty:
        return msg_fig(f"No backtest rows for {asset} T={T_days}d.")
    fig = make_subplots(rows=1, cols=2, subplot_titles=["by σ0 bucket (expanding percentiles)", "by decade"],
                        column_widths=[0.35, 0.65])
    for col, gt in ((1, "bucket"), (2, "decade")):
        g0 = b[b.group_type == gt]
        order = ["calm", "normal", "elevated", "crisis"] if gt == "bucket" else sorted(g0.group.unique())
        for s, g in g0.groupby("skey"):
            g = g.set_index("group").reindex(order)
            fig.add_trace(go.Bar(x=[f"{o} (n_eff {ne:.0f})" if np.isfinite(ne) else o for o, ne in zip(order, g.n_eff)],
                                 y=g.breach * 100, name=LABELS.get(s), marker={"color": COLORS.get(s)},
                                 showlegend=(col == 1),
                                 error_y={"type": "data", "symmetric": False, "array": (g.breach_hi - g.breach) * 100,
                                          "arrayminus": (g.breach - g.breach_lo) * 100}), 1, col)
    fig.add_hline(y=2, line={"dash": "dash", "color": "red"})
    fig.update_layout(barmode="group", height=520, template="plotly_white", yaxis_title="realised breach (%)",
                      title=f"Historical backtest {asset}, T={T_days / 252:.2f}y, knobs: {bm}. Overlapping windows: "
                            "weak evidence on its own (complements the PIT test).")
    return fig


@lru_cache(maxsize=4)
def load_dp_policy_file(asset: str):
    p = dp_policy_dir() / f"dp_{asset}.npz"
    if not p.exists():
        return None
    with np.load(p, allow_pickle=False) as z:
        return {k: z[k] for k in z.files if k != "B"}


def dp_policy_fig(asset, tau, lam_i, state_sigma=None, T_days=None):
    """phi*(y, sigma) heatmap at tau days remaining for the lam grid value lam_i (SPEC 04)."""
    d = load_dp_policy_file(asset)
    if d is None:
        return msg_fig(f"No DP policy for {asset}. Run: python scripts/05_dp.py all --asset {asset}")
    lam = d["lam"]
    tau = int(min(max(int(tau or 252), 1), int(d["tau"][-1])))
    lam_i = int(min(max(int(lam_i or 0), 0), len(lam) - 1))
    P = d["phi_grid"][d["phi_idx"][lam_i, tau]]
    y, sig = d["y"], np.sqrt(d["h"] * 252)
    m = y <= 2.5
    fig = go.Figure(go.Heatmap(z=P[m], x=sig * 100, y=y[m], zmin=0, zmax=1, colorscale="Viridis",
                               colorbar={"title": "φ*"},
                               hovertemplate="σ %{x:.1f}%<br>y = W/F %{y:.3f}<br>φ* %{z:.3f}<extra></extra>"))
    fig.add_hline(y=1.0, line={"dash": "dash", "color": "white"})
    note = ""
    if state_sigma is not None:
        fig.add_vline(x=state_sigma * 100, line={"dash": "dot", "color": "red"})
        if "lam_star_idx" in d and T_days is not None:
            from gf.strategies import lookup_lam_idx
            note = f" · λ* for this σ0, T={T_days / 252:.2f}y: {lam[lookup_lam_idx(d, state_sigma, int(T_days))]:g}"
    fig.update_xaxes(type="log", title="conditional vol σ (annualised %)")
    fig.update_yaxes(type="log", title="y = W / F (robust floor)")
    fig.update_layout(height=560, template="plotly_white",
                      title=f"{asset}: DP equity weight φ*(τ = {tau} days left, y, σ), λ = {lam[lam_i]:g} "
                            f"(r0 = {float(d['r0_ann']):.2%}){note}")
    return fig


def dp_slider_props(asset):
    d = load_dp_policy_file(asset)
    if d is None:
        return {0: "0"}, 0, 0, 0
    lam = d["lam"]
    return {i: f"{v:g}" for i, v in enumerate(lam)}, 0, len(lam) - 1, min(3, len(lam) - 1)


def custom_run(asset, sigma0, r0, T_days, m, z, keff, n_paths=5000, limit_s=20.0):
    """Live 5,000-path simulation with the same engine (not part of the report)."""
    from gf.data import load_config
    from gf.engine import SimMarket, run_engine
    from gf.metrics import row_metrics
    from gf.scenarios import load_context
    from gf.strategies import make_strategy
    cfg = load_config(ROOT / "config.yaml")
    est = 2.5e-7 * n_paths * T_days * 6
    if est > limit_s:
        return None, f"Disabled: estimated {est:.0f}s > {limit_s:.0f}s."
    t0 = time.time()
    ctx = load_context(cfg, asset)
    P = cfg["problem"]
    s0 = {"sigma0_ann": float(sigma0), "r0_ann": float(r0)}
    dgp, rate, _ = ctx.dgp("base", {}, n_paths)
    strats = [make_strategy("cash", T_days), make_strategy("merton_const", T_days), make_strategy("merton_vol", T_days),
              make_strategy("cppi", T_days, [m]), make_strategy("cppi_vol", T_days, [z]),
              make_strategy("obpi_merton", T_days, [keff], cfg=cfg, vol_input=cfg["strategies"]["obpi_vol_input"])]
    out = run_engine(SimMarket(dgp, ctx.investor(), rate, s0, n_paths, seed=12345), strats, P["W0"], P["K"],
                     P["gamma"], s0, ctx.model_info(s0["sigma0_ann"], P["gamma"], cfg["strategies"]["floor"]),
                     cfg["engine"])
    rows = []
    mms = [row_metrics(res, 0, P["K"], P["W0"], r0, P["gamma"], {**cfg["mc"], "bootstrap_reps": 0}, 0) for res in out]
    cash_ce = mms[0]["ce_ann"]                 # strats[0] is cash: CE quoted as excess over it
    for res, mm in zip(out, mms):
        rows.append({"strategy": LABELS.get(res["strategy"], res["strategy"]),
                     "knob": "" if np.isnan(res["knob_values"][0]) else f"{res['knob_values'][0]:.3g}",
                     "breach %": f"{100 * mm['breach']:.2f} [{100 * mm['breach_lo']:.2f}, {100 * mm['breach_hi']:.2f}]",
                     "CE − cash %/yr": f"{100 * (mm['ce_ann'] - cash_ce):.2f}", "avg φ": f"{mm['phi_avg']:.2f}",
                     "gap share": "" if np.isnan(mm["breach_gap_share"]) else f"{mm['breach_gap_share']:.0%}"})
    return rows, f"Live run: {n_paths} paths, {time.time() - t0:.1f}s (seed 12345). Not part of the report."


def manifest_text(run):
    res = load_results(run)
    m = res.manifest
    if not m:
        return "No run manifest (results not generated yet)."
    a = m.get("assets", {})
    npaths = ", ".join(f"{k}: {v.get('n_paths_eval')}" for k, v in a.items())
    hashes = ", ".join(f"{k} {v[:12]}…" for k, v in (m.get("data_sha256") or {}).items())
    lr = m.get("last_run", {})
    return (f"Run '{run}' · master seed {m.get('master_seed')} · n_paths {npaths} · data {hashes} · "
            f"commit {m.get('git_commit')} · finished {lr.get('finished')}")


# --------------------------------------------------------------------------- layout
def figure_card(gid, extra=None):
    return dbc.Card(dbc.CardBody([
        html.Div([dbc.Button("Download PNG", id=f"{gid}-png", size="sm", color="secondary", className="me-2"),
                  html.Span(id=f"{gid}-pngmsg", className="text-muted small"), *(extra or [])], className="mb-2"),
        dcc.Loading(dcc.Graph(id=gid)), dcc.Download(id=f"{gid}-dl")]), className="mb-3")


def build_layout():
    runs = available_runs() or ["full"]
    run0 = "full" if "full" in runs else runs[0]
    res = load_results(run0)
    assets = sorted(res.summary.asset.unique(), key=lambda a: (a != "us_mkt", a)) if res.ok else ["us_mkt"]
    sidebar = dbc.Col([
        html.H5("Grow the money, don't break the floor"),
        dbc.Label("Run"), dcc.Dropdown(id="run", options=runs, value=run0, clearable=False),
        dbc.Label("Asset", className="mt-2"), dcc.Dropdown(id="asset", options=assets, value=assets[0], clearable=False),
        dbc.Label("Strategies", className="mt-2"),
        dcc.Dropdown(id="strats", options=[{"label": LABELS[k], "value": k} for k in STRATEGY_KEYS],
                     value=DEFAULT_STRATS, multi=True),
        dbc.Label("Tune mode", className="mt-2"),
        dbc.RadioItems(id="tune_mode", options=[{"label": "uniform, robust (excl. extreme)", "value": "uniform"},
                                                {"label": "uniform (incl. extreme)", "value": "uniform_all"},
                                                {"label": "state-dependent (robust)", "value": "state"},
                                                {"label": "state-dependent (base-only tuning)", "value": "state_base"}],
                       value="state"),
        dbc.Label("Starting state", className="mt-2"),
        dbc.RadioItems(id="state_mode", options=[{"label": "named", "value": "named"},
                                                 {"label": "σ0 / r0 grid", "value": "grid"}], value="named", inline=True),
        dcc.Dropdown(id="state", clearable=False),
        html.Div([dbc.Label("σ0"), dcc.Slider(id="sigma", min=0, max=1, step=None, value=None),
                  dbc.Label("r0"), dcc.Slider(id="r0", min=0, max=1, step=None, value=None)], id="grid_box"),
        dbc.Label("Horizon T", className="mt-2"), dcc.Slider(id="T", min=0, max=1, step=None, value=None),
        dbc.Label("Scenario", className="mt-2"), dcc.Dropdown(id="scenario", clearable=False),
        html.Div(id="state_note", className="small text-muted mt-2"),
    ], width=3, style={"backgroundColor": "#f8f9fa", "padding": "16px", "minHeight": "100vh"})
    tabs = dbc.Tabs([
        dbc.Tab(label="Scorecard", tab_id="t1", children=[
            html.Div(id="score_msg", className="text-danger my-2"),
            dbc.Button("Download CSV", id="score-csv", size="sm", color="secondary", className="my-2"),
            dcc.Download(id="score-dl"),
            dash_table.DataTable(id="score", sort_action="native", style_table={"overflowX": "auto"},
                                 style_cell={"fontSize": 13, "padding": "4px", "fontFamily": "sans-serif"},
                                 style_data_conditional=[
                                     {"if": {"filter_query": '{flag} = "red"', "column_id": "breach %"},
                                      "backgroundColor": "#f8d7da"},
                                     {"if": {"filter_query": '{flag} = "amber"', "column_id": "breach %"},
                                      "backgroundColor": "#fff3cd"},
                                     {"if": {"filter_query": '{flag} = "green"', "column_id": "breach %"},
                                      "backgroundColor": "#d1e7dd"}],
                                 style_header={"fontWeight": "bold"}),
            html.P("Breach colour: red = 95% CI entirely above 2%; amber = CI straddles 2%; green = below. "
                   "Worst case = max breach over the worst-case scenario set; gap share = breaches caused by a "
                   "one-day move through the strategy's own floor.", className="small text-muted mt-2")]),
        dbc.Tab(label="Breach heatmap", tab_id="t2", children=[
            dbc.Row([dbc.Col(dcc.Dropdown(id="hm_strat", options=[{"label": LABELS[k], "value": k} for k in STRATEGY_KEYS],
                                          value="cppi_vol", clearable=False), width=4),
                     dbc.Col(dbc.Checklist(id="hm_worst", options=[{"label": " worst case over scenarios", "value": 1}],
                                           value=[], switch=True), width=4)], className="my-2"),
            figure_card("hm_fig")]),
        dbc.Tab(label="Distribution", tab_id="t3", children=[
            dbc.Checklist(id="logy", options=[{"label": " log-scale CDF (left tail)", "value": 1}], value=[1],
                          switch=True, className="my-2"), figure_card("dist_fig")]),
        dbc.Tab(label="Paths", tab_id="t4", children=[
            dbc.Checklist(id="show_paths", options=[{"label": " show 20 sample paths", "value": 1}], value=[],
                          switch=True, className="my-2"), figure_card("paths_fig")]),
        dbc.Tab(label="Frontier", tab_id="t5", children=[figure_card("front_fig")]),
        dbc.Tab(label="Robustness", tab_id="t6", children=[figure_card("rob_fig")]),
        dbc.Tab(label="Backtest", tab_id="t7", children=[figure_card("bt_fig")]),
        dbc.Tab(label="DP policy", tab_id="t9", children=[
            html.P("SPEC 04 dynamic-programming policy: equity weight as a function of wealth relative to the "
                   "robust floor (y) and the conditional vol, for a chosen breach multiplier λ and days remaining τ. "
                   "The red line marks the selected starting state's σ0.", className="small text-muted my-2"),
            dbc.Label("τ (trading days remaining)"),
            dcc.Slider(id="dp_tau", min=1, max=504, step=1, value=252,
                       marks={t: str(t) for t in (1, 21, 63, 126, 252, 378, 504)}),
            dbc.Label("λ (breach multiplier, grid)"),
            dcc.Slider(id="dp_lam", min=0, max=7, step=1, value=3),
            figure_card("dp_fig")]),
        dbc.Tab(label="Custom (live, not in report)", tab_id="t8", children=[
            dbc.Alert("Live 5,000-path simulation with the same engine and base model. NOT part of the report; "
                      "disabled if it would take more than 20 s.", color="warning", className="my-2"),
            dbc.Row([dbc.Col([dbc.Label("σ0"), dbc.Input(id="c_sigma", type="number", value=0.15, step=0.01)]),
                     dbc.Col([dbc.Label("r0"), dbc.Input(id="c_r0", type="number", value=0.03, step=0.005)]),
                     dbc.Col([dbc.Label("T (days)"), dbc.Input(id="c_T", type="number", value=252, step=63)]),
                     dbc.Col([dbc.Label("CPPI m"), dbc.Input(id="c_m", type="number", value=8, step=0.5)]),
                     dbc.Col([dbc.Label("gap-aware z"), dbc.Input(id="c_z", type="number", value=5, step=0.5)]),
                     dbc.Col([dbc.Label("OBPI K_eff/K"), dbc.Input(id="c_k", type="number", value=1.02, step=0.01)])]),
            dbc.Button("Run", id="c_run", color="primary", className="my-2"),
            dcc.Loading([html.Div(id="c_msg", className="small text-muted"), dash_table.DataTable(id="c_table")])]),
    ], id="tabs", active_tab="t1")
    return dbc.Container([dbc.Row([sidebar, dbc.Col([tabs], width=9, style={"padding": "16px"})]),
                          html.Footer(id="footer", className="small text-muted p-2 border-top")], fluid=True)


# --------------------------------------------------------------------------- callbacks
def control_options(run, asset, state_mode, state):
    res = load_results(run)
    if not res.ok:
        return [], None, {}, 0, 1, None, {}, 0, 1, None, {}, 0, 1, None, [], None, {"display": "none"}, MISSING
    d = res.summary[res.summary.asset == asset]
    st = d[["state", "state_source", "sigma0", "r0"]].drop_duplicates("state")
    named = st[st.state_source.isin(["named", "anchor", "named_x_rate_grid"])].sort_values(["state_source", "sigma0"])
    opts = [{"label": f"{r.state} (σ0 {r.sigma0:.0%}, r0 {r.r0:.2%})", "value": r.state} for r in named.itertuples()]
    sval = state if state in set(named.state) else ("normal" if "normal" in set(named.state) else named.state.iloc[0])
    dense = st[st.state_source == "dense_grid"]
    sig = sorted(dense.sigma0.unique()) if len(dense) else sorted(named.sigma0.unique())
    rr = sorted(dense.r0.unique()) if len(dense) else sorted(named.r0.unique())
    smarks = {float(s): f"{s:.0%}" for s in sig}
    rmarks = {float(r): f"{r:.1%}" for r in rr}
    Ts = sorted(d.T_days.unique())
    tmarks = {int(t): f"{t / 252:.2f}y" for t in Ts}
    scen = sorted(d.scenario.unique(), key=lambda s: (s != "base", s))
    box = {} if state_mode == "grid" else {"display": "none"}
    note = ("Grid states exist for base and dgp_garch_t (us_mkt only); other scenarios use named states."
            if len(dense) else "This asset has named states only.")
    return (opts, sval, smarks, min(sig), max(sig), float(sig[len(sig) // 2]), rmarks, min(rr), max(rr), float(rr[0]),
            tmarks, min(Ts), max(Ts), int(Ts[0]), [{"label": s, "value": s} for s in scen], "base", box, note)


def make_app():
    app = dash.Dash(__name__, external_stylesheets=[dbc.themes.BOOTSTRAP], title="Growth-floor strategies",
                    suppress_callback_exceptions=True)
    app.layout = build_layout

    app.callback(
        [Output("state", "options"), Output("state", "value"), Output("sigma", "marks"), Output("sigma", "min"),
         Output("sigma", "max"), Output("sigma", "value"), Output("r0", "marks"), Output("r0", "min"),
         Output("r0", "max"), Output("r0", "value"), Output("T", "marks"), Output("T", "min"), Output("T", "max"),
         Output("T", "value"), Output("scenario", "options"), Output("scenario", "value"), Output("grid_box", "style"),
         Output("state_note", "children")],
        [Input("run", "value"), Input("asset", "value"), Input("state_mode", "value")], State("state", "value"))(
        control_options)

    def cur_state(run, asset, state_mode, named, sigma, r0, scenario):
        return resolve_state(load_results(run), asset, state_mode, named, sigma, r0, scenario)

    common = [Input("run", "value"), Input("asset", "value"), Input("strats", "value"), Input("tune_mode", "value"),
              Input("state_mode", "value"), Input("state", "value"), Input("sigma", "value"), Input("r0", "value"),
              Input("T", "value"), Input("scenario", "value")]

    @app.callback([Output("score", "data"), Output("score", "columns"), Output("score_msg", "children")], common)
    def _score(run, asset, strats, tm, smode, st, sg, r0, T, scen):
        if T is None:
            return [], [], ""
        rows, msg = scorecard(run, asset, strats, tm, cur_state(run, asset, smode, st, sg, r0, scen), T, scen)
        cols = [{"name": c, "id": c} for c in (rows[0].keys() if rows else []) if c != "flag"]
        return rows, cols, msg

    @app.callback(Output("score-dl", "data"), Input("score-csv", "n_clicks"), State("score", "data"),
                  prevent_initial_call=True)
    def _csv(n, data):
        return dcc.send_data_frame(pd.DataFrame(data).to_csv, "scorecard.csv", index=False)

    @app.callback(Output("hm_fig", "figure"), [Input("run", "value"), Input("asset", "value"), Input("hm_strat", "value"),
                                               Input("tune_mode", "value"), Input("r0", "value"),
                                               Input("scenario", "value"), Input("hm_worst", "value")])
    def _hm(run, asset, s, tm, r0, scen, worst):
        return heatmap_fig(run, asset, s, tm, r0, scen or "base", bool(worst))

    @app.callback(Output("dist_fig", "figure"), common + [Input("logy", "value")])
    def _dist(run, asset, strats, tm, smode, st, sg, r0, T, scen, logy):
        if T is None:
            return msg_fig("loading…")
        return distribution_fig(run, asset, strats, tm, cur_state(run, asset, smode, st, sg, r0, scen), T, scen, bool(logy))

    @app.callback(Output("paths_fig", "figure"), common + [Input("show_paths", "value")])
    def _paths(run, asset, strats, tm, smode, st, sg, r0, T, scen, sp):
        if T is None:
            return msg_fig("loading…")
        return paths_fig(run, asset, strats, tm, cur_state(run, asset, smode, st, sg, r0, scen), T, scen, bool(sp))

    @app.callback(Output("front_fig", "figure"), common)
    def _front(run, asset, strats, tm, smode, st, sg, r0, T, scen):
        if T is None:
            return msg_fig("loading…")
        return frontier_fig(run, asset, strats, tm, cur_state(run, asset, smode, st, sg, r0, scen), T)

    @app.callback(Output("rob_fig", "figure"), common)
    def _rob(run, asset, strats, tm, smode, st, sg, r0, T, scen):
        if T is None:
            return msg_fig("loading…")
        return robustness_fig(run, asset, strats, tm, cur_state(run, asset, smode, st, sg, r0, scen), T)

    @app.callback(Output("bt_fig", "figure"), [Input("run", "value"), Input("asset", "value"), Input("strats", "value"),
                                               Input("tune_mode", "value"), Input("T", "value")])
    def _bt(run, asset, strats, tm, T):
        if T is None:
            return msg_fig("loading…")
        return backtest_fig(run, asset, strats, tm, T)

    @app.callback([Output("c_table", "data"), Output("c_table", "columns"), Output("c_msg", "children")],
                  Input("c_run", "n_clicks"),
                  [State("asset", "value"), State("c_sigma", "value"), State("c_r0", "value"), State("c_T", "value"),
                   State("c_m", "value"), State("c_z", "value"), State("c_k", "value")], prevent_initial_call=True)
    def _custom(n, asset, sg, r0, T, m, z, k):
        rows, msg = custom_run(asset, sg, r0, int(T), m, z, k)
        if rows is None:
            return [], [], msg
        return rows, [{"name": c, "id": c} for c in rows[0]], msg

    @app.callback([Output("dp_lam", "marks"), Output("dp_lam", "min"), Output("dp_lam", "max"), Output("dp_lam", "value")],
                  Input("asset", "value"))
    def _dp_lam(asset):
        return dp_slider_props(asset)

    @app.callback(Output("dp_fig", "figure"), [Input("asset", "value"), Input("dp_tau", "value"),
                                               Input("dp_lam", "value"), Input("run", "value"),
                                               Input("state", "value"), Input("T", "value")])
    def _dp(asset, tau, lam_i, run, st, T):
        sg = None
        res = load_results(run) if run else None
        if res is not None and res.ok and st is not None:
            x = res.summary[(res.summary.asset == asset) & (res.summary.state == st)]
            sg = float(x.sigma0.iloc[0]) if len(x) else None
        return dp_policy_fig(asset, tau, lam_i, sg, T)

    @app.callback(Output("footer", "children"), Input("run", "value"))
    def _foot(run):
        return manifest_text(run)

    for gid in ["hm_fig", "dist_fig", "paths_fig", "front_fig", "rob_fig", "bt_fig", "dp_fig"]:
        def _png(n, fig, gid=gid):
            try:
                img = go.Figure(fig).to_image(format="png", width=1400, height=int((fig.get("layout", {}).get("height") or 500) * 1.4 / 1.4), scale=2)
                return dcc.send_bytes(img, f"{gid}.png"), ""
            except Exception as e:  # kaleido needs a Chrome install
                return None, f"PNG export failed ({type(e).__name__}): kaleido needs Chrome; use the camera icon instead."
        app.callback([Output(f"{gid}-dl", "data"), Output(f"{gid}-pngmsg", "children")],
                     Input(f"{gid}-png", "n_clicks"), State(gid, "figure"), prevent_initial_call=True)(_png)
    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8050)
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    make_app().run(host="127.0.0.1", port=a.port, debug=a.debug)


if __name__ == "__main__":
    main()
