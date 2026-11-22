"""App smoke test: build tiny fixture results with the real pipeline functions, then import the app, build
its layout and run every callback on them."""
import copy
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fixture_root(tmp_path_factory):
    from gf.data import load_config
    from gf.scenarios import load_context
    from gf.tune import aggregate
    mc3 = _load("run_mc", ROOT / "scripts" / "03_run_mc.py")
    bt4 = _load("backtest", ROOT / "scripts" / "04_backtest.py")
    cfg = copy.deepcopy(load_config())
    cfg["tune"].update({"grid_points": 4, "passes": 1})
    cfg["mc"].update({"bootstrap_reps": 0, "terminal_subsample": 200, "sample_paths": 10, "fan_subsample": 200,
                      "frontier_points": 3})
    root = tmp_path_factory.mktemp("results")
    for d in ("mc/terminal", "mc/paths", "tuned", "backtest"):
        (root / d).mkdir(parents=True)
    ctx = load_context(cfg, "us_mkt")
    named = ctx.state_rows("named").set_index("state_name")
    rows = [named.loc[s].to_dict() | {"state_name": s} for s in ("normal", "crisis")]
    cells = [mc3.tune_task(cfg, "us_mkt", r, [252], 400, 1) for r in rows]
    tuned = {"strategies": aggregate(cells, mc3.KNOBBED, [252], cfg["strategies"]["knobs"]), "horizons": [252]}
    (root / "tuned" / "us_mkt_base.json").write_text(json.dumps(mc3.jsonable(tuned)))
    out = [mc3.eval_task(cfg, "us_mkt", rows[0], "base", [252], 400, 2, tuned, root, True, True),
           mc3.eval_task(cfg, "us_mkt", rows[1], "base", [252], 400, 2, tuned, root, True, False),
           mc3.eval_task(cfg, "us_mkt", rows[0], "fhs", [252], 400, 2, tuned, root, True, False)]
    summ = pd.DataFrame([x for o in out for x in o[0]])
    summ.to_parquet(root / "mc" / "summary.parquet")
    pd.DataFrame([x for o in out for x in o[1]]).to_parquet(root / "mc" / "frontier.parquet")
    mc3.worst_case(summ, cfg).to_parquet(root / "mc" / "worst_case.parquet")
    rng = np.random.default_rng(0)
    per = pd.DataFrame({"asset": "us_mkt", "start": pd.date_range("1990-01-01", periods=60, freq="MS"),
                        "T_days": 252, "T": 1.0, "strategy": np.repeat(["cppi", "merton_const"], 30),
                        "tune_mode": np.repeat(["state", "none"], 30), "W_T": rng.uniform(9e7, 1.2e8, 60)})
    per["breach"] = per.W_T < 95e6
    per["bucket"] = np.tile(["calm", "normal", "elevated", "crisis"], 15)
    per["decade"] = "1990s"
    per["cross"] = 1
    per["phi_avg"] = 0.5
    bt4.summarise(per, 95e6, 1e8).to_parquet(root / "backtest" / "summary.parquet")
    (root / "run_manifest.json").write_text(json.dumps({"master_seed": 1, "git_commit": "fixture",
                                                         "assets": {"us_mkt": {"n_paths_eval": 400}},
                                                         "config_snapshot": {"mc": cfg["mc"]}, "data_sha256": {}}))
    return root


@pytest.fixture(scope="module")
def A(fixture_root):
    sys.path.insert(0, str(ROOT / "app"))
    mod = _load("gf_app", ROOT / "app" / "app.py")
    mod.RUN_DIRS = {"fixture": str(fixture_root)}
    mod.load_results.cache_clear()
    return mod


