# SPEC 03 — Strategies, Monte Carlo harness, and Plotly Dash comparison app

Builds on SPEC 01 and SPEC 02 (same repo, config, DECISIONS.md). Read SPEC 01's "Project context" first.
This stage implements the candidate strategies, a Monte Carlo harness that reports breach probability
and CE for every strategy × starting state × horizon × scenario, a historical backtest, and a
**Plotly Dash app** for comparing strategies interactively.

Constants: W_0 = $100M, floor K = $95M, γ = 2, breach target 2%, horizons
T ∈ {1.0, 1.25, 1.5, 1.75, 2.0} years = {252, 315, 378, 441, 504} trading days. All of these go in config.

## 1. Wealth dynamics (`src/gf/engine.py`)

For each path, day t = 0..N−1, with equity weight φ_t chosen at the start of day t:

```
W_{t+1} = W_t · (1 + rf_t + φ_t · (r_t − rf_t))
```

- φ_t ∈ [0, 1] enforced by the engine (clip, and count clips per strategy: a strategy that needs
  clipping should be reported).
- Present value of the floor at the start of day t, using the observable short rate:
  `F_t = K / (1 + r_ann_t)^((N − t)/252)`. Cushion `C_t = W_t − F_t`.
- Vectorised over paths, loop over days. Strategies receive arrays for all paths at once.

## 2. Strategy interface (`src/gf/strategies.py`)

```python
class Strategy:
    name: str
    params: dict                      # tunable knobs, e.g. {"m": 4}
    def reset(self, n_paths, n_days, W0, K, gamma, state0, model_info): ...
    def weight(self, t, W, F, sigma_d, r_ann, history) -> np.ndarray:  # returns φ_t, shape (n_paths,)
```

- `sigma_d`: conditional daily vol for day t (known at the start of day t). `history`: read-only access
  to past returns up to t−1 only. **No access to future arrays.** Enforce this by passing slices, and add a test.
- `model_info`: the estimated premium μ_e, the unconditional σ, etc., i.e. what the investor knows at t=0.
- Registry: `STRATEGIES = {name: factory}`; new strategies plug in without touching the engine or the app.

### Strategies to implement

1. **cash**: φ = 0. Sanity baseline.
2. **merton_const**: φ = clip(μ_e / (γ σ̄²), 0, 1), with σ̄ the unconditional vol. No floor logic.
3. **merton_vol**: φ_t = clip(μ_e / (γ σ_t²), 0, 1), using the conditional annualised σ_t (vol timing).
4. **cppi**: φ_t = clip(m · max(C_t, 0) / W_t, 0, 1). Knob: multiplier m.
5. **cppi_vol** (gap-aware CPPI): set the multiplier so the cushion survives a one-day move of z
   conditional standard deviations: `m_t = 1 / (z · σ_d,t)`, then the CPPI rule. Knob: z.
   Rationale (put in DECISIONS.md): CPPI breaks the floor only if equity falls by more than 1/m in one
   rebalancing interval, so m should shrink when vol is high.
6. **obpi_merton** (lecture "wealth constraint / option analogy" solution under BSM): the optimal
   payoff with a hard floor is `max(K, X_T)`, where X is the unconstrained Merton wealth process with constant
   equity weight φ_M = clip(μ_e/(γσ²), 0, 1), so X has volatility σ_X = φ_M σ. Time-t value:
   `W_t = X_t N(d1) + K e^{−r τ} N(−d2)` with `d1 = [ln(X_t/K) + (r + σ_X²/2)τ]/(σ_X √τ)`, `d2 = d1 − σ_X √τ`.
   Equity weight: `φ_t = φ_M · X_t N(d1) / W_t`. Choose X_0 so that W_0 = 100M (solve once with a root finder).
   Track X_t along each path by applying φ_M to the realised returns.
   Knobs: `vol_input` ∈ {"sigma0_fixed", "conditional"} (which σ goes into σ_X and d1/d2 each day) and
   `K_eff` (protected level, default K; can be lowered to spend the 2% breach budget or raised for a buffer).
