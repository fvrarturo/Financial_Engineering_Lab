"""Spec 01 tests: French parser, asset frame, units sanity."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from gf.checks import UnitsError, check_units
from gf.data import build_asset_frame, load_config
from gf.io_french import FrenchParseError, list_sections, read_french

FIX = Path(__file__).parent / "fixtures" / "french_two_sections.csv"
CFG = load_config()


# ----------------------------------------------------------------- parser
def test_sections_detected():
    secs = list_sections(FIX)
    assert [s["title"] for s in secs] == [
        "Average Value Weighted Returns -- Daily", "Average Equal Weighted Returns -- Daily"]


def test_default_section_is_first_and_shape():
    df, meta = read_french(FIX)
    assert df.shape == (3, 2)
    assert list(df.columns) == ["AAA", "BBB"]
    assert meta["section_title"].startswith("Average Value Weighted")
    assert meta["stop_reason"] == "blank_line"
    assert df.attrs["units"] == "decimal"
    assert list(df.index) == list(pd.to_datetime(["2020-01-02", "2020-01-03", "2020-01-06"]))


def test_sentinels_to_nan_and_counted():
    df, meta = read_french(FIX)
    assert np.isnan(df.loc["2020-01-03", "AAA"])
    assert np.isnan(df.loc["2020-01-06", "BBB"])
    assert df.isna().sum().sum() == 2
    assert meta["sentinel_counts"] == {"AAA": 1, "BBB": 1}


def test_values_divided_by_100():
    df, _ = read_french(FIX)
    assert df.loc["2020-01-02", "AAA"] == pytest.approx(0.01)
    assert df.loc["2020-01-02", "BBB"] == pytest.approx(-0.02)
    assert df.loc["2020-01-06", "AAA"] == pytest.approx(0.0025)


def test_second_section_by_name_and_footer_stop():
    df, meta = read_french(FIX, section="equal_weighted")
    assert df.shape == (2, 2)
    assert df.loc["2020-01-03", "BBB"] == pytest.approx(0.06)
    assert meta["section_index"] == 1
    df1, _ = read_french(FIX, section=1)
    pd.testing.assert_frame_equal(df, df1)


def test_ambiguous_section_raises():
    with pytest.raises(FrenchParseError):
        read_french(FIX, section="weighted")


def test_monthly_dates_month_end(tmp_path):
    p = tmp_path / "m.csv"
    p.write_text("preamble\n\n,Mkt-RF,RF\n202001,   1.00,  0.10\n202002,  -2.00,  0.12\n\n"
                 " Annual Factors: January-December \n,Mkt-RF,RF\n  2020,  10.0,  1.0\n\nCopyright\n")
    df, meta = read_french(p)
    assert meta["frequency"] == "monthly"
    assert list(df.index) == [pd.Timestamp("2020-01-31"), pd.Timestamp("2020-02-29")]
    ann, meta_a = read_french(p, section="annual")
    assert meta_a["frequency"] == "annual" and ann.iloc[0, 0] == pytest.approx(0.10)


# ----------------------------------------------------------------- asset frame
def _synthetic(seed=0, n=300):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2001-01-01", periods=n)
    r = pd.Series(rng.normal(0.0004, 0.012, n), index=idx)
    rf = pd.Series(0.0001, index=idx)
    months = pd.period_range(idx[0], idx[-1], freq="M").to_timestamp(how="end").normalize()
    rf_m = pd.Series(np.linspace(0.002, 0.004, len(months)), index=months)
    return r, rf, rf_m


def test_x_is_log_excess_return():
    r, rf, rf_m = _synthetic()
    f = build_asset_frame(r, rf, rf_m)
    np.testing.assert_allclose(f["x"], np.log(1 + f["r"]) - np.log(1 + f["rf"]))
    assert list(f.columns) == ["r", "rf", "x", "rf_ann"]


def test_rf_ann_from_monthly_and_ffill():
    r, rf, rf_m = _synthetic()
    f = build_asset_frame(r, rf, rf_m.iloc[:-1])  # last month not published yet -> forward fill
    first_month = f.index[0].to_period("M")
    assert f.loc[f.index.to_period("M") == first_month, "rf_ann"].nunique() == 1
    assert f["rf_ann"].iloc[0] == pytest.approx((1 + rf_m.iloc[0]) ** 12 - 1)
    assert f["rf_ann"].iloc[-1] == pytest.approx((1 + rf_m.iloc[-2]) ** 12 - 1)
    assert f.attrs["alignment"]["rf_ann_days_forward_filled_from_earlier_month"] > 0
    assert f["rf_ann"].notna().all()


def test_non_us_asset_inner_join_reports_drops():
    """A non-US asset with its own calendar: dates missing from rf are dropped and reported."""
    r, rf, rf_m = _synthetic(seed=1)
    r = r.rename("ind_foreign")
    rf_short = rf.drop(rf.index[[5, 6]])
    r_nan = r.copy()
    r_nan.iloc[10] = np.nan
    f = build_asset_frame(r_nan, rf_short, rf_m)
    al = f.attrs["alignment"]
    assert len(al["dropped_asset_dates_not_in_rf"]) == 2
    assert len(al["dropped_nan_dates"]) == 1
    assert len(f) == len(r) - 3
    assert f.notna().all().all()


# ----------------------------------------------------------------- units
def test_units_check_passes_on_decimals():
    r, _, _ = _synthetic()
    assert check_units(r, CFG)["status"] == "PASS"


def test_units_check_raises_on_percent():
    r, _, _ = _synthetic()
    with pytest.raises(UnitsError):
        check_units(r * 100, CFG)
