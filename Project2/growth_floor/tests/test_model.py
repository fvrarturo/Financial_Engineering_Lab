"""Spec 02 tests: simulator, GARCH recursion vs arch, premium, no look-ahead."""
import numpy as np
import pandas as pd
import pytest

from gf import model as M
from gf.model import _arch
from gf.simulate import common_random_numbers, simulate_paths

RATE0 = {"mode": "constant"}
STATE = {"sigma0_ann": 0.15, "r0_ann": 0.03}
GJR = {"kind": "gjr", "mu": 0.0005, "omega": 1.1e-6, "alpha": 0.04, "gamma": 0.11, "beta": 0.89, "nu": 6.0}
PREM = {"mode": "constant_arith", "mu_e_daily": 0.04 / 252}


@pytest.fixture(scope="module")
def synth_x():
    """A GJR-t sample path (decimal daily log excess returns)."""
    sp = simulate_paths(GJR, RATE0, STATE, 6000, 1, seed=7, premium=PREM)
    idx = pd.bdate_range("1990-01-01", periods=6000)
    return pd.Series(sp.x[0], index=idx)


# ----------------------------------------------------------------- reproducibility / CRN
def test_same_seed_identical():
    a = simulate_paths(GJR, {"mode": "vasicek", "kappa": 0.3, "theta": 0.04, "s": 0.02, "rho": -0.1},
                       STATE, 50, 200, seed=11, premium=PREM)
    b = simulate_paths(GJR, {"mode": "vasicek", "kappa": 0.3, "theta": 0.04, "s": 0.02, "rho": -0.1},
                       STATE, 50, 200, seed=11, premium=PREM)
    for k in ("x", "rf", "r", "sigma", "r_ann"):
        np.testing.assert_array_equal(getattr(a, k), getattr(b, k))
    assert a.meta["params_hash"] == b.meta["params_hash"]
    c = simulate_paths(GJR, RATE0, STATE, 50, 200, seed=12, premium=PREM)
    assert not np.array_equal(a.x, c.x)


def test_common_random_numbers_across_states():
    crn = common_random_numbers(300, 40, seed=3, innovations="t", nu=GJR["nu"])
    lo = simulate_paths(GJR, RATE0, {"sigma0_ann": 0.10, "r0_ann": 0.02}, 40, 300, seed=999, premium=PREM, crn=crn)
    hi = simulate_paths(GJR, RATE0, {"sigma0_ann": 0.40, "r0_ann": 0.02}, 40, 300, seed=123, premium=PREM, crn=crn)
    # same shocks: day-1 standardised shocks identical even though states and seeds differ
    z_lo = (lo.x[:, 0] - (PREM["mu_e_daily"] - 0.5 * lo.sigma[:, 0] ** 2)) / lo.sigma[:, 0]
    z_hi = (hi.x[:, 0] - (PREM["mu_e_daily"] - 0.5 * hi.sigma[:, 0] ** 2)) / hi.sigma[:, 0]
    np.testing.assert_allclose(z_lo, z_hi, atol=1e-12)
    np.testing.assert_allclose(z_lo, crn["z"][:, 0], atol=1e-12)


# ----------------------------------------------------------------- iid reduction
def test_reduces_to_iid_normal():
    sig_d = 0.01
    p = {"kind": "garch", "mu": 0.0, "omega": sig_d ** 2, "alpha": 0.0, "gamma": 0.0, "beta": 0.0, "nu": np.inf}
    prem = {"mode": "constant_arith", "mu_e_daily": 0.0003}
    sp = simulate_paths(p, RATE0, {"sigma0_ann": sig_d * np.sqrt(252), "r0_ann": 0.0}, 100, 20000, seed=5,
                        innovations="normal", premium=prem)
    x = sp.x.ravel()
    m_true = 0.0003 - 0.5 * sig_d ** 2
    assert abs(x.mean() - m_true) < 4 * sig_d / np.sqrt(x.size)
    assert abs(x.var() / sig_d ** 2 - 1) < 0.01
    assert np.allclose(sp.sigma, sig_d)
    # no autocorrelation in squares
    assert abs(np.corrcoef(x[:-1] ** 2, x[1:] ** 2)[0, 1]) < 0.01


# ----------------------------------------------------------------- recursion identity with arch
@pytest.mark.parametrize("name", ["gjr_t", "garch_n", "egarch_t"])
def test_filter_matches_arch(synth_x, name):
    fit = M.fit_garch(synth_x, name)
    s2 = M.filter_variance(synth_x.to_numpy(), fit.params, fit.kind, fit.backcast)[:-1]
    spec = M.MODEL_SPECS[name]
    res = _arch(100 * synth_x.to_numpy(), spec["kind"], spec["dist"]).fit(disp="off")
    s2_arch = np.asarray(res.conditional_volatility) ** 2 / 1e4
    np.testing.assert_allclose(s2, s2_arch, rtol=1e-8)


