"""Spec 03 tests: engine, strategies, metrics, no look-ahead, reproducibility."""
import math

import numpy as np
import pytest

from gf.engine import ArrayMarket, SimMarket, run_engine
from gf.metrics import ce, lognormal_breach, row_metrics
from gf.strategies import STRATEGIES, cash_growth_lower, make_strategy, obpi_value, solve_x0
from gf.tune import KnobFunction, pick

W0, K, G = 100e6, 95e6, 2.0
ECFG = {"history_days": 63, "gap_min_sigmas": 1.0, "locked_phi": 0.01,
        "locked_days": 21, "rebalance_every": 1}
MCFG = {"terminal_quantiles": [0.01, 0.05, 0.5], "bootstrap_reps": 0}


def info(sigma0=0.15, mu=0.04, floor="spec"):
    return {"mu_e_ann": mu, "sigma_bar_ann": 0.15, "sigma0_ann": sigma0, "gamma": G,
            "rate_model": {"kappa": 0.3, "theta": 0.04, "s": 0.02}, "floor": {"mode": floor, "alpha": 0.005}}


def gbm_arrays(n, T, sig=0.15, mu_e=0.04, r=0.03, seed=0, dist="normal"):
    rng = np.random.default_rng(seed)
    rf = (1 + r) ** (1 / 252) - 1
    sd = sig / math.sqrt(252)
    z = rng.standard_normal((n, T)) if dist == "normal" else rng.standard_t(4, (n, T)) / math.sqrt(2)
    x = mu_e / 252 - 0.5 * sd ** 2 + sd * z
    R = np.expm1(x + math.log1p(rf))
    return R, np.full((n, T), rf), np.full((n, T), sd), np.full((n, T), r), x


def test_cash_compounds_rf_exactly():
    rng = np.random.default_rng(1)
    n, T = 50, 252
    rf = rng.uniform(0, 0.0003, (n, T))
    R = rng.normal(0, 0.02, (n, T))
    out = run_engine(ArrayMarket(R, rf, np.full((n, T), 0.01), np.full((n, T), 0.03)), [make_strategy("cash", T)],
                     W0, K, G, {"sigma0_ann": 0.15, "r0_ann": 0.03}, info(), ECFG)
    np.testing.assert_allclose(out[0]["W_T"][0], W0 * np.prod(1 + rf, axis=1), rtol=1e-12)
    # simulated, constant non-negative rate: never breaches
    dgp = {"kind": "gjr", "omega": 1e-6, "alpha": 0.05, "gamma": 0.1, "beta": 0.88, "nu": 6.0, "innovations": "t",
           "premium": {"mode": "constant_arith", "mu_e_daily": 0.0002}, "jump": None}
    inv = {"kind": "gjr", "omega": 1e-6, "alpha": 0.05, "gamma": 0.1, "beta": 0.88, "mu_e_daily": 0.0002}
    for r0 in (0.0, 0.03):
        s0 = {"sigma0_ann": 0.3, "r0_ann": r0}
        o = run_engine(SimMarket(dgp, inv, {"mode": "constant"}, s0, 2000, 4), [make_strategy("cash", 504)],
                       W0, K, G, s0, info(), ECFG)
        assert (o[0]["W_T"] < K).sum() == 0


def test_full_equity_lognormal_breach():
    n, T = 40000, 252
    R, rf, sd, ra, x = gbm_arrays(n, T, sig=0.2, seed=2)
    s = make_strategy("merton_const", T)          # mu/(gamma sigma_bar^2) >> 1 -> clipped to 1
    out = run_engine(ArrayMarket(R, rf, sd, ra), [s], W0, K, G, {"sigma0_ann": 0.2, "r0_ann": 0.03},
                     info(mu=1.0), ECFG)
    W = out[0]["W_T"][0]
    mu_L = T * (0.04 / 252 - 0.5 * (0.2 ** 2 / 252) + math.log(1.03) / 252)
    p = lognormal_breach(W0, K, mu_L, 0.2 * math.sqrt(T / 252))
    se = math.sqrt(p * (1 - p) / n)
    assert abs((W < K).mean() - p) < 4 * se
    assert out[0]["clip_hi"][0] == n * T          # every path-day clipped at 1 (reported, not hidden)


