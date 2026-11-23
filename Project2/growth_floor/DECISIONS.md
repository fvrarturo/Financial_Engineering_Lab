# DECISIONS

## How to run

From a clean checkout, with the raw French CSVs in `data/raw/` (see README):

```bash
cd growth_floor
pip install -r requirements.txt
python scripts/01_load_check.py   # Stage 1: data -> data/processed/*.parquet, reports/data_check/*
python scripts/02_fit_model.py --asset us_mkt   # Stage 2: model, states, validation (~2 min, 8 cores)
python scripts/02_fit_model.py --asset all      # Stage 2 for every processed asset
python scripts/02b_var_backtest.py              # VaR backtest of the t tail (fat_tails role)
pytest                            # tests
python scripts/03_run_mc.py --asset all [--quick]   # Stage 3: tuning + Monte Carlo (quick -> results/quick/)
python scripts/04_backtest.py --asset all [--quick]  # Stage 4: historical backtest
python app/app.py                                    # Dash app, http://127.0.0.1:8050
python scripts/05_dp.py ...; python scripts/06_dp_premrobust.py ...   # DP (see README)
python scripts/07_report_numbers.py   # numbers.json + tables (+ figure inputs when the large files exist)
python scripts/08_build_report.py     # figures, deliverables/report.{md,html,pdf}, deck.{html,pdf}
python scripts/09_package.py          # submission/ and deliverables/growth_floor_submission.zip
```

All settings live in `config.yaml`. Stage 1 has no randomness. Stage 2 seeds: `model.seed` = 20261120.
PIT start i uses seed + 10000 + i (the same across models, so the comparison uses common random numbers).
The stylised-facts simulation uses seed + 1 and the parameter draws use seed + 2. Expanding-window refits are
cached in `results/model/cache/`, keyed by a data hash, the settings and the arch version. Deleting the cache
reproduces the same numbers.

## Which script produces which number

| report item | script | output file |
|---|---|---|
| Raw file hashes, sizes, date ranges | `scripts/01_load_check.py` | `data/raw/MANIFEST.json` |
| Asset frames (r, rf, x, rf_ann) for us_mkt and ind_* | `scripts/01_load_check.py` | `data/processed/{asset}.parquet` |
| Sample coverage, trading days per year, gaps | `scripts/01_load_check.py` | `reports/data_check/data_check.json` → `assets.<a>.checks.coverage`, `.dates` |
| Sentinel / NaN counts, dropped rows | `scripts/01_load_check.py` | `data_check.json` → `assets.<a>.checks.missing` |
| 20 largest \|r\| days | `scripts/01_load_check.py` | `data_check.json` → `assets.<a>.checks.extremes` |
| Daily vs monthly cross-check (us_mkt) | `scripts/01_load_check.py` | `data_check.json` → `assets.us_mkt.checks.monthly_crosscheck`; `figures/us_mkt/monthly_crosscheck.png` |
| RF precision, zero/negative RF by decade | `scripts/01_load_check.py` | `data_check.json` → `rf`; `figures/rf_daily_vs_monthly.png` |
| Summary stats by decade (mean, vol, skew, kurtosis, worst 1y/2y, P(1y/2y < −5%)) | `scripts/01_load_check.py` | `data_check.json` → `assets.<a>.checks.summary.table` |
| Data figures (cum. log wealth, rolling vol, rf_ann, histogram of x, rolling 1y return) | `scripts/01_load_check.py` | `reports/data_check/figures/<asset>/*.png` |
| Base model parameters, SEs, loglik, persistence, half-life, unconditional vol, premium, rate model, data hash | `scripts/02_fit_model.py` | `results/model/params_{asset}.json` |
| Model comparison (loglik/BIC/ν/persistence + PIT + VaR summary) | `scripts/02_fit_model.py` | `results/model/model_comparison_{asset}.csv` |
| Named states, rate grid, dense grid, anchor-date (σ_0, r_0) | `scripts/02_fit_model.py` | `results/model/states_{asset}.csv` |
| Filtered σ history (full-sample and expanding), secondary descriptors | `scripts/02_fit_model.py` | `results/model/filtered_{asset}.parquet` |
| PIT values per start date / tail-frequency tables by bucket and era | `scripts/02_fit_model.py` | `results/model/pit_{asset}.csv`; `reports/model/{asset}/model_report.json` → `pit_tables` |
| VaR backtests, 1987 crash probability | `scripts/02_fit_model.py` | `model_report.json` → `var`, `crash` |
| Stylised facts (vol, kurtosis, ACF, 1y/2y window quantiles, drawdowns) | `scripts/02_fit_model.py` | `model_report.json` → `stylised`; `figures/acf.png`, `figures/stylised_windows.png` |
| Sub-sample and robustness-window fits | `scripts/02_fit_model.py` | `model_report.json` → `subsamples`; `figures/param_stability.png` |
| Parameter-uncertainty draws | `scripts/02_fit_model.py` | `results/model/param_draws_{asset}.npy` |
| FHS residual pool | `scripts/02_fit_model.py` | `results/model/zhat_{asset}.npy` |
| Tuned knobs (robust: per cell, binding scenario, uniform, uniform incl. extreme, state-dependent function; base-only comparison) | `scripts/03_run_mc.py` | `results/tuned/{asset}_robust.json`, `{asset}_base.json` |
| Daily VaR backtest of the t tail (fitted ν, ν = 3) | `scripts/02b_var_backtest.py` | `results/model/var_backtest.json`, `.csv` |
| Scorecard numbers: breach [Wilson CI], ES, quantiles, CE ± bootstrap SE, CE annualised / excess, φ, turnover, locked share, clip shares, gap/gradual/rate shares | `scripts/03_run_mc.py` | `results/mc/summary.parquet` |
| Worst case over scenarios (headline) | `scripts/03_run_mc.py` | `results/mc/worst_case.parquet` |
| CE vs breach frontier (knob sweeps, base) | `scripts/03_run_mc.py` | `results/mc/frontier.parquet` |
| Terminal-wealth distributions / fan charts | `scripts/03_run_mc.py` | `results/mc/terminal/*.npy`, `results/mc/paths/*.npz` |
| Seeds, n_paths, data hash, runtimes, param-draw acceptance, fallback windows | `scripts/03_run_mc.py` | `results/run_manifest.json`, `results/mc/timings.parquet` |
| Historical backtest: realised breach / CE by σ_0 bucket and decade, n_eff | `scripts/04_backtest.py` | `results/backtest/summary.parquet`, `per_start.parquet`, `fits_{asset}.json` |
| DP policy, λ\* calibration, validation V1–V6 (us_mkt) | `scripts/05_dp.py` | `results/policies/dp_{asset}.npz`, `reports/dp/{calibration,validation}_us_mkt.json`, `results_dp/mc/` |
| Premium-robust DP: λ\*, evaluation, industry comparators | `scripts/06_dp_premrobust.py` | `results/policies/dp_premrobust_{asset}.npz`, `reports/dp_mu/*`, `results_dp_mu/mc/`, `results_ind_extra/mc/` |
| Deployment rule output | `scripts/07_report_numbers.py` (`gf.deployment`) | `results/deployment.csv`; numbers.json → `deploy.*` |

**Report and deck (SPEC 05).** Every number quoted in `deliverables/report.pdf` and `deck.pdf` is read from
`results/report/numbers.json` or a table in `results/report/tables/`. Both are written by `scripts/07_report_numbers.py` from the
files above. `scripts/08_build_report.py` renders the templates in `report/templates/`. The numbers.json keys, by report section:

| report item | numbers.json key / table |
|---|---|
| Mandate (W0, K, γ, 2%, horizons) | `mandate.*` |
| Tuning target, floor α, path counts, seeds, scenario definitions, MC standard errors | `settings.*` |
| Executive summary table | `tables/exec_compact_us_mkt.md` (raw: `exec_summary_us_mkt.csv`) |
| Headline: CE range, bp/yr over best simple rule, worst-case breach, stress maxima | `headline.*` |
| Every cell (asset × strategy × state × T): breaches by scenario, CE − cash, φ, turnover, quantiles, breach split | `cells.<asset>.<strategy>.<state>.T<1|2>.*`; `tables/all_cells.csv` |
| Data checks (RF precision, negative months, gaps, Saturdays, 1968, cross-check, hashes) | `data.*`; `tables/data_windows.md` |
| Model (params, BIC table, premium, Vasicek, states, anchors, stylised facts, PIT, sub-samples) | `model.*`; `tables/model_comparison_us_mkt`, `pit_us_mkt`, `param_stability_us_mkt` |
| Fallback window and per-asset diagnostics | `fallback.*`; `tables/assets_model` |
| Deployment rule | `deploy.*`; `tables/deployment`; `results/deployment.csv` |
| Performance by state (near-floor probability, quantiles, φ, turnover) | `perf.*`; `tables/performance_dp_us_mkt` |
| DP policy readings, λ\* lookup, floor premium | `policy.*` |
| Stress tests and failures | `stress.*`; `tables/stress_us_mkt`, `stress_failures_us_mkt` |
| Breach decomposition | `decomp.*`, `decomp_dp.*`; `tables/breach_decomposition_us_mkt` |
| Backtest | `backtest.*`; `tables/backtest_us_mkt`, `backtest_decade_us_mkt` |
| Industries / external validity, DP misses | `ind.*`; `tables/industries`, `industries_dp_vs_comparators`, `dp_misses` |
| Gap-aware CPPI negative CE − cash | `cppi_neg.*`; `tables/cppi_gap_negative_ce` |
| DP validation V1–V6 | `validation.*` |
| Dead ends with result files (base-only tuning, untuned OBPI) | `dead_ends.*` |
| Reproducibility (seeds, commits, hashes) | `repro.*`, `data.manifest` |

---

## 2026-11-20: French CSV parser built on section detection, not fixed skip-rows

**Decision**: `src/gf/io_french.py` scans the file line by line. A header is the first line whose first
field is empty and whose other fields are non-numeric names (e.g. `,Mkt-RF,SMB,HML,RF`). A section's
title is the non-blank, comma-free line directly above its header (otherwise `section_<i>`). A section
ends at the first blank line or the first non-numeric date field (the copyright footer). Dates with 8
digits are daily, 6 are monthly (converted to month-end) and 4 are annual (Dec 31). The `section=`
argument takes an index, or a string whose tokens must all appear in the title (`"value_weighted"`,
`"annual"`). Ambiguous matches raise an error. The default is the first section: value-weighted for
portfolio files, the monthly block for the monthly factor file. Values are divided by 100, and the frame
gets `attrs["units"]="decimal"` plus a metadata dict.
**Alternatives considered**: `pd.read_csv(skiprows=k, skipfooter=m)` with hand-counted offsets. It
breaks silently when French changes the preamble length, and it cannot separate value- and
equal-weighted blocks. Regex on section titles only: too brittle across file families.
**What was tested**: `tests/test_data.py` (fixture `tests/fixtures/french_two_sections.csv` with a
preamble, two sections, `-99.99`, `-999`, a blank line and a footer; plus an inline monthly + annual
file). On the real files, `scripts/01_load_check.py` → `data/raw/MANIFEST.json`.
**Result**: All 12 tests pass. Real files: daily FF3 has 1 section, 26,338 rows (1926-07-01 to
2026-09-30). Monthly FF3 has monthly (1,203 rows) and "Annual Factors" (99 rows) sections. The industry
file has VW and EW sections of 26,338 rows each.
**Status**: kept.

## 2026-11-20: Missing-value sentinels (-99.99, -999) → NaN, counted per column

**Decision**: After parsing to float, and before dividing by 100, values exactly equal to −99.99 or −999
become NaN. Counts per column go into the parse metadata and the report. Blank fields are also NaN and
are counted separately. `build_asset_frame` drops rows with NaN in `r` or `rf` and records the dates in
`frame.attrs["alignment"]`. A check FAILs if more than `checks.missing.max_nan_share_fail` (1%) of rows
are dropped.
**Alternatives considered**: (a) Replace with 0. Rejected: it fabricates a flat day and biases vol down.
(b) Forward-fill prices. Rejected for the same reason. (c) Tolerance matching (`abs(v+99.99)<1e-9`). Not
needed, because float parsing of the literal string is exact.
**What was tested**: Fixture test `test_sentinels_to_nan_and_counted`. Real data:
`data_check.json` → `assets.*.checks.missing`.
**Result**: 0 sentinels and 0 NaNs in all three real files (all columns), so no rows were dropped for
any asset. The only `-99.99` in the industry file is in its preamble text.
**Status**: kept.

