"""Per-cell metrics (Spec 03 §4)."""
from __future__ import annotations

import math

import numpy as np

from .validation import wilson_ci


def ce(W: np.ndarray, gamma: float) -> float:
    W = np.asarray(W, dtype=float)
    if gamma == 1.0:
        return float(np.exp(np.mean(np.log(W))))
    return float(np.mean(W ** (1.0 - gamma)) ** (1.0 / (1.0 - gamma)))


def ce_bootstrap_se(W: np.ndarray, gamma: float, reps: int, seed: int) -> float:
    """Bootstrap SE of CE (resampling paths)."""
    if reps <= 0:
        return float("nan")
    rng = np.random.Generator(np.random.PCG64(seed))
    u = np.asarray(W, dtype=float) ** (1.0 - gamma)
    n = len(u)
    means = np.empty(reps)
    for b in range(reps):
        means[b] = u[rng.integers(0, n, n)].mean()
    vals = means ** (1.0 / (1.0 - gamma))
    return float(vals.std(ddof=1))


def terminal_metrics(W_T: np.ndarray, K: float, W0: float, r0: float, T_days: int, gamma: float,
                     quantiles: list[float], boot_reps: int = 200, boot_seed: int = 0, N: int = 252) -> dict:
    W = np.asarray(W_T, dtype=float)
    n = len(W)
    br = W < K
    nb = int(br.sum())
    lo, hi = wilson_ci(nb / n, n)
    c = ce(W, gamma)
    Ty = T_days / N
    ce_ann = (c / W0) ** (1.0 / Ty) - 1.0
    out = {"n_paths": n, "breach": nb / n, "breach_lo": lo, "breach_hi": hi, "n_breach": nb,
           "es_given_breach": float((K - W[br]).mean()) if nb else 0.0,
           "q0.1%": float(np.quantile(W, 0.001)),
           "ce": c, "ce_se": ce_bootstrap_se(W, gamma, boot_reps, boot_seed),
           "ce_ann": ce_ann, "ce_ann_excess": ce_ann - r0,
           "mean": float(W.mean()), "median": float(np.median(W))}
    for q, v in zip(quantiles, np.quantile(W, quantiles)):
        out[f"q{q * 100:g}%"] = float(v)
    return out


def row_metrics(res: dict, i: int, K: float, W0: float, r0: float, gamma: float, mcfg: dict,
                boot_seed: int, N: int = 252) -> dict:
    """Metrics for row i of an engine result dict."""
    W = res["W_T"][i]
    m = terminal_metrics(W, K, W0, r0, res["T_days"], gamma, mcfg["terminal_quantiles"],
                         mcfg.get("bootstrap_reps", 200), boot_seed, N)
    pm = res["phi_mean_path"][i]
    cross = res["cross_own"][i][W < K]          # relative to the floor the strategy protects
    cross_spec = res["cross"][i][W < K]         # relative to the engine's (spec) F_t
    nb = max(len(cross), 1)
    m.update({
        "phi_avg": float(pm.mean()),
        "phi_path_p10": float(np.quantile(pm, 0.10)), "phi_path_p50": float(np.quantile(pm, 0.50)),
        "phi_path_p90": float(np.quantile(pm, 0.90)),
        "turnover": float(res["turnover"][i].mean()),
        "locked_cash_share": float(res["locked"][i].mean()),
        "clip_hi_share": float(res["clip_hi"][i] / res["path_days"]),
        "clip_lo_share": float(res["clip_lo"][i] / res["path_days"]),
        "clip_nan": int(res["clip_nan"][i]),
        "breach_gap_share": float((cross == 1).sum() / nb) if len(cross) else float("nan"),
        "breach_gradual_share": float((cross == 2).sum() / nb) if len(cross) else float("nan"),
        "breach_rate_share": float((cross == 3).sum() / nb) if len(cross) else float("nan"),
        "n_breach_gap": int((cross == 1).sum()), "n_breach_gradual": int((cross == 2).sum()),
        "n_breach_rate": int((cross == 3).sum()),
        "breach_gap_share_specF": float((cross_spec == 1).sum() / nb) if len(cross) else float("nan"),
        "breach_rate_share_specF": float((cross_spec == 3).sum() / nb) if len(cross) else float("nan"),
    })
    inf = res.get("infeasible")
    m["infeasible_share"] = float(np.mean(inf[i])) if inf is not None else 0.0
    return m


def lognormal_breach(W0, K, mu_log, sig_log):
    """P(W0 exp(N(mu_log, sig_log^2)) < K)."""
    from scipy.stats import norm
    return float(norm.cdf((math.log(K / W0) - mu_log) / sig_log))