def test_simulator_recursion_matches_filter(synth_x):
    """The simulator's variance update equals the filter's: feed the simulated x back through the filter."""
    sp = simulate_paths(GJR, RATE0, STATE, 300, 3, seed=21, premium=PREM)
    for i in range(3):
        mu_filter = PREM["mu_e_daily"] - 0.5 * sp.sigma[i] ** 2  # time-varying mean used in the sim
        e = sp.x[i] - mu_filter
        s2 = np.empty(301)
        s2[0] = sp.sigma[i, 0] ** 2
        for t in range(300):
            s2[t + 1] = GJR["omega"] + (GJR["alpha"] + GJR["gamma"] * (e[t] < 0)) * e[t] ** 2 + GJR["beta"] * s2[t]
        np.testing.assert_allclose(np.sqrt(s2[:300]), sp.sigma[i], rtol=1e-12)


# ----------------------------------------------------------------- premium
def test_constant_arith_premium_mean():
    mu_e = 0.06
    prem = {"mode": "constant_arith", "mu_e_daily": mu_e / 252}
    sp = simulate_paths(GJR, {"mode": "constant"}, {"sigma0_ann": 0.15, "r0_ann": 0.03}, 252, 20000, seed=8,
                        premium=prem)
    simple_excess = (1 + sp.r) / (1 + sp.rf) - 1
    m, se = simple_excess.mean(), simple_excess.mean(axis=1).std() / np.sqrt(sp.r.shape[0]) / 1.0
    se_daily = se  # path means are independent; se of the grand mean
    assert abs(m - mu_e / 252) < 4 * se_daily, (m * 252, se_daily * 252)


def test_premium_modes_average():
    s2 = np.full(1000, 1e-4)
    prem = {"mode": "prop_variance", "annual_excess_used": 0.05}
    p = M.premium_params(prem, s2)
    assert np.allclose(M.premium_mean(p, s2), 0.05 / 252)
    p = M.premium_params({**prem, "mode": "constant_sharpe"}, s2)
    assert np.allclose(M.premium_mean(p, s2), 0.05 / 252)


# ----------------------------------------------------------------- no look-ahead
def test_expanding_sigma_no_lookahead(synth_x):
    min_obs, step = 2000, 1000
    fits = M.expanding_fits(synth_x, "gjr_t", min_obs, step)
    s2 = M.expanding_sigma2(synth_x.to_numpy(), fits, "gjr")
    t = 3500  # sigma for day t (uses data < t and params fitted on data < 3000)
    x2 = synth_x.copy()
    x2.iloc[t:] = x2.iloc[t:] * 3 + 0.01  # alter everything from day t on
    fits2 = M.expanding_fits(x2, "gjr_t", min_obs, step)
    s2b = M.expanding_sigma2(x2.to_numpy(), fits2, "gjr")
    np.testing.assert_allclose(s2[: t + 1], s2b[: t + 1], rtol=1e-12)
    assert not np.allclose(s2[t + 2:], s2b[t + 2:])
    assert np.all(np.isnan(s2[:min_obs]))


# ----------------------------------------------------------------- misc
def test_param_draws_valid(synth_x):
    fit = M.fit_garch(synth_x, "gjr_t")
    d, info = M.draw_params(fit, 50, seed=1)
    assert d.shape == (50, len(M.PARAM_ORDER))
    for row in d:
        p = dict(zip(M.PARAM_ORDER, row))
        assert M.valid_params(p, "gjr", 2.5)


def test_param_draws_blocks():
    draws = np.array([[0.0, 1e-6, 0.0, 0.0, 0.0, np.inf], [0.0, 4e-6, 0.0, 0.0, 0.0, np.inf]])
    p = {**GJR, "kind": "gjr"}
    sp = simulate_paths(p, RATE0, {"sigma0_ann": 0.2, "r0_ann": 0.0}, 3, 10, seed=1, premium=PREM,
                        param_draws=draws, innovations="normal")
    # after day 1 the variance equals omega of the block (alpha=beta=0)
    np.testing.assert_allclose(sp.sigma[:5, 1] ** 2, 1e-6)
    np.testing.assert_allclose(sp.sigma[5:, 1] ** 2, 4e-6)


def test_vasicek_recovers_params():
    rng = np.random.default_rng(0)
    k, th, s, dt = 0.5, 0.04, 0.01, 1 / 12
    r = [0.02]
    for _ in range(6000):
        r.append(r[-1] + k * (th - r[-1]) * dt + s * np.sqrt(dt) * rng.standard_normal())
    v = M.fit_vasicek(pd.Series(r, index=pd.period_range("1500-01", periods=len(r), freq="M")))
    assert abs(v["kappa"] - k) < 0.15 and abs(v["theta"] - th) < 0.005 and abs(v["s"] - s) < 0.001


def test_gap_scaling():
    idx = pd.bdate_range("2001-09-04", "2001-09-21")
    idx = idx[(idx < "2001-09-11") | (idx > "2001-09-14")]
    x = pd.Series(0.01, index=idx)
    gaps = [{"from": "2001-09-10", "to": "2001-09-17", "calendar_days": 7}]
    fl = M.gap_flags(idx, gaps, 4)
    assert fl.loc["2001-09-17", "busdays"] == 5
    y = M.adjust_for_gaps(x, fl, "scale")
    assert y.loc["2001-09-17"] == pytest.approx(0.01 / np.sqrt(5))
    assert np.isnan(M.adjust_for_gaps(x, fl, "exclude").loc["2001-09-17"])
