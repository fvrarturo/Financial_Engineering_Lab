"""CE convention: excess over the cash strategy's CE in the same cell (gf.metrics.add_ce_xcash)."""
import numpy as np
import pandas as pd

from gf.metrics import add_ce_xcash, ce_xcash_se


def test_add_ce_xcash_pairs_by_cell():
    df = pd.DataFrame({"asset": "a", "state": ["s1", "s1", "s2", "s2"], "T_days": 252, "scenario": "base", "seed": 1,
                       "strategy": ["cash", "cppi", "cash", "cppi"], "ce_ann": [0.03, 0.05, 0.02, 0.06]})
    out = add_ce_xcash(df)
    got = out.set_index(["state", "strategy"]).ce_xcash
    assert np.isclose(got[("s1", "cppi")], 0.02) and np.isclose(got[("s2", "cppi")], 0.04)
    assert np.isclose(got[("s1", "cash")], 0.0)


def test_add_ce_xcash_external_ref_and_missing_cash():
    ref = pd.DataFrame({"asset": "a", "state": "s1", "T_days": 252, "scenario": "base", "seed": 1,
                        "strategy": ["cash"], "ce_ann": [0.03]})
    df = pd.DataFrame({"asset": "a", "state": ["s1", "s9"], "T_days": 252, "scenario": "base", "seed": 1,
                       "strategy": "dp_policy", "ce_ann": [0.05, 0.05]})
    out = add_ce_xcash(df, ref)
    assert np.isclose(out.ce_xcash.iloc[0], 0.02) and np.isnan(out.ce_xcash.iloc[1])
    assert len(out) == len(df)


def test_ce_xcash_se_scales_with_horizon():
    assert np.isclose(ce_xcash_se(1e8, 1e5, 0.0, 504), 0.5 * ce_xcash_se(1e8, 1e5, 0.0, 252))