## 2026-11-20: US market total return r = (Mkt-RF) + RF

**Decision**: For `us_mkt`, `r = Mkt-RF + RF` from the same daily file (`add_rf: true` in config).
Industry portfolios are already total returns (`add_rf: false`). Their rf comes from the daily factor
file, inner-joined on dates, with dropped dates reported.
**Alternatives considered**: Using CRSP VW index directly: not available in the French library. Using
log-sum `log(1+Mkt-RF)+log(1+RF)`: incorrect, since Mkt-RF is an arithmetic difference.
**What was tested**: Compounding daily `Mkt-RF+RF` to calendar months and comparing with the monthly
file's `Mkt-RF+RF` (`data_check.json` → `assets.us_mkt.checks.monthly_crosscheck`;
`figures/us_mkt/monthly_crosscheck.png`).
**Result**: 1,203 months. Mean |diff| is 0.047% and p99 is 0.13%. The maximum is 0.40% (1931-09). No
month exceeds the 0.5% flag. Units check: median daily r = 0.0008, std = 0.0108. The 1987-10-19 return
is −17.41%, the largest |r| day. March 2020 has 4 days in the top 20. All 11 assets PASS units and the
expected-extremes check. One exception is `ind_durbl`, which WARNs on 1933-03-15 at +27.06%. That is
the reopening after the 12-day 1933 bank holiday, so the move is genuine. We keep it and do not
winsorise.
**Status**: kept.

## 2026-11-20: rf_ann from the monthly file; daily rf kept for wealth accounting

**Decision**: `rf_ann = (1 + rf_m)^12 − 1` uses the **monthly** FF RF and is forward-filled onto the
daily dates of that month. If the monthly file ends earlier than the daily file, the last published
month is carried forward and the count is reported. Later stages use `rf_ann` as the observable short
rate at t=0 and to fit the rate model. The daily `rf` column is kept because it is what cash actually
earned in backtests.
**Alternatives considered**: (a) Annualise daily RF (`rf_d × 252`). Rejected: it is heavily rounded,
see below. (b) External T-bill series (FRED DTB3). Rejected for now because it adds a non-French
dependency, and it may be revisited in Spec 02.
**What was tested**: `data_check.json` → `rf`; `figures/rf_daily_vs_monthly.png`;
`figures/<asset>/rf_ann.png`.
**Result**: Daily RF is constant within the month in 100% of 1,203 months. **Correction to the spec**:
the files carry daily RF with **2 decimals in percent**, not 3. The resolution is 0.01%/day ≈ 2.5% p.a.,
which is worse than the spec assumed. There are 9,795 zero-RF days (87.6% of the 1930s, 100% of the
1940s, 80% of the 2010s). 315 months have every daily RF at 0 while the monthly RF is > 0.
`prod(1+rf_d)−1` vs `rf_m`: mean |diff| is 4.4 bp/month and the max is 13 bp (1931-03). All months fall
within the rounding bound. The monthly RF is **negative in 12 months** (1933-02, 1938–1941). rf_ann
inherits these values, while daily RF has no negative days. Spec 02's rate model must allow slightly
negative rates or treat them explicitly.
**Status**: kept.

## 2026-11-20: Dead end: fixed 2 bp tolerance for daily-vs-monthly RF → rounding-aware bound

**Decision**: Flag a month only if |prod(1+rf_d)−1 − rf_m| > n_days × resolution/2 + 1 bp
(`checks.rf.tolerance_beyond_rounding`). The resolution is measured from the data as the smallest step
between distinct daily RF values.
**Alternatives considered**: A fixed 2 bp/month tolerance (first implementation).
**What was tested**: `scripts/01_load_check.py` with both rules.
**Result**: The fixed 2 bp rule flagged 803 of 1,203 months and produced a meaningless WARN, because
with 0.01%/day rounding a 25-day month can be off by up to 12.5 bp from rounding alone. The bound-based
rule flags 0 months, so the daily file is consistent with the monthly file up to rounding.
**Status**: fixed 2 bp tolerance dropped, replaced by the rounding-aware bound.

## 2026-11-20: Coverage check: explained out-of-band years in config (1968)

**Decision**: The trading-days-per-year check flags years outside [240, 260]. Three kinds of flagged
year are reported but not WARNed: partial first and last years (1926: 150 days, 2026: 187), the Saturday
trading era (1927–1952, up to 302 days/year), and years listed with a verified reason in
`checks.coverage.explained_years`.
**Alternatives considered**: WARN on 1968 forever, which makes every asset WARN and hides real issues.
Widening the band was rejected because it would hide future problems.
**What was tested**: Weekday-by-month counts for 1968 from `data/processed/us_mkt.parquet`.
**Result**: 1968 has 226 days. The data show 0–1 Wednesday sessions per month from June to December
1968, matching the NYSE "paperwork crisis" Wednesday closures, so it was added to `explained_years`.
Gaps over 4 calendar days: 1929-11, 1933-03 bank holiday (12 days), 1956-12, 1958-12, 1961-05,
1968-07, 2001-09 (7 days), 2006-12/2007-01 (Ford funeral), 2012-10 (Sandy). There are no weekend dates
after 1952, and dates are strictly increasing and unique.
**Status**: kept.

## 2026-11-20: Log excess return x = log(1+r) − log(1+rf) is the modelling variable

**Decision**: Spec 02 models `x`, and `rf` and `rf_ann` separately. Wealth is rebuilt from simple
returns, `W_{t+1} = W_t (1 + rf + φ (r − rf))`, so the no-short/no-borrow mix is exact daily.
**Alternatives considered**: Simple excess returns `r − rf`. These are not additive over time, and
their distribution is bounded at −1−rf, which is awkward for continuous-time and DP models.
**What was tested**: `test_x_is_log_excess_return`. Distribution of x: `figures/<asset>/hist_x.png`;
skew and excess kurtosis by decade in `data_check.json`.
**Result**: Full-sample us_mkt x has an annualised mean of 6.36%, annualised vol of 17.1%, skew of
−0.46 and excess kurtosis of 17.0. The tails are much fatter than normal, as the log-y histogram shows.
Spec 02 must handle this with volatility clustering and/or fat-tailed innovations, because the floor
lives in the tail.
**Status**: kept.

## 2026-11-20: Store full history; defer the estimation window

**Decision**: The parquet files hold the full history (1926-07-01 to 2026-09-30). The window is
`estimation.start_date` / `end_date` in `config.yaml` (default null = full sample).
`gf.data.read_asset_frame` applies it. `estimation.robustness_start_dates` (1963-07-01, 1990-01-01) are
to be tested in Spec 02.
**Alternatives considered**: Truncating at load time (e.g. post-1963). Rejected because the choice
would be baked into the data and could not be undone in robustness tests.
**What was tested**: n/a in this stage.
**Result**: Floor-relevant facts by decade (`data_check.json` → `assets.us_mkt.checks.summary`): the
worst 252-day total return is −64.3% (1930s), and P(252-day return < −5%) ranges from 1.7% (2010s) to
45% (1930s). The full sample is 19.1% for 1-year windows and 14.6% for 2-year windows. Conditions are
strongly regime-dependent, which supports conditioning on starting state in later specs.
**Status**: kept.

## 2026-11-20: Equity model: GJR-GARCH(1,1)-t kept as base (provisional, pending checkpoint review)

**Decision**: The base model is GJR-GARCH(1,1) with standardised-t innovations, fitted with `arch` on 100·x
and stored in decimal daily units (`config.yaml: model.base = gjr_t`).
**Alternatives considered**: GARCH-normal, GARCH-t and EGARCH-t (same table).
**What was tested**: `scripts/02_fit_model.py --asset us_mkt` → `results/model/model_comparison_us_mkt.csv`
and `reports/model/us_mkt/model_report.md` §1 and §6.
**Result** (us_mkt, 1926-07 to 2026-09, gap variant `none`; BIC in decimal-data units):
- **GARCH-n**: BIC −177,414. The normal is decisively rejected, with 1% VaR hit rate 1.93% and 0.1% VaR hit rate 0.62%.
- **GARCH-t**: BIC −179,361, persistence 0.997, half-life 212 days, unconditional vol 26%.
- **GJR-t**: BIC −179,707, ν 5.86, persistence 0.988, half-life 58 days, unconditional vol 15.3%.
- **EGARCH-t**: BIC −179,825, the best by 117. Persistence 0.985 (half-life 46 days).
- **PIT**: every model is consistent with nominal in every bucket that has enough data, so the test cannot
  rank them. PIT score: EGARCH 0.63, GARCH-n 0.63, GJR 0.72, GARCH-t 0.76. All are noisy.
- **Floor-relevant simulated 1–2-year tails**: GJR and EGARCH are almost identical. The 2% quantile of the
  2-year cumulative excess log return is −47% for GJR and −46% for EGARCH, against −68% in the data.

GJR is kept because EGARCH's BIC gain does not change the tail that drives the floor. GJR also gives
closed-form persistence and unconditional variance, and σ_0² is a transparent state. EGARCH is the first
alternative to revisit if the user prefers to follow BIC.
**Status**: kept (confirmed at the Spec 02 checkpoint). GARCH-t and EGARCH-t are not discarded: their full fits are
saved in `params_{asset}.json` under `alternative_models` for Spec 03 model-risk simulations.

## 2026-11-20: Closure-gap returns: scale by sqrt(Mon–Fri days spanned) (variant `scale`)

**Decision**: Returns that follow a gap of more than 4 calendar days (list from
`reports/data_check/data_check.json`) are divided by sqrt(k), where k is the number of Mon–Fri days since the
previous observation, before fitting. Simulation is in trading-day time. PIT realised returns use raw data.
**Alternatives considered**: (a) Leave the returns as they are (`none`). (b) Exclude them (NaN, skipped in the
likelihood while the variance recursion carries through). Option (b) is implemented in `gf.model` but needs a
custom likelihood, because `arch` rejects NaN. It was not fitted.
**What was tested**: All four models in both variants (`model_comparison_us_mkt.csv`). Log-likelihoods for
`scale` include the Jacobian −½Σlog k, which makes them comparable with `none`.
**Result**: There are 9 flagged returns, and only 1933-03-15 (+14.6% over 8 business days) and 2001-09-17
(−5.2% over 5) matter. For GJR-t, `scale` raises the log-likelihood by 4.8 (BIC −179,717 vs −179,707). ν
moves from 5.86 to 5.89 and persistence stays at 0.9881. The PIT at H=252, u<0.05, goes from 6.6% to 6.1%;
everything else is unchanged.
**Status**: kept. The `none` variant is kept as a comparison row.

## 2026-11-20: Premium: constant arithmetic premium, sample mean shrunk 50% to zero

**Decision**: `model.premium = {mode: constant_arith, annual_excess: estimate, shrink_to: 0, shrink_weight: 0.5}`.
The conditional log mean is `m = μ_e/252 − σ²/2`. The estimate is the mean of daily (r − rf) × 252 over the
estimation window. `prop_variance` and `constant_sharpe` are resolved to the same average premium, for use in
Spec 03 robustness tests.
**Alternatives considered**: The raw mean, which is noisy. The GARCH constant mean, which mixes in the log/arith
distinction and the noise. A fixed number such as 4%, which is not a procedure.
**What was tested**: `params_us_mkt.json` → `premium`.
**Result**: The raw estimate is 7.83% a year with SE 1.67% (104.5 years). Shrunk, it is **3.91%**, which is
2.4 SE below the raw estimate. The conservative choice matters little for floor control (low premium → less
equity) and is a scenario axis in Spec 03.
**Status**: kept.

## 2026-11-20: FHS variant (bootstrap of standardised residuals)

**Decision**: The simulator supports `innovations="fhs"`, which resamples ẑ = (x − μ)/σ_t i.i.d. after
demeaning ẑ and scaling it to unit variance.
**What was tested**: PIT with FHS (`model_report.md` §6) and the QQ plot (`figures/qq_zhat.png`).
**Result**: ẑ has skew −0.73 and excess kurtosis 6.1, against 3.2 implied by t(5.9). Its minimum is −17.2
on 1955-09-26. FHS makes the 1–2-year left tail fatter, so the realised 2% frequency falls from 1.4% to 0.3%
at H=252 and from 1.9% to 0.8% at H=504. That is more conservative, possibly overly so.
**Status**: kept as a variant for Spec 03 robustness tests. The base stays `t`.

