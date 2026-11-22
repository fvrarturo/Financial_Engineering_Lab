# SPEC 04 — Dynamic programming policy with a breach penalty

Builds on SPECs 01–03 (same repo, config, DECISIONS.md). Read SPEC 01's "Project context", the
SPEC 03 addenda and the current DECISIONS.md first. This stage solves the investor's problem directly
with dynamic programming (lectures 1 and 3) and plugs the policy into SPEC 03's existing `dp_policy` hook,
so it is evaluated by the same harness, scenarios, scorecard and Dash app as the other strategies.

## 0. Working conditions (read first: this runs overnight, unattended)

- **A full SPEC 03 run is in progress in the main project folder (started 20:59).** Do not modify, delete
  or move any file in that folder or its `results/` while it runs. Its worker processes reload code from there.
- **Setup, in this order:**
  1. In the main folder, commit the current state (`git add -A && git commit -m "pre-DP snapshot"`).
     This only writes to `.git` and is safe while the run continues.
  2. Create a worktree for the DP work: `git worktree add ../growth_floor_dp -b dp`.
  3. Copy `results/model/` and `data/processed/` from the main folder into the worktree (they may be git-ignored).
     Copy, don't move.
  4. Do **all** further work in `../growth_floor_dp` only.
- When the full run finishes, do not merge anything. Leave `results_dp/` and the `dp` branch in the worktree;
  merging is done by the user in the morning.
- Use at most 2 CPU cores (`OMP_NUM_THREADS=2`, `MKL_NUM_THREADS=2`, joblib `n_jobs ≤ 2`).
- Work autonomously. Do not stop to ask unless a decision would invalidate SPEC 01–03 results.
  At the end, write a summary to `reports/dp/SUMMARY.md` and log every decision in DECISIONS.md.
- **Time budget: everything must finish by 7 am.** After the first full backward solve, measure its time and
  project the total run. If the projection exceeds the budget, cut further in this order: (1) the industries,
  (2) T = 2 evaluation scenarios beyond the robust set, (3) λ values down to 6. Never skip V1, V2 or V5.
  Log every cut in DECISIONS.md.
- **Stop conditions:** if validation test V1 (Section 5) still fails after two fix attempts, stop, write up
  what failed in SUMMARY.md, and leave the branch in a clean state. Do not hand-tune anything to make a test pass.

## 1. The problem

Maximise E[U(W_T)] with U(W) = −1/W (CRRA, γ = 2; maximising E[U] is the same as maximising CE),
subject to P(W_T < K) ≤ target, K = $95M, W_0 = $100M, φ_t ∈ [0, 1], daily trading.

Lagrangian relaxation with multiplier λ ≥ 0:

```
max E[ U(W_T) − λ · 1{W_T < K} ]
```

For each λ the problem is an ordinary DP. Larger λ gives fewer breaches. λ is calibrated afterwards
(Section 4) so that the breach constraint holds. The indicator makes the problem non-convex, so there
may be a duality gap: report it if the achievable breach rates jump discontinuously as λ changes.

## 2. State, scaling and dynamics

**State:** (τ, y, h)
- τ = trading days remaining (0 … 504)
- y = W_t / F_t, wealth relative to the floor's present value (y = 1 is the floor; at τ = 0, F = K)
- h = σ²_{t+1}, the GJR conditional variance for the next day (the complete equity state; see SPEC 02)

**Floor (must match the other strategies exactly):** F_t = K × exp(−q), where q is the 0.1% quantile of
integrated cash growth over the remaining horizon under the fitted Vasicek model (closed form), recomputed
daily. This is the floor the SPEC 03 floor strategies use; reuse that code, don't reimplement it. Use its
daily growth (with the rate at the median r_0) as g below.

**Rate inside the DP:** hold the short rate fixed at the median r_0 (from `states_{asset}.csv`). Rate risk is
handled by the conservative floor discount from SPEC 03 (fix 3). **Solve for the median r_0 only** (trimmed for time);
other r_0 values are further work. Log it.

