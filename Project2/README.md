# Grow the money, don't break the floor

15.456 Financial Engineering · Independent Project 2 · Fall 2026

$100M, daily trading between equity and cash (φ ∈ [0, 1]), horizon 1–2 years. Maximise certainty-equivalent wealth
(CRRA, γ = 2) subject to P(W_T < $95M) ≤ 2% from every starting condition.

| Folder | Contents |
|---|---|
| [`growth_floor/`](growth_floor/README.md) | The pipeline: data loading, volatility model and named market states, strategies (OBPI, CPPI, Merton), Monte Carlo and backtest harness, the dynamic-programming policy, a Dash app, and tests. Setup and run instructions are in its README |
| [`growth_floor/reports/dp/SUMMARY.md`](growth_floor/reports/dp/SUMMARY.md) | Results of the dynamic-programming policy: validation, calibrated λ, scorecard against OBPI and CPPI, open issues |
| [`growth_floor/DECISIONS.md`](growth_floor/DECISIONS.md) | Every modelling and engineering decision, with dates and reasons |
| [`spec/`](spec/) | The four specifications the code was built against (data; model and states; strategies, MC and dashboard; DP) |
| [`docs/growth_floor_brief.pdf`](docs/growth_floor_brief.pdf) | The project brief |

**Included results**: the fitted model parameters and named states (`growth_floor/results/model/`, without the
filtered series and caches), the summary tables of the SPEC 03 Monte Carlo run (`growth_floor/results/mc/*.parquet`,
`results/backtest/summary.parquet`), the DP evaluation (`growth_floor/results_dp/`) and the DP policy file
(`growth_floor/results/policies/dp_us_mkt.npz`, 66 MB). With these, the tests pass and the Dash app
(`python app/app.py`) shows the scorecards and the DP policy tab without rerunning anything. Per-path files and caches
are not included; the scripts regenerate them (the SPEC 03 full run took 6.8 h on 8 cores, the DP pipeline about
3 h more on 2 cores).

**Data**: the Kenneth French Data Library CSVs used are in `growth_floor/data/raw/`, with `MANIFEST.json` recording
their hashes.