def test_layout_and_callbacks(A):
    app = A.make_app()
    layout = A.build_layout()
    assert layout is not None and app is not None
    opts = A.control_options("fixture", "us_mkt", "named", None)
    assert opts[1] == "normal" and opts[15] == "base"
    S = A.DEFAULT_STRATS + ["cash", "obpi_merton_untuned"]
    rows, msg = A.scorecard("fixture", "us_mkt", S, "state", "normal", 252, "base")
    assert rows and msg == "" and {r["flag"] for r in rows} <= {"red", "amber", "green"}
    figs = [A.heatmap_fig("fixture", "us_mkt", "cppi", "state", 0.03, "base", False),
            A.heatmap_fig("fixture", "us_mkt", "cppi", "uniform", 0.03, "base", True),
            A.distribution_fig("fixture", "us_mkt", S, "state", "normal", 252, "base", True),
            A.paths_fig("fixture", "us_mkt", S, "state", "normal", 252, "base", True),
            A.frontier_fig("fixture", "us_mkt", S, "state", "normal", 252),
            A.robustness_fig("fixture", "us_mkt", S, "uniform_all", "normal", 252),
            A.backtest_fig("fixture", "us_mkt", ["cppi", "merton_const"], "state", 252)]
    assert all(len(f.data) > 0 for f in figs)
    assert "fixture" in A.manifest_text("fixture")
    # a cell outside the plan gives a message, not a crash
    rows, msg = A.scorecard("fixture", "us_mkt", S, "state", "calm", 504, "base")
    assert rows == [] and msg
    # missing results -> friendly message
    A.RUN_DIRS["empty"] = str(ROOT / "does_not_exist")
    assert "scripts/03_run_mc.py" in A.scorecard("empty", "us_mkt", S, "state", "normal", 252, "base")[1]


def test_custom_tab_runs_quickly(A):
    rows, msg = A.custom_run("us_mkt", 0.2, 0.03, 63, 6.0, 5.0, 1.02, n_paths=500)
    assert rows and "Not part of the report" in msg


def test_dp_policy_merge_and_tab(A, fixture_root, tmp_path):
    """SPEC 04: dp_policy rows from a separate results_dp directory are merged into the run and shown like any
    other strategy (scorecard, distribution, paths); the DP policy tab reads the policy file."""
    import copy as _copy

    from gf import dp as D
    from gf.data import load_config
    from gf.scenarios import load_context
    dp5 = _load("dp5", ROOT / "scripts" / "05_dp.py")
    cfg = _copy.deepcopy(load_config())
    cfg["mc"].update({"bootstrap_reps": 0, "terminal_subsample": 200, "sample_paths": 10, "fan_subsample": 200})
    ctx = load_context(cfg, "us_mkt")
    m = D.model_from_context(ctx, cfg)
    y, h = D.y_grid(40), D.h_grid(0.04, 1.876, 6)
    sols = [D.solve(m, lam, 252, y, h, D.shock_quadrature(m.nu, 30), store_J=False) for lam in (0.0, 1e-3)]
    pol = tmp_path / "policies"
    pol.mkdir()
    named = ctx.state_rows("named")
    D.save_policy(pol / "dp_us_mkt.npz", sols, m, {"sigma": named.sigma0_ann.tolist(), "T_days": [252],
                                                    "idx": [[1] * len(named)]})
    droot = tmp_path / "results_dp"
    for d in ("mc/terminal", "mc/paths"):
        (droot / d).mkdir(parents=True)
    row = named[named.state_name == "normal"].iloc[0].to_dict()
    rows, _ = dp5._eval_task(cfg, "us_mkt", row, "base", [252], 400, 2, pol / "dp_us_mkt.npz", str(droot), True)
    pd.DataFrame(rows).to_parquet(droot / "mc" / "summary.parquet")
    A.dp_results_dir = lambda: droot
    A.dp_policy_dir = lambda: pol
    A.DP_RUNS = {"fixture"}
    A.load_results.cache_clear()
    A.load_dp_policy_file.cache_clear()
    S = ["cppi", "dp_policy"]
    rows, msg = A.scorecard("fixture", "us_mkt", S, "state", "normal", 252, "base")
    assert {r["strategy"] for r in rows} == {A.LABELS["cppi"], A.LABELS["dp_policy"]}
    f1 = A.distribution_fig("fixture", "us_mkt", S, "state", "normal", 252, "base", True)
    f2 = A.paths_fig("fixture", "us_mkt", S, "state", "normal", 252, "base", False)
    assert {t.name for t in f1.data} >= {A.LABELS["dp_policy"], A.LABELS["cppi"]}
    assert A.LABELS["dp_policy"] in {t.name for t in f2.data}
    f3 = A.dp_policy_fig("us_mkt", 126, 1, 0.12, 252)
    assert len(f3.data) == 1 and "λ*" in f3.layout.title.text
    assert A.dp_slider_props("us_mkt")[2] == 1
    A.load_results.cache_clear()
