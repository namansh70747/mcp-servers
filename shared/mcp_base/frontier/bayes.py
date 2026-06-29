"""F7 — Bayesian likelihood-ratio confidence fusion. Pure python; always available.

The additive weighted score (in emailverify) stays the default. This offers a calibrated
probability by combining per-signal evidence with learned hit / false-positive rates via
log-likelihood ratios, starting from a base rate prior. Signals with unknown reliability are
ignored, so a missing signal never hurts.
"""
from __future__ import annotations

import math

# Per-signal (P(signal | deliverable), P(signal | NOT deliverable)) — conservative priors.
# These are the "true positive rate" and "false positive rate" for a POSITIVE observation.
_LR = {
    "api_valid":        (0.95, 0.05),
    "enumeration_hit":  (0.80, 0.20),
    "smtp_250":         (0.75, 0.35),   # accept-alls inflate the FP rate
    "reacher_safe":     (0.90, 0.10),
    "gravatar":         (0.60, 0.25),
    "mx_ok":            (0.99, 0.70),   # weak on its own (almost everyone has MX)
    "published_on_site":(0.85, 0.15),
    "corroborated":     (0.88, 0.18),
    "pattern_match":    (0.70, 0.40),
}

# Negative observations (signal says NOT deliverable)
_LR_NEG = {
    "api_invalid":      (0.02, 0.90),   # (P(obs|deliverable), P(obs|not)) — strong negative
    "smtp_550":         (0.05, 0.80),
    "no_mx":            (0.01, 0.95),
    "disposable":       (0.02, 0.97),
}


def fuse(signals: dict, base_rate: float = 0.5) -> dict:
    """Combine observed boolean signals into a calibrated deliverability probability.

    `signals` maps signal-name → bool (True = observed). Recognized names are in _LR / _LR_NEG.
    Returns {probability: 0..1, log_odds, confidence: high|medium|low|none, used: [...]}.
    """
    # prior log-odds
    base_rate = min(max(base_rate, 1e-4), 1 - 1e-4)
    log_odds = math.log(base_rate / (1 - base_rate))
    used: list[str] = []

    for name, observed in (signals or {}).items():
        if not observed:
            continue
        if name in _LR:
            tpr, fpr = _LR[name]
        elif name in _LR_NEG:
            tpr, fpr = _LR_NEG[name]
        else:
            continue
        tpr = min(max(tpr, 1e-4), 1 - 1e-4)
        fpr = min(max(fpr, 1e-4), 1 - 1e-4)
        log_odds += math.log(tpr / fpr)
        used.append(name)

    prob = 1.0 / (1.0 + math.exp(-log_odds))
    conf = ("high" if prob >= 0.75 else "medium" if prob >= 0.45
            else "low" if prob >= 0.2 else "none")
    return {"probability": round(prob, 4), "log_odds": round(log_odds, 3),
            "confidence": conf, "used": used}