## 2026-11-20: Rate model: Vasicek on monthly rf_ann from 1954-01, plus equity–rate correlation

**Decision**: OLS AR(1) on monthly `rf_ann` (never on daily RF, per the Spec 01 addendum) from 1954-01 (after the
Treasury–Fed Accord). It is simulated daily with Euler steps at Δ = 1/252. `floor_at_zero = false`.
Correlation: **same-month pairing** (rate innovation e_m with the equity excess return over month m;
`config.yaml: rate.corr_alignment = same_month`), chosen at the checkpoint (see the entry below). The
previous-month pairing is reported as well. Daily η = ρz + √(1−ρ²)ξ (a linear
mix rather than a Gaussian copula; the correlation is exact).
**What was tested**: `model_report.md` §4.
**Result**: Base window (872 months): κ 0.34 (SE 0.10), θ 4.30%, s 2.57%, half-life 2.0 years. Full sample:
κ 0.30, θ 3.37%. From 1952-07: κ 0.33, θ 4.23%. The 12 negative-rate months (1933–1941) are all excluded
by the 1954 window. ρ is −0.12 with the same-month pairing (used) and −0.02 with the previous-month pairing. In the PIT, the rate model is re-estimated with data up to the start. Before 1959 (when fewer than 60 months
from 1954 exist) it uses all prior months.
**Status**: kept.

## 2026-11-20: Starting state = (σ_0, r_0); states defined by percentiles of the asset's own σ history

**Decision**: Under GJR-GARCH the one-step-ahead conditional variance σ_0² summarises everything the past tells
us about future equity variance; the sign of the last shock is already in it. The Vasicek state is r_0. So a
starting condition is (σ_0, r_0), and both are observable at t=0. The named states (calm P10, normal P50,
elevated P90, crisis P99, extreme = max) are percentiles of the asset's own filtered σ history, so they transfer
to any asset. r_0 for named states is the median rf_ann of the window. Secondary descriptors are 63-day realised
vol and trailing 252-day drawdown, both in `filtered_{asset}.parquet`, for Spec 03's "beyond (σ_0, r_0)" test.
**Result** (us_mkt): calm 7.9%, normal 11.9%, elevated 24.6%, crisis 51.0%, extreme 125.1% (1987-10-19);
r_0 = 2.67%. Anchors: 2020-03-16 at 115.8%, 2008-11-20 at 80.2%, 1987-10-16 at 40.1%, 2026-09-30 at 10.9%.
**Note on annualisation**: N = 252 is used throughout, as instructed. Contrary to the addendum, before 1952
this **understates** calendar-year vol slightly (mean per-year vol 18.3% with √252 vs 19.8% with √days, because
of Saturday sessions). It does not affect the model, which runs in trading-day time.
**Status**: kept.

## 2026-11-20: PIT calibration: what it shows and what it changed

**Decision**: The PIT test uses quarterly starts, expanding-window parameters (refit every 252 days, first fit on
10 years), and an expanding premium and rate model. It runs 2,000 paths and reports tail frequencies by σ_0
bucket (calm <P30, normal P30–70, elevated P70–95, crisis ≥P95, pooled high = elevated + crisis) and by era.
Effective n = n_starts × 63/H, with Wilson CIs. Buckets with n_eff < 8 are reported as "inconclusive" instead of
"calibrated", which is a change made after the first run.
**Result** (base, us_mkt): Overall, u<2% is 1.4% at H=252 (CI 0.3–6.4%, n_eff 91) and 1.9% at H=504 (CI
0.3–11.2%, n_eff 45). No bucket or era rejects. The test has **low power**, though: the 2% CI spans 9–34
points per bucket. Crisis starts (15, n_eff about 2–4) cannot be tested. The point estimates in the elevated
bucket are high: u<2% is 5.6% at H=252. In the 2000+ era, u<2% is 5.1% at H=504.
**Dead end / open issue**: The unconditional 1–2-year left tail of the GJR/EGARCH models is thinner than the
data. The 2% quantile of the 2-year excess log return is −47% in the model vs −68% in the data. The model's vol
mean-reverts too fast (half-life about 58 days) to produce 1929–32-style multi-year drawdowns. GARCH-t (half-life
212 days) matches these tails but overstates vol. Candidates are a component or two-factor GARCH, parameter-draw
scenarios, or FHS. Decided at the checkpoint: option (a), see the entry below.
**Status**: kept; the open issue is handled at the strategy level in Spec 03.

## 2026-11-20: Spec 02 checkpoint decisions (us_mkt reviewed by the user)

**Decision**:
1. **Thin unconditional 1–2-year tail → option (a)**: keep GJR-t as base. Parameter-uncertainty draws
   (`param_draws_{asset}.npy`) and FHS are stress scenarios in Spec 03. A component/two-factor GARCH (option b) is
   **not** added. Model risk is tested at the strategy level in Spec 03 by simulating from the fitted GARCH-t
   (slow vol mean reversion, fat multi-year tails) and EGARCH-t (best BIC) as alternative models.
2. **Equity–rate correlation → same-month pairing** (ρ = −0.12 for us_mkt, was −0.02 with the previous-month
   pairing). Implemented as `rate.corr_alignment: same_month`, used in the full fit and in the PIT's expanding
   rate fits. The effect on the equity PIT is negligible because |ρ| is small and rates enter only via rf.
3. **Alternative models saved**: `params_{asset}.json → alternative_models` holds the full-sample fits (params,
   robust SEs, backcast, kind, dist, persistence, half-life, unconditional vol, convergence flag) of every
   non-base comparison model in the base gap variant, including `garch_t` and `egarch_t`.
4. **Closure-gap scaling** (`scale`) confirmed as base.
5. **Annualisation**: the corrected statement is accepted. Using N = 252 before 1952 *understates*
   calendar-year vol slightly (18.3% vs 19.8%); it does not overstate it as the addendum said. N = 252 stays.
**Status**: kept. `--asset all` run after these changes.


## 2026-11-21: ind_telcm estimated from 1984-01 (per-asset start date)

> **Superseded** later on 2026-11-21 by the automatic fallback-window rule (below). `asset_start_dates` was removed.

**Decision**: `estimation.asset_start_dates: {ind_telcm: "1984-01-01"}`. For ind_telcm, no equity data before
1984-01 is used anywhere in Spec 02: full fits, expanding/rolling refits (first expanding fit therefore in
1994), filtered history, named-state percentiles, bucket edges, premium, PIT and VaR. The rate model still uses
the full rf history (base window from 1954); the equity–rate correlation uses post-1984 months only.
**Why**: Fitted from 1926, every GARCH variant for telecom sits on the integrated boundary: GJR-t persistence
0.99996 (half-life ≈ 17,000 days, implied unconditional vol 110%), GARCH-t did not converge (persistence 1.0001),
GARCH-n persistence 1.0, and only 1/3 of parameter draws were valid. Telecom vol has level shifts by decade
(7–9% in the 1940s–50s regulated AT&T era, 26% in the 2000s) that GARCH absorbs as near-unit persistence. Its
elevated-bucket PIT u<2% was the worst of all assets (7.2% at H=252, 6.2% at H=504). Fitted from 1984 (after the
AT&T divestiture), GJR-t is well behaved (persistence 0.983, half-life 40 days, ν 7.3).
**Alternatives considered**: dropping ind_telcm from the asset set; keeping the 1926 start and relying on
Spec 03 stress tests (rejected: the GARCH-t alternative needed by Spec 03 does not exist for that window).
**Trade-off**: 42 years of data instead of 100; no pre-1984 crises (1929–32, 1973–74) in the sample; fewer PIT
starts (n_eff roughly halves). The 1987 crash check has no expanding-parameter result (first refit is 1994).
Sub-samples with fewer than `expanding.min_obs` observations are reported as skipped.
**Result after the change** (2026-11-21 run): GJR-t ν 7.29, persistence 0.9827, half-life 40 days, uncond. vol
16.9%; all alternatives converge (GARCH-t half-life 56 days, EGARCH-t 47); 100% of draws valid. GJR and EGARCH
tie on BIC. **But the PIT now rejects as too thin**: overall u<2% = 8.7% at H=252 (n_eff 32, CI up to 23%) and
10.6% at H=504 (n_eff 15). PIT starts only begin in 1994, so the 2000–02 telecom bust (overlapping starts
1999–2001) dominates the test. Open issue for the user: keep (the bust is in-sample and Spec 03 tests the floor
against it), or treat ind_telcm as a stress asset only.
**Status**: superseded (see the fallback-window rule).

## 2026-11-21: Borderline fits noted for Spec 03 (no change made)

> Later on 2026-11-21, ind_enrgy, ind_shops and ind_utils moved to the fallback window (see the fallback-window rule). The numbers below are for their full-history fits.

- **ind_enrgy**: GJR-t half-life 275 days; 50% of parameter draws valid (others have persistence ≥ 1); draw
  unconditional vol 5–95% range 23–64%. GARCH-t alternative half-life 602 days.
- **ind_shops**: GJR-t half-life 166 days; 50% of draws valid; GARCH-t alternative half-life 1,555 days.
- **ind_durbl**: GJR-t and EGARCH-t tie on BIC (difference 1); EGARCH-t wins by 46–283 for every other asset.
Spec 03 stress runs using parameter draws or the GARCH-t alternative will be very fat-tailed for enrgy and shops.
- **ind_utils** (run 2026-11-21): same integrated boundary as ind_telcm had: GJR-t persistence 1.0000 (implied
  unconditional vol meaningless), GARCH-n and GARCH-t also at 1.0, 1/3 of draws valid (uncond. vol 5–95%
  range 16–83%). Sub-samples: pre-1963 1.0, from 1963 1.0, 1963–1999 0.9986, from 1990 0.991, 2000–end 0.976.
  PIT is nevertheless consistent (u<2% 2.2% at H=252, 0.8% at H=504). Not changed; flagged for the user (a
  1990 start, analogous to ind_telcm, is the candidate fix).

## 2026-11-21: Automatic fallback-window rule replaces hand-set per-asset start dates

**Decision**: Every asset is first fitted (base model GJR-t, base gap variant) on the full history. If
persistence ≥ 0.998 **or** fewer than 70% of the asymptotic-normal parameter draws are valid, all equity
estimation for that asset (full fits, expanding/rolling refits, filtered history, named states, bucket edges,
premium, PIT, VaR) uses data from `estimation.fallback_start` = 1990-01-01. Thresholds live in
`estimation.fallback_rule`. The rate model always uses the full rf history. `estimation.asset_start_dates` is
removed. The rule's inputs and outcome are written to `params_{asset}.json → fallback` and to the top of each
model report.
**Why a rule rather than per-asset dates**: the pipeline is a procedure that must run unattended on new assets.
A hand-picked date (e.g. 1984 for telecom, chosen from knowledge of the AT&T divestiture) cannot be applied to an
asset nobody has looked at, and it embeds look-ahead knowledge of where the regime break was. The rule uses only
fit diagnostics that are computed for every asset, with one fixed fallback date.
**Why these triggers**: persistence ≥ 0.998 (half-life ≥ ~350 days) means the model is treating level shifts in
vol as permanent shocks, so unconditional vol and multi-year tails are unreliable. A valid-draw share below 70%
means the estimate sits so close to the stationarity boundary that parameter-uncertainty scenarios (used in
Spec 03) are dominated by rejection.
**Result** (2026-11-21 run): 4 of 11 assets triggered.

| asset | full history: persistence / valid draws | after fallback: persistence / half-life / valid draws | PIT u<2% H=252 / H=504 |
|---|---|---|---|
| ind_enrgy | 0.99748 / 50% (draws) | 0.9947 / 130 d / **50%** | 1.9% / 2.0% |
| ind_telcm | 0.99996 / 33% (both) | 0.9843 / 44 d / 100% | **7.8% (too thin)** / 6.1% |
| ind_shops | 0.99583 / 50% (draws) | 0.9844 / 44 d / 100% | 0.0% / 0.0% |
| ind_utils | 1.00000 / 33% (both) | 0.9909 / 76 d / 100% | 2.9% / 3.0% |