7. **obpi_shortfall** (optional, if time): the lecture's "minimising shortfall" solution, which spends
   the 2% budget by not protecting the worst (highest-SPD) states. Implement only if obpi_merton works.
8. **dp_policy**: hook only. Loads `results/policies/{name}.npz` containing a grid over (τ, σ, W/F)
   and the φ table; trilinear interpolation. Not built in this spec; must not break if the file is missing
   (skip with a warning).

## 3. Tuning knobs to the breach constraint (`src/gf/tune.py`)

For strategies with a knob (m, z, K_eff), find the most aggressive value with breach ≤ target.

- Two modes, both reported:
  - **uniform**: one knob value for all starting states and horizons (the worst case binds).
  - **state-dependent**: knob as a function of (σ_0, T), tuned per grid cell and then smoothed /
    interpolated so it can be applied to any (σ_0, T).
- Tune to a **buffer** below 2% (config `tune.target_breach`, default 0.015) and explain the buffer
  (MC error, model error from SPEC 02's PIT results).
- **Tune on one seed and evaluate on a different, independent seed.** Never report tuning-seed numbers as performance.
- Monotone bisection on the knob (breach is monotone in m, z, K_eff; verify this, don't assume it).
- Save tuned knobs to `results/tuned/{asset}_{scenario}.json`.

## 4. Monte Carlo harness (`scripts/03_run_mc.py`)

Grid: strategies × starting states (named states and the dense σ_0 × r_0 grid from SPEC 02) × horizons × scenarios.

**Scenarios** (config list; each is a perturbation of the base model):
- `base`: SPEC 02 base model.
- `mu_low`: premium halved. `mu_zero`: premium 0. `premium_prop_var`: premium ∝ variance.
- `fat_tails`: ν set to max(3, ν̂ − 3).
- `persistent`: β raised so that persistence = min(0.995, p̂ + 0.01), holding unconditional variance fixed.
- `param_unc`: paths split across SPEC 02's parameter draws.
- `fhs`: bootstrap standardised residuals instead of t.
- `jump`: add a one-day −20% shock at a uniformly random day with probability 1% per path-year (stress test).
- `rate_const`: rates held at r_0.

Key rule: **the strategy always uses the investor's base estimates** (it does not know the scenario);
only the data-generating process changes. That is what makes this a robustness test.

Settings: `n_paths` default 50,000 (config; `--quick` flag uses 5,000). Common random numbers across
strategies within a (state, horizon, scenario) cell. Seeds derived deterministically from a master
seed + cell id; record all of them.

**Metrics per cell** (`src/gf/metrics.py`):
- breach probability P(W_T < K) with Wilson 95% CI
- expected shortfall given breach E[K − W_T | breach], and the 0.1% quantile of W_T
- CE = (E[W_T^(1−γ)])^(1/(1−γ)) (for γ = 2: CE = 1/E[1/W_T]), with a bootstrap SE
- CE expressed as annualised return and annualised excess over r_0
- mean, median, quantiles {0.5%, 1%, 2%, 5%, 25%, 50%, 75%, 95%} of W_T
- average φ, time-average φ along paths, turnover (mean Σ|Δφ|), fraction of paths that end up locked
  in cash (φ < 0.01 for the last 21 days)
- φ-clip counts

**Outputs**:
- `results/mc/summary.parquet`: one row per (asset, strategy, knob, tune_mode, state, sigma0, r0, T, scenario).
- `results/mc/terminal/{cell_id}.npy`: 5,000 subsampled W_T values per cell (float32), for distribution plots.
- `results/mc/paths/{cell_id}.npz`: 200 sample paths of W_t and φ_t per cell, plus pointwise quantiles
  (5/25/50/75/95%) over all paths, for fan charts.
- `results/mc/frontier.parquet`: CE vs breach for each strategy across a sweep of knob values (base scenario).
- `results/run_manifest.json`: git commit, config snapshot, master seed, data hash, timestamps, n_paths, runtime.

## 5. Historical backtest (`scripts/04_backtest.py`)

Run every strategy on real data from every historical start date (monthly grid), for each horizon, using
**expanding-window parameters estimated only on data before the start** (from SPEC 02) and tuned knobs
computed from those parameters (re-tune each year or use the state-dependent knob function; log the choice).
Report the realised breach frequency and CE-equivalent by σ_0 bucket and decade, with the effective
number of non-overlapping windows. Save to `results/backtest/summary.parquet` and per-start results.
Be explicit in the output that overlapping windows make this weak evidence on its own; it complements
the PIT test, it doesn't replace it.

Also run the same backtest on each industry asset with the procedure re-estimated from that asset's own
history (external validity).

## 6. Plotly Dash app (`app/app.py`)

`python app/app.py`, then open http://127.0.0.1:8050. The app **only reads files in `results/`**; it never
re-runs the main simulations, so its numbers always match the report. Use `dash`, `plotly`,
`dash-bootstrap-components`. Load results once at startup; cache.

**Controls (left sidebar)**
- Asset (us_mkt, industries)
- Strategies: multi-select (default: merton_const, cppi tuned, cppi_vol tuned, obpi_merton)
- Tune mode: uniform / state-dependent
- Starting state: named-state dropdown **or** σ_0 and r_0 sliders snapped to the grid
- Horizon T: slider over the configured horizons
- Scenario: dropdown (base + stress scenarios)

**Tabs**
1. **Scorecard**: DataTable, one row per selected strategy: breach % [CI], CE ($M and annualised),
   mean/median W_T, 1%/5% quantiles, ES given breach, average φ, turnover. Conditional formatting:
   breach CI entirely above 2% → red; CI straddles 2% → amber; below → green. Download-CSV button.
2. **Breach heatmap**: for one selected strategy, breach % over σ_0 (x) × T (y) at the chosen r_0 and
   scenario, with a 2% contour line and cell annotations. A second heatmap with CE. A toggle to show
   max breach across scenarios (the "worst case" view the committee cares about).
3. **Distribution**: overlaid empirical CDFs of W_T for the selected strategies, vertical line at $95M,
   horizontal line at 2%, with an option for a log-scale y-axis to zoom on the left tail; plus histograms.
4. **Paths**: fan charts of W_t (with the floor PV F_t drawn) and of φ_t for each selected strategy;
   toggle to show 20 individual sample paths.
5. **Frontier**: scatter of CE vs breach probability for every strategy and knob value (base scenario,
   chosen state and T), with a vertical line at 2%. This picture shows which design wins.
6. **Robustness**: grouped bars of breach % and CE across scenarios for the selected strategies at the
   chosen state and T; 2% reference line.
7. **Backtest**: realised breach frequency by σ_0 bucket and by decade from `results/backtest/`, with
   the effective sample size shown.
8. **Custom (optional, clearly labelled "live, not in report")**: run a quick 5,000-path simulation for
   an arbitrary σ_0, r_0, T and knob values, using the same engine. Show a spinner. Disabled if it takes > 20 s.

**General**
- Every figure has a "download PNG" button (`kaleido`), so slides use exactly the app's charts.
- Consistent colour per strategy across all tabs (define one mapping).
- Show the run manifest (seed, n_paths, data hash, commit) in a footer, so a screenshot is traceable.
- Missing result files → friendly message naming the script to run, not a crash.

## 7. Tests (`tests/test_engine.py`, `tests/test_app_smoke.py`)

- `cash`: W_T equals the compounded rf path exactly; breach = 0 when r_0 ≥ 0.
- φ = 1 under i.i.d. normal log returns with constant rf: the breach probability matches the analytic lognormal
  probability within MC error.
- CE formula: for lognormal W_T, CE matches the closed form within MC error.
- obpi_merton under GBM (constant σ, normal shocks, model vol = true vol): W_0 = 100M at t=0; breach → ~0
  as rebalancing frequency rises (test daily vs weekly); φ always in [0, 1].
- CPPI with m = 1 never breaches under bounded daily losses < 100%.
- No look-ahead: altering returns after day t leaves φ_0..φ_t unchanged for every strategy.
- Reproducibility: same seed → identical summary rows.
- App smoke test: the app imports, builds its layout, and each callback runs on the fixture results without exceptions.

## 8. DECISIONS.md entries to add (at least)

Engine conventions (floor PV with the observable rate); each strategy's rationale and the lecture result
it comes from; why the strategy uses base estimates under stress scenarios; tuning buffer and the
tune/evaluate seed split; uniform vs state-dependent knobs, with evidence; scenario list; n_paths and
the MC error it implies (at p = 2% and N = 50k, SE ≈ 0.06pp); dead ends (strategies or knobs that failed and why).
Update the "which script produces which number" table: every report number → script → results file.

## Acceptance criteria

- `python scripts/03_run_mc.py --asset us_mkt --quick` finishes in a few minutes; the full run
  completes and writes all outputs plus the manifest.
- `python scripts/04_backtest.py --asset us_mkt` runs.
- `python app/app.py` launches; all tabs render with the full and quick results.
- `pytest` passes.
- README explains: install → put data in data/raw → run scripts 01–04 → launch the app.

---

## Addendum from SPEC 02 findings (read before starting; overrides anything above it conflicts with)

Base model from SPEC 02: GJR-GARCH(1,1)-t with closure-gap returns scaled by sqrt(days), constant
arithmetic premium 3.91%/yr (raw 7.83% shrunk 50% toward 0). The conditional tail calibration
(PIT) does not reject it, but the test is weak. The unconditional 1–2-year left tail is thinner than history
(2-year 2% quantile −47% simulated vs −68% in the data) because volatility mean-reverts fast
(half-life 58 days). The point estimates in the elevated bucket and since 2000 are above 2%. So model
risk must be tested at the **strategy** level:

1. **Model-risk scenarios (add to the scenario list).** The data-generating process changes; the strategy
   still uses the base GJR-t estimates:
   - `dgp_garch_t`: SPEC 02's fitted GARCH(1,1)-t (half-life 212 days; matches the long-horizon tails).
     Implement as GJR with γ_lev = 0 and the GARCH-t parameters.
   - `dgp_egarch_t`: fitted EGARCH-t (best BIC). Requires an EGARCH recursion in the simulator; skip it with
     a logged note if it takes more than ~1 hour, because GJR and EGARCH gave near-identical 1–2-year tails.
   - Keep `fhs`, `param_unc`, `fat_tails`, `persistent`, `jump`, `mu_low`, `mu_zero`.
   Map each named starting state into each DGP by its σ_0 (same annualised conditional vol).
2. **Headline result = worst case over scenarios.** For each strategy, state and horizon, report the
   base breach and the max breach across {base, dgp_garch_t, fhs, param_unc, fat_tails, persistent};
   `jump` and the `mu_*` scenarios are reported separately as stress tests. The Dash heatmap's
   "worst case" toggle uses this set.
3. **Breach decomposition.** For every breaching path, record whether the breach was caused by a
   **gap** (a single day's loss larger than the cushion as a share of equity exposure at the start of that day, i.e.
   W fell from above F_t to below F_t in one day) or was **gradual**. Report the gap share per strategy and scenario,
   and in the Dash scorecard. Dynamic floor strategies should breach almost only through gaps; if gradual
   breaches appear, the strategy's rules are wrong.
4. **Equity–rate correlation.** Use the same-month (contemporaneous) pairing, ρ = −0.12, not the
   lagged one. Log the change. Effect is expected to be small (floor PV duration ≤ 2 years); report it once
   by rerunning the base cell with ρ = 0.
5. **States in the backtest.** For the historical backtest (script 04), classify start dates into σ_0
   buckets with **expanding-window** percentiles (no look-ahead); full-sample percentiles are fine for the
   simulated named states.
6. **Extreme state.** σ_0 = 125% (1987-10-19) stays in the grid; expect strategies to hold mostly cash.
   Report it and don't let it drive uniform-knob tuning on its own: report uniform tuning both with and without the
   `extreme` state.
7. **Pre-1952 annualisation.** Annualising with 252 *understates* pre-1952 calendar-year vol (18.3% vs 19.8%);
   the SPEC 02 addendum had the direction reversed. No code change, but use the correct statement anywhere it is reported.
