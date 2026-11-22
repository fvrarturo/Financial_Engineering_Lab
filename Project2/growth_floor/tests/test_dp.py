"""SPEC 04 tests: quadrature, V1 (Merton limit), V2 (limits in lam), extrapolation, dp_policy hook round-trip."""
import math

import numpy as np
import pytest

from gf import dp as D
from gf.engine import ArrayMarket, run_engine
from gf.strategies import cash_growth_lower, lookup_lam_idx, make_strategy

MU, RF = 0.0391323 / 252, 1.0465e-4
RATE = {"kappa": 0.338, "theta": 0.043, "s": 0.0257}


def gjr_model(**kw):
    base = dict(omega=1.1e-6, alpha=0.038, gamma_lev=0.115, beta=0.893, nu=5.9, mu_e_daily=MU, rf_daily=RF,
                r0_ann=0.0267, h_cap=(1.876 ** 2) / 252, rate_model=RATE, floor_alpha=0.001)
    base.update(kw)
    return D.DPModel(**base)


# --------------------------------------------------------------------------- quadrature
@pytest.mark.parametrize("nu", [None, 5.9, 4.0, 12.0])
def test_quadrature_moments(nu):
    q = D.shock_quadrature(nu, 60)
    assert len(q.z) == 60
    assert abs(q.w.sum() - 1) < 1e-12
    assert abs(q.w @ q.z) < 1e-4
    assert abs(q.w @ q.z ** 2 - 1) < 1e-4
    assert abs(q.info["p_below_q001"] - 0.001) < 1e-4
    assert np.all(np.diff(q.z) > 0)                     # nodes stay ordered (inside their bins)
    assert (q.w[:19] < 0.01).all() and q.w[:19].sum() == pytest.approx(0.01)   # left tail refined


def test_grids():
    y = D.y_grid()
    assert len(y) == 120 and y[0] == pytest.approx(0.3) and y[-1] == pytest.approx(4.0)
    assert (y == 1.0).sum() == 1
    dl = np.diff(np.log(y))
    dense = (y[:-1] >= 0.9) & (y[1:] <= 1.3)
    assert dl[dense].max() < dl[~dense].min()          # extra density on [0.9, 1.3]


def test_floor_matches_spec03_code():
    m = gjr_model()
    z = 3.090232306167813
    for t in (0, 1, 252, 504):
        assert m.log_disc(t) == pytest.approx(float(cash_growth_lower(0.0267, t / 252, RATE, z)), abs=1e-12)


# --------------------------------------------------------------------------- V1
@pytest.mark.parametrize("sig", [0.10, 0.15, 0.2, 0.3, 0.5])
def test_v1_merton_limit(sig):
    """Constant variance, normal shocks, lam = 0: phi* = clip(mu/(gamma sigma^2), 0, 1) at every (tau, y)."""
    h = sig * sig / 252
    m = gjr_model(omega=h, alpha=0, gamma_lev=0, beta=0, nu=None, h_fixed=True, h_cap=np.inf)
    y = D.y_grid(60)
    s = D.solve(m, 0.0, 15, y, np.array([h]), D.shock_quadrature(None), store_J=False)
    merton = min(max(MU * 252 / (2 * sig * sig), 0), 1)
    P = np.stack([s.policy(t) for t in range(1, 16)])
    assert np.abs(P - merton).max() <= 0.5 / 40 + 1e-3


# --------------------------------------------------------------------------- V2 + extrapolation
@pytest.fixture(scope="module")
def small_solves():
    m = gjr_model()
    y = D.y_grid(60)
    h = D.h_grid(0.04, 1.876, 10)
    q = D.shock_quadrature(m.nu, 40)
    lams = [0.0, 1e-4, 1e-3, 1e-2, 1.0]
    return m, y, h, [D.solve(m, l, 40, y, h, q) for l in lams]


def test_v2_limits_in_lambda(small_solves):
    m, y, h, sols = small_solves
    T = 40
    y0 = 100e6 / (95e6 * math.exp(-sols[0].q[T]))
    h0 = np.array([0.25 ** 2 / 252])
    b = [float(D.interp2(s.B[T].astype(float), y, h, np.array([y0]), h0)[0]) for s in sols]
    assert all(b[i + 1] <= b[i] + 1e-9 for i in range(len(b) - 1))    # non-increasing in lam
    assert b[0] == max(b)
    # utility part E[U] = J + lam B is highest at lam = 0
    eu = [float(D.interp2(s.J[T].astype(float), y, h, np.array([y0]), h0)[0]) + s.lam * bb for s, bb in zip(sols, b)]
    assert eu[0] >= max(eu) - 1e-12
    # very large lam: no equity just above the floor
    P = sols[-1].policy(T)
    assert np.all(P[y == 1.0] == 0.0)                                # at the floor
    assert P[np.searchsorted(y, 1.0) + 1].mean() < P[np.searchsorted(y, 1.3)].mean()
    # below the floor the indicator makes the problem non-convex: 'gambling for resurrection' (genuine, reported)
    assert P[np.searchsorted(y, 1.0) - 1, 0] > 0.5
    # lam = 0: homogeneity, y J constant in y (exact, including the extrapolation rules)
    Jt = sols[0].J[T].astype(float) * y[:, None]
    assert np.allclose(Jt, Jt[0][None, :], rtol=1e-5)
    assert np.unique(sols[0].phi_idx[T], axis=0).shape[0] == 1     # policy independent of y


