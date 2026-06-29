"""F10 — IDN / EAI unicode handling. Stdlib only; always available.

Internationalized domains (punycode) and unicode local-parts are normalized end-to-end so a
non-ASCII address round-trips correctly and name→pattern synthesis works for non-English names.
"""
from __future__ import annotations

import unicodedata


def to_ascii_domain(domain: str) -> str:
    """Punycode-encode an IDN domain (möbel.de → xn--mbel-5qa.de). Returns input on failure."""
    d = (domain or "").strip().lower().lstrip("@")
    if not d:
        return d
    try:
        return d.encode("idna").decode("ascii")
    except Exception:
        try:
            return ".".join(
                lbl.encode("idna").decode("ascii") if lbl else lbl for lbl in d.split("."))
        except Exception:
            return d


def to_unicode_domain(domain: str) -> str:
    """Decode a punycode domain back to unicode (xn--mbel-5qa.de → möbel.de)."""
    d = (domain or "").strip().lower().lstrip("@")
    try:
        return d.encode("ascii").decode("idna")
    except Exception:
        return d


def normalize_email(email: str) -> str:
    """NFC-normalize the whole address and punycode the domain so comparisons are stable."""
    email = (email or "").strip()
    if "@" not in email:
        return unicodedata.normalize("NFC", email).lower()
    local, _, domain = email.rpartition("@")
    local = unicodedata.normalize("NFC", local)
    return f"{local.lower()}@{to_ascii_domain(domain)}"


def ascii_fold(text: str) -> str:
    """Transliterate accents to ASCII for pattern synthesis (José → jose, Müller → muller)."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in nfkd if not unicodedata.combining(c))
    # common ligatures/eszett the NFKD pass misses
    folded = (folded.replace("ß", "ss").replace("æ", "ae").replace("ø", "o")
              .replace("œ", "oe").replace("Ð", "d").replace("ð", "d").replace("þ", "th"))
    return folded