The 7 other assets are unchanged (persistence 0.985–0.995, 100% valid draws). Fallback assets have PIT starts
only from 2000 (n_eff 26 at H=252, 12 at H=504), so their elevated buckets are inconclusive.
**Known gap**: ind_enrgy still has only 50% valid draws after the fallback. The rule has one fallback step and
no second trigger; this is left as is (instruction: no other changes) and flagged for Spec 03.
**Status**: kept.

## 2026-11-21: ind_telcm kept as an ordinary asset; PIT failure is a known model limitation

**Decision**: ind_telcm stays in the asset set as an ordinary asset (fallback window from 1990).
**Known limitation**: its conditional PIT rejects as too thin at H=252: u<2% = 7.8% (CI 2.2–24%, n_eff 26).
At H=504 it is 6.1% (CI 0.9–32%, not rejected but high). The test window starts in 2000 and is dominated by the
2000–02 telecom bust (≈ −80%), which GJR-t fitted on 1990+ data does not put enough probability on.
**Consequence**: the floor constraint for ind_telcm may be violated more often than 2% under the base model.
To be tested at the strategy level in Spec 03 (realised floor breaches in backtest, and simulations from the
GARCH-t / EGARCH-t alternatives, FHS and parameter draws).
**Status**: open limitation, carried into Spec 03.

## 2026-11-21: Spec 03 engine conventions

**Decision**: `src/gf/engine.py`. Day t: the strategy sees W_t, the floor PV F_t = K/(1 + r_ann,t)^((T−t)/252) with
the observable short rate (spec §1), the investor's conditional daily vol σ_d,t, r_ann,t and a read-only ring
buffer of past returns (`History`, 63 days). It returns a raw φ_t; the engine clips to [0, 1] and counts clips
per strategy (reported as `clip_hi_share` / `clip_lo_share`). Then the market reveals day t's returns:
W_{t+1} = W_t(1 + rf_t + φ_t(r_t − rf_t)). Market sources generate (SimMarket) or slice (ArrayMarket) day t only
after every strategy has chosen φ_t, so look-ahead is impossible by construction (test `test_no_lookahead`).
All strategies, knob values, tune modes and horizons of a (state, scenario) cell run in one pass over the same
paths (common random numbers); a strategy instance holds one horizon and k knob rows.
**SimMarket**: simulates the scenario DGP day by day (no path storage; 50k paths × 504 days fits in memory per
worker) and filters the investor's σ with the *base* GJR-t from the realised returns. Under `base` the filter equals
the DGP variance exactly; under any other scenario the investor does not know the DGP changed.
**Breach decomposition** (addendum 3): the last crossing of the cushion from ≥ 0 to < 0 before T is classified
`gap` (that day's equity move φW(r − rf) alone exceeded the start-of-day cushion C, and C could absorb at least a
1-conditional-sd move: C ≥ φ·W·σ_d), `gradual` (the equity move crossed a cushion already eroded below 1σ) or
`rate` (the equity move did not exceed the cushion: the floor PV rose or cash under-earned). Reported against the
floor the strategy itself protects (`breach_*_share`) and against the engine's F_t (`breach_*_share_specF`).
**Dead end**: a first rule called a crossing a gap only if C ≥ 0.5% of W. It labelled 15–27% of CPPI breaches
`gradual` and 4–21% `rate` although every CPPI crossing needs a one-day loss > 1/m: near the floor CPPI holds a small
cushion *and* a proportionally small exposure. The buffer must be measured relative to the exposure (in σ), not W.
**Status**: kept.

## 2026-11-21: Rate-robust floor for floor-based strategies (user decision at the Spec 03 start)

**Problem**: the investor's only safe asset is cash at the *floating* short rate. A CPPI path that has locked into
cash at the observable-rate floor PV still ends below K if rates fall later (Vasicek, s = 2.6%/yr). us_mkt,
normal state, 5k paths: CPPI m = 5, T = 2: 1.5% breach, 88% of them with φ ≈ 0; with constant rates CPPI
breaches almost vanish (m = 5, T = 1: 0.30% → 0.08%). The addendum says gradual breaches mean the rules are wrong.
**Decision** (chosen by the user from three options): cppi, cppi_vol and obpi_merton protect
F̃_t = K·exp(−q_α), q_α the α-quantile of log cash growth over the remaining horizon under the investor's own
Vasicek model (closed form for the integral of an OU process), α = 0.5% (`strategies.floor`). It reduces to the
spec's F_t when the rate vol is 0. The engine's accounting still uses the spec F_t.
**Correction to what I told the user**: I estimated the cushion cost at ~0.5–1% at T = 2. It is much larger before
tuning: F̃_0 ≈ $97.3M vs F_0 = $90.1M at T = 2 (median r_0), because the 0.5% quantile of the 2-year average
Vasicek rate is about −1.2%/yr. **But after tuning the CE cost is negligible or negative**, because the
multiplier is re-tuned upward (us_mkt, 10k tune / 10k eval paths, independent seeds):

| state, T | CPPI CE ann (spec F → robust α=0.5%) | gap-aware CPPI | CPPI breach gap share (spec → robust) |
|---|---|---|---|
| normal, 1y | 4.79% → 5.10% | 5.20% → 5.15% | 8% → 63% |
| normal, 2y | 4.73% → 5.06% | 5.27% → 5.16% | 1% → 54% |
| crisis, 1y | 3.51% → 3.50% | 4.18% → 3.87% | 1% → 68% |
| crisis, 2y | 3.79% → 4.27% | 4.77% → 4.60% | 0% → 48% |

With the robust floor, CPPI breaches are mostly gaps, as the addendum expects; with the spec floor they are 80–99%
rate-driven. α = 5% and 20% were also tested: in between on both counts.
**Status**: kept (α = 0.5%).

## 2026-11-21: Strategies and their sources

- **cash**: φ = 0. Baseline; W_T = W_0 ∏(1 + rf).
- **merton_const**: φ = μ_e/(γσ̄²), σ̄ = base unconditional vol (lecture: Merton constant-weight solution for CRRA
  with i.i.d. returns). No floor logic. us_mkt: 3.91%/(2·0.153²) = 0.84.
- **merton_vol**: φ_t = μ_e/(γσ_t²) with the conditional vol (vol timing; Merton applied with the current σ).
- **cppi**: φ = m·max(W − F̃, 0)/W (Black–Jones/Perold CPPI; protects F̃ in continuous time).
- **cppi_vol** (gap-aware): m_t = 1/(z·σ_d,t). CPPI breaks the floor only if equity falls by more than 1/m in one
  rebalancing interval, so m should shrink when vol is high; z is the size of one-day move (in conditional σ) the
  cushion survives.
- **obpi_merton**: lecture "wealth constraint / option analogy": with a hard floor under BSM the optimal payoff is
  max(K, X_T), X the unconstrained Merton wealth (φ_M = clip(μ_e/(γσ_0²), 0, 1), σ_X = φ_M σ).
  φ_t = φ_M X_t N(d1)/W_t; X_0 solved so W_0 = $100M (vectorised bisection). φ_M uses the starting state's σ_0;
  `vol_input = conditional` (main run) re-prices with σ_t daily, `sigma0_fixed` is shown in the frontier sweep.
  K_eff (relative to K) is the knob; the discount uses the rate-robust floor rate.
  Finding: with K_eff = K, OBPI breaches 14% even under exact GBM (daily rebalancing) because it pins ~25% of the
  terminal mass at K and discretisation error puts half of it just below; shortfalls shrink with rebalancing
  frequency (P(W_T < 0.99K) 0.1% daily vs 2% weekly; test `test_obpi_gbm_value_and_rebalancing`). So K_eff must
  be tuned above K, and its breaches are mostly `gradual` by construction (tiny shortfalls at the pinned mass).
- **obpi_shortfall**: not implemented (optional; obpi_merton needed a K_eff buffer to work at all).
- **dp_policy**: hook only; skipped with a warning if `results/policies/{name}.npz` is missing.
**Status**: kept.

## 2026-11-21: Why strategies use base estimates under stress scenarios

The strategy knows only what the investor estimated (base premium, base unconditional vol, base GJR-t filter, base
Vasicek). A scenario changes only the DGP. Otherwise a stress test would measure a strategy that already knows the
stress, which is not a robustness test. Starting states map into each DGP by the same annualised σ_0 (addendum 1).
**Status**: kept.

## 2026-11-21: Tuning: buffer 1.5%, tune and evaluate on independent seeds, grid search with refinement

**Decision**: for each (state, T) cell, 12 candidates over the knob range (log-spaced for m and z, linear for
K_eff) on the base model at `tune.n_paths` = 20k paths with the tuning seed; the tuned value is the last
candidate (least → most aggressive) before the first one whose breach exceeds 1.5%; a second pass refines
between those two. Breach is evaluated, not assumed, to be monotone: pairs where breach falls by > 2 SE as the knob
gets more aggressive are counted (`monotone_violations`; 0 in the quick run). All reported performance uses the
evaluation seed (independent, `mc.n_paths` = 50k).
**Why a 1.5% target**: MC error of the tuned value (at 20k paths, SE at 1.5% ≈ 0.09pp, and the knob is picked on a
noisy step function) plus model error: SPEC 02's PIT is weak and its point estimates in the elevated bucket and
since 2000 are above 2%; the unconditional 1–2-year tail is thinner than history.
**Modes**: `uniform` = most conservative per-cell value over named states except `extreme` and all horizons;
`uniform_all` = including `extreme` (addendum 6); `state` = per-cell values at the dense σ grid + named states
(median r_0), linear in (log σ_0, T), flat outside the tuned range (`KnobFunction`). The r_0 dependence is not
tuned; dense-grid cells at other r_0 show its effect.
**Status**: superseded by robust tuning (below); the base-only values remain as the `state_base` comparison row.

## 2026-11-21: Scenario list and Monte Carlo settings

Scenarios (`mc.scenarios`): base; mu_low (premium × 0.5); mu_zero; premium_prop_var; fat_tails (ν = max(3, ν̂ − 3));
persistent (β raised so persistence = min(0.995, p̂ + 0.01), unconditional variance fixed); param_unc (paths split
across SPEC 02's 200 valid parameter draws; SPEC 02 drew by rejection sampling until 200 were valid, acceptance
logged per asset in `run_manifest.json`); fhs; jump (one-day −20% with probability 1% per path-year, fed into the
GARCH recursion); rate_const; dgp_garch_t and dgp_egarch_t (SPEC 02 alternative fits; EGARCH recursion was already
in the simulator); rho_zero (ρ = 0, addendum 4). Worst case = max breach over {base, dgp_garch_t, fhs, param_unc,
persistent} (fat_tails moved to the stress set after the VaR backtest, see below); fat_tails, jump and mu_* are
reported separately as stress tests.
n_paths = 50k: SE of a 2% breach ≈ 0.063pp (Wilson CI ≈ ±0.12pp). Seeds: tuning seed = H(master, asset, "tune"),
evaluation seed = H(master, asset, "eval"), shared by all cells of an asset (common random numbers across states,
horizons and strategies; the scenario changes the DGP, not the draws, except where the innovation family differs).
**Compute plan** (user budget, < 2 h): dense σ_0 × r_0 grid only for base and dgp_garch_t; all horizons only for
base; T ∈ {1, 2} for stress scenarios; industries: named states × {1, 2} × {base, dgp_garch_t, fhs, jump}.
Sample paths (200, every 2nd day) and fan quantiles (over the first 5k paths) are stored for named states only.
**Status**: kept.

## 2026-11-21: Backtest: state-dependent knobs, investor re-estimated every 5 years

**Decision** (user): `scripts/04_backtest.py` refits the base GJR-t every 1,260 days (5 years) on data strictly
before the refit date (first after 10 years), and re-estimates the premium, unconditional vol and the Vasicek
model (rates from the full rf history) at the same dates. A start date uses the latest refit on or before it.
Knobs: the state-dependent knob function from `results/tuned/{asset}_base.json` evaluated at each start's σ_0
(also the uniform knob). That function was tuned on the full-sample model: **the knob carries a mild look-ahead**;
the investor's estimates and the σ_0 buckets (expanding percentiles, addendum 5) do not. n_eff = n × 21/T.
**Status**: kept.

## 2026-11-21: Near-zero terminal wealth (CE ≈ −93%): diagnosis before the full run

**Question** (user): where do the CE ≈ −93% cells come from? Diagnostic over the first quick run (us_mkt, 5k paths,
`scratchpad` scripts; numbers are min W_T / paths below $50M / −99% return clips, summed over cells):
- merton_const: min W_T ≈ $0 in every scenario (no floor; φ = 0.84 held through simulated vol of several hundred
  % per year). Expected for a no-floor rule; it drives most of the −93% CE cells.
