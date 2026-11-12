"""Convert one day's TAQ CSV extracts to parquet (zstd) and delete the CSVs. Usage: python3 taq_to_parquet.py YYYYMMDD"""
import sys, os, pandas as pd
d = sys.argv[1]; base = os.path.join(os.environ["YC_SCRATCH"], "taq")
for k in ("tr", "nb"):
    f = f"{base}/{k}_{d}.csv"
    df = pd.read_csv(f, dtype={"SYM_ROOT": "category", "EX": "category", "TR_SCOND": "string"})
    df.columns = [c.lower() for c in df.columns]
    t = "time_m" if "time_m" in df else "sec"
    df[t] = pd.to_timedelta(df[t]).dt.total_seconds() if df[t].dtype == object else df[t]
    df.insert(0, "date", pd.Timestamp(d))
    df.to_parquet(f"{base}/{k}_{d}.parquet", index=False, compression="zstd")
    os.remove(f)
print(d, "ok")
