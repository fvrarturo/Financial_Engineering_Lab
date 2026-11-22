"""Data checks for asset frames (Spec 01). Every check returns a dict with a ``status`` key
(PASS / WARN / FAIL), a ``notes`` list and check-specific details. Thresholds come from
``config.yaml`` (``checks:``); nothing here is specific to the US market.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


class UnitsError(ValueError):
    """Daily returns look like percent rather than decimals (or are otherwise mis-scaled)."""


def _worst(*statuses: str) -> str:
    order = {PASS: 0, WARN: 1, FAIL: 2}
    return max(statuses, key=order.__getitem__) if statuses else PASS


def _d(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- 1. coverage
def check_coverage(frame: pd.DataFrame, cfg: dict) -> dict:
    c = cfg["checks"]["coverage"]
    idx = frame.index
    years = pd.Series(idx.year, index=idx)
    per_year = years.groupby(years).size()
    sat = pd.Series(idx.dayofweek == 5, index=idx).groupby(years).sum()
    first_y, last_y = int(per_year.index[0]), int(per_year.index[-1])

    flagged = []
    for y, n in per_year.items():
        if c["min_days_per_year"] <= n <= c["max_days_per_year"]:
            continue
        explained = {int(k): v for k, v in (c.get("explained_years") or {}).items()}
        if y in (first_y, last_y):
            reason = "partial year (sample start/end)"
            unexplained = False
        elif n > c["max_days_per_year"] and sat.get(y, 0) > 0 and y <= c["saturday_trading_last_year"]:
            reason = f"Saturday trading era ({int(sat[y])} Saturdays)"
            unexplained = False
        elif y in explained:
            reason = f"explained in config: {explained[y]}"
            unexplained = False
        else:
            reason = "unexplained by partial year or Saturday trading"
            unexplained = True
        flagged.append({"year": int(y), "days": int(n), "saturdays": int(sat.get(y, 0)),
                        "reason": reason, "unexplained": unexplained})
    status = WARN if any(f["unexplained"] for f in flagged) else PASS
    notes = []
    if status == WARN:
        notes.append("Years outside the day-count band not explained by partial-year or Saturday "
                     "trading; see `flagged_years` (inspect for exchange closures).")
    return {
        "status": status,
        "first_date": _d(idx[0]), "last_date": _d(idx[-1]), "n_days": int(len(idx)),
        "days_per_year": {int(k): int(v) for k, v in per_year.items()},
        "saturdays_per_year": {int(k): int(v) for k, v in sat.items() if v > 0},
        "flagged_years": flagged, "notes": notes,
    }


# --------------------------------------------------------------------------- 2. dates
def check_dates(frame: pd.DataFrame, cfg: dict) -> dict:
    c, cov = cfg["checks"]["dates"], cfg["checks"]["coverage"]
    idx = frame.index
    increasing = bool(idx.is_monotonic_increasing)
    unique = bool(idx.is_unique)
    late = idx[(idx.year > cov["saturday_trading_last_year"]) & (idx.dayofweek >= 5)]
    gaps_days = pd.Series(idx[1:] - idx[:-1], index=idx[1:]).dt.days
    big = gaps_days[gaps_days > c["gap_report_days"]]
    gaps = [{"from": _d(idx[idx.get_loc(d) - 1]), "to": _d(d), "calendar_days": int(g)}
            for d, g in big.items()]
    statuses, notes = [], []
    if not (increasing and unique):
        statuses.append(FAIL)
        notes.append("Dates are not strictly increasing and unique.")
    if len(late):
        statuses.append(WARN)
        notes.append(f"{len(late)} weekend dates after {cov['saturday_trading_last_year']}.")
    if (gaps_days > c["gap_warn_days"]).any():
        statuses.append(WARN)
        notes.append(f"Gap longer than {c['gap_warn_days']} calendar days.")
    return {
        "status": _worst(*statuses), "strictly_increasing": increasing, "unique": unique,
        "weekend_dates_after_saturday_era": [_d(d) for d in late],
        "gaps": gaps, "notes": notes,
    }


# --------------------------------------------------------------------------- 3. missing values
def check_missing(frame: pd.DataFrame, info: dict, cfg: dict) -> dict:
    c = cfg["checks"]["missing"]
    col = info["spec"]["column"]
    src, rfd, rfm = info["parse"]["source"], info["parse"]["rf_daily"], info["parse"]["rf_monthly"]
    al = frame.attrs["alignment"]
    rf_col = cfg["data"]["rf"]["daily"]["column"]
    sentinels = {
        "source": src["sentinel_counts"], "rf_daily": rfd["sentinel_counts"],
        "rf_monthly": rfm["sentinel_counts"],
    }
    used = {
        f"source:{col}": src["sentinel_counts"].get(col, 0) + src["blank_field_counts"].get(col, 0),
        f"rf_daily:{rf_col}": rfd["sentinel_counts"].get(rf_col, 0) + rfd["blank_field_counts"].get(rf_col, 0),
    }
    n_drop = (len(al["dropped_asset_dates_not_in_rf"]) + len(al["dropped_nan_dates"]))
    nan_frame = {k: int(v) for k, v in frame.isna().sum().items()}
    share = n_drop / max(al["n_input_returns"], 1)
    statuses, notes = [PASS], []
    if any(used.values()) or n_drop or al["dropped_rf_dates_not_in_asset"] or any(nan_frame.values()):
        statuses.append(WARN)
        notes.append("Missing values, NaNs or alignment drops present; see details.")
    if share > c["max_nan_share_fail"]:
        statuses.append(FAIL)
        notes.append(f"{share:.2%} of asset rows dropped (> {c['max_nan_share_fail']:.2%}).")
    return {
        "status": _worst(*statuses),
        "sentinel_counts": sentinels,
        "nan_counts_parsed": {"source": src["nan_counts"], "rf_daily": rfd["nan_counts"],
                              "rf_monthly": rfm["nan_counts"]},
        "missing_in_used_columns": used,
        "nan_counts_asset_frame": nan_frame,
        "rows_dropped": {
            "asset_dates_not_in_rf (inner join)": al["dropped_asset_dates_not_in_rf"],
            "rf_dates_not_in_asset (inner join)": al["dropped_rf_dates_not_in_asset"],
            "nan_in_r_or_rf": al["dropped_nan_dates"],
        },
        "rf_ann_days_forward_filled_from_earlier_month": al["rf_ann_days_forward_filled_from_earlier_month"],
        "rf_ann_days_without_monthly_rate": al["rf_ann_days_without_monthly_rate"],
        "notes": notes,
    }


# --------------------------------------------------------------------------- 4. units
def check_units(r: pd.Series, cfg: dict) -> dict:
    """Raise ``UnitsError`` if daily returns look mis-scaled (most likely still in percent)."""
    c = cfg["checks"]["units"]
    r = r.dropna()
    med, sd = float(r.median()), float(r.std())
    problems = []
    if not abs(med) < c["max_abs_median"]:
        problems.append(f"|median| = {abs(med):.4g} >= {c['max_abs_median']}")
    if not (c["min_std"] <= sd <= c["max_std"]):
        problems.append(f"std = {sd:.4g} outside [{c['min_std']}, {c['max_std']}]")
    if problems:
        raise UnitsError(
            "UNITS CHECK FAILED for daily returns: " + "; ".join(problems)
            + ". The series was probably not converted from percent to decimals (divide by 100)."
        )
    return {"status": PASS, "median": med, "std": sd, "notes": []}


# --------------------------------------------------------------------------- 5. extremes
def check_extremes(frame: pd.DataFrame, cfg: dict) -> dict:
    c = cfg["checks"]["extremes"]
    r = frame["r"]
    top = r.reindex(r.abs().sort_values(ascending=False).index[: c["top_n"]])
    table = [{"rank": i + 1, "date": _d(d), "r": float(v), "x": float(frame.at[d, "x"])}
             for i, (d, v) in enumerate(top.items())]
    top_dates = {t["date"] for t in table}
    first, last = frame.index[0], frame.index[-1]
    missing_dates = [d for d in c["expected_dates"]
                     if first <= pd.Timestamp(d) <= last and d not in top_dates]
    missing_months = [m for m in c["expected_months"]
                      if first <= pd.Period(m, "M").end_time and pd.Period(m, "M").start_time <= last
                      and not any(t.startswith(m) for t in top_dates)]
    prev_gap = pd.Series(r.index, index=r.index).diff().dt.days
    suspicious = [{"date": _d(d), "r": float(v), "calendar_days_since_previous_obs": int(prev_gap[d]) if pd.notna(prev_gap[d]) else None}
                  for d, v in r[r.abs() > c["suspicious_abs_return"]].items()]
    notes = []
    if missing_dates:
        notes.append(f"Expected extreme dates not in top {c['top_n']}: {missing_dates}")
    if missing_months:
        notes.append(f"Expected crash months not in top {c['top_n']}: {missing_months}")
    if suspicious:
        notes.append(f"{len(suspicious)} days with |r| > {c['suspicious_abs_return']:.0%}")
    expected_present = {d: (d in top_dates) for d in c["expected_dates"]}
    expected_values = {d: float(r.get(pd.Timestamp(d), np.nan)) for d in c["expected_dates"]}
    return {
        "status": WARN if notes else PASS, "top": table,
        "expected_dates_in_top": expected_present, "expected_date_returns": expected_values,
        "expected_months_missing": missing_months, "suspicious": suspicious, "notes": notes,
    }


# --------------------------------------------------------------------------- 6. daily vs monthly
def check_daily_vs_monthly(daily_total: pd.Series, monthly_total: pd.Series, cfg: dict) -> dict:
    """Compound daily simple returns to calendar months and compare with a monthly series."""
    c = cfg["checks"]["monthly_crosscheck"]
    d = daily_total.dropna()
    comp = np.expm1(np.log1p(d).groupby(d.index.to_period("M")).sum())
    m = monthly_total.dropna().copy()
    m.index = m.index.to_period("M")
    both = pd.concat([comp.rename("daily_compounded"), m.rename("monthly")], axis=1, join="inner")
    diff = both["daily_compounded"] - both["monthly"]
    ad = diff.abs()
    flagged = both[ad > c["flag_abs_diff"]].assign(diff=diff[ad > c["flag_abs_diff"]])
    share = len(flagged) / max(len(both), 1)
    status = FAIL if share > c["max_flag_share_fail"] else (WARN if len(flagged) else PASS)
    q = ad.quantile([0.5, 0.9, 0.95, 0.99]).to_dict()
    return {
        "status": status, "n_months": int(len(both)),
        "first_month": str(both.index[0]), "last_month": str(both.index[-1]),
        "abs_diff": {"mean": float(ad.mean()), "median": float(q[0.5]), "p90": float(q[0.9]),
                     "p95": float(q[0.95]), "p99": float(q[0.99]), "max": float(ad.max()),
                     "max_month": str(ad.idxmax())},
        "mean_signed_diff": float(diff.mean()),
        "n_flagged": int(len(flagged)), "flag_threshold": c["flag_abs_diff"],
        "flagged": [{"month": str(k), "daily_compounded": float(v.daily_compounded),
                     "monthly": float(v.monthly), "diff": float(v["diff"])} for k, v in flagged.iterrows()],
        "notes": [f"{len(flagged)} months with |diff| > {c['flag_abs_diff']:.2%}"] if len(flagged) else [],
        "_series": diff,  # dropped before JSON
    }


# --------------------------------------------------------------------------- 7. RF
def check_rf(rf_daily: pd.Series, rf_monthly: pd.Series, cfg: dict) -> dict:
    c = cfg["checks"]["rf"]
    d = rf_daily.dropna()
    per = d.index.to_period("M")
    g = d.groupby(per)
    n_unique = g.nunique()
    comp = np.expm1(np.log1p(d).groupby(per).sum())
    m = rf_monthly.dropna().copy()
    m.index = m.index.to_period("M")
    both = pd.concat([comp.rename("compounded_daily"), m.rename("monthly")], axis=1, join="inner")
    diff = both["compounded_daily"] - both["monthly"]
    ad = diff.abs()
    resolution = float(np.round(np.diff(np.unique(np.round(d.to_numpy(), 10))).min(), 10)) if d.nunique() > 1 else 0.0
    # Daily RF is rounded to `resolution`; compounding n rounded days can be off by up to n*resolution/2.
    bound = g.size().reindex(both.index) * resolution / 2 + c["tolerance_beyond_rounding"]
    flagged = ad[ad > bound]
    all_zero_pos_m = both[(g.max().reindex(both.index) == 0) & (both["monthly"] > 0)]
    decade = (d.index.year // 10) * 10
    by_dec = pd.DataFrame({
        "days": d.groupby(decade).size(),
        "zero_days": (d == 0).groupby(decade).sum(),
        "negative_days": (d < 0).groupby(decade).sum(),
    })
    by_dec["zero_share"] = by_dec["zero_days"] / by_dec["days"]
    notes = [
        f"Daily RF constant within the month in {float((n_unique == 1).mean()):.1%} of months.",
        f"Smallest step between distinct daily RF values: {resolution:.2e} "
        f"(≈ {resolution * cfg['trading_days_per_year']:.2%} p.a.).",
    ]
    if len(all_zero_pos_m):
        notes.append(f"{len(all_zero_pos_m)} months where every daily RF is 0 but the monthly RF is > 0 "
                     f"(daily RF rounded to {resolution:.0e} = {resolution*100:g}%/day). rf_ann uses the monthly file for this reason.")
    if (m < 0).any():
        notes.append(f"Monthly RF is negative in {int((m < 0).sum())} months "
                     f"({', '.join(str(k) for k in m[m < 0].index[:12])}{' …' if (m < 0).sum() > 12 else ''}); "
                     "rf_ann inherits these. Daily RF has no negative days (rounded/floored).")
    status = WARN if len(flagged) else PASS
    if len(flagged):
        notes.append(f"{len(flagged)} months where |prod(1+rf_d)-1 - rf_m| exceeds the rounding bound "
                     f"(n_days x resolution/2 + {c['tolerance_beyond_rounding'] * 1e4:.1f} bp).")
    else:
        notes.append("All months agree with the monthly RF within the rounding bound "
                     f"(n_days x resolution/2 + {c['tolerance_beyond_rounding'] * 1e4:.1f} bp).")
    return {
        "status": status,
        "share_months_constant": float((n_unique == 1).mean()),
        "n_months": int(len(both)),
        "months_by_n_unique_values": {int(k): int(v) for k, v in n_unique.value_counts().sort_index().items()},
        "daily_resolution": resolution,
        "compounded_vs_monthly_abs_diff": {
            "mean": float(ad.mean()), "median": float(ad.median()), "p95": float(ad.quantile(0.95)),
            "max": float(ad.max()), "max_month": str(ad.idxmax())},
        "mean_signed_diff": float(diff.mean()),
        "n_flagged": int(len(flagged)),
        "n_months_abs_diff_above_1bp": int((ad > 1e-4 + 1e-12).sum()),
        "flagged_months": {str(k): float(diff[k]) for k in flagged.index},
        "months_all_zero_daily_but_positive_monthly": [str(k) for k in all_zero_pos_m.index],
        "by_decade": {int(k): {kk: (float(vv) if kk == "zero_share" else int(vv)) for kk, vv in row.items()}
                      for k, row in by_dec.iterrows()},
        "negative_days_total": int((d < 0).sum()),
        "negative_months_monthly_file": {str(k): float(v) for k, v in m[m < 0].items()},
        "zero_days_total": int((d == 0).sum()),
        "notes": notes,
        "_series": both.assign(diff=diff),
    }


# --------------------------------------------------------------------------- 8. summary stats
def summary_stats(frame: pd.DataFrame, cfg: dict) -> dict:
    """Stats by decade and full sample. Rolling windows are computed on the full series and
    assigned to the decade in which the window *ends*."""
    N = cfg["trading_days_per_year"]
    cs = cfg["checks"]["summary"]
    logr = np.log1p(frame["r"])
    rolls = {w: np.expm1(logr.rolling(w).sum()) for w in cs["window_days"]}
    decade = (frame.index.year // 10) * 10
    groups = [("full", slice(None))] + [(f"{d}s", decade == d) for d in sorted(set(decade))]
    rows = {}
    for label, sel in groups:
        f = frame.loc[sel]
        row = {
            "start": _d(f.index[0]), "end": _d(f.index[-1]), "n_days": int(len(f)),
            "ann_mean_r": float(f["r"].mean() * N), "ann_mean_x": float(f["x"].mean() * N),
            "ann_vol_r": float(f["r"].std() * np.sqrt(N)), "ann_vol_x": float(f["x"].std() * np.sqrt(N)),
            "skew_x": float(f["x"].skew()), "exkurt_x": float(f["x"].kurt()),
            "min_r": float(f["r"].min()), "min_r_date": _d(f["r"].idxmin()),
            "max_r": float(f["r"].max()), "max_r_date": _d(f["r"].idxmax()),
            "mean_rf_ann": float(f["rf_ann"].mean()),
        }
        for w, cum in rolls.items():
            cw = cum.loc[sel].dropna()
            row[f"worst_{w}d"] = float(cw.min()) if len(cw) else np.nan
            row[f"worst_{w}d_end"] = _d(cw.idxmin()) if len(cw) else None
            row[f"share_{w}d_below_{cs['loss_threshold']}"] = float((cw < cs["loss_threshold"]).mean()) if len(cw) else np.nan
            row[f"n_windows_{w}d"] = int(len(cw))
        rows[label] = row
    return {"status": PASS, "loss_threshold": cs["loss_threshold"], "windows": cs["window_days"],
            "table": rows, "notes": ["Overlapping windows; window assigned to decade of its end date."]}