- obpi_merton under dgp_garch_t: min W_T $0.016 (state), 109 / 59 / 169 paths < $50M (state / uniform / untuned);
  CE −87% to −105% in calm/normal/elevated T = 2 cells **with no −99% clips in those cells**. Trace of the worst path:
  the model portfolio X ran ahead of actual W (X/W ≈ 1.13), so raw φ = φ_M X N(d1)/W was 1.04–1.15 on 158 days
  (clipped to 1), and with the conditional σ_X at 300–650% p.a. N(d1) ≈ 1: OBPI stayed all-in through −30% to −64%
  days. Clip-high counts: 1.8M path-days (state) / 4.7M (untuned) in dgp_garch_t.
- cppi / cppi_vol: min W_T $1.3–6.3M only in dgp_garch_t / fat_tails high-σ cells; −99% simulated-return clips:
  83 (dgp_garch_t), 18 (fat_tails), 5 (fhs), all in cells with σ_0 ≥ 0.65 (extreme, dense 0.65/0.80, anchors)
  except two in fat_tails/crisis.
**Root causes**: (1) OBPI's carried-forward X (user's hypothesis, confirmed); (2) unbounded simulated conditional
variance: even base GJR-t paths reach σ = 14 (1,400% p.a.) from the extreme state and 3.4 from calm; GARCH-t
(α = 0.10 with t(5.5) innovations, α²κ + 2αβ + β² ≈ 1.05 > 1: no finite variance of variance) reaches 23.
**Fixes**: the next two entries. Ablation (us_mkt, 5k paths, T = 2, OBPI K_eff = 1.0): dgp_garch_t/calm min W_T
$0.69M → $90M with re-anchoring alone; fat_tails/calm $0.007M → $45M (one −55% day with φ ≈ 1 far above the floor:
genuine gap risk, not a bug). The cap alone removes all −99% clips but not the OBPI problem.
**Status**: kept (diagnosis).

## 2026-11-21: OBPI re-anchored daily on actual wealth

