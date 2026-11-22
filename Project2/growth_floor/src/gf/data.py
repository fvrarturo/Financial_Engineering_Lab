"""Constructed series: the standard *asset frame* consumed by every later stage.

Asset frame columns (DatetimeIndex ``date``, decimal units):

=======  ==============================================================
r        simple total return of the asset on day t
rf       simple risk-free return for day t (daily file; what cash earned)
x        log excess return  log(1+r) - log(1+rf)
rf_ann   annualised short rate from the *monthly* file, (1+rf_m)^12 - 1,
         forward-filled onto the daily dates of that month
=======  ==============================================================

Nothing here assumes the asset is the US market.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from .io_french import read_french

MONTHS_PER_YEAR = 12  # definitional (compounding a monthly rate to a year), not a tunable


# --------------------------------------------------------------------------- config / paths
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else repo_root() / "config.yaml"
    with open(path) as fh:
        cfg = yaml.safe_load(fh)
    cfg["_root"] = str(path.resolve().parent)
    return cfg


def resolve(cfg: dict, rel: str) -> Path:
    return Path(cfg["_root"]) / rel


def source_path(cfg: dict, source: str) -> Path:
    return resolve(cfg, cfg["data"]["raw_dir"]) / cfg["data"]["sources"][source]["file"]


# --------------------------------------------------------------------------- asset frame
def build_asset_frame(returns: pd.Series, rf_daily: pd.Series, rf_monthly: pd.Series) -> pd.DataFrame:
    """Build the asset frame from a daily total-return series and risk-free series.

    Parameters
    ----------
    returns : daily simple total returns (decimal), DatetimeIndex.
    rf_daily : daily simple risk-free returns (decimal), DatetimeIndex.
    rf_monthly : monthly simple risk-free returns (decimal), indexed by any date within the month
        (French monthly files are parsed to month-end).

    The two daily series are inner-joined on dates; rows with NaN in ``r`` or ``rf`` are dropped.
    What was dropped, and why, is recorded in ``frame.attrs["alignment"]``.
    """
    for name, s in (("returns", returns), ("rf_daily", rf_daily), ("rf_monthly", rf_monthly)):
        if not isinstance(s.index, pd.DatetimeIndex):
            raise TypeError(f"{name} must have a DatetimeIndex")

    only_asset = returns.index.difference(rf_daily.index)
    only_rf = rf_daily.index.difference(returns.index)
    df = pd.concat([returns.rename("r"), rf_daily.rename("rf")], axis=1, join="inner").sort_index()

    nan_mask = df[["r", "rf"]].isna().any(axis=1)
    nan_dates = df.index[nan_mask]
    df = df.loc[~nan_mask].copy()

    if (df["r"] <= -1).any() or (df["rf"] <= -1).any():
        bad = df.index[(df["r"] <= -1) | (df["rf"] <= -1)]
        raise ValueError(f"returns <= -100% on {list(bad[:5].strftime('%Y-%m-%d'))}: check units")

    df["x"] = np.log1p(df["r"]) - np.log1p(df["rf"])

    m = rf_monthly.dropna().copy()
    m.index = m.index.to_period("M")
    m = m[~m.index.duplicated(keep="last")].sort_index()
    rf_ann_m = (1.0 + m) ** MONTHS_PER_YEAR - 1.0
    months = df.index.to_period("M")
    direct = pd.Series(rf_ann_m.reindex(months).to_numpy(), index=df.index)
    n_missing_month = int(direct.isna().sum())
    df["rf_ann"] = direct.ffill()
    n_leading_nan = int(df["rf_ann"].isna().sum())

    df.index.name = "date"
    df.attrs["units"] = "decimal"
    df.attrs["alignment"] = {
        "n_input_returns": int(len(returns)),
        "n_input_rf_daily": int(len(rf_daily)),
        "n_rows": int(len(df)),
        "dropped_asset_dates_not_in_rf": [d.strftime("%Y-%m-%d") for d in only_asset],
        "dropped_rf_dates_not_in_asset": [d.strftime("%Y-%m-%d") for d in only_rf],
        "dropped_nan_dates": [d.strftime("%Y-%m-%d") for d in nan_dates],
        "rf_ann_days_forward_filled_from_earlier_month": n_missing_month - n_leading_nan,
        "rf_ann_days_without_monthly_rate": n_leading_nan,
    }
    return df


def load_asset(cfg: dict, name: str) -> tuple[pd.DataFrame, dict]:
    """Load raw files for asset ``name`` per config and build its asset frame.

    Returns (frame, info) where info carries the parse metadata of every file touched.
    """
    spec = cfg["data"]["assets"][name]
    df_src, meta_src = read_french(source_path(cfg, spec["source"]), section=spec.get("section"))
    rf_d_cfg, rf_m_cfg = cfg["data"]["rf"]["daily"], cfg["data"]["rf"]["monthly"]
    df_rfd, meta_rfd = read_french(source_path(cfg, rf_d_cfg["source"]), section=rf_d_cfg.get("section"))
    df_rfm, meta_rfm = read_french(source_path(cfg, rf_m_cfg["source"]), section=rf_m_cfg.get("section"))

    returns = df_src[spec["column"]]
    if spec.get("add_rf", False):
        # Source column is an excess return; add the RF column of the *same* file.
        if rf_d_cfg["column"] not in df_src:
            raise KeyError(f"{name}: add_rf=true but source has no {rf_d_cfg['column']} column")
        returns = returns + df_src[rf_d_cfg["column"]]
    frame = build_asset_frame(returns.rename(name), df_rfd[rf_d_cfg["column"]], df_rfm[rf_m_cfg["column"]])
    frame.attrs["asset"] = name
    info = {"asset": name, "spec": {k: v for k, v in spec.items()}, "parse": {
        "source": meta_src, "rf_daily": meta_rfd, "rf_monthly": meta_rfm}}
    return frame, info


def save_asset_frame(cfg: dict, name: str, frame: pd.DataFrame) -> Path:
    out = resolve(cfg, cfg["data"]["processed_dir"]) / f"{name}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(out)
    return out


def read_asset_frame(cfg: dict, name: str, apply_window: bool = True) -> pd.DataFrame:
    """Read a processed asset frame; optionally cut to the configured estimation window."""
    frame = pd.read_parquet(resolve(cfg, cfg["data"]["processed_dir"]) / f"{name}.parquet")
    if apply_window:
        est = cfg.get("estimation", {})
        frame = frame.loc[est.get("start_date") : est.get("end_date")]
    return frame
