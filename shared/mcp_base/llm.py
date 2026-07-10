"""Provider-agnostic LLM router for OpenAI-compatible chat/completions APIs."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .config import get_env
from . import http

_DEFAULT_CHAIN = "cline,nvidia,grok,gemini,ollama"

_CLINE_BASE = "https://api.cline.bot/api/v1"
_CLINE_MODEL = "anthropic/claude-sonnet-4-6"

_PROVIDER_SPECS: dict[str, dict[str, Any]] = {
    "cline": {
        "base_url_env": "CLINE_API_BASE_URL",
        "base_url_default": _CLINE_BASE,
        "model_env": "CLINE_MODEL",
        "model_default": _CLINE_MODEL,
        "key_envs": ("CLINE_API_KEY",),
    },
    "nvidia": {
        "base_url_env": "NVIDIA_API_BASE_URL",
        "base_url_default": "https://integrate.api.nvidia.com/v1",
        "model_env": "NVIDIA_MODEL",
        "model_default": "moonshotai/kimi-k2-instruct",
        "key_envs": ("NVIDIA_API_KEY",),
    },
    "grok": {
        "base_url_env": "GROK_API_BASE_URL",
        "base_url_default": "https://api.x.ai/v1",
        "model_env": "GROK_MODEL",
        "model_default": "grok-4-fast",
        "key_envs": ("GROK_API_KEY", "XAI_API_KEY"),
    },
    "gemini": {
        "base_url_env": "GEMINI_API_BASE_URL",
        "base_url_default": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model_env": "GEMINI_MODEL",
        "model_default": "gemini-2.0-flash",
        "key_envs": ("GEMINI_API_KEY",),
    },
    "ollama": {
        "base_url_env": "OLLAMA_BASE_URL",
        "base_url_default": "http://localhost:11434/v1",
        "model_env": "OLLAMA_MODEL",
        "model_default": "qwen2.5-coder:7b",
        "key_envs": ("OLLAMA_API_KEY",),
        "dummy_key": "ollama",
    },
}


@dataclass(frozen=True)
class LLMProvider:
    id: str
    base_url: str
    api_key: str
    model: str


def _first_key(*env_names: str) -> str | None:
    for name in env_names:
        val = (get_env(name) or "").strip()
        if val:
            return val
    return None


def _build_provider(provider_id: str) -> LLMProvider | None:
    spec = _PROVIDER_SPECS.get(provider_id)
    if not spec:
        return None
    api_key = _first_key(*spec["key_envs"])
    if not api_key:
        dummy = spec.get("dummy_key")
        if dummy and provider_id == "ollama":
            api_key = dummy
        else:
            return None
    base_url = (get_env(spec["base_url_env"]) or spec["base_url_default"]).rstrip("/")
    model = (get_env(spec["model_env"]) or spec["model_default"]).strip()
    return LLMProvider(id=provider_id, base_url=base_url, api_key=api_key, model=model)


def provider_chain() -> list[LLMProvider]:
    """Build ordered provider list from LLM_PROVIDER_CHAIN, skipping unset keys."""
    raw = (get_env("LLM_PROVIDER_CHAIN") or _DEFAULT_CHAIN).strip()
    ids = [p.strip().lower() for p in raw.split(",") if p.strip()]
    providers: list[LLMProvider] = []
    seen: set[str] = set()
    for pid in ids:
        if pid in seen:
            continue
        seen.add(pid)
        prov = _build_provider(pid)
        if prov:
            providers.append(prov)
    return providers


def cline_config() -> dict[str, str | None]:
    """Return resolved Cline API settings from environment."""
    prov = _build_provider("cline")
    if not prov:
        return {"api_key": None, "base_url": _CLINE_BASE, "model": _CLINE_MODEL}
    return {"api_key": prov.api_key, "base_url": prov.base_url, "model": prov.model}


def _extract_chat_content(resp_text: str) -> str | None:
    try:
        data = json.loads(resp_text or "{}")
        choices = data.get("choices") or []
        if not choices:
            return None
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        return content if isinstance(content, str) and content.strip() else None
    except (json.JSONDecodeError, TypeError, KeyError, IndexError):
        return None


def openai_compatible_chat(
    provider: LLMProvider,
    messages: list[dict[str, str]],
    model: str | None = None,
    max_tokens: int = 400,
    timeout: int = 20,
) -> str | None:
    """POST to {base_url}/chat/completions. Returns assistant text or None."""
    body: dict[str, Any] = {
        "model": model or provider.model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = http.request(
            "POST",
            f"{provider.base_url}/chat/completions",
            json_body=body,
            headers=headers,
            timeout=timeout,
        )
    except Exception:
        return None
    if not resp.get("ok"):
        return None
    return _extract_chat_content(resp.get("text") or "")


def llm_chat(
    messages: list[dict[str, str]],
    model: str | None = None,
    max_tokens: int = 400,
    timeout: int = 20,
) -> tuple[str | None, str | None]:
    """Try each configured provider in chain order.

    Returns (assistant_text, provider_id) on first success, or (None, None).
    """
    for provider in provider_chain():
        text = openai_compatible_chat(
            provider, messages, model=model, max_tokens=max_tokens, timeout=timeout,
        )
        if text:
            return text, provider.id
    return None, None


def cline_chat(
    messages: list[dict[str, str]],
    model: str | None = None,
    max_tokens: int = 400,
    timeout: int = 20,
) -> str | None:
    """Backward-compatible Cline-only chat helper."""
    prov = _build_provider("cline")
    if not prov:
        return None
    return openai_compatible_chat(prov, messages, model=model, max_tokens=max_tokens, timeout=timeout)


def parse_json_object(text: str) -> dict | None:
    """Best-effort extraction of a single JSON object from LLM output."""
    if not text:
        return None
    text = text.strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[^{}]*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def llm_resolve_identity(target: dict, evidence: list[dict]) -> dict | None:
    """Pick best LinkedIn/company/title from web evidence using the LLM provider chain.

    Separate from Apollo candidate tiebreak. Returns None on any failure.
    Accepts text-only evidence (team bios, search snippets) even without linkedin_url.
    """
    if not evidence:
        return None
    lines = []
    for i, ev in enumerate(evidence[:8]):
        lines.append(
            f"{i}: url={ev.get('url') or 'n/a'} "
            f"linkedin={ev.get('linkedin_url') or 'n/a'} "
            f"snippet={str(ev.get('snippet') or '')[:160]!r} "
            f"title_hint={ev.get('title_hint') or 'n/a'}"
        )
    prompt = (
        "Pick the best identity match for this person from web evidence. "
        "LinkedIn slugs may be opaque (e.g. mlech26l) — use snippet text, not just the URL slug. "
        "If evidence strongly suggests a person but no linkedin_url is listed, infer the most "
        "likely linkedin.com/in/ URL from snippets when possible, else null. "
        "Reply ONLY with JSON:\n"
        '{"linkedin_url": "<url or null>", "company": "<str>", "title": "<str>", '
        '"location": "<str or null>", "confidence": "high|medium|low", "reason": "<brief>"}\n\n'
        f"Target: name={target.get('name')!r} company={target.get('company')!r} "
        f"title={target.get('title')!r} domain={target.get('domain')!r}\n\n"
        "Evidence:\n" + "\n".join(lines)
    )
    raw, provider_id = llm_chat([{"role": "user", "content": prompt}], max_tokens=300)
    data = parse_json_object(raw or "")
    if not data:
        return None
    if data.get("linkedin_url"):
        data["linkedin_url"] = str(data["linkedin_url"]).rstrip("/")
    data["llm_provider"] = provider_id
    return data
