# Data

Everything was downloaded on 10 Nov 2026. `raw/` is untouched; `processed/` is rebuilt by `code/run_analysis.py` and `code/make_figures.py`.

## Sources

| path | source | coverage | notes |
|---|---|---|---|
| `raw/crsp_treasuries/*.parquet` | CRSP US Treasury database, WRDS (`/wrds/crsp/sasdata/q_treasuries`) | daily 1961-06-14 → 2026-06-30 | `tfz_dly`: bid, ask, mid price, accrued interest, yield and duration for every bill, note and bond (3.67M rows). `tfz_iss`: coupon, maturity, issue date and type per security; join on `KYTREASNO`. Also `tfz_mth_fb` (Fama-Bliss 1–5y zeros, monthly from 1952), `tfz_dly_ft` (fixed-term 1–30y yields), `tfz_idx` (series labels). |
| `raw/fed_gsw/feds200628.csv` | Federal Reserve, Gürkaynak, Sack & Wright (2007) curve, [federalreserve.gov](https://www.federalreserve.gov/data/yield-curve-tables/feds200628.csv) | daily 1961-06-14 → 2026-10-30 | Svensson fit to off-the-run notes and bonds. Zero yields `SVENY01..30` (continuously compounded, %), forwards, par yields, fitted parameters. 9 header lines. It is itself a fitted curve, so it is never used to test pricing. |
| `raw/frb_h15/frb_rates_daily.parquet` | Fed H.15 release, WRDS (`/wrds/frb/sasdata/rates_daily`) | daily 1954 → 2025-02-13 | constant-maturity yields 1m–30y (`dgs*`), 3-month bill rate, fed funds, SOFR, swaps, TIPS |
| `raw/treasury_par/par_yield_YYYY.csv` | US Treasury daily par yield curve, home.treasury.gov | 2024 → 2026-11-09 | fills the period after H.15 on WRDS ends |
| `raw/acm/ACMTermPremium.xls` | NY Fed, Adrian-Crump-Moench term structure model, [newyorkfed.org](https://www.newyorkfed.org/research/data_indicators/term-premia-tabs) | monthly 1961-06 → 2026-10 (also daily) | fitted yields `ACMY01..10`, term premia `ACMTP01..10`, risk-neutral yields `ACMRNY01..10`; used only to validate our term premium |
| `raw/events/event_calendar.csv` | FOMC statements (federalreserve.gov calendars); CPI and Employment Situation release dates (bls.gov archives) | 2018-01 → 2026-11 | 77 FOMC (7 unscheduled or non-policy, flagged), 106 CPI, 106 jobs reports; release times ET |
| `raw/events/day_list.csv` | built from the calendar and TAQ's trading days | 2018-01-04 → 2026-11-06 | 281 event days + 281 control days (nearest earlier trading day with no event). The 15 Mar 2020 Sunday cut is mapped to Monday 16 Mar. No TAQ on Good Fridays (2020-04-10, 2021-04-02, 2023-04-07, 2026-04-03) or 2026-11-10. |
| `raw/taq_etf/{tr,nb}_YYYYMMDD.parquet` | NYSE millisecond TAQ, WRDS | the 562 days above (1.8 GB) | 32 Treasury ETFs: bills SGOV BIL SHV TBIL XBIL XHLF; single-maturity XONE UTWO UTRE UFIV USVN UTEN UTWY UTHY; broad SHY IEI IEF TLH TLT GOVT VGSH VGIT VGLT SCHO SCHR SCHQ SPTS SPTI SPTL; zero-coupon EDV ZROZ GOVZ. `tr_`: every regular trade 04:00–20:00 ET (`time_m` in seconds after midnight, price, size, exchange, condition). `nb_`: best bid/ask, last quote of each second. Many ETFs launched after 2018. |

Constant-maturity yields used in the charts combine H.15 (to Feb 2025) and the Treasury's files (after), in `code/yc/data.py::load_cmt`. Before Sep 1981 the 3-month rate is the T-bill discount rate, a few bp below the bond-equivalent yield.

## Bond cross-sections (CRSP)

`code/yc/data.py::cross_section(date)` keeps plain nominal Treasuries: types bond, note and bill (`ITYPE` 1, 2, 4), fully taxable, no flower bonds, `IUNIQ = 0`, positive bid and ask, already issued on the date (`TDATDT <= date`). Bills need at least 30 days to maturity, and notes and bonds at least 1 year: shorter coupon issues are old, illiquid and stale-priced. Price = CRSP mid (`TDNOMPRC`) + accrued interest. Cash flows: semi-annual coupons counted back from maturity. Yields are continuously compounded yields to maturity solved from those cash flows; settlement lag is ignored.

## Monthly zero panel

`code/yc/timeseries.py::monthly_zero_panel` takes month-end values, Jan 1990 – Oct 2026 (442 months): 3m and 6m from the bill constant-maturity yields (bond-equivalent, converted to continuous compounding; bills are zero-coupon), 1, 2, 3, 5, 7, 10, 15, 20, 30y from the Fed's GSW curve.

## What is (and isn't) in this repository

CRSP and TAQ are licensed through WRDS and cannot be redistributed, so `raw/crsp_treasuries/`, `raw/taq_etf/`, `raw/frb_h15/` (pulled from WRDS) and the processed `.pkl` files built from them are **not included**. Everything public is: the Fed GSW curve, the Treasury par yields, the NY Fed ACM file and the event calendars. `processed/numbers.json` holds every number quoted in the README.

## Rebuilding the raw data

With WRDS access, set `YC_SCRATCH` to a working folder on WRDS (e.g. your scratch directory) and run the scripts in `code/wrds/` there:

- `crsp_export.py`: CRSP Treasuries and the Fed H.15 rates → parquet (`qsub -cwd -b y python3 crsp_export.py`)
- `taq_day.sas`, `taq_array.sh`, `taq_to_parquet.py`: TAQ ETF extraction, one array task per day (`qsub -cwd -t 1-562 -tc 8 taq_array.sh`, after copying `raw/events/day_list.csv` there as `day_list.txt`, one `YYYYMMDD` per line)

Then copy the outputs into `data/raw/crsp_treasuries/`, `data/raw/frb_h15/` and `data/raw/taq_etf/`, and run the scripts listed in the main README.