def test_ce_lognormal_closed_form():
    rng = np.random.default_rng(3)
    mu_L, s_L, n = 0.05, 0.15, 400000
    W = W0 * np.exp(mu_L + s_L * rng.standard_normal(n))
    closed = W0 * math.exp(mu_L - (G - 1) * s_L ** 2 / 2)
    se = closed * s_L / math.sqrt(n) * 3       # generous: delta-method SE of the inverse mean
    assert abs(ce(W, G) - closed) < 4 * se


def test_obpi_gbm_value_and_rebalancing():
    sig, r, T = 0.15, 0.03, 252
    phiM = min(1.0, 0.04 / (G * sig ** 2))
    x0 = solve_x0(W0, K, math.log1p(r), phiM * sig, T / 252)
    v, _ = obpi_value(np.array(x0), K, math.log1p(r), phiM * sig, T / 252)
    assert abs(float(v) - W0) / W0 < 1e-9
    short = {}
    for reb in (1, 5):
        R, rf, sd, ra, _ = gbm_arrays(20000, T, sig=sig, r=r, seed=4)
        o = run_engine(ArrayMarket(R, rf, sd, ra), [make_strategy("obpi_merton", T, [1.0], vol_input="sigma0_fixed")],
                       W0, K, G, {"sigma0_ann": sig, "r0_ann": r}, info(sigma0=sig), {**ECFG, "rebalance_every": reb},
                       keep_phi=True)
        W = o[0]["W_T"][0]
        short[reb] = (W < 0.99 * K).mean()
        ph = o[0]["phi_full"]
        assert ph.min() >= 0 and ph.max() <= 1
    # shortfalls below the floor shrink as rebalancing frequency rises; daily is small
    assert short[1] < short[5] and short[1] < 0.005


def test_cppi_m1_never_breaches_bounded_losses():
    n, T = 5000, 504
    R, rf, sd, ra, _ = gbm_arrays(n, T, sig=0.6, seed=5, dist="t")
    R = np.maximum(R, -0.95)
    o = run_engine(ArrayMarket(R, rf, sd, ra), [make_strategy("cppi", T, [1.0])], W0, K, G,
                   {"sigma0_ann": 0.6, "r0_ann": 0.03}, info(), ECFG)
    assert (o[0]["W_T"][0] < K).sum() == 0


@pytest.mark.parametrize("name", ["cash", "merton_const", "merton_vol", "cppi", "cppi_vol", "obpi_merton"])
def test_no_lookahead(name):
    n, T, t = 300, 120, 60
    R, rf, sd, ra, _ = gbm_arrays(n, T, sig=0.25, seed=6)
    rng = np.random.default_rng(7)
    sd = sd * np.exp(0.2 * rng.standard_normal((n, T)))   # time-varying vol input
    kv = {"cppi": [4.0], "cppi_vol": [5.0], "obpi_merton": [1.0]}.get(name)
    runs = []
    for alter in (False, True):
        R2 = R.copy()
        if alter:
            R2[:, t + 1:] = R2[:, t + 1:] * 3 - 0.01
        s = make_strategy(name, T, kv)
        o = run_engine(ArrayMarket(R2, rf, sd, ra), [s], W0, K, G, {"sigma0_ann": 0.25, "r0_ann": 0.03},
                       info(floor="robust"), ECFG, keep_phi=True)
        runs.append(o[0]["phi_full"])
    np.testing.assert_array_equal(runs[0][:, :, : t + 1], runs[1][:, :, : t + 1])
    if name in ("cppi", "cppi_vol", "obpi_merton"):   # wealth-dependent rules react to later returns
        assert not np.array_equal(runs[0][:, :, t + 2:], runs[1][:, :, t + 2:])