def test_extrapolation_exact_last_step():
    """tau = 1 on a narrow y grid with huge vol: transitions leave the grid on both sides. Above y_max (Jt flat)
    and below y_min (terminal-style rule) the extrapolated terminal value is exact, so E[J] at phi* matches a
    brute-force expectation with the exact terminal function up to interior interpolation error."""
    m = gjr_model(omega=0.002, alpha=0, gamma_lev=0, beta=0, h_fixed=True, h_cap=np.inf)
    y = D.y_grid(80, lo=0.97, hi=1.05, dense=(0.99, 1.01))
    h = np.array([0.002])
    q = D.shock_quadrature(m.nu, 60)
    lam = 2e-3
    s = D.solve(m, lam, 1, y, h, q)
    lg = s.q[1] - s.q[0]
    x = np.maximum(m.mean_log(h[0]) + math.sqrt(h[0]) * q.z, math.log(0.01) - math.log1p(RF))
    R = np.expm1(x + math.log1p(RF))
    for i in range(0, len(y), 7):
        phi = s.policy(1)[i, 0]
        yn = y[i] * (1 + RF + phi * (R - RF)) / math.exp(lg)
        # last step: utility by quadrature, breach probability exact via the shock CDF
        pb = D.last_step_breach_prob(m, np.array([y[i]]), np.array([phi]), h, lg)[0, 0, 0]
        exact = float(q.w @ (-1.0 / (yn * 95.0))) - lam * pb
        assert s.J[1][i, 0] == pytest.approx(exact, rel=1e-6)
        assert s.B[1][i, 0] == pytest.approx(pb, abs=1e-6)
    # tau = 2: transitions leave the grid; the extrapolated values follow the stated rules (smoke check)
    s2 = D.solve(m, lam, 2, y, h, q)
    assert np.isfinite(s2.J[2]).all() and s2.below_ymin_share > 0


# --------------------------------------------------------------------------- hook round-trip
@pytest.fixture(scope="module")
def policy_file(tmp_path_factory, small_solves):
    m, y, h, sols = small_solves
    p = tmp_path_factory.mktemp("pol") / "dp_test.npz"
    lk = {"sigma": [0.08, 0.25, 0.5], "T_days": [20, 40], "idx": [[0, 1, 2], [1, 2, 3]]}
    D.save_policy(p, sols, m, lk, {"test": True})
    return p, m, y, h, sols


def _info(alpha=0.001):
    return {"mu_e_ann": MU * 252, "sigma_bar_ann": 0.15, "sigma0_ann": 0.25, "gamma": 2.0, "N": 252,
            "rate_model": RATE, "floor": {"mode": "robust", "alpha": alpha}}


def test_hook_roundtrip_nodes(policy_file):
    p, m, y, h, sols = policy_file
    T = 40
    st = make_strategy("dp_policy", T, [0.0, 1e-3], path=str(p))
    n = 6
    st.reset(n, 100e6, 95e6, 2.0, {"sigma0_ann": 0.25, "r0_ann": 0.0267}, _info())
    r_ann = np.full(n, 0.0267)
    F = np.full((2, n), 95e6)
    Fp = st.protect_floor(0, F, r_ann)
    assert np.allclose(Fp, 95e6 * math.exp(-sols[0].q[T]))           # same floor as the DP
    iy = np.array([3, 20, 33, 40, 50, 59])
    ik = np.array([0, 2, 4, 6, 8, 9])
    W = np.broadcast_to(y[iy] * Fp[0], (2, n)).copy()
    sig = np.sqrt(h[ik])
    phi = st.weight(0, W, F, sig, r_ann, None)
    for r, s in enumerate([sols[0], sols[2]]):
        assert np.allclose(phi[r], s.policy(T)[iy, ik], atol=1e-9)
    # flat beyond the grid, counted
    W2 = np.vstack([np.full(n, 10.0 * Fp[0]), np.full(n, 0.1 * Fp[0])])
    phi2 = st.weight(1, W2, F, sig, r_ann, None)
    assert np.allclose(phi2[0], sols[0].policy(T - 1)[-1, ik]) and np.allclose(phi2[1], sols[2].policy(T - 1)[0, ik])
    assert st.n_above_ymax[0] == n and st.n_below_ymin[1] == n


