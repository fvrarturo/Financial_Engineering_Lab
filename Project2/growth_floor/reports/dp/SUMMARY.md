# SPEC 04: DP policy, summary (us_mkt)

Branch `dp` in worktree `../growth_floor_dp`, not merged. Run on the night of 21–22 Nov 2026, alongside the SPEC 03 full run
(main folder untouched apart from the `pre-DP snapshot` commit; see DECISIONS for the `git init` that required).
Full per-cell tables: `reports/dp/tables_us_mkt.md`. Machine-readable files: `reports/dp/validation_us_mkt.json`,
`calibration_us_mkt.json`, `scorecard_us_mkt.csv`. Pipeline log: `reports/dp/run_us_mkt.log`.

## What was built

- **Solver** `src/gf/dp.py`: backward induction for max E[−1/W_T − λ·1{W_T < K}] on the state (τ, y = W/F̃, h), with
  F̃ the SPEC 03 robust floor (K·e^{−q}, q from `cash_growth_lower`) and the rate fixed at the median r0.
  - Grid: 120 y points in bands on [0.3, 4], concentrated on [0.97, 1.08] (Δlog y ≈ 0.0022). 40 h points (log). 60-node
    t-shock quadrature (conditional-mean nodes; mean, variance and the 0.1% tail are exact). 41 φ values in [0, 1].
  - τ = 1 … 504, with an exact last step (shock CDF). Interpolation is monotone cubic (PCHIP) in log y and log h on the
    homogeneous transform y·J. A breach-probability recursion B under φ\* runs alongside.
  - Solve time: 18–25 min per λ at 1 thread under load 20–30 on the 8-core machine (the SPEC 03 run was sharing it); 8 λ
    values in ≈ 90 min wall on 2 workers. The doubled-y solves for V4 took 35–41 min each.
- **Policy file** `results/policies/dp_us_mkt.npz` (format `dp_v2`): uint8 φ tables (8 λ × 505 τ × 120 y × 40 h),
  B, metadata, and the λ\* lookup by (σ0 band, T).
- **Hook** `dp_policy` in `src/gf/strategies.py`: bilinear φ in (log y, log h), y from SPEC 03's `protect_floor`,
  h = σ², λ\* lookup or explicit grid λ rows, and out-of-grid counters.
- **Pipeline** `scripts/05_dp.py` (stages: solve, calibrate, validate, v4, evaluate, compare, figures, tables).
  Evaluation results are in `results_dp/` (same schema as `results/mc/`, 50k paths, eval seed, paired with the full SPEC 03 run).
- **App**: `results_dp/` is merged into the "full" run (`dp.results_dir` in config); `dp_policy` shows as "DP (λ\*)".
  A new "DP policy" tab has τ and λ sliders and a φ\*(y, σ) heatmap.
- **Figures**: `reports/dp/policy_heatmap_us_mkt.png` (φ\* over (y, σ) at τ = 252 and 21, normal state, λ\* for T = 2) and
  `lambda_curves_us_mkt.png` (breach and CE versus λ).
- **Tests**: `tests/test_dp.py` covers the quadrature moments, the grids, floor agreement with SPEC 03, V1, V2,
  last-step extrapolation, hook round-trip and lookup, PCHIP against an exact random walk, banded-grid doubling and
  PCHIP in h. Also `tests/test_app_smoke.py::test_dp_policy_merge_and_tab`. **Full suite: 67 passed.**

## Cuts (time budget)

- **Cut 1: industries.** None were run, because one asset costs about 1.5 h of solves plus calibration with the cores
  shared. us_mkt is complete: all named states, T ∈ {1, 2}, base + robust set + 4 stress scenarios, 50k paths. No
  further cuts were needed (all 8 λ, all T = 2 scenarios).
- V4 is trimmed to the y grid, as the spec says; doubling h and z is further work.

## λ\* (target: worst case over the robust set ≤ 1.25%; tuning seed, 20k paths; grid rule, no logistic fallback needed)

| state | T = 1 | worst breach at λ\* | T = 2 | worst breach at λ\* |
|---|---|---|---|---|
| calm | 7.07e-4 | 0.94% | 1.36e-3 | 1.23% |
| normal | 7.07e-4 | 0.97% | 2.61e-3 | 0.43% |
| elevated | 7.07e-4 | 0.55% | 1.36e-3 | 0.99% |
| crisis | 3.68e-4 | 0.91% | 1.36e-3 | 0.66% |
| extreme | 1.92e-4 | 1.03% | 1.36e-3 | 0.40% |