def test_history_is_read_only_and_past_only():
    seen = {}

    class Peek(STRATEGIES["cash"]):
        def weight(self, t, W, F, sigma_d, r_ann, history):
            r, _ = history.returns(5)
            seen[t] = r.copy()
            with pytest.raises(ValueError):
                r[0, 0] = 1.0
            return super().weight(t, W, F, sigma_d, r_ann, history)
    n, T = 4, 10
    R = np.arange(n * T, dtype=float).reshape(n, T) / 1000
    run_engine(ArrayMarket(R, np.zeros((n, T)), np.full((n, T), 0.01), np.zeros((n, T))), [Peek(T)], W0, K, G,
               {"sigma0_ann": 0.15, "r0_ann": 0.0}, info(), ECFG)
    assert seen[0].shape == (n, 0)
    np.testing.assert_array_equal(seen[7], R[:, 2:7])


def test_reproducible_rows():
    dgp = {"kind": "gjr", "omega": 1e-6, "alpha": 0.04, "gamma": 0.11, "beta": 0.89, "nu": 6.0, "innovations": "t",
           "premium": {"mode": "constant_arith", "mu_e_daily": 0.00016}, "jump": {"p_day": 0.01 / 252, "x_jump": math.log(0.8)}}
    inv = {k: dgp[k] for k in ("kind", "omega", "alpha", "gamma", "beta")} | {"mu_e_daily": 0.00016}
    rate = {"mode": "vasicek", "kappa": 0.3, "theta": 0.04, "s": 0.02, "rho": -0.12}
    s0 = {"sigma0_ann": 0.2, "r0_ann": 0.03}
    rows = []
    for _ in range(2):
        st = [make_strategy("cppi", 252, [5.0, 8.0]), make_strategy("obpi_merton", 252, [1.02])]
        o = run_engine(SimMarket(dgp, inv, rate, s0, 2000, 99), st, W0, K, G, s0, info(floor="robust"), ECFG)
        rows.append([row_metrics(res, i, K, W0, 0.03, G, MCFG, 0) for res in o for i in range(res["W_T"].shape[0])])
    import pandas as pd
    assert pd.DataFrame(rows[0]).equals(pd.DataFrame(rows[1]))   # NaN-aware identity


def test_robust_floor_reduces_to_spec_without_rate_vol():
    r, tau = 0.03, 1.5
    assert cash_growth_lower(np.array([r]), tau, None, 2.5)[0] == pytest.approx(math.log1p(r) * tau)
    assert cash_growth_lower(np.array([r]), tau, {"kappa": 0.3, "theta": 0.03, "s": 0.0}, 2.5)[0] == pytest.approx(
        math.log1p(r) * tau)
    lo = cash_growth_lower(np.array([r]), tau, {"kappa": 0.3, "theta": 0.03, "s": 0.02}, 2.5)[0]
    assert lo < math.log1p(r) * tau


def test_tune_pick_and_knob_function():
    vals = np.array([1.0, 2.0, 4.0, 8.0])
    v, *_ = pick(vals, np.array([0.0, 0.005, 0.012, 0.03]), 10000, +1, 0.015)
    assert v == 4.0
    v, _, _, flag, *_ = pick(vals, np.array([0.0, 0.0, 0.0, 0.0]), 10000, +1, 0.015)
    assert v == 8.0 and flag == "cap_most_aggressive"
    f = KnobFunction([0.1, 0.4], [252, 504], [[10, 4], [12, 6]])
    assert f(0.1, 252) == pytest.approx(10)
    assert f(0.2, 252) == pytest.approx(7)          # midpoint in log sigma
    assert f(0.4, 378) == pytest.approx(5)
    assert f(2.0, 504) == pytest.approx(6)          # flat beyond the tuned range