**Decision**: each day solve V(X_t; τ, σ_X,t, K_eff, robust discount) = W_t for X_t (safeguarded Newton on the call
value, bracket [W − Kd, W], warm start = yesterday's X grown at φ_M; |V − W|/W < 1e-8 in tests), then
φ_t = φ_M X_t N(d1)/W_t. φ_t = 0 when W_t ≤ Kd = K_eff·exp(−lower cash growth) (insuring K_eff no longer
affordable; counted as `n_unaffordable`). Since V = Kd + Call(X) ≥ X N(d1), φ ≤ φ_M ≤ 1: OBPI never needs clipping.
Off-model moves (gaps, vol changes, discrete rebalancing) no longer accumulate. `reanchor=False` keeps the old rule
for comparison (test `test_obpi_reanchor_bounds`).
**Status**: kept.

## 2026-11-21: Simulated conditional vol capped at 1.5 × the asset's historical maximum

**Decision**: `engine.sigma_cap_mult = 1.5`: the DGP variance recursion is truncated at (1.5 × the asset's historical
max filtered σ)² (us_mkt: 1.5 × 125% = 188% p.a., 11.8% daily). Path-days at the cap are counted per cell
(`sigma_capped_share`). The investor's filter is not capped (it does not know the cap).
**Why**: the history the model was fitted on never exceeded 125% (1987-10-19); unbounded GARCH-t / ν = 3 recursions
produce 10–30× that and −99% days, which say more about the recursion's explosive tail than about markets. 1.5× keeps
room for a crisis worse than 1987. Binding share (5k paths, T = 2): base calm 0.0005%, crisis 0.006%, extreme 0.1%;
dgp_garch_t extreme 0.2%.
**Status**: kept.

## 2026-11-21: fat_tails is a stress test, not part of the robust set (ν = 3 rejected by the daily VaR backtest)

**Test** (`scripts/02b_var_backtest.py` → `results/model/var_backtest.{csv,json}`): in-sample one-step-ahead residuals
of the base GJR-t; exceedance if z_t < t_ν⁻¹(p)·√((ν−2)/ν); Kupiec POF and Christoffersen independence.
us_mkt (n = 26,338):

| ν | 1%: exceed. / exp. (Kupiec p) | 0.1%: exceed. / exp. (Kupiec p) |
|---|---|---|
| fitted 5.89 | 353 / 263 (1e-7) | 46 / 26 (5e-4) |
| 3 (= fat_tails ν for us_mkt) | 325 / 263 (2e-4) | 14 / 26 (0.008) |

ν = 3 is rejected for us_mkt and 9 of 10 industries (not ind_telcm), at 0.1% in the "too few" direction: ν = 3 is
too heavy at the far tail. **Decision** (user rule): fat_tails, jump and mu_* are stress tests reported outside the
tuning set; worst case = max over {base, dgp_garch_t, fhs, param_unc, persistent}.
**Also found**: the fitted ν is rejected too, in the other direction (too many exceedances, 1.34× at 1%, 1.75× at
0.1%): the left tail is heavier than a symmetric t (consistent with SPEC 02's PIT). FHS, which resamples the
empirical residuals, is in the robust set and carries that asymmetry.
**Status**: kept.

## 2026-11-21: Robust tuning (supersedes base-only tuning)

**Decision** (user): each knob is tuned so the **maximum** breach across {base, dgp_garch_t, fhs, param_unc,
persistent} (`tune.robust_set`) is ≤ 1.5% on the tuning seed (each scenario on the same tuning seed); evaluation on
the independent seed. Same two-pass grid; pass 2 refines both the robust and the base-only brackets, and the final
picks use every candidate evaluated (all on common paths). The base-only tuned state function is kept as a
comparison row (`tune_mode = state_base`) so the CE cost of robustness is measured on the same evaluation paths.
Outputs: `results/tuned/{asset}_robust.json` (primary; per-cell binding scenario and breach by scenario) and
`{asset}_base.json`. The backtest uses the robust knobs (plus `state_base`).
Cost: tuning ≈ 5× (5 scenarios) × ~1.5 (union grid in pass 2).
**Status**: kept.

## 2026-11-21: Resumable Monte Carlo run

Each tuning / evaluation task's result is cached in `results/[quick/]cache/{tune,eval}/{key}.pkl` (written atomically
after its output files). The key hashes the task, n_paths, seed, the tuned knobs, the config (minus n_jobs/plan) and
the source code of `src/gf` and the script, so a rerun after an interruption skips finished tasks and any code or
config change recomputes. Delete `cache/` to force a full recompute.
**Status**: kept.

## 2026-11-21: Quick-run gate after the fixes: not passed, full run not started

Gate (user): start the full run only if the floor strategies' worst-case breach ≤ 2% and no floor strategy has
near-zero paths. us_mkt `--quick` (5k paths, robust tuning) result:
- Worst case over the robust set, named states excl. extreme: cppi 1.32% (state) / 1.18% (uniform); obpi 1.52% /
  0.96%; **cppi_vol state 2.22%** (normal, T = 2, fhs; tuning seed 1.50%, eval seed 2.22% at 5k paths: ≈ 2.6 SE of the
  difference plus selection bias of picking the last candidate under the target).
- **Near-zero paths**: CPPI 4 paths < $10M (min $1.24M), all in `fat_tails` (stress set). 4 simulated −99% days
  remain under ν = 3 even at the vol cap (|z| ≈ 39 standardised at 11.8% daily vol) and CPPI holds φ = 1 when
  far above the floor. Robust-set scenarios: CPPI min W_T $46M, OBPI ≥ $68M, cppi_vol ≥ $67M; OBPI clips: 0.
Gate failed on both criteria → stopped for the user's decision.
**Status**: resolved by the next four entries.

## 2026-11-21: Tuning target 1.25% and logistic selection (user decision after the failed gate)

**Target**: `tune.target_breach` 1.5% → 1.25%: the quick gate showed a 0.7pp tuning-seed → evaluation-seed gap for
cppi_vol under fhs.
**Selection** (`tune.selection: logistic`): the old rule (last grid candidate with breach ≤ target) is biased: among
noisy candidates near the target it picks the one whose estimate happened to fall low. Now, after the two passes,
for each scenario a binomial logistic regression logit p = a + b·u (u = ln knob for m and z, u = K_eff) is fitted
by maximum likelihood to every candidate evaluated inside the pass-1 bracket (the refinement grid plus its two
endpoints, 12 points, all on the same tuning paths) and solved for p = target. The robust value is the most
conservative crossing over the robust-set scenarios (`binding_scenario`, `crossing_by_scenario`); the base-only value
uses the base scenario alone. Crossings are clipped to the bracket; a fit with the wrong slope sign or too few points
falls back to that scenario's grid pick (`fit_by_scenario` records which). The grid pick is kept as `grid_value`.
**Status**: kept.

## 2026-11-21: fat_tails / jump / mu_* stay stress tests; CPPI's near-zero paths under fat_tails are a finding

Innovations are not truncated (user). Under fat_tails (ν = 3, rejected by the VaR backtest) the simulator still
produces a few −99% days at the vol cap, and CPPI, which is fully invested whenever its cushion × m exceeds W
(φ = 1 on 33–73% of path-days), loses almost everything on such a day: 4 paths < $10M, min W_T $1.24M (first
robust-tuning quick run). OBPI's min W_T over all scenarios is ≥ $68M: its exposure is φ_M·X·N(d1) ≤ φ_M·W.
**Status**: kept (reported as a finding).

## 2026-11-21: Floor quantile α 0.5% → 0.1% (user decision; replaces the proposed 2nd-percentile discount)

The user proposed discounting the floor at the 2nd percentile of the average short rate under Vasicek, recomputed
daily, believing the floor discounted at the current rate. The floor strategies already protected exactly that
construction at α = 0.5% (the 42–64% "rate" shares were measured against it). Test (us_mkt, base, T = 2, 20k paths,
m = 10, z = 5.6, K_eff = 1.006), breach % (rate share):

| floor | cppi normal | cppi_vol normal | cppi crisis | cppi_vol crisis |
|---|---|---|---|---|
| current rate (spec F) | 9.4 (0.94) | 18.8 (0.98) | 26.0 (0.92) | 30.7 (0.98) |
| α = 2% | 0.65 (0.50) | 1.61 (0.77) | 2.08 (0.76) | 2.19 (0.81) |
| α = 0.5% | 0.38 (0.29) | 0.66 (0.50) | 0.71 (0.54) | 0.82 (0.57) |
| α = 0.1% | 0.27 (0.17) | 0.35 (0.20) | 0.35 (0.21) | 0.40 (0.21) |

**Decision** (user): α = 0.1% for all floor strategies. What remains is reinvestment risk: when rates fall the floor's
PV rises while cash (zero duration) does not gain, and no cash/equity strategy can hedge that.
**Status**: kept.

## 2026-11-21: Industries tuned at 10k and evaluated at 20k paths; us_mkt first

Runtime (user): industries use `tune.n_paths_industries` = 10k and `mc.n_paths_industries` = 20k (SE of a 2% breach
≈ 0.10pp); us_mkt keeps 20k / 50k. `--asset all` runs us_mkt first, then the industries, each group writing its
results when done; the task cache makes both resumable.
**Status**: kept.

## 2026-11-21: Quick gate passed; full run started

us_mkt `--quick` (5k paths) with target 1.25%, logistic selection, floor α = 0.1%:
- Worst case over the robust set (named states; uniform excludes extreme by definition): cppi 1.06% (state) /
  0.78% (uniform), cppi_vol 1.72% / 1.32%, obpi 1.52% / 0.72%. Max 1.72% (cppi_vol, calm, T = 2, fhs) ≤ 1.75%.
  The `state_base` and `untuned` comparison rows are not gated.
- Robust-set floor paths < $10M: 0 (min W_T $45.9M). Under fat_tails CPPI still reaches $3.9M (finding above).
- Breach split (gap/gradual/rate, own floor, pooled over robust-set scenarios, named states, state mode):
  T = 2 0.62/0.05/0.33 → 0.75/0.11/0.14; T = 1 0.81/0.10/0.09 → 0.85/0.12/0.03.
Gate passed → `03_run_mc.py --asset all` then `04_backtest.py --asset all` started 2026-11-21 20:59 under caffeinate;
projection 5.1 h (us_mkt tuning 92 min, industry tuning 47, us_mkt evaluation 127, industry evaluation 30, backtest 10).
**Status**: kept.

## 2026-11-22: Full run results

Finished 03:47 (6.8 h against the 5.1 h projection). us_mkt tuning took 166 min instead of 92 because the
5k→20k paths scaling was worse than linear; industry tuning 54 min, us_mkt evaluation 168 min, industry
evaluation 20 min, backtest 1 min. No crashes and no resumes.

**us_mkt (tune 20k, evaluate 50k on an independent seed)**
- Worst floor breach over the robust set (named states; uniform excludes extreme): cppi 1.25% (state) / 1.24%
  (uniform); cppi_vol 1.33% / 1.18%; obpi 1.47% / 0.80%. The maximum, 1.47%, is obpi, elevated, T = 2, fhs.
  The quick run's 1.72% was 5k-path noise.
- Floor paths < $10M in any scenario, stress included: 0. Min W_T $22.4M (cppi, crisis, dgp_garch_t);
  fat_tails min $25.5M. merton_const (no floor) goes down to $0.7M.
- Stress maxima (floor rows): fat_tails 3.6%, jump 2.2%, mu_low 1.9%, mu_zero 1.9%.
- Breach split (gap/gradual/rate) pooled over the robust set: T = 2 0.62/0.05/0.33 before the α change, 0.79/0.08/0.13
  after; T = 1 0.81/0.10/0.09 → 0.88/0.10/0.02.
- CE cost of robustness (state vs state_base, base scenario): cppi +15–21 bp/yr in calm and normal, and negative
  in crisis and extreme (the lower multiplier also helps CE); cppi_vol −1 to −3 bp in calm and normal (the
  robust z is higher and CE is better); obpi +5–7 bp in calm and normal, about 0 elsewhere. The base-only knobs
  reach worst-case breaches of 3–4.5%.
- Binding scenarios across the 25 state×T cells: cppi dgp_garch_t 21 and fhs 4; cppi_vol fhs 25; obpi fhs 16,
  persistent 4, garch_t 3, base 2. In crisis and extreme, obpi sits at the K_eff = 0.8 cap.

**Industries (tune 10k, evaluate 20k)**: the worst case excluding extreme is cppi 1.60%, cppi_vol 1.76% (telcm,
crisis, T = 1, fhs; CI 1.58–1.95%) and obpi 1.65% (state) / 0.91% (uniform). 139 of the 1440 cells exceed 1.25%,
consistent with tuning-sample noise at 10k paths (SE ≈ 0.11pp). No floor paths < $10M; min W_T $35.5M.

**Backtest (us_mkt, overlapping windows)**: robust-state breach 0.7–1.2% for cppi, 0.6–1.2% for cppi_vol and
0–0.3% for obpi. The state_base knobs give 1.9–3.2% and merton_const 6.6–8.7%. The worst industry robust-state
cells are telcm cppi 3.7% / obpi 5.1% and utils cppi 3.4%; these are short samples (309 starts, n_eff ≈ 26).

---

# SPEC 04: dynamic-programming policy (branch `dp`, worktree `../growth_floor_dp`)

## 2026-11-21: Setup — main folder was not a git repository; `git init` with ignores in `.git/info/exclude`
**Decision**: SPEC 04 §0 step 1 assumes the main folder is a repo; it was not. Ran `git init` in the main folder
(writes only `.git/`), put the ignore rules in `.git/info/exclude` instead of a `.gitignore` (so no file in the
running project changed): `results/`, `results_dp/`, `data/processed/`, caches, `.venv`, `.DS_Store`. Then exactly
§0: commit "pre-DP snapshot" (9773244), `git worktree add ../growth_floor_dp -b dp`, copy (not move)
`results/model/` and `data/processed/` into the worktree. `data/raw/` is committed (9 MB, makes the worktree self-contained).
`gf` is imported from `ROOT/src` by every script and by `tests/conftest.py` (no editable install), so the worktree never
loads code from the main folder; the running SPEC 03 job was not touched.
**Status**: kept.

## 2026-11-21: Lagrangian relaxation of the breach constraint; possible duality gap
**Decision**: solve max E[U(W_T) − λ·1{W_T < K}], U(W) = −1/(W/$1M), for each λ on a grid, then pick λ per
(σ0, T) so the robust worst-case breach is ≤ the SPEC 03 target. The indicator makes the problem non-convex: the
achievable (breach, CE) set need not be convex, so some breach levels may be unreachable by any λ (duality gap).
We check for it by looking for jumps in breach between adjacent λ (V2 `max_jump_pp`), and the policy shows the
non-convexity directly: below the floor at large λ the DP *raises* φ ("gambling for resurrection": once a breach is
likely the penalty is already mostly paid, so variance helps). Reported in V3, not suppressed.
**Status**: kept.

## 2026-11-21: State (τ, y, h); rate held at the median r0 inside the DP
**Decision**: state = (days remaining τ, y = W/F̃ with F̃ the SPEC 03 robust floor K·exp(−q(τ)), next-day GJR variance h).
Complete under the base model: next-day returns depend only on h (GJR-t, constant arithmetic premium), next-day h only on
(h, z), the rate is fixed, and U is homogeneous of degree −1 so wealth enters only through W/F̃. q(τ) is computed by
`strategies.cash_growth_lower` (the SPEC 03 floor code, reused, α = 0.1%) at the median r0 = 2.67%; g_τ = exp(q(τ) − q(τ−1)).
Note q(τ) < 0 for long τ (the 0.1% quantile of Vasicek cash growth is negative), so F̃ > K far from the horizon.
Rate risk is handled only through that conservative floor. **Only the median r0 is solved** (trimmed for time; other r0
are further work). In the MC the hook uses each path's actual r_ann for F̃ (as the other floor strategies do), and the
investor's filtered σ_d² for h (uncapped; the DP clamps h at the SPEC 03 variance cap, flat beyond).
**Status**: kept.

## 2026-11-21: Grids, quadrature, interpolation (lecture 3)
**Decision**:
- y: 120 points on [0.3, 4.0], piecewise uniform in log y with 35% of the points on [0.9, 1.3] (Δlog y ≈ 0.009 there
  vs ≈ 0.029 outside), y = 1 an exact node. h: 40 points log-spaced in σ from 0.5 × calm σ0 (3.9%) to the variance
  cap (187.6%). φ: 41 points {0, 0.025, …, 1}; no golden-section refinement.
- z: 60 bins on the probability scale (19 log-spaced bins below u = 0.01 with exact edges at 1e-4 and 1e-3, 36 uniform
  bins on [0.01, 0.99], 5 right-tail bins); weights = CDF differences; nodes = conditional means within each bin
  (closed-form partial expectation of the t), which gives E[z] = 0 exactly; the within-bin variance lost (0.5% for
  ν = 5.89) is restored by one common rescale (×1.0024). Checks: E[z] = 0, E[z²] = 1, P(z < q_0.001) = 0.001 to 1e-12.
- Interpolation: bilinear in (log y, log h), but of **Jt = y·J** rather than J. By homogeneity Jt is exactly constant in y
  at λ = 0 (so linear interpolation adds no curvature bias and V1 is exact up to the control grid), and the spec's
  above-y_max rule J(y) = J(y_max)·y_max/y is simply "Jt flat". Below y_min: J(y) = (J(y_min)+λ)·y_min/y − λ.
- Last step (τ = 1) is exact in the penalty: P(y' < 1) from the shock CDF (threshold z*), the utility term by
  quadrature. Without it the terminal indicator was smeared over one y cell (test showed a 3e-5 bias in J at λ = 2e-3).
- Implementation: h-interpolation weights are time-invariant (h' depends on (h, z) only); per step one index lookup over
  the (y, φ, h, z) tensor in chunks of 4 h values, float64 (near the optimum the one-step value difference between
  adjacent φ is ~1e-10, below float32 resolution). exp(−A) is folded into the quadrature weights. ≈0.65 s per step
  standalone on 2 threads (main run competing for the CPU).
**Status**: kept.

## 2026-11-21: dp_policy hook format `dp_v2`
**Decision**: the SPEC 03 hook interpolated φ over (τ, σ, W/F) with the engine's spec F. Replaced (SPEC 04 §4 allows it):
file `results/policies/dp_{asset}.npz` holds axes τ (0…504), y, h, φ grid, λ grid, φ index tables uint8
(n_λ, 505, 120, 40), q(τ), r0, floor α, the DP breach recursion B, and the λ*(σ0, T) lookup. J is in a separate
`dp_{asset}_J.npz` (float32). Rows = λ values (must be on the grid) or, with no knob, the lookup; σ0/T off the lookup
take the larger λ of the bracketing cells (λ is never interpolated). y uses `protect_floor` (robust F̃), h = σ_d².
The hook counts path-days below y_min / above y_max / outside the h grid (reported per row).
**Status**: kept.

## 2026-11-21: λ grid and time projection
**Decision**: λ grid (units: $M utility, U = −1/(W/$1M)) = {0} ∪ geomspace(1e-4, 5e-3, 7) =
{0, 1e-4, 1.92e-4, 3.68e-4, 7.07e-4, 1.36e-3, 2.61e-3, 5e-3}. Chosen from a scan at τ = 252 (DP breach recursion,
before the exact last step): λ = 0 → 15–18% breach (T = 1), 1e-4 → 5–10%, 1e-3 → 0.1–1.7%, 1e-2 → ~0.01%; the
1.25% robust target sits between 1e-3 and 1e-2 and ~0.1% is reached near 5e-3. Ratio 1.92 between grid points; rounding
λ* up to the next grid point is conservative (costs some CE).
**Time projection** (first full backward solve: 389 s, 1 thread, main run competing; target ≤ 10 min met): us_mkt
≈ 75 min end to end; industries ≈ 33 min each → total ≈ 7 h ending ≈ 04:45 < 07:00. No cut needed at this point.
**Status**: kept.

## 2026-11-21: Monotone-cubic (PCHIP) interpolation in log y instead of bilinear (deviation from SPEC 04 §3)
**Problem**: linear interpolation in y, repeated every day, is numerically diffusive. For a convex/concave value the
interpolation error at y' is ½ f''·(y'−y_a)(y_b−y'), i.e. like adding variance ≈ Δ²/6 in log y per step. On the
120-point grid that is ≈ 1.4e-5/day in the dense band (Δ ≈ 0.009) and ≈ 1.4e-4/day outside (Δ ≈ 0.029), against a true
daily variance of ≈ 5.7e-5 (φ = 1, σ = 12%): +25% to +250% spurious spread, accumulated over up to 504 steps. V1 cannot
see it (at λ = 0, y·J is exactly constant in y, so linear interpolation is exact there); the breach recursion B and the
penalised J can. The tiny-grid dry run showed DP breach ≈ 2× the Monte Carlo.
**Test** (constant variance, normal shocks, λ = 0, so φ is constant and log wealth is a random walk; breach from
400k exact simulated paths, se 0.06 pp):

| σ, T | φ | B linear | B PCHIP | exact |
|---|---|---|---|---|
| 12%, 1y | 1.0 | 20.10% | 18.06% | 18.12% |
| 12%, 2y | 1.0 | 17.63% | 16.09% | 16.15% |
| 20%, 1y | 0.5 | 20.92% | 17.86% | 17.89% |
| 20%, 2y | 0.5 | 19.75% | 17.30% | 17.35% |

**Decision**: interpolate y·J (and B) with monotone cubic Hermite (Fritsch–Carlson slopes) in log y; keep linear in log h
(time-invariant weights; the value is smooth in h). Monotone → no overshoot, B stays in [0, 1]. Config `dp.interp_y`
(`linear` reproduces the spec's bilinear scheme). Not tuned to any validation target: chosen because the bilinear scheme is
demonstrably biased on a case with a known answer, before any V4/V5 result on the real grid existed. Cost: ≈ 1.6× per step.
The two linear solves already finished are kept as `results/policies/cache/dp_us_mkt_linear_*` (dead end).
The policy hook still interpolates φ bilinearly (no accumulation over time there).
**Status**: kept. Test: `tests/test_dp.py::test_pchip_breach_matches_random_walk`.

## 2026-11-21: Cut 1 — industries dropped from the plan (time budget)
**Decision**: with PCHIP and the main run still loading all 8 cores, a full solve takes ≈ 24 min (2.9 s/step, 1 thread).
Projection: us_mkt (8 solves, calibration, V4 on the doubled y grid, evaluation) ≈ 4 h → ≈ 02:00; industries ≥ 45 min
each → 10 assets do not fit before 07:00. Per SPEC 04 §0 the first cut is the industries. If us_mkt finishes early they
are run best-effort (named states × T ∈ {1, 2} × {base, dgp_garch_t, fhs}) with a hard stop, and reported as partial.
**Status**: kept.

## 2026-11-21: First full us_mkt pass (PCHIP in y): V1 pass, V5 fails in mid-range λ — policy resolution near the floor
**Result** (8 solves 921–1293 s each, load ≈ 19 on 8 cores; calibration 611 s): λ* = 1.36e-3 (calm/normal T = 1),
2.61e-3 (calm/normal/elevated T = 2), 7.07e-4 (elevated/crisis T = 1), 1.36e-3 (crisis/extreme T = 2), 3.68e-4 (extreme T = 1);
all by the grid rule. V1 pass (max |φ − Merton| = 0.0108). V2: breach monotone in λ in every cell; λ = 0 has the highest CE
except normal T = 2 where λ = 1e-4 is $580 higher (0.3 bp/yr). V5 failed in 46/80 cells (rate held constant) — agreement
at λ = 0 and λ ≥ 2.6e-3, disagreement in both directions for λ ∈ [1e-4, 1.4e-3], DP breach even non-monotone in λ.
**Diagnosis**: the policy near the floor (gamble below y = 1 at large φ, φ ≈ 0 at y = 1, rising above) changes on a scale
smaller than the 0.9% y cell. The DP's breach recursion assumes B is smooth between nodes; the hook interpolates φ across
the jump. Evidence: the same MC paths with the hook reading the *nearest* y node instead of bilinear φ move breach by
1.5–2.6 pp (normal T = 1: λ = 1e-4 9.47% → 11.25%; 7.07e-4 1.26% → 2.71%).
A second, smaller error: linear interpolation in log h is also diffusive in vol: at λ = 0, T = 2 the DP CE was ≈ $0.15M below
the MC. PCHIP in log h with 40 points reproduces the 80-point result to ≤ $40k (linear needs 80) → adopted (`dp.interp_h`).
**Fix attempt 1 (in progress)**: concentrate the same 120 y points where the policy jumps: bands [0.3, 0.9] 22 intervals,
[0.9, 0.97] 10, [0.97, 1.0] 14, [1.0, 1.08] 34, [1.08, 1.3] 15, [1.3, 4.0] 24 (Δlog y ≈ 0.0022 on [0.97, 1.08], 4× finer
than before; outer bands ≈ 0.05, where the value is smooth and PCHIP is accurate). Same cost per step.
**Fix attempt 1 result** (τ_max = 252, banded grid, PCHIP in y and h; MC rate constant, 20k paths, tuning seed; breach %):
normal λ = 1e-4: DP 7.60 vs MC 8.00 (old grid 6.99 vs 9.47); normal 3.68e-4: 2.83 vs 3.03 (5.21 vs 4.05);
crisis 1e-4: 5.03 vs 5.37 (4.54 vs 6.82); crisis 3.68e-4: 0.71 vs 0.78 (2.99 vs 1.17) — all inside 2.58 SE + 0.1 pp.
The nearest-node vs bilinear sensitivity fell from 1.5–2.6 pp to 0.7–1.4 pp. Adopted (config `dp.y_grid.bands`);
all us_mkt solves redone at 23:30. The uniform-band solves are kept in `results/policies/old_uniform_y/` (dead end).
Stage order changed to solve → calibrate → validate → evaluate → compare → figures → tables → V4 (V4 is the costliest;
evaluation first so the app/scorecard exist even if V4 runs late).
**Status**: kept.

## 2026-11-22: Banded-grid us_mkt pass: validation results (V1, V2, V3, V5)
**Run**: 8 solves 1080–1505 s each (load 25–30 on 8 cores, 2 workers × 1 thread), calibration 687 s.
**λ\*** (grid rule, robust set, 20k paths, tuning seed): calm 7.07e-4 / 1.36e-3 (T = 1 / 2); normal 7.07e-4 / 2.61e-3;
elevated 7.07e-4 / 1.36e-3; crisis 3.68e-4 / 1.36e-3; extreme 1.92e-4 / 1.36e-3. Lower than on the uniform grid
(by one grid step in most T = 1 cells): the finer floor resolution removed spurious breaches the coarse policy produced.
**V1** pass (max |φ − Merton| = 0.0108, below the φ step 0.025).
**V2** breach non-increasing in λ in all 10 cells (no rise beyond 2 SE, base MC and DP recursion alike); λ = 0 has the
highest breach everywhere. CE highest at λ = 0 in 9/10 base cells; normal T = 2: CE(1e-4) − CE(0) = +$630 (0.3 bp/yr).
In the DP's own model (DP value, and MC with the rate held constant) CE is strictly decreasing in λ in every cell, so the
miss comes from the stochastic rate in the base scenario, which the fixed-rate DP does not model, combined with an MC difference
far below any meaningful CE scale. Reported as a strict fail; not adjusted.
"φ ≈ 0 near y = 1 at very large λ": not met. Mean φ\* on y ∈ [1, 1.02] at λ = 5e-3 is 0.18 / 0.28 / 0.33 / 0.38 at
τ = 21 / 126 / 252 / 504. Reason: y = 1 is the *robust* floor F̃ = K·e^{−q}; at the fixed median rate, cash alone grows
F̃ to K·e^{r0 τ − q} > K for long τ, so in the DP's model y = 1 still carries a cushion worth risking. The remaining breaches
at λ_max are only 22–42% gap-driven at T = 2 (54–100% at T = 1); the rest are rate-driven breaches the DP cannot see.
This is a limitation of the fixed-rate state (§2 of the spec), not a numerical problem.
**V3** J increasing in y and φ ∈ [0, 1]: no violations. "φ rises with the cushion" has 487–2081 violating
(τ, h, y) triples for λ > 0 (0 at λ = 0), all from the gambling-for-resurrection region below/near y = 1 (φ large just
below the floor, ≈ 0 at the floor, rising above). "φ falls with h" has 2–45 violations, also all in that region.
Genuine features of the Lagrangian problem (non-concave objective); explained, not suppressed.
**V5** with the rate held constant (the model the DP solves): 80/80 cells within 2.58 SE + 0.1 pp (fixed by attempt 1).
Base scenario (spec wording): 16/80 fail, all at T = 2. At λ ≤ 3.68e-4 the base MC breach is 0.5–1.2 pp *below* the DP
recursion; at λ ≥ 2.61e-3 it is 0.2 pp *above* (0.24% vs 0.04%). The rate-constant MC matches the DP in every one of these cells,
so the gap is the stochastic rate, not the grid/quadrature. Reported as a base-scenario fail with this attribution; no second
fix attempt (it would mean adding r to the state, a model change rather than a numerical fix).
**Status**: kept.

## 2026-11-22: Stale V4 caches from orphaned workers; grid check added to the solve cache
**Finding**: when the first V4 (uniform grid, y_mult = 2) was stopped at 23:10, its joblib workers kept running and wrote
`dp_us_mkt_y2_lam{7.07e-4, 1.36e-3, 2.61e-3}.npz` at 00:08–00:56 (exactly the old-grid λ\* set). These were made on the
*old uniform* doubled grid (same length 239), and the cache key did not encode the grid, so the relaunched V4 would have
reused 3 of them. They also explain part of the 25–30 load during the relaunched solves.
**Decision**: V4 stopped before it produced any result; the three files were moved to `results/policies/old_uniform_y/orphan_y2/`.
Solve files now store `y` and `h`, and a cached solve is reused only if its grid matches (`_cache_ok` in 05_dp.py). The
y_mult = 1 cache files (all written 23:48–01:00 by the relaunched run) were the only files left without a stored grid.
V4 rerun from scratch at 01:29 (4 solves: λ\* of normal/crisis × T = 1, 2 = 3.68e-4, 7.07e-4, 1.36e-3, 2.61e-3).
**Status**: kept.

## 2026-11-22: V4, V6 and final status (us_mkt)
**V4** (doubled y grid, 239 points, same bands; λ\* of normal/crisis × T = 1, 2): pass. ΔCE −0.22 … +0.13 bp/yr,
Δbreach −0.075 … +0.03 pp. Doubled solves took 2127–2486 s each.
**V6**: the spec's literal reading ("at the same or lower breach" at λ\*) gives 0 comparable cells once paired with the full
SPEC 03 run, because DP at λ\* has a higher base breach than every tuned comparator. Since DP is a one-parameter family, the
check is made on its frontier instead: base scenario, eval seed, 50k paths, all 8 grid λ (`v6frontier` cache). For each
comparator (OBPI-Merton and gap-aware CPPI, state and uniform), take the best-CE DP λ with base breach ≤ the comparator's.
Result: DP CE ≥ comparator in 32/32 comparable cells (+13 … +164 bp/yr). 8 cells are not comparable (comparator base
breach ≤ 0.06%, below the DP's reach at λ ≤ 5e-3). Both versions are in validation_us_mkt.json (`V6`, `V6_frontier`).
**Stress finding**: under `mu_zero` the DP's breach reaches 3.25% (calm T = 2), against ≤ 0.8% for OBPI/CPPI. The DP is the
most premium-dependent strategy. Reported, not changed (stress scenarios are outside the calibration set by design).
**Industries**: not run (cut 1 stands). At 03:00, one asset needs ≈ 1.5 h of solves plus calibration on 2 cores shared
with the SPEC 03 run; a partial 1–2-asset result was not worth the risk to the 07:00 budget.
**Final**: pytest 67 passed on `dp`. App merge checked against the main run's results (read-only): 8057 + 90 rows.
Nothing merged; main folder untouched since the pre-DP snapshot commit.
**Status**: kept.

## 2026-11-22: Premium-robust λ\* for the DP (`dp_premrobust`, branch `dp_mu`)
**Question**: under `mu_zero` the DP breach reaches 3.25% (calm T = 2) vs ≤ 0.8% for OBPI/CPPI, because λ\* was calibrated
only on the robust set at the base premium (3.91%). Does recalibrating λ against a lower premium fix this without re-solving?
**Decision**: no new solves. The 8 SPEC 04 solves (base estimates, base premium; φ tables copied unchanged from
`results/policies/dp_us_mkt.npz`, checked identical) are reused; only the data-generating process in calibration changes.
λ\* is recalibrated per named state × T ∈ {1, 2} with the unchanged SPEC 04 rule (`05_dp.select_lam`: 1.25% on the worst case,
logistic crossing if noisy, next grid λ at or above; tuning seed, 20k paths) over `tune.robust_set` **+ mu_low** (premium × 0.5).
mu_zero stays a held-out stress (not in calibration). Saved as a separate strategy `dp_premrobust`
(`results/policies/dp_premrobust_us_mkt.npz`, `reports/dp_mu/calibration_dp_premrobust_us_mkt.json`); `dp_policy`'s λ\* untouched.
Code: `scripts/06_dp_premrobust.py` (calibrate / evaluate / report), config `dp_premrobust`, 6 joblib workers (8 cores − 2).
**Check**: the robust-set-only rule on the same runs reproduces the original lookup exactly ([[4 4 4 3 2], [5 6 5 5 5]]).
**λ\* result**: new lookup [[5 4 4 3 2], [6 6 6 5 5]]: one grid step up in calm T = 1 (7.07e-4 → 1.36e-3), calm T = 2 and
elevated T = 2 (1.36e-3 → 2.61e-3); mu_low binds in 9/10 cells. Every cell reaches the target on the existing grid
(largest λ = 5e-3 never needed), so no extra solves.
**Evaluation** (eval seed, 50k paths, same seeds as SPEC 03 full run, paired; base + robust set + mu_low, mu_zero, fat_tails,
jump; `results_dp_mu/`, app label "DP (premium-robust)"). Unchanged cells reproduce `dp_policy` exactly (pairing check).
mu_zero breach (orig → premium-robust): calm T1 1.78% → 0.34%, calm T2 3.25% → 1.16%, elevated T2 2.50% → 0.83%; max over all
10 cells 1.65% (crisis T2, λ unchanged). Base CE excess cost vs dp_policy: −7 bp/yr (calm T1), −8 (calm T2), −7 (elevated T2),
0 elsewhere. vs gap-aware CPPI: still +13 … +63 bp/yr in every cell, at lower worst-case breach (≤ 0.95% vs 1.17–1.33%).
**Open**: mu_zero margins are thinner in cells where λ did not move (crisis T2 1.65%, calm/normal T1 1.75–1.78% under
dp_policy were already < 2%). fat_tails (outside the calibration set by design) still exceeds 2% for the DP in crisis T1
(2.64%) and extreme T1 (3.45%), as for gap-aware CPPI (2.2–2.7%); unchanged by this recalibration.
**Status**: on branch `dp_mu`, not merged (user decides). App merge checked: 8057 + 90 + 90 rows; pytest 67 passed.

## 2026-11-22: CE convention: quoted as excess over the cash return (%/yr)
**Decision (user)**: from now on CE is quoted everywhere as the excess over the cash return, %/yr.
**Definition**: `ce_xcash = ce_ann(strategy) − ce_ann(cash strategy)` in the same cell (asset, state, T, scenario, seed).
Both run on the same simulated paths (paired seeds), so the cash leg uses the same Vasicek rate paths. It is the annualised
certainty-equivalent return earned over holding cash in that world. Computed at read/report time by
`gf.metrics.add_ce_xcash` (no result file rewritten). The stored `ce_ann_excess` (= ce_ann − r0) stays in the parquet
files for continuity but is no longer quoted: r0 is the starting short rate, not the cash return over T. The two
differ by up to ≈ 25 bp/yr (e.g. us_mkt normal T = 1: cash CE 2.90% vs r0 2.67%) because rates mean-revert.
- SE shown in the app: delta method on the annualisation, cash CE treated as known (`ce_xcash_se`).
- Frontier rows (20k paths, eval seed) use the cash CE of the 50k summary row of the same cell. The cash CE is
  nearly deterministic given the rate paths, so this is a negligible mismatch.
- Validation tables on the tuning seed (V2, V4, λ curves) subtract a cash run on the same seed and path count
  (`05_dp.cash_ce`, cached in the policy cache).
- CE *differences* (ΔCE bp/yr in V4, V6, scorecard comparisons) are unchanged by the convention.
- Earlier DECISIONS entries quote CE as annualised levels or as `ce_ann_excess` (vs r0). Those numbers are historical.
  Regenerated: reports/dp/tables_us_mkt.md, reports/dp/lambda_curves_us_mkt.png, reports/dp/SUMMARY.md scorecard,
  reports/dp_mu/scorecard_dp_premrobust_us_mkt.csv. App: scorecard, breach/CE heatmap, frontier, robustness and the
  live "Custom" tab.
**Not found in the repo**: no slide deck or deck figures, and no results table in README.md (a convention note was
added there instead).
**Status**: kept.

## 2026-11-22: External validity: premium-robust DP on ind_telcm, ind_enrgy, ind_hlth (branch `dp_ind`)
**Run**: the full premium-robust procedure per asset, unchanged from us_mkt. 8 SPEC 04 backward solves (same λ grid
0 + geomspace(1e-4, 5e-3, 7), same y/h/z/φ grids, each asset's own base GJR-t fit and base premium, median r0). Then λ\*
per named state × T ∈ {1, 2} with the SPEC 04 rule (1.25% target on the worst case over the robust set + mu_low;
logistic crossing if noisy; next grid λ at or above). Tuning seed, **10k** paths (= tune.n_paths_industries).
Evaluation on the eval seed at **20k** paths (= mc.n_paths_industries) under base, robust set, mu_low, mu_zero,
fat_tails, jump. Seeds = cell_seed(master, asset, tune|eval), identical to the SPEC 03 industry run (checked).
6 workers (8 cores − 2). The 24 solves ran in one pool, assets in order, 2424 s wall (490–690 s per solve). Calibration
≈ 40 s per asset, evaluation ≈ 35 s per asset. Total 14:36–15:32. No industry V1–V6 validation was run (not asked;
same code and grids as the validated us_mkt solve).
**Comparators**: the SPEC 03 industry plan evaluated cppi/cppi_vol/obpi only under base, dgp_garch_t, fhs, jump. To give
them a robust-set worst case and stress numbers, their **existing** robust-tuned knobs (results/tuned/{asset}_robust.json,
no retuning) were evaluated with `03_run_mc.eval_task` on the same eval seed and 20k paths under param_unc, persistent,
mu_low, mu_zero, fat_tails (→ results_ind_extra/, `06_dp_premrobust.py compare`). Pairing check: re-running base reproduces
the SPEC 03 rows exactly (160 rows per asset, Δbreach = ΔCE = 0).
**λ\***: on the grid in all 30 cells (largest λ never needed); binding scenario at λ\* (from the calibration log): mu_low 17, dgp_garch_t 9, fhs 3, param_unc 1.
**Result** (DP premium-robust; CE as excess over cash, %/yr; gap-aware CPPI = cppi_vol state, OBPI = obpi_merton state):
- Worst case over the robust set ≤ 1.75% in **all 30 cells** (max 1.30%, enrgy normal T = 2). Comparators: cppi_vol
  max 1.80% (telcm extreme T = 1), obpi max 1.35%.
- mu_zero < 2% in **29/30 cells**. Exception: **ind_enrgy normal T = 2: 2.24%** (≈ 0.10 pp SE at 20k, so above 2% beyond
  noise); there mu_low gave worst 1.30% at λ\* = 1.36e-3. Next highest: hlth elevated T = 1 1.69%, hlth calm T = 2 1.57%.
  Comparators under mu_zero: cppi_vol ≤ 0.57%, obpi ≤ 1.74%. Halving the premium in calibration is not enough for every
  cell. The DP stays the most premium-sensitive strategy.
- Base CE − cash: DP above gap-aware CPPI in all 30 cells (telcm +24…+108 bp/yr, enrgy +33…+230, hlth +20…+78) and above
  OBPI in all 30 (telcm +15…+57, enrgy +17…+73, hlth +36…+166). DP base breach max 0.98%; it is above
  gap-aware CPPI's in some cells and below in others (no uniform ordering), so the CE gaps are not breach-matched.
- Other stresses for the DP (max over cells): fat_tails telcm 1.06%, enrgy 1.24%, hlth 1.94%; jump ≤ 1.20%.
Outputs: results/policies/dp_{ind}.npz, dp_premrobust_{ind}.npz, results_dp_mu/ (us_mkt + 3 industries),
results_ind_extra/, reports/dp_mu/{calibration,scorecard}_dp_premrobust_{ind}.json/csv, logs.
**Status**: on `dp_ind`, not merged or pushed (user decides).

---

# SPEC 05: report, deck, submission

## 2026-11-22: Why gap-aware CPPI has CE − cash < 0 in some industry cells (investigation; no strategy changed)
**Question**: gap-aware CPPI (cppi_vol, state-dependent robust knobs) has negative CE − cash in 11 of 110 industry cells
(base scenario), worst ind_enrgy extreme T = 1 at −2.09%/yr. Expected cause: lock-in to cash after a drawdown from extreme
starts. **Script**: `scripts/07_report_numbers.py` → `results/report/tables/cppi_gap_negative_ce.{csv,md}` (summary rows +
5k-path terminal subsamples, base scenario, eval seed).
**Result**: the expected cause is only partly right. Hard lock-in, measured as `locked_cash_share` (φ < 1% on each of the
last 21 days), is small in these cells: 0.7–4.6%. What happens is a *soft* lock-in, where the cushion is eroded rather
than lost in one move. All 11 cells are crisis or extreme starts (σ0 52–155%), and 46–69% of paths end within 2% above the
floor (p_near_floor; the median for cells with CE − cash ≥ 0 is 35%). Median W_T is $95.7–97.4M, while mean W_T is $103.9–109.8M,
above the cash outcome. Across all 110 cells, CE − cash correlates −0.82 with p_near_floor and −0.73 with the strict lock-in share.
**Mechanism**: gap-aware CPPI sets m = 1/(z·σ_d). When φ = m·C/W < 1, the cushion's daily volatility is m·σ_d = 1/z ≈ 20–21%
(z = 4.7–6.2 in these cells), whatever the vol level. A cushion that volatile has a strongly negative median log growth
(≈ −1/(2z²) per day before the premium), so most paths bleed the cushion to near zero within weeks of a high-vol start and
then hold φ ≈ 0.1–0.4 of a thin cushion. They end near the robust floor K·e^{−q} grown at cash, which is ≈ $95–97M, below
the ≈ $103M of all-cash. With γ = 2 the CE weights that mass heavily, so CE − cash < 0 although E[W_T] > cash.
Where vol is normal, φ is clipped at 1 most of the time and the mechanism does not bind.
**Consequence for deployment**: three fallback assets (ind_enrgy, ind_telcm, ind_utils) have deployed cells with CE − cash < 0
(crisis/extreme, mostly T = 1). The floor still holds there; the cost is CE. The report states that from such starts on
flagged assets the CPPI fallback can earn less than cash, and that cash itself would meet the floor.
**Status**: explained; no strategy changed (SPEC 05 instruction).

## 2026-11-22: Deployment rule implemented as code (team decision, before results were seen)
**Decision**: `gf.deployment.deployment_choice(asset)` reads only SPEC 02 diagnostics. It returns `cppi_vol` (gap-aware CPPI,
state-dependent robust knobs) if (a) the fallback window was triggered, (b) fewer than 70% of parameter draws are valid
after the fallback, or (c) the base model's overall conditional PIT test gives "too thin" or "too fat" at H = 252 or 504 at
any reported tail level (1, 2, 5, 10%). Otherwise it returns `dp_premrobust`. Bucket- and era-level PIT verdicts are not used:
their n_eff is too small (crisis 2–4), and with them in, the rule would flag on noise.
**Result** (`results/deployment.csv`): CPPI for ind_enrgy (fallback, 50% valid draws after it), ind_shops (fallback),
ind_telcm (fallback, PIT too thin at H = 252) and ind_utils (fallback). All other assets get the DP. The outcome does not
depend on how (c) is read: telcm is the only PIT rejection under any reading, and it is also a fallback asset.
**Test**: `tests/test_deployment.py`.
**Status**: kept.

## 2026-11-22: Premium-robust DP run on the remaining 7 industries
**Why**: the rule deploys the DP on ind_durbl, hitec, manuf, nodur and other, but the DP had been run only on us_mkt, telcm,
enrgy and hlth. Reporting the deployed strategy's performance needs DP results on those assets. Shops and utils are run
too, so both candidates are shown on every asset.
**Run**: `scripts/run_dp_ind_rest.sh`: `06_dp_premrobust.py` solve / calibrate / evaluate / compare / report, unchanged
procedure and settings from the dp_ind run (10k tune, 20k eval, same seeds). Logs: `reports/dp_mu/{solve,run}_ind2.log`.
**Result** (finished 18:05; 56 solves at 559–706 s each, 6 workers): λ\* on the grid in every cell. Across all 10 industries
the DP has a higher CE − cash than gap-aware CPPI in 100/100 cells. The only DP cell above 2% under the robust set or zero premium
is still ind_enrgy normal T = 2 (mu_zero 2.24%), and the rule routes enrgy to CPPI. Thin zero-premium margins on deployed-DP
assets: ind_other normal T = 2 1.98%, ind_nodur normal T = 1 1.89% (SE ≈ 0.10 pp at 20k paths). Under fat_tails (stress set) the
DP exceeds 2% in several crisis/extreme cells on durbl, manuf, nodur, other and utils (max 2.94%, other extreme T = 1).
Tables: `results/report/tables/industries.md`, `dp_fat_tails_failures.md`.
**Status**: kept; the report shows the deployed strategy on all 11 assets.

## 2026-11-22: Report tooling: Jinja2 → Markdown → HTML → PDF (headless Chromium)
**Decision**: no LaTeX, Quarto or weasyprint is installed; pandoc is, but it needs a PDF engine. Templates
(`report/templates/*.j2`) are rendered with Jinja2 (StrictUndefined, so a missing key fails the build), the Markdown is
converted with python-markdown and printed to PDF with Playwright's Chromium. The deck is an HTML template printed at
13.333 × 7.5 in. Added `jinja2`, `markdown` and `playwright` to requirements (`python -m playwright install chromium`).
Figure inputs too large to commit (66 MB policy files, terminal-wealth arrays) are extracted by 07 into
`results/report/inputs/` (≈ 8 MB) and committed with the small summary files (whitelist in `.gitignore`), so the fast path
runs from a fresh clone.
**Status**: kept.
