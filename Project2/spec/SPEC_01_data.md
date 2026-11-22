# SPEC 01 — Data ingestion and checks

You are building the first stage of a reproducible research pipeline for a course project
("Grow the Money, Don't Break the Floor", MIT 15.456). Read this whole spec before writing code.

## Project context (applies to all three specs)

- Investor holds a diversified equity portfolio and cash (earning the short rate). Starts with
  $100M at t=0, trades daily, no borrowing, no shorting (equity weight φ ∈ [0, 1]), no trading costs.
- Horizon T is known at t=0 and ranges from 1 to 2 years.
- Objective: maximise the certainty equivalent CE = (E[W_T^(1−γ)])^(1/(1−γ)) with γ = 2,
  subject to P(W_T < $95M) ≤ 2% **for every starting market condition** (calm through crisis).
- The deliverable is a **procedure**: it must estimate everything it needs from an asset's own
  history. Nothing may be hard-coded to the US market. Every function that touches data must
  work for any daily return series plus a risk-free series.
- Every number in the final report must be reproducible from code, with fixed seeds.
- A decision log `DECISIONS.md` lives at the repo root (format below). You must add entries to it.

## Repository layout (create it now; later specs fill it in)

```
growth_floor/
  DECISIONS.md            # how to run + dated decision entries (see format below)
  README.md               # setup, how to run each stage, how to launch the Dash app
  requirements.txt        # pinned versions
  config.yaml             # all tunable settings live here, nothing magic in code
  data/
    raw/                  # French CSVs exactly as downloaded (never modified)
    processed/            # parquet outputs of this stage
  src/gf/                 # importable package
    __init__.py
    io_french.py          # this spec
    data.py               # this spec
  scripts/
    01_load_check.py      # this spec: entry point
  reports/data_check/     # this spec: check report + figures
  results/                # later specs
  app/                    # later specs (Dash app)
  tests/
```

Python 3.11+. Use pandas, numpy, pyarrow, matplotlib, pyyaml, pytest. Pin versions in requirements.txt.

## Inputs

The user downloads the files manually from Kenneth French's Data Library and places them in `data/raw/`:

1. `F-F_Research_Data_Factors_daily.CSV`: **required**. Columns Mkt-RF, SMB, HML, RF.
2. `F-F_Research_Data_Factors.CSV` (monthly): **required**, used for the cross-check and for RF precision.
3. `10_Industry_Portfolios_Daily.CSV`: optional, used for the out-of-sample "other assets" tests.
4. Any other French daily portfolio file: optional, handled by the same parser.

Record in `data/raw/MANIFEST.json`, for each file: file name, SHA-256, size, the date the user downloaded
it (ask in README that it be filled in; default to the file mtime), and the first and last dates in the file.
French revises these files over time, so the hash is what ties the report to its data.

## Parser requirements (`src/gf/io_french.py`)

French CSVs are messy. The parser must:

- Skip free-text preamble lines. Detect the header row as the first line whose first field is empty and
  whose other fields are column names (e.g. `,Mkt-RF,SMB,HML,RF`).
- Handle **multi-section files** (industry files contain e.g. "Average Value Weighted Returns -- Daily"
  then "Average Equal Weighted Returns -- Daily", and monthly files contain an "Annual Factors" block).
  Parse sections by their title line; expose `section=` argument; default to the value-weighted
  (first) section for portfolio files and the monthly block for monthly factor files.
- Stop a section at the first blank line or non-numeric date field (e.g. the copyright line).
- Parse dates: daily `YYYYMMDD`, monthly `YYYYMM` (convert to month-end timestamp).
- Strip whitespace; coerce to float.
- Replace missing-value sentinels `-99.99` and `-999` (exact matches after float parse) with NaN, and
  **count** how many were replaced per column.
- Convert percent to decimals (divide by 100). Never leave percent units downstream.
- Return a tidy DataFrame indexed by date with a `units="decimal"` attribute, plus a small dict of
  parse metadata (section title, rows, sentinel counts).

## Constructed series (`src/gf/data.py`)

Produce a standard **asset frame** that every later stage consumes, for any asset:

| column    | meaning |
|-----------|---------|
| `r`       | simple total return of the asset on day t (decimal) |
| `rf`      | simple risk-free return for day t (decimal) |
| `x`       | log excess return: `log(1+r) − log(1+rf)` |
| `rf_ann`  | annualised short rate, best available estimate (see RF precision below) |

- US market: `r = (Mkt-RF) + RF`.
- Industry/other portfolios (already total returns): `r = portfolio return`, `rf` taken from the
  daily factor file, aligned on dates (inner join; report dropped dates).
- Function signature: `build_asset_frame(returns: pd.Series, rf_daily: pd.Series, rf_monthly: pd.Series) -> pd.DataFrame`.
  Must not assume the asset is the US market.
