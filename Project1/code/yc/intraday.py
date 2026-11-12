"""Intraday Treasury moves from ETF quotes (TAQ NBBO, last quote each second).

Each ETF is mapped to one point on the curve: an effective maturity M and a modified duration D, taken as that of a
par bond of maturity M at the day's constant-maturity yield. A change in the ETF's mid price is turned into a yield
change   dy = -(dP / P) / D.   Four liquid funds trade before the 8:30 releases and cover 2018-2026:
SHY (1-3y), IEI (3-7y), IEF (7-10y), TLT (20y+)."""
from pathlib import Path

import numpy as np
import pandas as pd

from . import data

RAW = Path(__file__).resolve().parents[2] / "data" / "raw"
ETF_MAT = {"SHY": 1.9, "IEI": 4.6, "IEF": 8.3, "TLT": 25.0}          # effective maturity (years)
CORE = list(ETF_MAT)
_CMT = None


def cmt():
    global _CMT
    if _CMT is None:
        _CMT = data.load_cmt()
    return _CMT


def par_duration(M, y):
    """Modified duration of a semi-annual par bond, maturity M years, yield y (decimal)."""
    y = max(y, 1e-4)
    return (1 / y) * (1 - (1 + y / 2) ** (-2 * M))


def durations(date):
    """Modified duration for each core ETF on `date`, from the previous available CMT curve."""
    c = cmt()
    row = c.loc[: pd.Timestamp(date) - pd.Timedelta(days=1)].ffill().iloc[-1]
    mats = np.array([0.25, 0.5, 1, 2, 3, 5, 7, 10, 20, 30])
    ys = row[["3m", "6m", "1y", "2y", "3y", "5y", "7y", "10y", "20y", "30y"]].values.astype(float)
    ok = ~np.isnan(ys)
    return {s: par_duration(M, np.interp(M, mats[ok], ys[ok]) / 100) for s, M in ETF_MAT.items()}


def load_mids(date, syms=CORE):
    """NBBO mid by second for each symbol (last quote of each second). Returns {sym: (sec array, mid array)}."""
    d = pd.Timestamp(date).strftime("%Y%m%d")
    nb = pd.read_parquet(RAW / "taq_etf" / f"nb_{d}.parquet", columns=["sym_root", "sec", "best_bid", "best_ask"])
    nb = nb[nb.sym_root.isin(syms)]
    out = {}
    for s, g in nb.groupby("sym_root", observed=True):
        g = g.sort_values("sec")
        spr = (g.best_ask - g.best_bid) / ((g.best_ask + g.best_bid) / 2)
        g = g[spr < 0.005]                                      # drop quotes wider than 50bp of price
        out[s] = (g.sec.values, ((g.best_bid + g.best_ask) / 2).values)
    return out


def mid_at(mids, sym, t, max_age=600):
    """Last mid at or before second t (None if missing or older than max_age seconds)."""
    if sym not in mids:
        return None
    sec, mid = mids[sym]
    i = np.searchsorted(sec, t, side="right") - 1
    if i < 0 or t - sec[i] > max_age:
        return None
    return mid[i]


def hm(s):
    h, m = map(int, s.split(":"))
    return h * 3600 + m * 60


def window_moves(date, t0, t1, syms=CORE):
    """Yield change (bp) of each ETF's curve point between clock times t0 and t1 ('HH:MM')."""
    mids = load_mids(date, syms)
    D = durations(date)
    out = {}
    for s in syms:
        a, b = mid_at(mids, s, hm(t0)), mid_at(mids, s, hm(t1))
        out[s] = np.nan if a is None or b is None else -(b / a - 1) / D[s] * 1e4
    return out


def grid_moves(date, step=300, start="04:00", end="20:00", syms=CORE):
    """Yield changes (bp) over consecutive `step`-second intervals for each ETF; DataFrame index = interval end."""
    mids = load_mids(date, syms)
    D = durations(date)
    ts = np.arange(hm(start), hm(end) + 1, step)
    out = {}
    for s in syms:
        v = np.array([mid_at(mids, s, t, max_age=step * 4) or np.nan for t in ts], dtype=float)
        out[s] = -(v[1:] / v[:-1] - 1) / D[s] * 1e4
    return pd.DataFrame(out, index=ts[1:])


def path_from(date, start, end, step=60, syms=CORE, max_age=1800):
    """Yield change (bp) of each ETF's curve point relative to `start`, sampled every `step` seconds."""
    mids = load_mids(date, syms)
    D = durations(date)
    ts = np.arange(hm(start), hm(end) + 1, step)
    out = {}
    for s in syms:
        v = np.array([mid_at(mids, s, t, max_age=max_age) or np.nan for t in ts], dtype=float)
        ok = np.flatnonzero(~np.isnan(v))
        out[s] = -(v / v[ok[0]] - 1) / D[s] * 1e4 if len(ok) else v      # relative to the first available quote
    return pd.DataFrame(out, index=ts)