The binding scenario is dgp_garch_t everywhere except extreme (param_unc). The λ grid is coarse (factor ≈ 1.9), so
λ\* often overshoots the target: normal T = 2 lands at 0.43% because the next λ down gives 1.33%.

## Validation

| test | result | notes |
|---|---|---|
| V1 Merton limit | **pass** | max \|φ − clip(μ/γσ²)\| = 0.0108 < φ step |
| V2 limits in λ | **breach part pass; two strict items fail (explained)** | breach is non-increasing in λ in all 10 cells, and λ = 0 has the highest breach everywhere. CE is highest at λ = 0 in 9/10 cells; normal T = 2 has CE(1e-4) − CE(0) = +$630 (0.3 bp/yr) under base, but CE is strictly decreasing in the DP itself and in the rate-constant MC. "φ ≈ 0 near y = 1 at large λ" is **not** met (mean φ\* on y ∈ [1, 1.02] at λ = 5e-3: 0.18 / 0.28 / 0.33 / 0.38 at τ = 21 / 126 / 252 / 504); see open issue 1 |
| V3 shape | J increasing, φ ∈ [0, 1]: pass. Monotonicity violations reported | φ falls with the cushion in 487–2081 of 38 080 (τ, h, y) checks for λ > 0 (0 at λ = 0); φ rises with h in 2–45. All of these are in the gambling-for-resurrection band below the floor: the Lagrangian objective is non-concave, so near y = 1 the policy is φ large just below the floor, ≈ 0 at it, and rising above (see the heatmap) |
| V4 y-grid convergence | **pass** | ΔCE −0.22 … +0.13 bp/yr, Δbreach −0.075 … +0.03 pp (normal and crisis, T = 1 and 2) |
| V5 DP vs MC | **pass for the model the DP solves; 16/80 base cells fail (explained)** | Against MC with the rate held constant: 80/80 cells within 2.58 SE + 0.1 pp. Against base MC: the 16 failures are all at T = 2. Base MC breach is 0.5–1.2 pp *below* the DP at small λ and 0.1–0.2 pp *above* at λ ≥ 2.6e-3. The rate-constant MC matches the DP in those same cells, so the gap is stochastic-rate risk, which the fixed-rate DP does not model. It is not a grid or quadrature error |
| V6 optimality | **pass (frontier version); λ\* version has no comparable cells** | Paired with the full SPEC 03 run. At λ\* the DP's base breach (0.35–1.06%) is above every tuned comparator's (their tuning leaves the base breach further below target), so "same or lower breach" holds in 0/40 cells. Frontier comparison: for each comparator, take the DP λ with the best CE among those with base breach ≤ the comparator's. DP CE ≥ comparator in **32/32** comparable cells, by +13 to +164 bp/yr. The 8 non-comparable cells are OBPI-uniform/extreme-state OBPI with base breach ≤ 0.06%, which the DP does not reach with λ ≤ 5e-3 |

Fix history (details in DECISIONS):
- The spec's bilinear y interpolation was diffusive: about 2–3 pp of breach bias against an exact random walk. Replaced by PCHIP.
- V5 failed in 46/80 cells on a uniform-band y grid because the policy jump near the floor was unresolved. Fix attempt 1, a
  banded grid with the same 120 points, cleared every rate-constant cell.
- Linear h interpolation lost about $0.15M of CE at T = 2. Replaced by PCHIP in h.

Nothing was tuned to a test target.

## Scorecard (base / worst case over the robust set; evaluation seed, 50k paths, paired)

