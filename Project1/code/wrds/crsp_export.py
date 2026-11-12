"""Export CRSP Treasury (quarterly-update library, data to 2026-06-30) and Fed H.15 daily rates to parquet.
Run on WRDS: qsub -sync y -cwd -b y python3 crsp_export.py"""
import pandas as pd, os
OUT = os.path.join(os.environ["YC_SCRATCH"], "crsp")
os.makedirs(OUT, exist_ok=True)
SRC = "/wrds/crsp/sasdata/q_treasuries"
small = ["tfz_iss", "tfz_mast", "tfz_idx", "tfz_pay", "tfz_mth_fb", "tfz_mth_rf", "tfz_mth_ft",
         "tfz_dly_ft", "tfz_dly_rf2", "tfz_dly_ts2", "tfz_mth_bp"]
for f in small:
    p = f"{SRC}/{f}.sas7bdat"
    if not os.path.exists(p):
        print("missing", f); continue
    df = pd.read_sas(p, encoding="latin1")
    df.to_parquet(f"{OUT}/{f}.parquet", index=False)
    print(f, df.shape, flush=True)
# big daily bond-level quotes: stream in chunks, keep all rows
parts = []
for i, ch in enumerate(pd.read_sas(f"{SRC}/tfz_dly.sas7bdat", encoding="latin1", chunksize=2_000_000)):
    parts.append(ch)
    print("tfz_dly chunk", i, flush=True)
dly = pd.concat(parts, ignore_index=True)
dly.to_parquet(f"{OUT}/tfz_dly.parquet", index=False, compression="zstd")
print("tfz_dly", dly.shape, dly.columns.tolist(), dly["CALDT"].min(), dly["CALDT"].max())
r = pd.read_sas("/wrds/frb/sasdata/rates_daily.sas7bdat", encoding="latin1")
r.to_parquet(f"{OUT}/frb_rates_daily.parquet", index=False)
print("frb", r.shape)
