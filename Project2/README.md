# Grow the money, don't break the floor

15.456 Financial Engineering · Independent Project 2 · Fall 2026

$100M, daily trading between equity and cash (φ ∈ [0, 1]), horizon 1–2 years. Maximise certainty-equivalent wealth
(CRRA, γ = 2) subject to P(W_T < $95M) ≤ 2% from every starting condition.

**Headline strategy: the premium-robust DP policy** (`dp_premrobust`). It is the dynamic-programming policy, with its
breach penalty λ calibrated so the 1.25% breach target also holds when the equity premium is halved. On the US market
(50k paths, paired with every other strategy):

| Strategy | Max base breach | Max worst-case breach (robust set) | Max breach, zero premium | Avg CE/yr (base) |
|---|---|---|---|---|
| **DP premium-robust** | 0.91% | **0.95%** | 1.75% | **4.99%** |
| DP (λ\*), base calibration | 1.06% | 1.12% | 3.25% | 5.01% |
| CPPI gap-aware | 0.38% | 1.33% | 0.43% | 4.66% |
| OBPI-Merton | 0.71% | 1.47% | 1.06% | 3.99% |

It beats gap-aware CPPI by 13–63 bp/yr of CE in every state and horizon. Caveats: under fat-tailed stress its breach
exceeds 2% in the crisis and extreme states at T = 1 (as gap-aware CPPI's does); the interest rate is held fixed
inside the DP; only the US market was run. Details: `growth_floor/reports/dp/SUMMARY.md` and the
"Premium-robust λ\*" entry in `growth_floor/DECISIONS.md`.

| Folder | Contents |
|---|---|
| [`growth_floor/`](growth_floor/README.md) | The pipeline: data loading, volatility model and named market states, strategies (OBPI, CPPI, Merton), Monte Carlo and backtest harness, the dynamic-programming policy, a Dash app, and tests. Setup and run instructions are in its README |
| [`growth_floor/reports/dp/SUMMARY.md`](growth_floor/reports/dp/SUMMARY.md) | Results of the dynamic-programming policy: validation, calibrated λ, scorecard against OBPI and CPPI, open issues |
| [`growth_floor/reports/dp_mu/`](growth_floor/reports/dp_mu/) | Premium-robust recalibration: λ\* table and scorecard |
| [`growth_floor/DECISIONS.md`](growth_floor/DECISIONS.md) | Every modelling and engineering decision, with dates and reasons |
| [`spec/`](spec/) | The four specifications the code was built against (data; model and states; strategies, MC and dashboard; DP) |
| [`docs/growth_floor_brief.pdf`](docs/growth_floor_brief.pdf) | The project brief |

**Included results**: the fitted model parameters and named states (`growth_floor/results/model/`, without the
filtered series and caches), the summary tables of the SPEC 03 Monte Carlo run (`growth_floor/results/mc/*.parquet`,
`results/backtest/summary.parquet`), the DP evaluations (`growth_floor/results_dp/`, `growth_floor/results_dp_mu/`) and the DP policy file
(`growth_floor/results/policies/dp_us_mkt.npz`, 66 MB). With these, the tests pass and the Dash app
(`python app/app.py`) shows the scorecards and the DP policy tab without rerunning anything. Per-path files and caches
are not included; the scripts regenerate them (the SPEC 03 full run took 6.8 h on 8 cores, the DP pipeline about
3 h more on 2 cores). The premium-robust policy file (same φ tables, different λ\* lookup) is not included:
`python scripts/06_dp_premrobust.py calibrate` rebuilds it from `dp_us_mkt.npz` in a few minutes.

**Data**: the Kenneth French Data Library CSVs used are in `growth_floor/data/raw/`, with `MANIFEST.json` recording
their hashes.
