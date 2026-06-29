"""Frontier email-discovery layers (F1–F10) — all free, all lazy-optional.

Each module degrades to a no-op when its optional dependency is absent, so importing
`frontier` never fails and nothing here is ever in the critical path. The finder/verifier
call these only in deep mode (or when the relevant dep/service is present).

  F1  ocr            — OCR email-in-image extraction (pytesseract/opencv)
  F2  llm_extract    — local-LLM "quote-or-abstain" extraction/disambiguation (Ollama)
  F3  pattern_ml     — ML email-pattern ranker (scikit-learn); static fallback
  F4  fingerprint    — mail-provider fingerprint (SPF/DKIM/DMARC/MTA-STS/BIMI) + playbook
  F5  searxng        — self-hosted SearXNG (+ optional Tor) metasearch
  F6  (agent_browse.deep_research) — agentic deep-research fallback
  F7  bayes          — Bayesian likelihood-ratio confidence fusion
  F8  graph          — entity-graph corroboration (networkx)
  F9  (harvest stealth render) — stealth crawler hooks
  F10 idn            — IDN/EAI punycode + unicode normalization
"""
from __future__ import annotations


def capabilities() -> dict:
    """Report which frontier layers are currently active (their optional dep/service present)."""
    caps: dict[str, bool] = {}

    def _has(mod: str) -> bool:
        try:
            __import__(mod)
            return True
        except Exception:
            return False

    import os
    caps["f1_ocr"] = _has("pytesseract")
    caps["f2_llm"] = bool(os.environ.get("OLLAMA_HOST") or _has("ollama"))
    caps["f3_pattern_ml"] = _has("sklearn")
    caps["f4_fingerprint"] = True  # pure DNS, always available (DoH fallback)
    caps["f5_searxng"] = bool(os.environ.get("SEARXNG_URL"))
    caps["f7_bayes"] = True        # pure python, always available
    caps["f8_graph"] = _has("networkx")
    caps["f10_idn"] = True         # stdlib idna/unicodedata, always available
    return caps