- Save to `data/processed/{asset_name}.parquet`. Asset names: `us_mkt`, `ind_<name>`.

### RF precision

Daily RF is published with 3 decimals in percent (0.001% per day ≈ 0.25% per year resolution), and is
derived from the monthly T-bill rate, so it is roughly constant within each month and heavily rounded
(many exact zeros 2009–2015 and 2020–2021). Therefore:

- Check and report whether daily RF is constant within months and how it compares with the monthly RF
  compounded: `(1+rf_m)` vs `prod(1+rf_d)` per month.
- Build `rf_ann` from the **monthly** file: `rf_ann = (1+rf_m)^12 − 1`, forward-filled onto the daily
  dates of that month. Later stages use `rf_ann` as the observable short rate at t=0 and to fit the rate model.
- Keep the daily `rf` column for the wealth accounting in backtests (it is what cash actually earned in the data).
- Log this choice in DECISIONS.md.

## Data checks (`scripts/01_load_check.py`)

Run all checks and write `reports/data_check/data_check.md` plus `data_check.json` (machine-readable)
and figures. Checks:

1. **Coverage**: first/last date, number of trading days, trading days per calendar year
   (flag years with < 240 or > 260; 1926 is partial; pre-1952 Saturday trading may show up, so report it).
2. **Dates**: strictly increasing, unique, no weekends after 1952. List all gaps > 4 calendar days with
   dates (expect e.g. the March 1933 bank holiday, Sept 2001, Oct 2012 Hurricane Sandy, state funerals).
3. **Missing values**: sentinel counts and NaN counts per column; rows dropped and why.
4. **Units sanity**: assert |median daily r| < 0.005 and the daily std is between 0.003 and 0.05. If it
   fails, the file was probably not converted from percent: raise a loud error.
5. **Extreme days**: table of the 20 largest |r| days. Must contain 1987-10-19 (about −17% to −18%)
   and the March 2020 crash days; if not, flag it. Also list any |r| > 25% as suspicious.
6. **Daily vs monthly cross-check**: compound daily Mkt-RF+RF to months and compare with the monthly
   file. Report the distribution of absolute differences; flag months with |diff| > 0.5%.
7. **RF checks**: as in "RF precision"; also count negative RF days and zero days per decade.
8. **Summary statistics by decade and full sample**: annualised mean of r and x, annualised vol,
   skew, excess kurtosis, min, max, worst 252-day and 504-day cumulative return (the floor-relevant
   numbers), and the share of 1-year and 2-year windows with cumulative return below −5%.
9. **Figures**: cumulative log wealth; rolling 63-day annualised vol; rf_ann; histogram of daily x
   with a normal overlay (log-y axis); rolling 252-day returns.

Each check gets a PASS / WARN / FAIL status in the JSON. The script exits non-zero on any FAIL.

## Sample window

Do **not** pick the estimation window here. Store the full history. The window is a config setting
(`estimation.start_date`, `estimation.end_date`) used by Spec 02. Default: full sample, with
alternatives (`1963-07-01`, `1990-01-01`) to be tested as robustness in Spec 02.

## config.yaml (initial content)

```yaml
data:
  raw_dir: data/raw
  processed_dir: data/processed
  assets:
    us_mkt: {source: ff3_daily, column: Mkt-RF, add_rf: true}
    # ind_*: {source: ind10_daily, section: value_weighted, column: <Name>, add_rf: false}
trading_days_per_year: 252
estimation:
  start_date: null    # null = first available
  end_date: null      # null = last available
```

## Tests (`tests/test_data.py`)

- Parser on a small hand-written fixture CSV containing a preamble, two sections, a `-99.99`, a blank
  line, and a copyright footer: correct shape, NaN placed correctly, values divided by 100.
- `build_asset_frame` on synthetic data: `x` equals `log(1+r) − log(1+rf)`; works for a non-US asset.
- Units-sanity check raises when fed percent-unit data.

## DECISIONS.md format

The file must open with:
1. **How to run**: exact commands, in order.
2. **Which script produces which number**: a table (report item → script → output file). Start it
   now and keep it updated in later specs.

Then dated entries (`## YYYY-MM-DD: <title>`), each with:
**Decision**, **Alternatives considered**, **What was tested** (script + output path), **Result**,
**Status** (kept / dropped / replaced). Include dead ends.

Entries to add in this stage (at least): the parser approach; the `-99.99` handling; total return =
Mkt-RF + RF; RF from the monthly file for `rf_ann`; log excess returns as the modelling variable;
storing the full history and deferring the window choice.

## Acceptance criteria

- `python scripts/01_load_check.py` runs from a clean checkout with the raw files present, produces
  the parquet files, the report, the JSON and the figures, and exits 0 (or non-zero with a clear FAIL).
- `pytest` passes.
- README and DECISIONS.md updated.
- No hard-coded dates, asset names or constants outside config.yaml, except the sentinel values.
