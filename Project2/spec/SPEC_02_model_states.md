# SPEC 02 — Probability model and starting states

Builds on SPEC 01 (same repo, same `config.yaml`, same `DECISIONS.md`). Read SPEC 01's
"Project context" section first if you have not. This stage fits the model under which breach
probabilities and the CE are computed, defines the starting conditions, and provides a fast path
simulator that SPEC 03 uses.

## Design principles

- **Asset-agnostic.** Every function takes an asset frame (`r`, `rf`, `x`, `rf_ann`) and returns
  estimates. Run the whole stage for `us_mkt` and for every available industry asset.
- **Only time-0 information.** A starting condition must be computable from data up to and including
  the start date. Any historical evaluation must use parameters estimated on data before the start date.
- **The model is a claim that must be tested.** The floor guarantee is only as good as the model's
  lower tail of 1- to 2-year cumulative returns *conditional on the starting state*. This spec
  requires a direct calibration test of exactly that (Section 5).

## 1. Equity return model (`src/gf/model.py`)

Modelling variable: daily log excess return `x_t`.

**Base model: GJR-GARCH(1,1) with standardised Student-t innovations.**

```
x_{t+1} = m_{t+1} + σ_{t+1} z_{t+1},     z ~ standardised t(ν) (unit variance)
σ²_{t+1} = ω + (α + γ_lev · 1{ε_t<0}) ε_t² + β σ²_t
```

- Fit with the `arch` package (`arch_model(100*x, mean='Constant', vol='GARCH', p=1, o=1, q=1, dist='t')`).
  Scale by 100 for numerical stability and convert back. Store parameters in decimal daily units.
- Report: parameters with robust standard errors, log-likelihood, AIC/BIC, persistence
  `α + γ_lev/2 + β`, shock half-life in days, unconditional annualised vol, ν.
- Also fit these comparison models and put them in one table: GARCH(1,1)-normal, GARCH(1,1)-t,
  GJR-t (base), EGARCH-t. Choose by BIC **and** by the Section 5 calibration test. Log the choice.

**Expected return (the shakiest input). Keep it separate from the GARCH mean.**

The GARCH constant mean is a noisy estimate. Parameterise the premium explicitly in `config.yaml`:

```yaml
model:
  premium:
    mode: constant_arith      # constant_arith | prop_variance | constant_sharpe
    annual_excess: estimate   # 'estimate' or a number, e.g. 0.04
    shrink_to: 0.0            # shrinkage target for the estimate
    shrink_weight: 0.5        # 0 = raw sample mean, 1 = target
```

- `constant_arith`: conditional arithmetic expected excess return is constant μ_e;
  log mean `m_{t+1} = μ_e/252 − σ²_{t+1}/2`.
- `prop_variance`: μ_{t+1} = λ σ²_{t+1} (premium rises in crises).
- `constant_sharpe`: μ_{t+1} = S · σ_{t+1}.
- `estimate` = full-window sample mean of arithmetic daily excess return × 252, then shrunk.
  Report the raw estimate and its standard error (it will be large: about σ/√years).
- Log the base choice and why. These modes are robustness scenarios in SPEC 03.

**Innovations variant: filtered historical simulation (FHS).** Store the standardised residuals
`ẑ_t = (x_t − m_t)/σ_t` from the fit. The simulator must support drawing `z` either from the fitted t
or by resampling `ẑ` (i.i.d. bootstrap). Report the skew and kurtosis of ẑ and a QQ plot vs the fitted t.

## 2. Interest rate model

The short rate matters for the cash return and for the present value of the floor.

- Base: discrete Vasicek / AR(1) on `rf_ann` sampled monthly:
  `r_{m+1} = r_m + κ(θ − r_m)Δ + s√Δ η`, Δ = 1/12. Estimate on a configurable window
  (default from 1954-01, after the Treasury–Fed Accord; log the choice and test the full sample too).
  Simulate daily with Δ = 1/252 using the same parameters.
- Estimate the correlation between monthly rate innovations and monthly equity excess returns.
  Include it in the simulator (correlate η with the equity shock aggregated to the day; a simple
  daily Gaussian copula with the estimated correlation is acceptable).
- Option `rate.floor_at_zero: true|false` (default false; report how often simulated rates go negative).
- Alternative for robustness: `rate.mode: constant` (hold r at r0 for the whole horizon).
- Daily cash return in simulation: `rf_t = (1 + r_t)^(1/252) − 1`.

## 3. Path simulator (`src/gf/simulate.py`)

```python
simulate_paths(model_params, rate_params, state0, n_days, n_paths, seed,
               innovations="t"|"fhs", premium=..., param_draws=None) -> SimPaths
```

