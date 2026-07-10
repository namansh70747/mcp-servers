"""Unit tests for multi-provider LLM router (mcp_base.llm)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / "shared"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

os.environ.setdefault("MCP_NO_DOTENV", "1")

from mcp_base.llm import (  # noqa: E402
    LLMProvider,
    cline_chat,
    llm_chat,
    llm_resolve_identity,
    openai_compatible_chat,
    parse_json_object,
    provider_chain,
)


class LLMProviderChainTests(unittest.TestCase):
    def test_provider_chain_respects_order(self):
        env = {
            "LLM_PROVIDER_CHAIN": "nvidia,grok,cline",
            "NVIDIA_API_KEY": "nv-key",
            "GROK_API_KEY": "grok-key",
            "CLINE_API_KEY": "cline-key",
        }
        with mock.patch("mcp_base.llm.get_env", side_effect=lambda k, d=None: env.get(k, d)):
            chain = provider_chain()
        self.assertEqual([p.id for p in chain], ["nvidia", "grok", "cline"])

    def test_provider_chain_skips_missing_keys(self):
        env = {
            "LLM_PROVIDER_CHAIN": "nvidia,grok,gemini",
            "NVIDIA_API_KEY": "nv-key",
        }
        with mock.patch("mcp_base.llm.get_env", side_effect=lambda k, d=None: env.get(k, d)):
            chain = provider_chain()
        self.assertEqual([p.id for p in chain], ["nvidia"])

    def test_grok_accepts_xai_api_key(self):
        env = {
            "LLM_PROVIDER_CHAIN": "grok",
            "XAI_API_KEY": "xai-key",
        }
        with mock.patch("mcp_base.llm.get_env", side_effect=lambda k, d=None: env.get(k, d)):
            chain = provider_chain()
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0].id, "grok")
        self.assertEqual(chain[0].api_key, "xai-key")

    def test_ollama_included_with_dummy_key(self):
        env = {"LLM_PROVIDER_CHAIN": "ollama"}
        with mock.patch("mcp_base.llm.get_env", side_effect=lambda k, d=None: env.get(k, d)):
            chain = provider_chain()
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0].id, "ollama")
        self.assertEqual(chain[0].api_key, "ollama")


class LLMChatRouterTests(unittest.TestCase):
    def test_llm_chat_tries_chain_in_order(self):
        p1 = LLMProvider(id="cline", base_url="https://a", api_key="k1", model="m1")
        p2 = LLMProvider(id="nvidia", base_url="https://b", api_key="k2", model="m2")

        def fake_chat(provider, messages, model=None, max_tokens=400, timeout=20):
            if provider.id == "cline":
                return None
            return '{"best_index": 1}'

        with mock.patch("mcp_base.llm.provider_chain", return_value=[p1, p2]), \
             mock.patch("mcp_base.llm.openai_compatible_chat", side_effect=fake_chat):
            text, pid = llm_chat([{"role": "user", "content": "hi"}])
        self.assertEqual(text, '{"best_index": 1}')
        self.assertEqual(pid, "nvidia")

    def test_llm_chat_all_fail_returns_none(self):
        p1 = LLMProvider(id="cline", base_url="https://a", api_key="k1", model="m1")
        with mock.patch("mcp_base.llm.provider_chain", return_value=[p1]), \
             mock.patch("mcp_base.llm.openai_compatible_chat", return_value=None):
            text, pid = llm_chat([{"role": "user", "content": "hi"}])
        self.assertIsNone(text)
        self.assertIsNone(pid)

    def test_cline_chat_calls_cline_only(self):
        prov = LLMProvider(id="cline", base_url="https://cline", api_key="k", model="m")
        with mock.patch("mcp_base.llm._build_provider", return_value=prov), \
             mock.patch("mcp_base.llm.openai_compatible_chat", return_value="ok") as chat:
            out = cline_chat([{"role": "user", "content": "hi"}])
        self.assertEqual(out, "ok")
        chat.assert_called_once()


class ParseJsonObjectTests(unittest.TestCase):
    def test_parse_plain_json(self):
        self.assertEqual(parse_json_object('{"a": 1}'), {"a": 1})

    def test_parse_embedded_json(self):
        self.assertEqual(
            parse_json_object('Here is the answer: {"best_index": 0, "confidence": "high"}'),
            {"best_index": 0, "confidence": "high"},
        )

    def test_parse_invalid_returns_none(self):
        self.assertIsNone(parse_json_object("not json"))


class LLMResolveIdentityTests(unittest.TestCase):
    def test_llm_resolve_identity_valid_json(self):
        target = {"name": "Mathias Lechner", "company": "Liquid AI", "title": "CTO", "domain": "liquid.ai"}
        evidence = [
            {"linkedin_url": "https://linkedin.com/in/mathias-lechner",
             "snippet": "Co-founder CTO at Liquid AI", "title_hint": "CTO"},
        ]
        payload = (
            '{"linkedin_url": "https://linkedin.com/in/mathias-lechner", '
            '"company": "Liquid AI", "title": "CTO", "location": null, '
            '"confidence": "high", "reason": "matches company and title"}'
        )
        with mock.patch("mcp_base.llm.llm_chat", return_value=(payload, "nvidia")):
            out = llm_resolve_identity(target, evidence)
        self.assertIsNotNone(out)
        self.assertIn("mathias-lechner", out["linkedin_url"])
        self.assertEqual(out["llm_provider"], "nvidia")
        self.assertEqual(out["confidence"], "high")

    def test_llm_resolve_identity_failure_returns_none(self):
        target = {"name": "A", "company": "B", "title": "", "domain": ""}
        with mock.patch("mcp_base.llm.llm_chat", return_value=(None, None)):
            out = llm_resolve_identity(target, [{"linkedin_url": "https://linkedin.com/in/a", "snippet": "x"}])
        self.assertIsNone(out)

    def test_llm_resolve_identity_empty_evidence(self):
        self.assertIsNone(llm_resolve_identity({"name": "A"}, []))


class OpenAICompatibleChatTests(unittest.TestCase):
    def test_extracts_message_content(self):
        provider = LLMProvider(id="test", base_url="https://api", api_key="k", model="m")
        resp = {
            "ok": True,
            "text": '{"choices":[{"message":{"content":"hello"}}]}',
        }
        with mock.patch("mcp_base.llm.http.request", return_value=resp):
            out = openai_compatible_chat(provider, [{"role": "user", "content": "hi"}])
        self.assertEqual(out, "hello")


if __name__ == "__main__":
    unittest.main()