**Scaling:** U is homogeneous of degree −1, so write J in units of K: J(τ, y, h) = value with W = y·F.
Let g = F_{t+1}/F_t (daily floor growth at the DP's discount rate). Then

```
y' = y · (1 + rf + φ (R − rf)) / g
x  = m(h) + √h · z,      R = exp(x + log(1+rf)) − 1,     m(h) from the base premium mode
h' = ω + (α + γ_lev · 1{z<0}) h z² + β h,                then apply SPEC 03's variance cap
```

with z ~ standardised t(ν) and the base GJR-t parameters from `results/model/params_{asset}.json`.

**Terminal condition:** J(0, y, h) = −1/(y·K/1e6) − λ · 1{y < 1}  (wealth in $M; record the units).

**Bellman step:** J(τ, y, h) = max_{φ ∈ Φ} E_z[ J(τ−1, y', h') ].

## 3. Numerical method (follow lecture 3's discretisation)

- **y grid:** ~120 points, log-spaced on [0.3, 4.0], with extra density on [0.9, 1.3] (where the policy
  changes fastest). Above y_max, use homogeneity, J(τ, y, h) ≈ J(τ, y_max, h) · y_max / y (the floor no
  longer binds). Below y_min, extrapolate flat in the policy and use the terminal-style value; report how often
  simulated paths go below y_min (should be ≈ 0).
- **h grid:** ~40 points, log-spaced in σ from 0.5 × the calm state's σ_0 to the variance cap.
- **Shock quadrature:** discretise z on ~60 nodes chosen on the probability scale, with denser nodes in
  the **left tail** (gap risk drives breaches): e.g. nodes at the quantiles of a grid on u ∈ (0, 1) that is
  fine below u = 0.01. Weights are CDF differences between bin edges (same idea as the binned densities in lecture 3);
  normalise them to sum to 1. Check that the quadrature reproduces E[z] = 0, E[z²] = 1 and P(z < q_0.001) to 1e-4.
- **Controls:** φ ∈ {0, 0.025, …, 1} (41 values). Optionally refine with a local golden-section search; log it if used.
- **Interpolation:** bilinear in (log y, log h). Vectorise over the full (y, h, φ, z) tensor per time step, chunked
  to fit memory. Target: one full backward solve (504 steps) in ≤ 10 minutes on 2 cores. Report the measured time.
- Store φ*(τ, y, h) and J(τ, y, h) for every τ. One backward solve from τ = 504 covers **every horizon
  T ≤ 2 years and every starting σ_0** for that λ.

## 4. Calibrating λ

- Solve on a log-spaced λ grid of **8 values (λ = 0 plus 7 positive values)**, with a range chosen so the breach rate goes from the
  unconstrained level down to about 0.1%. Record the grid.
- For each λ and each cell of the **named states × T ∈ {1, 2}** (trimmed; not the dense grid), evaluate the policy with SPEC 03's harness
  (`dp_policy` strategy, tuning seed, 20k paths, the **robust scenario set**). This must use the same machinery
  as SPEC 03's robust tuning.
- λ*(σ_0, T) = the smallest grid λ whose **robust worst-case** breach ≤ 1.25% (the same target and robust
  scenario set as SPEC 03). Breach is measured against the SPEC 03 floor described in Section 2.
  If the breach estimates across λ are noisy, use the same logistic-curve fit that SPEC 03 uses for knob selection
  to locate the crossing, then take the next grid λ at or above it.
  Interpolating between grid values is not allowed. Taking the next grid point up is conservative; log it.
- Save `results/policies/dp_{asset}.npz` containing the axes (τ, y, h), the r_0 used, the λ grid, the φ tables for each λ,
  and the λ*(σ_0, T) lookup, in the format the `dp_policy` hook reads (update the hook if the format must change,
  and update its tests).

## 5. Validation (required before any results are reported)

- **V1, Merton limit (lecture 1):** constant variance (α = γ_lev = β = 0, h fixed), normal shocks, λ = 0.
  The DP policy must equal the capped Merton weight clip(μ_e/(γσ²), 0, 1) at every (τ, y), within control-grid
  resolution. This is the myopic result for CRRA with IID returns.
- **V2, limits in λ:** λ = 0 gives the highest breach and CE; breach is non-increasing in λ (check across the grid);
  for very large λ, the equity weight near y = 1 is ≈ 0 and remaining breaches are gap-driven.
- **V3, shape:** J is increasing in y; φ* is in [0, 1]; at fixed τ and h, φ* rises with the cushion; at fixed y,
  φ* falls with h. Report any violations (some non-monotonicity in φ near y = 1 can be genuine; explain it rather than suppress it).
- **V4, grid convergence (trimmed: y grid only):** double the y grid once. The base CE should
  change by < 2 bp a year and the base breach by < 0.1 pp for the normal and crisis states at T = 1 and 2.
  Doubling the h grid and z nodes is further work; log it.
- **V5, DP vs Monte Carlo consistency:** compute the breach probability by backward recursion of the indicator
  under the optimal policy on the grid. It must match the harness's Monte Carlo estimate (base scenario) within MC error
  plus a small grid tolerance.
- **V6, optimality sanity:** under the **base** scenario, at the same or lower breach, DP's CE should be ≥ OBPI's and
  ≥ gap-aware CPPI's in every cell. If not, investigate (grid, quadrature, λ calibration) before reporting; if it
  persists, report it honestly.

## 6. Evaluation

- Evaluate `dp_policy` with λ* in SPEC 03's harness on the **evaluation seed**, for us_mkt: all named states,
  T ∈ {1, 2}, base + robust set + stress tests, 50k paths. Use the same seeds as the full SPEC 03 run, so
  the random draws match and the comparison with the other strategies is paired.
- Write results into a separate results directory (`results_dp/`, same schema as `results/mc/`). The Dash app
  must be able to load and merge both, so `dp_policy` appears as one more strategy. Add a config entry for the extra path.
- Only if the time projection allows: the industries at named states × T ∈ {1, 2} × {base, dgp_garch_t, fhs}, 20k paths
  (first thing to cut).
- Report the policy itself in a figure: φ* as a heatmap over (y, σ) at τ = 252 and τ = 21, for one λ*. Add a Dash
  tab "DP policy" with τ and λ sliders.

## 7. Outputs

- `reports/dp/SUMMARY.md`: what was built; V1–V6 results; the λ* table; the scorecard vs OBPI and gap-aware CPPI
  (base / worst-case breach / CE, normal and crisis, T = 1 and 2); solve time; open issues.
- `results/policies/dp_us_mkt.npz`, `results_dp/…`, figures in `reports/dp/`.
- Tests in `tests/test_dp.py` covering V1, V2, quadrature moments, extrapolation and the hook round-trip.
- DECISIONS.md entries: the Lagrangian approach and possible duality gap; the state reduction (τ, y, h) and why it is
  complete under the model; fixed rate inside the DP; grids and quadrature choices; the λ calibration rule; validation
  results; dead ends.

## Acceptance criteria

- V1–V5 pass; V6 passes or its failure is explained.
- `dp_policy` appears in the Dash app with numbers from `results_dp/`.
- `pytest` passes on the `dp` branch in the worktree; the running SPEC 03 job and the main folder were not disturbed.
