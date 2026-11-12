"""Data loaders for the yield-curve project. All yields returned in percent unless stated."""
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"

# NBER business-cycle recessions (peak month -> trough month). None dated after 2020.
NBER_RECESSIONS = [
    ("1960-04-01", "1961-02-28"), ("1969-12-01", "1970-11-30"), ("1973-11-01", "1975-03-31"),
    ("1980-01-01", "1980-07-31"), ("1981-07-01", "1982-11-30"), ("1990-07-01", "1991-03-31"),
    ("2001-03-01", "2001-11-30"), ("2007-12-01", "2009-06-30"), ("2020-02-01", "2020-04-30"),
]


def load_gsw() -> pd.DataFrame:
    """Fed (Gurkaynak-Sack-Wright) daily zero-coupon curve. Columns: zero yields y1..y30 (continuously
    compounded, %), plus the Svensson parameters."""
    g = pd.read_csv(RAW / "fed_gsw" / "feds200628.csv", skiprows=9, parse_dates=["Date"], index_col="Date")
    g = g.apply(pd.to_numeric, errors="coerce")
    out = g[[f"SVENY{m:02d}" for m in range(1, 31)]].copy()
    out.columns = [f"y{m}" for m in range(1, 31)]
    for c in ["BETA0", "BETA1", "BETA2", "BETA3", "TAU1", "TAU2"]:
        out[c] = g[c]
    return out


def load_cmt() -> pd.DataFrame:
    """Daily constant-maturity Treasury yields (bond-equivalent, %): Fed H.15 to Feb 2025, then the
    Treasury's own par-yield files. 3m uses the 3m CMT where available, else the 3m bill rate (dtb3)."""
    h = pd.read_parquet(RAW / "frb_h15" / "frb_rates_daily.parquet").set_index("date").sort_index()
    m = {"dgs1mo": "1m", "dgs3mo": "3m", "dgs6mo": "6m", "dgs1": "1y", "dgs2": "2y", "dgs3": "3y",
         "dgs5": "5y", "dgs7": "7y", "dgs10": "10y", "dgs20": "20y", "dgs30": "30y"}
    h15 = h[list(m)].rename(columns=m)
    h15["3m"] = h15["3m"].fillna(h["dtb3"])          # pre-1981: 3m T-bill (discount basis)
    h15["ff"] = h["dff"]
    tp = pd.concat([pd.read_csv(f, parse_dates=["Date"]) for f in sorted((RAW / "treasury_par").glob("*.csv"))])
    tm = {"1 Mo": "1m", "3 Mo": "3m", "6 Mo": "6m", "1 Yr": "1y", "2 Yr": "2y", "3 Yr": "3y", "5 Yr": "5y",
          "7 Yr": "7y", "10 Yr": "10y", "20 Yr": "20y", "30 Yr": "30y"}
    tp = tp.set_index("Date").sort_index()[list(tm)].rename(columns=tm)
    tp = tp[tp.index > h15.dropna(subset=["10y"]).index.max()]
    out = pd.concat([h15, tp]).sort_index()
    out.index.name = "date"
    return out.dropna(how="all")


# ---------------------------------------------------------------- CRSP bond cross-sections
_DLY = None
_ISS = None


def _crsp():
    global _DLY, _ISS
    if _DLY is None:
        _DLY = pd.read_parquet(RAW / "crsp_treasuries" / "tfz_dly.parquet",
                               columns=["KYTREASNO", "CALDT", "TDBID", "TDASK", "TDNOMPRC", "TDACCINT", "TDYLD",
                                        "TDDURATN", "TDTOTOUT"])
        _ISS = pd.read_parquet(RAW / "crsp_treasuries" / "tfz_iss.parquet")
    return _DLY, _ISS


ITYPE = {1: "bond", 2: "note", 4: "bill"}


def cross_section(date, min_days_bill: int = 30, min_years_coupon: float = 1.0) -> dict:
    """Plain nominal Treasuries quoted on `date` (CRSP): non-callable bonds, notes and bills, fully taxable, no
    flower bonds, already issued (drops when-issued). Bills kept from `min_days_bill`; notes/bonds only with at least
    `min_years_coupon` left, because short coupon issues are old, illiquid and stale-priced (bills cover the short end;
    the Fed's GSW curve uses a similar rule). Returns dict with a bond table and padded cash-flow matrices
    T (years from `date`) and CF (per 100 face). Dirty price = CRSP mid (TDNOMPRC) + accrued interest."""
    dly, iss = _crsp()
    date = pd.Timestamp(date)
    q = dly[dly.CALDT == date].merge(iss, on="KYTREASNO", how="left")
    q = q[q.ITYPE.isin(ITYPE) & (q.IFLWR == 1) & (q.ITAX == 1) & (q.IUNIQ == 0)
          & (q.TDBID > 0) & (q.TDASK > 0)]
    q = q[q.TDATDT <= date].copy()
    q["type"] = q.ITYPE.map(ITYPE)
    q["tau"] = (q.TMATDT - date).dt.days / 365.25
    q = q[((q.type == "bill") & (q.tau >= min_days_bill / 365.25)) | ((q.type != "bill") & (q.tau >= min_years_coupon))].copy()
    q["coupon"] = q.TCOUPRT.fillna(0.0)
    q["clean"] = q.TDNOMPRC
    q["dirty"] = q.TDNOMPRC + q.TDACCINT.fillna(0.0)
    q["half_spread"] = (q.TDASK - q.TDBID) / 2
    q["issued"] = q.TDATDT
    q = q.sort_values("tau").reset_index(drop=True)
    flows = []
    for _, b in q.iterrows():
        if b.type == "bill" or b.coupon == 0:
            flows.append(([b.tau], [100.0]))
            continue
        n = int(b.TNIPPY) if b.TNIPPY and b.TNIPPY > 0 else 2
        step = 12 // n
        ts, k = [], 0
        while True:
            d = b.TMATDT - pd.DateOffset(months=step * k)
            if d <= date:
                break
            ts.append((d - date).days / 365.25)
            k += 1
        ts = ts[::-1]
        cf = [b.coupon / n] * len(ts)
        cf[-1] += 100.0
        flows.append((ts, cf))
    K = max(len(t) for t, _ in flows)
    T = np.zeros((len(q), K))
    CF = np.zeros((len(q), K))
    for i, (t, c) in enumerate(flows):
        T[i, :len(t)] = t
        CF[i, :len(c)] = c
    q["ytm"] = ytm_cont(q.dirty.values, T, CF) * 100
    q["dur"] = macaulay(q.ytm.values / 100, T, CF)
    return {"date": date, "bonds": q[["KYTREASNO", "TCUSIP", "type", "issued", "TMATDT", "tau", "coupon", "clean",
                                      "dirty", "half_spread", "ytm", "dur", "TDTOTOUT"]], "T": T, "CF": CF}


def ytm_cont(P, T, CF, y0=0.04, it=50):
    """Continuously compounded yield to maturity for each bond (vectorised Newton)."""
    y = np.full(len(P), y0, dtype=float)
    for _ in range(it):
        d = CF * np.exp(-y[:, None] * T)
        f = d.sum(1) - P
        fp = -(T * d).sum(1)
        y = y - f / fp
    return y


def macaulay(y, T, CF):
    d = CF * np.exp(-y[:, None] * T)
    return (T * d).sum(1) / d.sum(1)
