"""Tests for web-first person identity enrichment."""
from __future__ import annotations

import importlib.util
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

from mcp_base.person_identity import (  # noqa: E402
    confirm_identity_web,
    enrich_person_identity,
    guess_domain_from_company,
    _score_linkedin_hit,
)

ef_spec = importlib.util.spec_from_file_location(
    "email_finder_identity_test", ROOT / "servers" / "email-finder" / "server.py")
ef_mod = importlib.util.module_from_spec(ef_spec)
ef_spec.loader.exec_module(ef_mod)


class SearchMergeTests(unittest.TestCase):
    def test_search_links_merged_dedupes_engines(self):
        def ddg(q, n):
            return ["https://a.com", "https://b.com"]

        def bing(q, n):
            return ["https://b.com", "https://c.com"]

        def mojeek(q, n):
            return ["https://d.com"]

        with mock.patch.object(ef_mod, "_ddg_result_links", side_effect=ddg), \
             mock.patch.object(ef_mod, "_bing_links", side_effect=bing), \
             mock.patch.object(ef_mod, "_mojeek_links", side_effect=mojeek):
            links = ef_mod._search_links_merged("test query", max_links=8)
        self.assertEqual(links, ["https://a.com", "https://b.com", "https://c.com", "https://d.com"])

    def test_extract_linkedin_urls(self):
        text = (
            'See https://www.linkedin.com/in/mathias-lechner and '
            'https://linkedin.com/in/other-person for details.'
        )
        urls = ef_mod._extract_linkedin_urls(text)
        self.assertEqual(len(urls), 2)
        self.assertIn("https://www.linkedin.com/in/mathias-lechner", urls)

    def test_snippet_harvest_finds_opaque_slug(self):
        bing_html = (
            '<html><body><li class="b_algo">'
            '<h2><a href="https://www.bing.com/ck/a">Result</a></h2>'
            '<p>Mathias Lechner Co-founder CTO at Liquid AI '
            'https://www.linkedin.com/in/mlech26l profile</p></li></body></html>'
        )
        with mock.patch.object(ef_mod, "_bing_html", return_value=bing_html), \
             mock.patch.object(ef_mod, "_ddg_html", return_value=""), \
             mock.patch.object(ef_mod, "_mojeek_html", return_value=""), \
             mock.patch.object(ef_mod, "_bing_links", return_value=[]), \
             mock.patch.object(ef_mod, "_ddg_result_links", return_value=[]), \
             mock.patch.object(ef_mod, "_mojeek_links", return_value=[]):
            pack = ef_mod._search_snippets_merged('"Mathias Lechner" "Liquid AI"')
        self.assertIn("https://www.linkedin.com/in/mlech26l", pack["linkedin_urls"])


class RuleScoringTests(unittest.TestCase):
    def test_score_linkedin_hit_name_and_company(self):
        hit = {
            "linkedin_url": "https://linkedin.com/in/mathias-lechner",
            "query": "Mathias Lechner Liquid AI CTO",
            "snippet": "Mathias Lechner Co-founder CTO Liquid AI",
        }
        score = _score_linkedin_hit(hit, "Mathias Lechner", "Liquid AI", "CTO")
        self.assertGreaterEqual(score, 55)

    def test_score_opaque_slug_from_snippet(self):
        hit = {
            "linkedin_url": "https://linkedin.com/in/mlech26l",
            "query": "Mathias Lechner Liquid AI",
            "snippet": "Mathias Lechner Co-founder & CTO @ Liquid AI | Researcher @ MIT",
        }
        score = _score_linkedin_hit(hit, "Mathias Lechner", "Liquid AI", "Co-founder & CTO")
        self.assertGreaterEqual(score, 55)

    def test_guess_domain_liquid_ai(self):
        self.assertEqual(guess_domain_from_company("Liquid AI"), "liquid.ai")

    def test_enrich_user_linkedin_short_circuit(self):
        card = enrich_person_identity(
            "Mathias Lechner",
            company="Liquid AI",
            linkedin_url="https://linkedin.com/in/mlech26l",
        )
        self.assertEqual(card["source"], "user")
        self.assertEqual(card["confidence"], "high")
        self.assertIn("mlech26l", card["linkedin_url"])

    def test_enrich_rule_based_from_mocked_search(self):
        hits = [{
            "linkedin_url": "https://linkedin.com/in/mlech26l",
            "query": "Mathias Lechner Liquid AI Co-founder CTO",
            "snippet": "Mathias Lechner Co-founder & CTO @ Liquid AI",
            "source_url": "https://linkedin.com/in/mlech26l",
        }]
        fake_ef = mock.MagicMock()
        fake_ef._discover_linkedin_urls.return_value = hits
        with mock.patch("mcp_base.person_identity._email_finder", return_value=fake_ef), \
             mock.patch("mcp_base.person_identity._fetch_team_evidence", return_value=[]):
            card = enrich_person_identity("Mathias Lechner", company="Liquid AI", title="CTO")
        self.assertEqual(card["source"], "web_rule")
        self.assertIn("mlech26l", card["linkedin_url"] or "")
        self.assertIn(card["confidence"], ("high", "medium"))

    def test_llm_runs_with_team_bio_only(self):
        team_evidence = [{
            "url": "https://liquid.ai/team",
            "snippet": "Mathias Lechner is the Co-founder and CTO of Liquid AI",
            "linkedin_url": None,
            "title_hint": "Co-founder & CTO",
            "score": 30,
        }]
        fake_ef = mock.MagicMock()
        fake_ef._discover_linkedin_urls.return_value = []
        llm_out = {
            "linkedin_url": "https://linkedin.com/in/mlech26l",
            "company": "Liquid AI",
            "title": "Co-founder & CTO",
            "confidence": "high",
            "llm_provider": "nvidia",
        }
        with mock.patch("mcp_base.person_identity._email_finder", return_value=fake_ef), \
             mock.patch("mcp_base.person_identity._fetch_team_evidence", return_value=team_evidence), \
             mock.patch("mcp_base.person_identity.llm_resolve_identity", return_value=llm_out) as llm:
            card = enrich_person_identity("Mathias Lechner", company="Liquid AI", title="CTO")
        llm.assert_called_once()
        self.assertEqual(card["source"], "web_llm")
        self.assertIn("mlech26l", card["linkedin_url"] or "")


class ConfirmIdentityWebTests(unittest.TestCase):
    def test_confirm_identity_web_returns_linkedin_url(self):
        pack = {
            "linkedin_urls": ["https://www.linkedin.com/in/mlech26l"],
            "urls": ["https://liquid.ai/team/mathias-lechner"],
            "snippets": ["Mathias Lechner Co-founder & CTO @ Liquid AI"],
        }
        fake_ef = mock.MagicMock()
        fake_ef._search_snippets_merged.return_value = pack
        with mock.patch("mcp_base.person_identity._email_finder", return_value=fake_ef):
            out = confirm_identity_web("Mathias Lechner", "Liquid AI")
        self.assertIn("mlech26l", out["linkedin_url"] or "")
        self.assertEqual(out["source"], "web_confirm")


if __name__ == "__main__":
    unittest.main()
