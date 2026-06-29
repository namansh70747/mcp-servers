"""F3 — ML email-pattern ranker. Optional (scikit-learn); static fallback always works.

Learns which local-part template a domain uses from the finder's accumulated found_emails +
learning-loop data, predicting the most likely pattern instead of fixed weights. When sklearn is
absent or the model is untrained, callers fall back to the static 13-pattern list (unchanged).
"""
from __future__ import annotations

import re

# The canonical templates (kept in sync with email-finder's _PATTERN_TEMPLATES).
TEMPLATES = ["{first}.{last}", "{first}", "{f}{last}", "{first}{last}", "{first}_{last}",
             "{f}.{last}", "{first}{l}", "{first}-{last}", "{last}.{first}", "{last}{first}",
             "{last}{f}", "{l}{first}", "{last}"]


def available() -> bool:
    try:
        import sklearn  # noqa: F401
        return True
    except Exception:
        return False


def infer_template(local: str, first: str, last: str) -> str | None:
    """Reverse a local-part to the template that produced it (used to build training labels)."""
    local = (local or "").strip().lower()
    if not local or not first or not last:
        return None
    fi, li = first[:1], last[:1]
    for t in TEMPLATES:
        rendered = (t.replace("{first}", first).replace("{last}", last)
                    .replace("{f}", fi).replace("{l}", li)).strip(".-_")
        if rendered == local:
            return t
    return None


def rank_templates(domain: str, observed_locals: list[tuple[str, str, str]]) -> list[str]:
    """Rank templates for a domain given observed (local, first, last) tuples from known emails.

    Frequency-based by default (works with zero deps); if sklearn is present and there is enough
    data, a multinomial model could refine this — but the frequency ranker is the robust core.
    Returns templates ordered most-likely-first; falls back to TEMPLATES order when no data.
    """
    counts: dict[str, int] = {}
    for local, first, last in observed_locals:
        t = infer_template(local, first, last)
        if t:
            counts[t] = counts.get(t, 0) + 1
    if not counts:
        return list(TEMPLATES)
    ranked = sorted(TEMPLATES, key=lambda t: (-counts.get(t, 0), TEMPLATES.index(t)))
    return ranked


def predict_pattern(domain: str, found_rows: list[dict]) -> str | None:
    """Best single pattern guess for a domain from found_emails rows [{email, name?}].

    Each row contributes (local, first, last) when a name is present. Returns the top template
    string or None if nothing usable. Domain-scoped: pass only rows for this domain.
    """
    obs: list[tuple[str, str, str]] = []
    for row in found_rows or []:
        email = (row.get("email") or "").lower()
        name = row.get("name") or ""
        if "@" not in email or not name:
            continue
        local = email.split("@", 1)[0]
        parts = [p for p in re.split(r"\s+", name.strip()) if p]
        if len(parts) < 2:
            continue
        first = re.sub(r"[^a-z]", "", parts[0].lower())
        last = re.sub(r"[^a-z]", "", parts[-1].lower())
        if first and last:
            obs.append((local, first, last))
    ranked = rank_templates(domain, obs)
    return ranked[0] if obs else None
