# Grow the Money, Don't Break the Floor (MIT 15.456)

A reproducible research pipeline. It starts with $100M in cash, trades daily between a diversified equity
portfolio and cash (φ ∈ [0, 1]), and targets a horizon T of 1 to 2 years. The objective is to maximise CE
with γ = 2, subject to P(W_T < $95M) ≤ 2% from every starting condition. The pipeline is a *procedure*: it
estimates everything from an asset's own daily return history plus a risk-free series.

**CE convention.** CE is quoted everywhere (report tables, app, figures) as the excess over the cash return in %/yr:
the strategy's annualised CE minus the annualised CE of the `cash` strategy in the same cell (same asset, state, T,
scenario and seed, so the same simulated rate paths). Column `ce_xcash`, computed by `gf.metrics.add_ce_xcash`.
The stored `ce_ann_excess` (CE minus the starting rate r0) is not quoted.

## Setup

```bash
cd growth_floor
python3 -m venv .venv && source .venv/bin/activate     # Python >= 3.11 (tested on 3.12.6)
pip install -r requirements.txt
pip install -e .                                         # optional: makes `import gf` work everywhere
```

## Data (manual download)

Download these files from the
[Kenneth French Data Library](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html),
unzip them, and put the CSVs **unmodified** in `data/raw/`:

| file | required | used for |
|---|---|---|
| `F-F_Research_Data_Factors_daily.csv` | yes | US market total return (Mkt-RF + RF), daily RF |
| `F-F_Research_Data_Factors.csv` (monthly) | yes | rf_ann (short rate), daily-vs-monthly cross-check |
| `10_Industry_Portfolios_Daily.csv` | no | out-of-sample "other assets" (value-weighted section) |

File names are set in `config.yaml` (`data.sources`). Any other French daily portfolio file can be added
there: add a source plus asset entries (`ind_<name>`). The same parser handles it.

**Please record the download date.** `scripts/01_load_check.py` writes `data/raw/MANIFEST.json` with each
file's SHA-256, size, sections and first/last dates. `download_date` defaults to the file's modification
time. Edit it to the real date and set `"download_date_source": "user"`. The value is kept on later
runs as long as the file's hash does not change. French revises these files, so the hash is what ties
results to their data. The files used here were built from the **202609 CRSP database** and run
through 2026-09-30.

## Run

```bash
python scripts/01_load_check.py                    # 1. data: parse, build asset frames, checks (exit 1 on any FAIL)
python scripts/02_fit_model.py --asset all         # 2. model, states, validation (~2 min per asset)
python scripts/03_run_mc.py --asset all --quick    # 3. tune + Monte Carlo, quick (5k paths) -> results/quick/
python scripts/04_backtest.py --asset all --quick  # 4. historical backtest using the quick knobs
python scripts/03_run_mc.py --asset all            # 3. full run (50k eval / 20k tune paths) -> results/
python scripts/04_backtest.py --asset all          # 4. backtest with the full-run knobs
python app/app.py                                  # 5. Dash app at http://127.0.0.1:8050
pytest                                             # tests
```

`--asset` takes `us_mkt`, `industries`, `all` or one asset name. Stage 3 uses all cores (joblib, `mc.n_jobs`);
on a laptop with little free memory use `--jobs 4`. The app only reads `results/` (full) and `results/quick/`
(quick); pick the run in its sidebar. PNG export uses `kaleido`, which needs a Chrome install; the plotly camera
icon works without it.

Stage 3 outputs: `results/tuned/{asset}_base.json` (tuned knobs, uniform and state-dependent),
`results/mc/summary.parquet` (one row per asset × strategy × knob/tune mode × state × T × scenario),
`results/mc/worst_case.parquet`, `results/mc/frontier.parquet`, `results/mc/terminal/*.npy` (5k terminal wealths
per cell), `results/mc/paths/*.npz` (named states: 200 sample paths + fan quantiles), `results/mc/timings.parquet`,
`results/run_manifest.json`. Stage 4: `results/backtest/{per_start,summary}.parquet`, `fits_{asset}.json`.