| state | T | strategy | base breach | worst breach | CE − cash, %/yr (base) |
|---|---|---|---|---|---|
| normal | 1 | **DP (λ\*)** | 0.79% | 0.88% | **2.38%** |
| normal | 1 | OBPI-Merton (state) | 0.22% | 0.80% | 1.97% |
| normal | 1 | CPPI gap-aware (state) | 0.26% | 1.28% | 2.17% |
| normal | 2 | **DP (λ\*)** | 0.37% | 0.39% | **2.36%** |
| normal | 2 | OBPI-Merton (state) | 0.32% | 0.85% | 1.70% |
| normal | 2 | CPPI gap-aware (state) | 0.37% | 1.26% | 1.99% |
| crisis | 1 | **DP (λ\*)** | 0.77% | 0.86% | **1.54%** |
| crisis | 1 | OBPI-Merton (state) | 0.71% | 1.26% | 0.26% |
| crisis | 1 | CPPI gap-aware (state) | 0.22% | 1.18% | 1.19% |
| crisis | 2 | **DP (λ\*)** | 0.63% | 0.63% | **1.97%** |
| crisis | 2 | OBPI-Merton (state) | 0.43% | 0.78% | 0.28% |
| crisis | 2 | CPPI gap-aware (state) | 0.35% | 1.33% | 1.65% |

Uniform-tuned variants and the calm/elevated/extreme states are in `scorecard_us_mkt.csv` and `tables_us_mkt.md`.
Worst case = max over the robust set (base, dgp_garch_t, fhs, param_unc, persistent).
CE is quoted as excess over the cash return (annualised CE minus the cash strategy's, same cell and paths; DECISIONS 2026-11-22). CE differences (bp/yr) are unchanged by this convention.

The DP's worst-case breach (0.39–1.12% over all states and T) sits close to its base breach. The comparators' base
breach is lower, but their worst case is wider (gap-aware CPPI up to 1.33%, OBPI-Merton state up to 1.47%). Across all 10
cells, DP at λ\* earns +20 to +66 bp/yr CE over gap-aware CPPI and +40 to +171 bp/yr over OBPI-Merton. Because the DP
also takes more base breach, the fair comparison is the breach-matched V6 frontier: +13 to +164 bp/yr.

**Stress scenarios** (outside the calibration set): DP breach under `mu_zero` rises to 1.1–3.3% (calm T = 2: 3.25%), and to
1.5–3.5% under `fat_tails`. CPPI/OBPI hold 0.2–0.8% under `mu_zero`. The DP spends the premium it is told exists, so it is
more exposed to a wrong μ than the rule-based strategies. See `tables_us_mkt.md` / `results_dp/` for all scenarios.

## Open issues

1. **Fixed rate inside the DP.** y = 1 is the *robust* floor K·e^{−q}. At the median rate, cash alone grows it past K
   for long τ, so in the DP's model the floor still carries cushion and φ\* there is 0.2–0.4 at long τ, even at λ = 5e-3.
   With stochastic rates, part of that cushion is not there. This causes the V2 "φ ≈ 0" miss, the V5 base-scenario gap
   at T = 2, and the low gap-share of residual T = 2 breaches (22–42%; the rest are rate-driven). The proper fix is r as
   a fourth state variable, or a floor indexed to the realised cash account. That is a model change, so it was not done
   as a "fix".
2. **Other r0.** The policy is solved at one r0 (median). States with very different r0 use the same table.
3. **Grid convergence in h and z** is untested (V4 trimmed by spec). PCHIP in h at 40 points matched an 80-point linear
   solve to ≤ $40k CE in a spot check.
4. **Duality gap and gambling.** The Lagrangian (non-concave) gives a policy that gambles just below the floor and a
   breach-λ curve with jumps of 4.5–9 pp between grid λ values. Some breach targets are unreachable exactly, and λ\*
   overshoots the target (e.g. normal T = 2 at 0.43%). A finer λ grid, or randomising between two λ values, would close
   that gap.
5. **V2 CE miss** (normal T = 2, +$630) is within stochastic-rate effects and MC noise. It is reported, not adjusted.
6. **μ misspecification** (stress above): the DP is the most premium-dependent strategy in the comparison.
7. **Industries** were not run (cut 1).
8. **Dead ends** are kept for reference: linear-y solves (`results/policies/old_uniform_y/dp_us_mkt_linear_*`); uniform-band
   y solves, with their calibration and validation (`results/policies/old_uniform_y/`); and orphaned old-grid V4 solves
   (`old_uniform_y/orphan_y2/`, see DECISIONS: these led to a grid fingerprint on the solve cache).

## For the merge (user)

Nothing is merged. `results_dp/`, `results/policies/` and the `dp` branch are in the worktree. After merging the branch,
copy `results_dp/` and `results/policies/dp_us_mkt.npz` into the main folder. The app then picks up `results_dp/` via
`dp.results_dir` in `config.yaml`.
