"""Zero-coupon curve models. Every model exposes  zero(tau, p) -> continuously compounded zero yield (decimal)
and the discount factor  exp(-zero * tau).  Parameters are risk-neutral (Q) where the model is dynamic.

Ladder:  NS, Svensson (static benchmarks)  ->  Vasicek  ->  CIR  ->  G2++ (two-factor Gaussian)."""
import numpy as np

EPS = 1e-8


def _safe(tau):
    return np.maximum(np.asarray(tau, dtype=float), EPS)


# ------------------------------------------------------------------ static benchmarks
def ns_zero(tau, p):
    """Nelson-Siegel (1987). p = (b0 level, b1 slope, b2 curvature, lam decay in years)."""
    b0, b1, b2, lam = p
    x = _safe(tau) / lam
    f1 = (1 - np.exp(-x)) / x
    return b0 + b1 * f1 + b2 * (f1 - np.exp(-x))


def sv_zero(tau, p):
    """Svensson (1994): Nelson-Siegel plus a second hump. p = (b0, b1, b2, b3, lam1, lam2)."""
    b0, b1, b2, b3, l1, l2 = p
    x1, x2 = _safe(tau) / l1, _safe(tau) / l2
    f1 = (1 - np.exp(-x1)) / x1
    f2 = (1 - np.exp(-x2)) / x2
    return b0 + b1 * f1 + b2 * (f1 - np.exp(-x1)) + b3 * (f2 - np.exp(-x2))


# ------------------------------------------------------------------ one-factor short-rate models
def vasicek_zero(tau, p):
    """Vasicek (1977) under Q:  dr = kappa (theta - r) dt + sigma dW.   p = (kappa, theta, sigma, r0).
    P(tau) = A exp(-B r0),  B = (1 - e^{-kappa tau}) / kappa,
    ln A = (theta - sigma^2 / (2 kappa^2)) (B - tau) - sigma^2 B^2 / (4 kappa)."""
    k, th, s, r0 = p
    t = _safe(tau)
    B = (1 - np.exp(-k * t)) / k
    lnA = (th - s**2 / (2 * k**2)) * (B - t) - s**2 * B**2 / (4 * k)
    return (B * r0 - lnA) / t


def vasicek_long_yield(k, th, s):
    """Limit of the Vasicek zero yield as tau -> infinity."""
    return th - s**2 / (2 * k**2)


def cir_zero(tau, p):
    """Cox-Ingersoll-Ross (1985) under Q:  dr = kappa (theta - r) dt + sigma sqrt(r) dW.  p = (kappa, theta, sigma, r0)."""
    k, th, s, r0 = p
    t = _safe(tau)
    g = np.sqrt(k**2 + 2 * s**2)
    e = np.exp(g * t) - 1
    den = (g + k) * e + 2 * g
    B = 2 * e / den
    lnA = (2 * k * th / s**2) * np.log(2 * g * np.exp((k + g) * t / 2) / den)
    return (B * r0 - lnA) / t


# ------------------------------------------------------------------ two-factor Gaussian (G2++ with constant shift)
def g2_zero(tau, p):
    """Two-factor Gaussian (G2++, Brigo-Mercurio) with constant shift:  r = delta + x1 + x2,
    dx_i = -a_i x_i dt + s_i dW_i,  corr(dW1, dW2) = rho.   p = (a1, a2, s1, s2, rho, delta, x1, x2)."""
    a1, a2, s1, s2, rho, d, x1, x2 = p
    t = _safe(tau)
    B1 = (1 - np.exp(-a1 * t)) / a1
    B2 = (1 - np.exp(-a2 * t)) / a2

    def v(a, b, sa, sb):
        return sa * sb / (a * b) * (t - B_(a) - B_(b) + (1 - np.exp(-(a + b) * t)) / (a + b))

    def B_(a):
        return (1 - np.exp(-a * t)) / a

    V = v(a1, a1, s1, s1) + v(a2, a2, s2, s2) + 2 * rho * v(a1, a2, s1, s2)
    lnP = -d * t - B1 * x1 - B2 * x2 + V / 2
    return -lnP / t


MODELS = {
    "Nelson-Siegel": ns_zero,
    "Svensson": sv_zero,
    "Vasicek": vasicek_zero,
    "CIR": cir_zero,
    "G2++": g2_zero,
}


def discount(model, tau, p):
    return np.exp(-MODELS[model](tau, p) * np.asarray(tau, dtype=float))


def forward(model, tau, p, h=1e-4):
    """Instantaneous forward rate f(tau) = d/dtau [tau * y(tau)] (numerical)."""
    z = lambda t: MODELS[model](t, p) * t
    return (z(np.asarray(tau) + h) - z(np.maximum(np.asarray(tau) - h, EPS))) / (2 * h)
