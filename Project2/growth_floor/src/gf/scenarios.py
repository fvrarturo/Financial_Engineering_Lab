"""Asset context (what SPEC 02 estimated) and scenario DGPs (Spec 03 §4 + addendum).

Key rule: the strategy always uses the investor's *base* estimates (premium, unconditional vol, base GJR-t
filter); a scenario changes only the data-generating process.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import model as M
from .data import resolve


def cell_seed(master: int, *keys) -> int:
    h = hashlib.sha256(json.dumps([int(master), *[str(k) for k in keys]]).encode()).hexdigest()
    return int(h[:15], 16)


@dataclass
class AssetContext:
    asset: str
    params: dict          # params_{asset}.json
    states: pd.DataFrame  # states_{asset}.csv
    zhat: np.ndarray
    draws: np.ndarray
    draws_info: dict
    N: int
    cap_mult: float | None = None

    # -- investor (base estimates) -------------------------------------------------------------
    @property
    def base(self) -> dict:
        return {"kind": self.params["kind"], **self.params["params"]}

    @property
    def mu_e_ann(self) -> float:
        return float(self.params["premium"]["annual_excess_used"])

    @property
    def mu_e_daily(self) -> float:
        return float(self.params["premium"]["resolved"]["mu_e_daily"])

    @property
    def sigma_bar_ann(self) -> float:
        return float(self.params["uncond_vol_ann"])

    def investor(self) -> dict:
        b = self.base
        return {"kind": b["kind"], "omega": b["omega"], "alpha": b["alpha"], "gamma": b.get("gamma", 0.0),
                "beta": b["beta"], "mu_e_daily": self.mu_e_daily}

    def model_info(self, sigma0_ann: float, gamma: float, floor_cfg: dict | None = None) -> dict:
        rp = self.params["rate"]["params"]
        return {"mu_e_ann": self.mu_e_ann, "sigma_bar_ann": self.sigma_bar_ann, "sigma0_ann": sigma0_ann,
                "gamma": gamma, "N": self.N, "base_params": self.base,
                "rate_model": {"kappa": rp["kappa"], "theta": rp["theta"], "s": rp["s"]},
                "floor": floor_cfg or {"mode": "spec"}}

    def rate(self) -> dict:
        return dict(self.params["rate"]["params"])

    # -- states --------------------------------------------------------------------------------
    def state_rows(self, which: str) -> pd.DataFrame:
        s = self.states
        if which == "named":
            return s[s.source == "named"]
        if which == "all":
            return s
        if which == "tune":   # sigma points at the median r0 used for state-dependent tuning
            r0 = float(s[s.source == "named"].r0_ann.iloc[0])
            d = s[(s.source == "dense_grid") & np.isclose(s.r0_ann, r0)]
            return pd.concat([s[s.source == "named"], d])
        raise ValueError(which)

    @property
    def sigma_cap_daily(self):
        """DGP vol cap: engine.sigma_cap_mult x the asset's historical max filtered sigma (the 'extreme' state)."""
        m = self.cap_mult
        if not m:
            return None
        s = self.states
        return float(m * s[s.state_name == "extreme"].sigma0_ann.iloc[0] / math.sqrt(self.N))

    # -- scenarios -----------------------------------------------------------------------------
    def dgp(self, scenario: str, scfg: dict, n_paths: int) -> tuple[dict, dict, dict]:
        """Return (dgp, rate, info) for a scenario name with its config dict."""
        b = self.base
        prem = dict(self.params["premium"]["resolved"])
        dgp = {"kind": b["kind"], "omega": b["omega"], "alpha": b["alpha"], "gamma": b.get("gamma", 0.0),
               "beta": b["beta"], "nu": b["nu"] if b["nu"] is not None else np.inf, "innovations": "t",
               "premium": prem, "jump": None, "sigma_cap_daily": self.sigma_cap_daily}
        rate = self.rate()
        info = {"scenario": scenario}
        if "premium_mult" in scfg:
            dgp["premium"] = {**prem, "mu_e_daily": prem["mu_e_daily"] * scfg["premium_mult"]}
        if "premium_mode" in scfg:
            dgp["premium"] = dict(self.params["premium"]["all_modes_resolved"][scfg["premium_mode"]])
        if "nu_shift" in scfg:
            dgp["nu"] = max(scfg.get("nu_min", 3.0), float(dgp["nu"]) + scfg["nu_shift"])
            info["nu"] = dgp["nu"]
        if "persistence_add" in scfg:
            p = M.persistence(b, b["kind"])
            p_new = min(scfg["persistence_cap"], p + scfg["persistence_add"])
            v = M.uncond_var_daily(b, b["kind"])
            dgp["beta"] = b["beta"] + (p_new - p)
            dgp["omega"] = (1 - p_new) * v
            info.update({"persistence_from": p, "persistence_to": p_new,
                         "half_life_to": M.half_life({**b, "beta": dgp["beta"]}, b["kind"])})
        if scfg.get("param_draws"):
            D = self.draws
            J = len(D)
            if n_paths % J:
                raise ValueError(f"n_paths {n_paths} not a multiple of {J} parameter draws")
            rep = n_paths // J
            for i, k in enumerate(M.PARAM_ORDER):
                if k == "mu":
                    continue
                dgp[k] = np.repeat(D[:, i], rep)
            info["n_draws"] = J
        if scfg.get("innovations") == "fhs":
            dgp["innovations"] = "fhs"
            dgp["zpool"] = self.zhat
        if "jump_prob_per_year" in scfg:
            dgp["jump"] = {"p_day": scfg["jump_prob_per_year"] / self.N, "x_jump": math.log1p(scfg["jump_return"])}
        if scfg.get("rate_mode") == "constant":
            rate["mode"] = "constant"
        if "rho" in scfg:
            rate["rho"] = scfg["rho"]
        if "alt_model" in scfg:
            alt = self.params["alternative_models"][scfg["alt_model"]]
            p = alt["params"]
            dgp.update({"kind": alt["kind"], "omega": p["omega"], "alpha": p["alpha"], "gamma": p.get("gamma", 0.0),
                        "beta": p["beta"], "nu": p["nu"] if p["nu"] is not None else np.inf})
            info.update({"alt_model": scfg["alt_model"], "half_life": alt["half_life_days"],
                         "persistence": alt["persistence"]})
        return dgp, rate, info


def load_context(cfg: dict, asset: str) -> AssetContext:
    res = resolve(cfg, cfg["model"]["results_dir"])
    params = json.loads((res / f"params_{asset}.json").read_text())
    states = pd.read_csv(res / f"states_{asset}.csv")
    zhat = np.load(res / f"zhat_{asset}.npy")
    draws = np.load(res / f"param_draws_{asset}.npy")
    rep = json.loads((resolve(cfg, cfg["model"]["report_dir"]) / asset / "model_report.json").read_text())
    dinfo = rep.get("param_draws", {})
    # verify the stored draws are all valid (rejection sampling in SPEC 02 kept only valid ones)
    kind = params["kind"]
    ok = [M.valid_params(dict(zip(M.PARAM_ORDER, d)), kind, cfg["model"]["nu_min"]) for d in draws]
    dinfo = {"n_valid": int(sum(ok)), "n": len(draws), "acceptance_rate": dinfo.get("acceptance_rate"),
             "n_tried": dinfo.get("n_tried"), "seed": dinfo.get("seed")}
    if not all(ok):
        draws = draws[np.array(ok)]
    return AssetContext(asset, params, states, zhat, draws, dinfo, cfg["trading_days_per_year"],
                        cfg.get("engine", {}).get("sigma_cap_mult"))