def test_breach_classification_gap_gradual_rate():
    T, n = 252, 1
    rf = np.full((n, T), (1.03) ** (1 / 252) - 1)
    ra = np.full((n, T), 0.03)
    sd = np.full((n, T), 0.01)
    s0 = {"sigma0_ann": 0.15, "r0_ann": 0.03}
    # gap: CPPI with a large cushion buffer, then a -60% day
    R = np.zeros((n, T)); R[0, 10] = -0.60
    o = run_engine(ArrayMarket(R, rf, sd, ra), [make_strategy("cppi", T, [3.0])], W0, K, G, s0, info(), ECFG)
    assert o[0]["W_T"][0, 0] < K and o[0]["cross"][0, 0] == 1
    # gradual: constant-weight Merton eroding slowly through the floor
    R = np.full((n, T), -0.0006)
    o = run_engine(ArrayMarket(R, rf, sd, ra), [make_strategy("merton_const", T)], W0, K, G, s0, info(), ECFG)
    assert o[0]["W_T"][0, 0] < K and o[0]["cross"][0, 0] == 2
    # rate: all cash, the floor PV jumps above wealth when the observable rate collapses
    ra2 = ra.copy(); ra2[0, 200:] = -0.40
    o = run_engine(ArrayMarket(np.zeros((n, T)), rf, sd, ra2), [make_strategy("cash", T)], W0, K, G, s0, info(), ECFG)
    assert o[0]["cross"][0, 0] == 3


def test_obpi_reanchor_bounds():
    """Re-anchored OBPI: V(X_t) = W_t, phi <= phi_M <= 1 every day, phi = 0 once W <= Kd (no clips)."""
    from gf.strategies import solve_x_call
    rng = np.random.default_rng(3)
    n = 20000
    Kd = rng.uniform(80e6, 100e6, n)
    W = Kd * rng.uniform(1.0001, 3, n)
    sx = rng.uniform(0.001, 3, n)
    X, nd1 = solve_x_call(W - Kd, Kd, None, sx, 0.7, X0=W * rng.uniform(0.1, 3, n))
    v, _ = obpi_value(X, Kd, 0.0, sx, 0.7)
    assert np.max(np.abs(v - W) / W) < 1e-8
    assert np.all(X * nd1 <= W * (1 + 1e-12))
    # in the engine under a crash path: no clips, phi = 0 below the floor
    T, n = 252, 400
    r = rng.standard_t(3, size=(n, T)) * 0.03
    r[:, 100] = -0.35
    rf = np.full((n, T), 0.0001)
    o = run_engine(ArrayMarket(r, rf, np.full((n, T), 0.03), np.full((n, T), 0.025)),
                   [make_strategy("obpi_merton", T, [1.0, 1.05])], 1e8, 95e6, 2.0,
                   {"sigma0_ann": 0.3, "r0_ann": 0.025},
                   {"mu_e_ann": 0.06, "sigma_bar_ann": 0.18, "sigma0_ann": 0.3}, ECFG, keep_phi=True)[0]
    assert o["clip_hi"].sum() == 0 and o["clip_lo"].sum() == 0


def test_dgp_sigma_cap():
    from gf.engine import SimMarket
    dgp = {"kind": "gjr", "omega": 1e-6, "alpha": 0.2, "gamma": 0.2, "beta": 0.85, "nu": 3.0, "innovations": "t",
           "premium": {"mode": "constant_arith", "mu_e_daily": 0.0002}, "jump": None, "sigma_cap_daily": 0.05}
    inv = {"kind": "gjr", "omega": 1e-6, "alpha": 0.05, "gamma": 0.1, "beta": 0.9, "mu_e_daily": 0.0002}
    mk = SimMarket(dgp, inv, {"mode": "constant"}, {"sigma0_ann": 0.5, "r0_ann": 0.02}, 2000, 1)
    for t in range(300):
        mk.realise(t)
        assert np.all(mk.s2 <= 0.05 ** 2 * (1 + 1e-12))
    assert mk.n_cap > 0
