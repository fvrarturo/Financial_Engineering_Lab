"""Deployment rule (SPEC 05 §0.3, decided by the team before results were seen).

Recommended strategy: premium-robust DP (`dp_premrobust`). Fallback, applied per asset from SPEC 02 model diagnostics
only (no strategy results are read): deploy gap-aware CPPI (`cppi_vol`, state-dependent robust knobs) if
  (a) the SPEC 02 fallback estimation window was triggered, or
  (b) fewer than 70% of the parameter draws are valid after the fallback, or
  (c) the conditional PIT test of the base model rejects calibration at H = 252 or H = 504 days
      (any tail level in the overall test, verdict 'too thin' or 'too fat').
Inputs: results/model/params_{asset}.json (fallback), reports/model/{asset}/model_report.json (param_draws, PIT).
"""
from __future__ import annotations

import json
from pathlib import Path

RECOMMENDED = "dp_premrobust"
FALLBACK = "cppi_vol"
MIN_VALID_DRAWS = 0.70
PIT_HORIZONS = (252, 504)
BASE_PIT_KEY = "gjr_t|scale|t"


def _root(root: str | Path | None) -> Path:
    return Path(root) if root is not None else Path(__file__).resolve().parents[2]


def deployment_flags(asset: str, root: str | Path | None = None) -> dict:
    """The three diagnostic flags and their inputs for one asset."""
    R = _root(root)
    params = json.loads((R / "results" / "model" / f"params_{asset}.json").read_text())
    rep = json.loads((R / "reports" / "model" / asset / "model_report.json").read_text())
    fb = params.get("fallback", {})
    draws = rep["param_draws"]
    valid = float(draws["acceptance_rate"])
    pit = rep["pit_tables"][BASE_PIT_KEY]
    rej = sorted({(int(r["H"]), float(r["level"]), r["verdict"]) for r in pit
                  if r["group_type"] == "overall" and int(r["H"]) in PIT_HORIZONS
                  and r["verdict"] in ("too thin", "too fat")})
    return {
        "asset": asset,
        "flag_fallback_window": bool(fb.get("triggered", False)),
        "fallback_reasons": "; ".join(fb.get("reasons", [])),
        "valid_draw_share": valid,
        "flag_valid_draws": valid < MIN_VALID_DRAWS,
        "flag_pit_reject": len(rej) > 0,
        "pit_rejections": "; ".join(f"H={h} u<{lv:g}: {v}" for h, lv, v in rej),
    }


def deployment_choice(asset: str, root: str | Path | None = None) -> str:
    """Strategy to deploy on `asset`: dp_premrobust unless any diagnostic flag is set, then cppi_vol."""
    f = deployment_flags(asset, root)
    return FALLBACK if (f["flag_fallback_window"] or f["flag_valid_draws"] or f["flag_pit_reject"]) else RECOMMENDED


def deployment_table(assets: list[str], root: str | Path | None = None):
    import pandas as pd
    rows = []
    for a in assets:
        f = deployment_flags(a, root)
        f["any_flag"] = f["flag_fallback_window"] or f["flag_valid_draws"] or f["flag_pit_reject"]
        f["chosen_strategy"] = deployment_choice(a, root)
        rows.append(f)
    return pd.DataFrame(rows)