`SimPaths` (numpy arrays, float64, shape `(n_paths, n_days)` unless noted):
- `x`: daily log excess returns
- `rf`: daily simple risk-free return
- `r`: daily simple total equity return = `exp(x + log(1+rf)) − 1`
- `sigma`: conditional daily vol **known at the start of each day** (σ_{t+1} given info to t);
  strategies may use it because it is a function of past observations
- `r_ann`: annualised short rate known at the start of each day
- metadata: seed, params hash, state0

Requirements:
- Vectorised over paths. Loop over days only. Must do 50,000 paths × 504 days in well under a minute.
- Use `numpy.random.Generator(PCG64(seed))`. Same seed gives identical output. Provide `common_random_numbers`:
  the same `z` and `η` draws can be reused across strategies and states.
- `param_draws`: optional array of parameter vectors (for parameter uncertainty). Paths are split
  into equal blocks, one parameter vector per block.
- Guard: clip `x` so that a simple return cannot fall below −99%. Count and report clips (should be ~0).

## 4. Starting states (`src/gf/states.py`)

Under GJR-GARCH, the one-day-ahead conditional variance σ²_0 (together with the sign of the last shock,
which is already absorbed into σ²_0) is the **complete equity state**. The rate model's state is r_0.
So a starting condition is the pair **(σ_0, r_0)**, both observable at t=0. State this argument in
DECISIONS.md and in the report. Also record secondary descriptors (63-day realised vol, trailing
252-day drawdown, VIX if the team adds it) to test whether they predict breaches *beyond* (σ_0, r_0)
in SPEC 03.

Produce:

1. **Filtered history.** σ_t (annualised) for every historical date, using full-sample parameters
   (for describing states) **and** using expanding-window parameters refit every 252 days with data
   strictly before t (for honest historical tests). Save both to `results/model/filtered_{asset}.parquet`.

2. **Named state grid** (config-driven, defined by percentiles of the asset's own filtered σ history,
   so it transfers to other assets):

   | state     | σ_0 definition                       |
   |-----------|--------------------------------------|
   | calm      | 10th percentile of filtered σ        |
   | normal    | 50th percentile                      |
   | elevated  | 90th percentile                      |
   | crisis    | 99th percentile                      |
   | extreme   | historical maximum of filtered σ     |

   r_0 for the named states: the median `rf_ann` of the estimation window, plus a rate grid
   `{0.0, 0.02, 0.05}` crossed with each σ_0 for the heatmaps.