def test_hook_lookup_and_engine(policy_file):
    p, m, y, h, sols = policy_file
    d = np.load(p)
    assert lookup_lam_idx(d, 0.25, 40) == 2
    assert lookup_lam_idx(d, 0.30, 40) == 3                          # bracketing cells -> the larger lam
    assert lookup_lam_idx(d, 0.25, 30) == 2                          # max over T = 20 and 40 rows
    st = make_strategy("dp_policy", 40, None, path=str(p))
    n, Tn = 200, 40
    rng = np.random.default_rng(0)
    sig = np.full((n, Tn), 0.25 / math.sqrt(252))
    r = np.expm1(MU - 0.5 * sig ** 2 + sig * rng.standard_normal((n, Tn)) + math.log1p(RF))
    mk = ArrayMarket(r, np.full((n, Tn), RF), sig, np.full((n, Tn), 0.0267))
    out = run_engine(mk, [st], 100e6, 95e6, 2.0, {"sigma0_ann": 0.25, "r0_ann": 0.0267}, _info(),
                     {"locked_phi": 0.01, "locked_days": 21, "gap_min_sigmas": 1.0})
    assert st.k == 1 and st.knob_values[0] == sols[2].lam
    assert out[0]["clip_hi"][0] == 0 and out[0]["clip_lo"][0] == 0 and out[0]["clip_nan"][0] == 0
    assert np.isfinite(out[0]["W_T"]).all()


def test_hook_rejects_off_grid_lambda(policy_file):
    p, *_ = policy_file
    st = make_strategy("dp_policy", 40, [5e-4], path=str(p))
    with pytest.raises(ValueError):
        st.reset(4, 100e6, 95e6, 2.0, {"sigma0_ann": 0.25, "r0_ann": 0.0267}, _info())


def test_pchip_breach_matches_random_walk():
    """Constant variance, normal shocks, lam = 0 -> constant phi, log wealth a random walk with known daily moments.
    The DP breach recursion with PCHIP in y matches a large simulation; linear interpolation is diffusive (higher)."""
    sig, T = 0.20, 126
    hh = sig * sig / 252
    m = gjr_model(omega=hh, alpha=0, gamma_lev=0, beta=0, nu=None, h_fixed=True, h_cap=np.inf)
    q = D.shock_quadrature(None)
    out = {}
    for it in ("linear", "pchip"):
        s = D.solve(m, 0.0, T, D.y_grid(), np.array([hh]), q, interp_y=it, store_J=False)
        y0 = 100e6 / (95e6 * math.exp(-s.q[T]))
        out[it] = float(np.interp(math.log(y0), np.log(s.y), s.B[T][:, 0]))
    phi = s.policy(T)[0, 0]
    rng = np.random.default_rng(1)
    n = 200_000
    lw = np.zeros(n)
    for _ in range(T):
        x = MU - 0.5 * hh + math.sqrt(hh) * rng.standard_normal(n)
        lw += np.log1p(RF + phi * (np.expm1(x + math.log1p(RF)) - RF))
    mc = float((lw < math.log(0.95)).mean())
    se = math.sqrt(mc * (1 - mc) / n)
    assert abs(out["pchip"] - mc) < 3 * se + 1e-3
    assert out["linear"] > out["pchip"] + 0.005


def test_pchip_slopes_monotone():
    x = np.log(D.y_grid(40))
    F = np.column_stack([1 / (1 + np.exp((np.exp(x) - 1) * 30)), np.exp(x)])   # decreasing S-curve, increasing
    d = D.pchip_slopes(F, x)
    assert (d[:, 0] <= 0).all() and (d[:, 1] >= 0).all()


def test_banded_y_grid_and_doubling():
    e, c = [0.3, 0.9, 0.97, 1.0, 1.08, 1.3, 4.0], [22, 10, 14, 34, 15, 24]
    y = D.y_grid_bands(e, c)
    y2 = D.y_grid_bands(e, c, 2)
    assert len(y) == 120 and len(y2) == 239 and (y == 1.0).sum() == 1
    assert np.allclose(y2[::2], y)                                     # doubling nests the base grid
    d = np.diff(np.log(y))
    core = (y[:-1] >= 0.97) & (y[1:] <= 1.08)
    assert d[core].max() < 0.0025 < d[~core].min()


def test_pchip_in_h_matches_finer_linear():
    """At lam = 0 (y J constant in y), PCHIP in log h with n points ~ linear with 2n points (less vol diffusion)."""
    m = gjr_model()
    y = D.y_grid_bands([0.3, 1.0, 4.0], [2, 2])
    q = D.shock_quadrature(m.nu, 40)
    T, h0 = 126, np.array([0.12 ** 2 / 252])
    ce = {}
    for nh, ih in ((12, "linear"), (12, "pchip"), (24, "linear")):
        h = D.h_grid(0.04, 1.876, nh)
        s = D.solve(m, 0.0, T, y, h, q, interp_y="pchip", interp_h=ih, store_J=True)
        J = float(D.interp2(s.J[T].astype(float), y, h, np.array([1.0]), h0)[0])
        ce[(nh, ih)] = -1 / J
    assert abs(ce[(12, "pchip")] - ce[(24, "linear")]) < abs(ce[(12, "linear")] - ce[(24, "linear")])
