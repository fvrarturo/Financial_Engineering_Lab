"""SPEC 05 deployment rule: dp_premrobust unless a model diagnostic flags the asset, then cppi_vol."""
import json

import pytest

from gf.deployment import deployment_choice, deployment_flags


def _write(root, asset, triggered=False, acc=1.0, verdict="consistent"):
    (root / "results" / "model").mkdir(parents=True, exist_ok=True)
    (root / "reports" / "model" / asset).mkdir(parents=True, exist_ok=True)
    (root / "results" / "model" / f"params_{asset}.json").write_text(json.dumps(
        {"fallback": {"triggered": triggered, "reasons": ["x"] if triggered else []}}))
    pit = [{"group_type": "overall", "group": "all", "H": H, "level": 0.02, "verdict": verdict} for H in (252, 504)]
    pit.append({"group_type": "bucket", "group": "crisis", "H": 252, "level": 0.02, "verdict": "too thin"})
    (root / "reports" / "model" / asset / "model_report.json").write_text(json.dumps(
        {"param_draws": {"acceptance_rate": acc}, "pit_tables": {"gjr_t|scale|t": pit}}))


@pytest.mark.parametrize("kw, expected", [
    ({}, "dp_premrobust"),                       # bucket-only PIT rejection does not count
    ({"triggered": True}, "cppi_vol"),
    ({"acc": 0.69}, "cppi_vol"),
    ({"acc": 0.70}, "dp_premrobust"),
    ({"verdict": "too thin"}, "cppi_vol"),
    ({"verdict": "too fat"}, "cppi_vol"),
])
def test_rule(tmp_path, kw, expected):
    _write(tmp_path, "a", **kw)
    assert deployment_choice("a", tmp_path) == expected
    f = deployment_flags("a", tmp_path)
    assert set(f) >= {"flag_fallback_window", "flag_valid_draws", "flag_pit_reject"}