3. **Dense σ_0 grid** for heatmaps: annualised `[0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.65, 0.80]`
   (configurable; clip at the asset's historical max × 1.1).

4. **Historical anchor dates**: report σ_0 and r_0 on, e.g., 2006-12-29, 1987-10-19, 2008-10-10,
   2008-11-20, 2020-03-16, 2022-06-16, the latest date. Configurable list; skip dates outside the data.

Save the state table to `results/model/states_{asset}.csv`
(columns: state_name, sigma0_ann, r0_ann, definition, source).

## 5. Model validation (the most important part of this spec)

Script `scripts/02_fit_model.py` runs everything above, then these tests, writing
`reports/model/model_report.md`, `model_report.json` and figures.

**5a. Stylised facts.** Simulate long paths from the fitted model and compare with the data:
annualised vol, kurtosis of daily x, ACF of |x| and x² (lags 1–100), distribution of 252- and 504-day
cumulative log excess returns, and maximum drawdown within 252/504 days. Overlay plots.

**5b. Conditional tail calibration (PIT test).** For each historical start date on a quarterly grid,
using **expanding-window parameters** (data before the start only) and that date's σ_0 and r_0:
simulate 2,000 paths of H ∈ {252, 504} days and compute the model CDF value u of the realised
cumulative log total return (equity) over the next H days. Then:
- Report, overall and **by σ_0 bucket** (named-state buckets), the observed frequency of
  u < 0.01, 0.02, 0.05, 0.10 against nominal, with binomial CIs that account for overlap
  (overlapping windows: report the effective sample size as the number of non-overlapping windows).
- Histogram of u by bucket.
- This directly tests: "when the model says a 2% left tail, is it 2% in history, from calm and crisis starts alike?"
- Run for the base model and each comparison model; include the result in the model choice.

**5c. One-day tail check.** VaR backtest of daily x at 1% and 0.1% (Kupiec unconditional coverage,
Christoffersen independence) by σ bucket. Report the model probability of a ≥ 17% one-day loss from the
1987-10-16 state, and the realised 1987-10-19 standardised shock. This sizes the gap risk that SPEC 03
strategies must survive.

**5d. Parameter stability.** Plot expanding-window and rolling 20-year parameter estimates over time.
Fit on sub-samples (pre-1963, 1963–1999, 2000–end) and report the table.

**5e. Parameter uncertainty draws.** Generate `n_param_draws` (default 200) parameter vectors from
the asymptotic normal of the estimates (reject draws violating stationarity, ω>0, ν>2.5) and save them
to `results/model/param_draws_{asset}.npy` for SPEC 03's uncertainty scenario. Optional extra: a
block-bootstrap refit (blocks of 252 days) if time permits.

## 6. Outputs

- `results/model/params_{asset}.json`: base model parameters, SEs, estimation window, n_obs, loglik,
  premium settings and estimates, rate model parameters, the correlation, the data hash from SPEC 01's MANIFEST.
- `results/model/model_comparison_{asset}.csv`
- `results/model/states_{asset}.csv`, `filtered_{asset}.parquet`, `param_draws_{asset}.npy`
- `reports/model/` report and figures.

## Tests (`tests/test_model.py`)

- Simulator reproducibility (same seed, identical arrays); common random numbers across calls.
- With α = γ_lev = β = 0 and normal innovations, the simulator reduces to i.i.d. normal: check mean and variance.
- GARCH recursion in the simulator matches `arch`'s filtered conditional variance on historical data
  (to 1e-8 after unit conversion), so the state σ_0 is computed the same way it is simulated.
- `constant_arith` premium: the mean of simulated simple excess returns ≈ μ_e/252 within MC error.
- No look-ahead: expanding-window σ_t for date t is unchanged when data after t is altered.

## DECISIONS.md entries to add (at least)

GJR-t as base (with the comparison evidence); premium parameterisation and base value; FHS variant;
Vasicek rate model and window; equity–rate correlation; state = (σ_0, r_0) and why; percentile-based
state definitions (transferable across assets); PIT calibration results and what they changed; any dead ends.

## Acceptance criteria

- `python scripts/02_fit_model.py --asset us_mkt` (and `--asset all`) runs and produces all outputs.
- The PIT table by σ bucket is in the report, with a plain-language verdict on whether the model's left tail
  is well calibrated, too thin or too fat in each bucket.
- `pytest` passes; DECISIONS.md "which script produces which number" table updated.

---

## Addendum from SPEC 01 findings (read before starting; overrides anything above it conflicts with)

1. **Risk-free precision.** Daily RF is published with 2 decimals in percent (0.01%/day ≈ 2.5%/yr steps)
   and is zero in 315 months where the monthly rate is positive. Use `rf_ann` (from the monthly file)
   for r_0 and for fitting the rate model, never the daily RF column.
2. **Negative rates.** The monthly RF is negative in 12 months (1933, 1938–1941). The rate model must allow
   r < 0 (keep `rate.floor_at_zero: false` as the default). Report whether the default 1954+ window
   excludes them, and include the full-sample rate fit as a robustness row.
3. **Multi-day returns.** Some "daily" returns span market closures (e.g. 1933-03-15 after the 12-day bank
   holiday; the 2001 and 2012 closures). GARCH treats them as one-day shocks, which inflates the
   tail estimates. Using the gap list from `reports/data_check/data_check.json`, flag returns that follow a
   gap of more than 4 calendar days. Fit the base model (a) as is and (b) with those returns excluded
   (set to NaN and skipped in the likelihood, carrying the variance recursion through) or scaled by
   sqrt(gap trading days). Report the effect on ν, persistence and the PIT results, pick one as base,
   and log the choice in DECISIONS.md.
4. **Trading days per year.** Pre-1952 data include Saturday sessions (about 280+ days per year), and 1968 has 226
   (Wednesday closures). GARCH runs in trading-day time, which is fine. When annualising vol or setting state
   percentiles, use 252 consistently and note that pre-1952 annualised figures are slightly overstated.
   Also test `estimation.start_date: 1952-07-01` as a robustness window.
5. **Strong regime dependence.** The chance of a one-year loss worse than 5% ranges from 1.7% (2010s)
   to 45% (1930s). This is exactly what the conditional PIT test (section 5b) must check: report its
   results by σ_0 bucket **and** by era (pre-1952, 1952–1999, 2000–), so we can tell whether the
   model's dependence on σ_0 explains the regime differences or whether something else is missing.

## Run order and checkpoint

- Run everything for `us_mkt` first. Then **stop and report**:
  (1) the model comparison table (log-likelihood, BIC, ν, persistence, half-life, unconditional vol);
  (2) the PIT tail frequencies (u < 0.01/0.02/0.05/0.10) by σ_0 bucket and by era, with CIs and effective sample sizes;
  (3) the with/without closure-gap comparison (item 3);
  (4) the named starting states (σ_0, r_0) and the anchor-date values;
  (5) the premium estimate, its standard error and the shrunk value used.
  Wait for confirmation before running all assets.
- If the PIT test is slow, run it for the base model first and the comparison models afterwards.
- Read the current DECISIONS.md and data/processed/ outputs before starting; do not modify SPEC 01 outputs.