Stage 2 outputs: `results/model/{params,model_comparison,states,filtered,pit,param_draws,zhat}_{asset}.*`,
and `reports/model/{asset}/model_report.md` (+ `.json`, `figures/`). Requires `arch` and `scipy`.

Stage 1 outputs:

- `data/processed/{asset}.parquet`: asset frames with columns `r`, `rf`, `x`, `rf_ann`, all in decimal units
- `reports/data_check/data_check.md`: human-readable report; `data_check.json`: machine-readable, with PASS/WARN/FAIL per check
- `reports/data_check/figures/<asset>/*.png` plus `figures/rf_daily_vs_monthly.png`

## Report, deck and submission (SPEC 05)

**Fast path** (minutes; from the committed results, no simulation):

```bash
pip install -r requirements.txt && python -m playwright install chromium
python scripts/07_report_numbers.py    # -> results/report/numbers.json, results/report/tables/*, results/deployment.csv
python scripts/08_build_report.py      # -> deliverables/figures/*, report.{md,html,pdf}, deck.{html,pdf}
python scripts/09_package.py           # -> submission/ and deliverables/growth_floor_submission.zip
```

07 runs in *extract* mode when the large files exist (`results/policies/*.npz`, `results/mc/terminal/`) and rebuilds
`results/report/inputs/`. Otherwise it runs in *fast* mode on the committed inputs. Both give the same numbers.json.
Every number in the report and deck comes from numbers.json or a table (map: top of DECISIONS.md). To edit wording,
change `report/templates/report.md.j2` / `deck.html.j2` and re-run 08. Do not edit `deliverables/report.md`: it is overwritten.
Deployment rule: `src/gf/deployment.py` (`deployment_choice(asset)`).

**Full path**: stages 01–04 above, then `scripts/05_dp.py` (us_mkt DP: solve, calibrate, validate, evaluate) and
`scripts/06_dp_premrobust.py` (solve / calibrate / evaluate / compare / report per asset; see its docstring and
`scripts/run_dp_ind_rest.sh`), then 07–09. Run times are in DECISIONS.md.

## Layout

```
config.yaml         every tunable setting (paths, assets, thresholds, estimation window)
src/gf/io_french.py French CSV parser (sections, sentinels, percent -> decimal, manifest entries)
src/gf/data.py      asset frame construction (build_asset_frame), config loading, parquet I/O
src/gf/checks.py    data checks used by scripts/01_load_check.py
src/gf/model.py     GARCH-family fits, variance filter, premium, Vasicek, expanding refits, param draws
src/gf/simulate.py  vectorised path simulator (simulate_paths, common_random_numbers)
src/gf/states.py    filtered history, named/dense/anchor starting states
src/gf/validation.py PIT calibration, VaR backtests, stylised-fact helpers
src/gf/engine.py    wealth engine, SimMarket (scenario DGP + investor filter), ArrayMarket, breach decomposition
src/gf/strategies.py strategy interface + registry (cash, merton_const, merton_vol, cppi, cppi_vol, obpi_merton, dp_policy hook)
src/gf/scenarios.py asset context (SPEC 02 estimates) and scenario DGPs
src/gf/tune.py      knob tuning (grid + refinement), uniform / state-dependent knob functions
src/gf/metrics.py   breach (Wilson CI), CE (+ bootstrap SE), quantiles, phi / turnover / breach-type stats
app/app.py          Plotly Dash comparison app (reads results/ only)
scripts/            stage entry points
tests/              pytest suite (+ fixtures/)
DECISIONS.md        how to run, number -> script map, dated decision log
```

Using processed data downstream:

```python
from gf.data import load_config, read_asset_frame
cfg = load_config()
df = read_asset_frame(cfg, "us_mkt")   # cut to estimation.start_date / end_date from config.yaml
```
