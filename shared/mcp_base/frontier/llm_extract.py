"""F2 — local-LLM extraction & disambiguation via Ollama. Optional; no-op if Ollama absent.

Reads messy/multilingual pages or org charts with a strict "quote-or-abstain" prompt: the model
may only return addresses that appear VERBATIM in the text (never invented), and picks the right
person for a role. Every returned address must still pass email_extract+verify downstream.
"""
from __future__ import annotations

import json
import os
import re

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
_MODEL = os.environ.get("OLLAMA_EMAIL_MODEL", "llama3.2")


def available() -> bool:
    try:
        from ..http import get_json
        r = get_json(f"{_OLLAMA_HOST}/api/tags", timeout=3, cache_ttl=60)
        return isinstance(r, dict)
    except Exception:
        return False


def _chat(prompt: str, timeout: float = 30.0) -> str:
    from ..http import request
    r = request("POST", f"{_OLLAMA_HOST}/api/generate",
                json_body={"model": _MODEL, "prompt": prompt, "stream": False,
                           "options": {"temperature": 0.0}},
                timeout=timeout)
    if r.get("ok") and isinstance(r.get("json"), dict):
        return r["json"].get("response", "") or ""
    return ""


def extract_emails(text: str, name: str = "", role: str = "") -> list[str]:
    """Ask a local LLM to extract emails that appear VERBATIM in `text` for the target person/role.

    Quote-or-abstain: any address not literally present in `text` is discarded, so the model
    cannot fabricate. Returns [] if Ollama is unavailable.
    """
    if not text or not available():
        return []
    who = (f" for {name}" if name else "") + (f" ({role})" if role else "")
    prompt = (
        "Extract every email address that appears VERBATIM in the text below"
        f"{who}. Rules: only output addresses that literally appear in the text — never guess, "
        "complete, or invent one. If none, output an empty JSON array. "
        'Respond with ONLY a JSON array of strings, e.g. ["a@b.com"].\n\n'
        f"TEXT:\n{text[:8000]}"
    )
    resp = _chat(prompt)
    # parse a JSON array if present, else regex the response
    cand: list[str] = []
    m = re.search(r"\[.*?\]", resp, re.S)
    if m:
        try:
            cand = [str(x) for x in json.loads(m.group(0))]
        except Exception:
            cand = []
    if not cand:
        cand = _EMAIL_RE.findall(resp)
    # quote-or-abstain guard: keep only addresses literally in the source text
    low = text.lower()
    out: list[str] = []
    for e in cand:
        e = e.strip().lower()
        if e and "@" in e and e in low and e not in out:
            out.append(e)
    return out


def pick_person(candidates: list[dict], company: str, role: str) -> dict | None:
    """Disambiguate which candidate person best matches a role at a company (LLM judgment).

    `candidates` = [{name, title?, ...}]. Returns the chosen dict or None. No fabrication: the
    choice is restricted to the provided list. Falls back to None if Ollama unavailable.
    """
    if not candidates or not available():
        return None
    listing = "\n".join(f"{i}. {c.get('name','?')} — {c.get('title','')}"
                        for i, c in enumerate(candidates))
    prompt = (
        f"Which ONE person below is most likely the {role} of {company}? "
        "Answer with ONLY the integer index. If none clearly fits, answer -1.\n\n" + listing
    )
    resp = _chat(prompt, timeout=20.0)
    m = re.search(r"-?\d+", resp)
    if not m:
        return None
    idx = int(m.group(0))
    return candidates[idx] if 0 <= idx < len(candidates) else None
