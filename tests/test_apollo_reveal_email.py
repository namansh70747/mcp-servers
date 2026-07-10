"""Offline tests for Apollo's Access Email reveal flow (mocked Playwright — never spends credits)."""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("MCP_NO_DOTENV", "1")

spec = importlib.util.spec_from_file_location(
    "apollo_srv_reveal", ROOT / "servers" / "apollo" / "server.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["apollo_srv_reveal"] = mod
spec.loader.exec_module(mod)


def _mock_row(text: str, button=None):
    row = mock.MagicMock()
    row.inner_text.return_value = text
    btn_locator = mock.MagicMock()
    btn = mock.MagicMock()
    btn.is_visible.return_value = button is not None
    btn_locator.first = btn
    row.get_by_role.return_value = btn_locator
    return row, btn


class RevealEmailTests(unittest.TestCase):
    def test_already_revealed_email_skips_click(self):
        row, btn = _mock_row("Lee Sonic\nCEO\nCometAPI\nlee@cometapi.com")
        with mock.patch.object(mod, "_find_row_by_name", return_value=row), \
             mock.patch.object(mod, "_close_apollo_modals", return_value=0):
            out = mod._reveal_email_for_row_sync(mock.MagicMock(), "Lee Sonic")
        self.assertEqual(out["email"], "lee@cometapi.com")
        self.assertFalse(out["clicked"])
        btn.click.assert_not_called()

    def test_no_email_on_file_skips_click(self):
        row, btn = _mock_row("Lee Sonic\nCEO\nCometAPI\nNo email")
        with mock.patch.object(mod, "_find_row_by_name", return_value=row), \
             mock.patch.object(mod, "_close_apollo_modals", return_value=0):
            out = mod._reveal_email_for_row_sync(mock.MagicMock(), "Lee Sonic")
        self.assertTrue(out["no_email_on_file"])
        self.assertFalse(out["clicked"])
        btn.click.assert_not_called()

    def test_row_not_found_returns_error(self):
        with mock.patch.object(mod, "_find_row_by_name", return_value=None), \
             mock.patch.object(mod, "_close_apollo_modals", return_value=0):
            out = mod._reveal_email_for_row_sync(mock.MagicMock(), "Nobody")
        self.assertIsNone(out["email"])
        self.assertIn("not found", out["error"])

    def test_click_reveals_email(self):
        row, btn = _mock_row("Lee Sonic\nCEO\nCometAPI\nAccess email", button=True)
        # After click, row text updates to include the revealed email.
        row.inner_text.side_effect = [
            "Lee Sonic\nCEO\nCometAPI\nAccess email",  # before
            "Lee Sonic\nCEO\nCometAPI\nlee@cometapi.com",  # after
        ]
        with mock.patch.object(mod, "_find_row_by_name", return_value=row), \
             mock.patch.object(mod, "_close_apollo_modals", return_value=0), \
             mock.patch.object(mod, "_accept_tos_if_present", return_value=False), \
             mock.patch("time.sleep"):
            out = mod._reveal_email_for_row_sync(mock.MagicMock(), "Lee Sonic")
        self.assertTrue(out["clicked"])
        self.assertEqual(out["email"], "lee@cometapi.com")
        btn.click.assert_called_once()

    def test_click_then_no_email_on_file(self):
        row, btn = _mock_row("Lee Sonic\nCEO\nCometAPI\nAccess email", button=True)
        row.inner_text.side_effect = [
            "Lee Sonic\nCEO\nCometAPI\nAccess email",
            "Lee Sonic\nCEO\nCometAPI\nNo email\nRequest phone number",
        ]
        with mock.patch.object(mod, "_find_row_by_name", return_value=row), \
             mock.patch.object(mod, "_close_apollo_modals", return_value=0), \
             mock.patch.object(mod, "_accept_tos_if_present", return_value=False), \
             mock.patch("time.sleep"):
            out = mod._reveal_email_for_row_sync(mock.MagicMock(), "Lee Sonic")
        self.assertTrue(out["clicked"])
        self.assertIsNone(out["email"])
        self.assertTrue(out["no_email_on_file"])
        btn.click.assert_called_once()

    def test_find_ceo_email_uses_apollo_result_over_pattern(self):
        candidate = {"name": "Lee Sonic", "title": "CEO", "linkedin_url": "https://linkedin.com/in/x"}
        with mock.patch.object(mod, "_ensure_ready_sync", return_value={"ready": True}), \
             mock.patch.object(mod, "_find_people_web_scrape_sync",
                               return_value={"people": [candidate]}), \
             mock.patch.object(mod, "_reveal_apollo_email_sync",
                               return_value={"email": "lee@cometapi.com", "clicked": True}):
            out = mod._find_ceo_email_sync("cometapi.com", "CometAPI", None)
        self.assertEqual(out["email"], "lee@cometapi.com")
        self.assertEqual(out["email_source"], "apollo_access_email")

    def test_find_ceo_email_reports_no_email_on_file_without_fabricating(self):
        candidate = {"name": "Lee Sonic", "title": "CEO", "linkedin_url": None}
        with mock.patch.object(mod, "_ensure_ready_sync", return_value={"ready": True}), \
             mock.patch.object(mod, "_find_people_web_scrape_sync",
                               return_value={"people": [candidate]}), \
             mock.patch.object(mod, "_reveal_apollo_email_sync",
                               return_value={"email": None, "no_email_on_file": True, "clicked": True}):
            out = mod._find_ceo_email_sync("cometapi.com", "CometAPI", None)
        self.assertIsNone(out["email"])
        self.assertIsNone(out["email_source"])
        self.assertIn("No email", out["hint"])

    def test_find_ceo_email_falls_back_to_web_search_when_apollo_empty(self):
        with mock.patch.object(mod, "_ensure_ready_sync", return_value={"ready": True}), \
             mock.patch.object(mod, "_find_people_web_scrape_sync", return_value={"people": []}), \
             mock.patch.object(mod, "_web_find_ceo_name_sync",
                               return_value={"name": "Jane Doe", "source": "duckduckgo_web_search"}), \
             mock.patch.object(mod, "_reveal_apollo_email_sync",
                               return_value={"email": None, "no_email_on_file": True, "clicked": True}):
            out = mod._find_ceo_email_sync("example.com", "Example", None)
        self.assertEqual(out["candidate"]["name"], "Jane Doe")
        self.assertEqual(out["name_source"], "web_search_fallback")

    def test_reveal_caps_at_one_person_per_call(self):
        """_reveal_apollo_email_sync must target exactly one named person, never bulk-reveal."""
        import inspect
        sig = inspect.signature(mod._reveal_apollo_email_sync)
        self.assertIn("person_name", sig.parameters)
        self.assertNotIn("limit", sig.parameters)


class NamedPersonSearchTests(unittest.TestCase):
    def _mock_page(self, found_target=True, click_navigates=True):
        page = mock.MagicMock()
        box = mock.MagicMock()
        placeholder_locator = mock.MagicMock()
        placeholder_locator.first = box
        page.get_by_placeholder.return_value = placeholder_locator
        page.inner_text.return_value = "John Werner Managing Director Link Ventures"

        page.evaluate.return_value = (
            {"x": 100, "y": 200, "text": "John WernerManaging Director, Link Ventures"}
            if found_target else None
        )

        urls = ["https://app.apollo.io/#/people"]
        urls.append("https://app.apollo.io/#/contacts/abc123" if click_navigates else urls[0])
        page._idx = 0

        def get_url():
            return urls[min(page._idx, len(urls) - 1)]

        def mouse_click(*a, **k):
            page._idx = min(page._idx + 1, len(urls) - 1)

        page.mouse.click.side_effect = mouse_click
        type(page).url = mock.PropertyMock(side_effect=get_url)
        return page

    def test_search_person_profile_finds_company_hint_result(self):
        page = self._mock_page()
        with mock.patch.object(mod, "_close_apollo_modals", return_value=0), \
             mock.patch("time.sleep"):
            out = mod._search_person_profile_url_sync(page, "John Werner", "Link Ventures")
        self.assertTrue(out["ok"])
        self.assertIn("/contacts/", out["url"])
        page.mouse.click.assert_called_once_with(100, 200)

    def test_search_person_profile_no_match_found(self):
        page = self._mock_page(found_target=False)
        with mock.patch.object(mod, "_close_apollo_modals", return_value=0), \
             mock.patch("time.sleep"):
            out = mod._search_person_profile_url_sync(page, "Nobody Real", "Nowhere Inc")
        self.assertFalse(out["ok"])
        self.assertIn("no matching", out["error"])

    def test_search_person_profile_reports_no_navigation(self):
        page = self._mock_page(click_navigates=False)
        with mock.patch.object(mod, "_close_apollo_modals", return_value=0), \
             mock.patch("time.sleep"):
            out = mod._search_person_profile_url_sync(page, "Someone", "Some Co")
        self.assertFalse(out["ok"])
        self.assertIn("did not navigate", out["error"])

    def test_reveal_email_on_profile_finds_existing_email(self):
        page = mock.MagicMock()
        page.inner_text.return_value = "Contact information\njwerner@mit.edu\nPrimary"
        with mock.patch.object(mod, "_close_apollo_modals", return_value=0):
            out = mod._reveal_email_on_profile_sync(page)
        self.assertEqual(out["email"], "jwerner@mit.edu")
        self.assertFalse(out["clicked"])

    def test_reveal_email_on_profile_clicks_and_reveals(self):
        page = mock.MagicMock()
        page.inner_text.side_effect = [
            "Contact information\n****@****.com\nAccess email",
            "Contact information\njwerner@mit.edu\nPrimary",
        ]
        btn = mock.MagicMock()
        btn.is_visible.return_value = True
        locator_chain = mock.MagicMock()
        locator_chain.first = btn
        text_result = mock.MagicMock()
        text_result.locator.return_value = locator_chain
        page.get_by_text.return_value.first = text_result
        with mock.patch.object(mod, "_close_apollo_modals", return_value=0), \
             mock.patch.object(mod, "_accept_tos_if_present", return_value=False), \
             mock.patch("time.sleep"):
            out = mod._reveal_email_on_profile_sync(page)
        self.assertTrue(out["clicked"])
        self.assertEqual(out["email"], "jwerner@mit.edu")
        btn.click.assert_called_once()

    def test_find_person_email_tool_labels_apollo_source(self):
        with mock.patch.object(mod, "_ensure_ready_sync", return_value={"ready": True}), \
             mock.patch.object(mod, "_find_person_email_sync",
                               return_value={"email": "jwerner@mit.edu",
                                             "email_source": "apollo_access_email",
                                             "path": "apollo_app",
                                             "profile_url": "https://app.apollo.io/#/people/x",
                                             "apollo_status": "ok"}):
            out = mod.find_person_email("John Werner", company="Link Ventures")
        self.assertEqual(out["email"], "jwerner@mit.edu")
        self.assertEqual(out["email_source"], "apollo_access_email")
        self.assertEqual(out["path"], "apollo_app")

    def test_find_person_email_requires_name(self):
        out = mod.find_person_email("")
        self.assertIn("error", out)


class PersonMatchingTests(unittest.TestCase):
    def test_score_linkedin_exact_match_is_decisive(self):
        candidates = [
            {"name": "Ramin Hasani", "title": "CEO", "linkedin_url": "https://linkedin.com/in/wrong"},
            {"name": "Ramin Hasani", "title": "CEO", "linkedin_url": "https://linkedin.com/in/ramin-hasani"},
        ]
        out = mod._score_person_candidates(
            candidates,
            "Ramin Hasani",
            company="Liquid AI",
            linkedin_url="https://linkedin.com/in/ramin-hasani",
        )
        self.assertEqual(out["confidence"], "decisive")
        self.assertEqual(out["ranked"][0]["linkedin_url"], "https://linkedin.com/in/ramin-hasani")

    def test_score_company_title_outranks_name_only(self):
        candidates = [
            {"name": "John Werner", "title": "Engineer", "linkedin_url": None, "company_hint": "WernerCo"},
            {"name": "John Werner", "title": "Managing Director", "linkedin_url": None, "company_hint": "Link Ventures"},
        ]
        out = mod._score_person_candidates(
            candidates,
            "John Werner",
            company="Link Ventures",
            title="Managing Director",
        )
        self.assertGreater(out["ranked"][0]["match_score"], out["ranked"][1]["match_score"])
        self.assertIn("Link", out["ranked"][0].get("company_hint", ""))

    def test_score_same_name_different_company_is_ambiguous(self):
        candidates = [
            {"name": "Ramin Hasani", "title": "Co-founder & CEO", "linkedin_url": None, "company_hint": "Liquid AI"},
            {"name": "Ramin Hasani", "title": "Co-founder", "linkedin_url": None, "company_hint": "Liquid AI"},
        ]
        out = mod._score_person_candidates(candidates, "Ramin Hasani", company="Liquid AI")
        self.assertEqual(out["confidence"], "ambiguous")

    def test_llm_pick_valid_json(self):
        candidates = [
            {"name": "A", "title": "CEO", "linkedin_url": None},
            {"name": "B", "title": "CTO", "linkedin_url": None},
        ]
        target = {"name": "A", "company": "Acme", "title": "CEO", "location": "", "linkedin_url": ""}
        with mock.patch("mcp_base.llm.llm_chat",
                        return_value=('{"best_index": 0, "confidence": "high", "reason": "CEO at Acme"}', "nvidia")):
            out = mod._llm_pick_best_candidate(target, candidates)
        self.assertIsNotNone(out)
        self.assertEqual(out["best_index"], 0)
        self.assertEqual(out["confidence"], "high")
        self.assertEqual(out["llm_provider"], "nvidia")

    def test_llm_pick_malformed_returns_none(self):
        candidates = [{"name": "A", "title": "CEO", "linkedin_url": None}]
        target = {"name": "A", "company": "Acme", "title": "CEO", "location": "", "linkedin_url": ""}
        with mock.patch("mcp_base.llm.llm_chat", return_value=("not json at all", "cline")):
            out = mod._llm_pick_best_candidate(target, candidates)
        self.assertIsNone(out)

    def test_llm_pick_network_failure_returns_none(self):
        candidates = [{"name": "A", "title": "CEO", "linkedin_url": None}]
        target = {"name": "A", "company": "Acme", "title": "CEO", "location": "", "linkedin_url": ""}
        with mock.patch("mcp_base.llm.llm_chat", return_value=(None, None)):
            out = mod._llm_pick_best_candidate(target, candidates)
        self.assertIsNone(out)

    def test_resolve_person_ambiguous_uses_llm(self):
        page = mock.MagicMock()
        candidates = [
            {"name": "Ramin Hasani", "title": "CEO", "linkedin_url": None,
             "company_hint": "Liquid AI", "search_url": "https://app.apollo.io/#/people?q=1"},
            {"name": "Ramin Hasani", "title": "Director", "linkedin_url": None,
             "company_hint": "Kelly", "search_url": "https://app.apollo.io/#/people?q=1"},
        ]
        scoring = {
            "ranked": [
                {**candidates[0], "match_score": 70, "score_reasons": ["name_match"]},
                {**candidates[1], "match_score": 68, "score_reasons": ["name_match"]},
            ],
            "confidence": "ambiguous",
            "reason": "close scores",
        }
        nav_ok = {"ok": True, "url": "https://app.apollo.io/#/people/abc", "matched_text": "Ramin Hasani"}
        reveal_ok = {"email": "ramin@liquid.ai", "clicked": True, "no_email_on_file": False}
        with mock.patch.object(mod, "_search_person_candidates_sync", return_value=candidates), \
             mock.patch.object(mod, "_score_person_candidates", return_value=scoring), \
             mock.patch.object(mod, "_llm_pick_best_candidate",
                               return_value={"best_index": 0, "confidence": "high",
                                             "reason": "Liquid AI CEO", "llm_provider": "nvidia"}), \
             mock.patch.object(mod, "_navigate_to_candidate_profile_sync", return_value=nav_ok), \
             mock.patch.object(mod, "_profile_matches_hint", return_value=True), \
             mock.patch.object(mod, "_reveal_email_on_profile_sync", return_value=reveal_ok), \
             mock.patch.object(mod, "_search_person_profile_url_sync") as quick:
            out = mod._resolve_person_sync(page, "Ramin Hasani", company="Liquid AI", title="CEO")
        quick.assert_not_called()
        self.assertEqual(out["email"], "ramin@liquid.ai")
        self.assertIn("llm (nvidia):", out["match_reason"])
        self.assertEqual(out["llm_provider"], "nvidia")

    def test_resolve_person_llm_unavailable_falls_back_to_top_scored(self):
        page = mock.MagicMock()
        candidates = [
            {"name": "Ramin Hasani", "title": "CEO", "linkedin_url": None,
             "company_hint": "Liquid AI", "search_url": "https://app.apollo.io/#/people?q=1"},
            {"name": "Ramin Hasani", "title": "Director", "linkedin_url": None,
             "company_hint": "Kelly", "search_url": "https://app.apollo.io/#/people?q=1"},
        ]
        scoring = {
            "ranked": [
                {**candidates[0], "match_score": 70, "score_reasons": ["name_match", "company_match"]},
                {**candidates[1], "match_score": 68, "score_reasons": ["name_match"]},
            ],
            "confidence": "ambiguous",
            "reason": "close scores",
        }
        nav_ok = {"ok": True, "url": "https://app.apollo.io/#/people/abc", "matched_text": "Ramin Hasani"}
        reveal_ok = {"email": "ramin@liquid.ai", "clicked": True, "no_email_on_file": False}
        with mock.patch.object(mod, "_search_person_candidates_sync", return_value=candidates), \
             mock.patch.object(mod, "_score_person_candidates", return_value=scoring), \
             mock.patch.object(mod, "_llm_pick_best_candidate", return_value=None), \
             mock.patch.object(mod, "_navigate_to_candidate_profile_sync", return_value=nav_ok), \
             mock.patch.object(mod, "_profile_matches_hint", return_value=True), \
             mock.patch.object(mod, "_reveal_email_on_profile_sync", return_value=reveal_ok):
            out = mod._resolve_person_sync(page, "Ramin Hasani", company="Liquid AI")
        self.assertEqual(out["email"], "ramin@liquid.ai")
        self.assertEqual(out["candidate"]["company_hint"], "Liquid AI")

    def test_find_person_email_passes_title_and_linkedin(self):
        with mock.patch.object(mod, "_ensure_ready_sync", return_value={"ready": True}), \
             mock.patch.object(mod, "_find_person_email_sync",
                               return_value={"email": "ramin@liquid.ai",
                                             "email_source": "apollo_access_email",
                                             "path": "apollo_app",
                                             "apollo_status": "ok"}) as sync:
            out = mod.find_person_email(
                "Ramin Hasani",
                company="Liquid AI",
                title="CEO",
                linkedin_url="https://linkedin.com/in/ramin-hasani",
            )
        sync.assert_called_once()
        args = sync.call_args[0]
        self.assertEqual(args[0], "Ramin Hasani")
        self.assertEqual(args[1], "Liquid AI")
        self.assertEqual(args[2], "CEO")
        self.assertEqual(args[4], "https://linkedin.com/in/ramin-hasani")
        self.assertEqual(out["email"], "ramin@liquid.ai")
        self.assertEqual(out["email_source"], "apollo_access_email")

    def test_resolve_person_rejects_wrong_company(self):
        page = mock.MagicMock()
        page.inner_text.return_value = "Mathias Lechner\nConsultant\nzeb consulting"
        nav_ok = {"ok": True, "url": "https://app.apollo.io/#/contacts/wrong", "matched_text": "Mathias Lechner"}
        with mock.patch.object(mod, "_search_person_candidates_sync", return_value=[]), \
             mock.patch.object(mod, "_open_apollo_profile_by_linkedin_sync", return_value={"ok": False}), \
             mock.patch.object(mod, "_search_person_profile_url_sync", return_value=nav_ok), \
             mock.patch.object(mod, "_profile_matches_hint", return_value=False):
            out = mod._resolve_person_sync(page, "Mathias Lechner", company="Liquid AI")
        self.assertIsNone(out.get("email"))
        self.assertEqual(out["apollo_status"], "wrong_person_rejected")

    def test_find_person_email_mathias_fallback_pattern_guess(self):
        with mock.patch.object(mod, "_ensure_ready_sync", return_value={"ready": True}), \
             mock.patch.object(mod, "_find_person_email_sync",
                               return_value={"email": "mlechner@mit.edu",
                                             "email_source": "pattern_guess",
                                             "path": "fallback",
                                             "apollo_status": "not_on_file",
                                             "email_hint": "Pattern guess on academic domain mit.edu"}):
            out = mod.find_person_email(
                "Mathias Lechner", company="Liquid AI", title="Co-founder & CTO",
                allow_fallback=True,
            )
        self.assertEqual(out["email"], "mlechner@mit.edu")
        self.assertEqual(out["email_source"], "pattern_guess")
        self.assertEqual(out["apollo_status"], "not_on_file")

    def test_mathias_regression_end_to_end_mocked(self):
        with mock.patch.object(mod, "_ensure_ready_sync", return_value={"ready": True}), \
             mock.patch.object(mod, "_find_person_email_sync",
                               return_value={"email": "mlechner@mit.edu",
                                             "email_source": "apollo_extension_linkedin",
                                             "path": "linkedin_extension",
                                             "linkedin_url": "https://www.linkedin.com/in/mlech26l",
                                             "apollo_status": "ok"}):
            out = mod.find_person_email("Mathias Lechner", company="Liquid AI", title="Co-founder & CTO",
                                        domain="liquid.ai",
                                        linkedin_url="https://www.linkedin.com/in/mlech26l")
        self.assertEqual(out["email"], "mlechner@mit.edu")
        self.assertEqual(out["email_source"], "apollo_extension_linkedin")
        self.assertIn("mlech26l", out["linkedin_url"])

    def test_academic_email_fallback_prefers_mit(self):
        identity = {
            "title": "Co-founder & CTO | Researcher @ MIT",
            "evidence": [{"snippet": "Research Affiliate at MIT CSAIL"}],
        }
        fake_ef = mock.MagicMock()
        fake_ef._web_search_emails.return_value = [
            {"email": "mlechner@mit.edu", "source_url": "https://example.com", "weight": 5},
        ]
        with mock.patch.object(mod, "_email_finder_mod", return_value=fake_ef):
            out = mod._resolve_email_fallback_sync("Mathias Lechner", "Liquid AI", "liquid.ai", identity)
        self.assertEqual(out["email"], "mlechner@mit.edu")
        self.assertEqual(out["email_source"], "web_published")

    def test_profile_matches_hint_dual_affiliation_mit(self):
        page = mock.MagicMock()
        page.inner_text.return_value = "Mathias Lechner\nResearch Affiliate\nMIT CSAIL"
        self.assertTrue(mod._profile_matches_hint(page, "Liquid AI", "Researcher @ MIT"))

    def test_resolve_person_uses_identity_linkedin(self):
        page = mock.MagicMock()
        identity = {
            "company": "Liquid AI", "title": "Co-founder & CTO",
            "linkedin_url": "https://linkedin.com/in/mlech26l",
        }
        nav_ok = {"ok": True, "url": "https://app.apollo.io/#/contacts/abc", "matched_text": "Mathias Lechner"}
        reveal_ok = {"email": None, "no_email_on_file": True, "clicked": True}
        with mock.patch.object(mod, "_search_person_candidates_sync", return_value=[]), \
             mock.patch.object(mod, "_open_apollo_profile_by_linkedin_sync", return_value=nav_ok) as li_nav, \
             mock.patch.object(mod, "_profile_matches_hint", return_value=True), \
             mock.patch.object(mod, "_reveal_email_on_profile_sync", return_value=reveal_ok):
            out = mod._resolve_person_sync(
                page, "Mathias Lechner", company="Liquid AI", title="Co-founder & CTO",
                identity=identity,
            )
        li_nav.assert_called_once()
        call_args = li_nav.call_args[0]
        self.assertIn("mlech26l", call_args[1])
        self.assertEqual(out["apollo_status"], "not_on_file")

    def test_apollo_lookalikes_blocked_detected(self):
        page = mock.MagicMock()
        page.url = "https://app.apollo.io/#/people?recommendationConfigId=abc"
        page.inner_text.return_value = "Cannot access people lookalikes filter on free plan"
        self.assertTrue(mod._apollo_lookalikes_blocked(page))

    def test_apollo_lookalikes_not_triggered_by_trial_upsell(self):
        page = mock.MagicMock()
        page.url = "https://app.apollo.io/#/people"
        page.inner_text.return_value = "Start a trial to unlock more credits on your free plan"
        self.assertFalse(mod._apollo_lookalikes_blocked(page))

    def test_apollo_lookalikes_not_triggered_by_sidebar_filter(self):
        page = mock.MagicMock()
        page.url = "https://app.apollo.io/#/people?page=1&perPage=25"
        page.inner_text.return_value = "job titles\npeople lookalikes\ncompany\nlocation"
        self.assertFalse(mod._apollo_lookalikes_blocked(page))

    def test_linkedin_url_not_used_as_search_term(self):
        page = mock.MagicMock()
        filled: list[str] = []
        box = mock.MagicMock()
        box.fill.side_effect = lambda t, **k: filled.append(t)
        placeholder = mock.MagicMock()
        placeholder.first = box
        page.get_by_placeholder.return_value = placeholder
        page.evaluate.return_value = None
        page.url = "https://app.apollo.io/#/people"
        page.mouse = mock.MagicMock()
        with mock.patch.object(mod, "_dismiss_apollo_errors", return_value=0), \
             mock.patch.object(mod, "_apollo_lookalikes_blocked", return_value=False), \
             mock.patch.object(mod, "_search_person_profile_url_sync",
                               return_value={"ok": False, "error": "not found"}), \
             mock.patch("time.sleep"):
            mod._open_apollo_profile_by_linkedin_sync(
                page, "https://www.linkedin.com/in/mlech26l/",
                "Mathias Lechner", "Liquid AI", "Co-founder & CTO",
            )
        for term in filled:
            self.assertNotIn("linkedin.com", term.lower())

    def test_resolve_person_returns_lookalikes_blocked(self):
        page = mock.MagicMock()
        with mock.patch.object(mod, "_search_person_candidates_sync", return_value=[]), \
             mock.patch.object(mod, "_open_apollo_profile_by_linkedin_sync",
                               return_value={"ok": False, "apollo_status": "lookalikes_blocked",
                                             "error": "blocked", "hint": mod._LOOKALIKES_BLOCKED_HINT}), \
             mock.patch.object(mod, "_search_person_profile_url_sync",
                               return_value={"ok": False, "apollo_status": "lookalikes_blocked"}):
            out = mod._resolve_person_sync(
                page, "Mathias Lechner", company="Liquid AI",
                linkedin_url="https://www.linkedin.com/in/mlech26l/",
            )
        self.assertEqual(out["apollo_status"], "lookalikes_blocked")

    def test_find_person_email_lookalikes_no_auto_fallback(self):
        with mock.patch.object(mod, "_ensure_ready_sync", return_value={"ready": True}), \
             mock.patch.object(mod, "_find_person_email_sync",
                               return_value={"apollo_status": "lookalikes_blocked",
                                             "hint": mod._LOOKALIKES_BLOCKED_HINT,
                                             "email": None, "path": "apollo_app"}):
            out = mod.find_person_email(
                "Mathias Lechner", company="Liquid AI",
                title="Co-founder & CTO | Researcher @ MIT",
                linkedin_url="https://www.linkedin.com/in/mlech26l/",
            )
        self.assertEqual(out["apollo_status"], "lookalikes_blocked")
        self.assertIsNone(out["email"])
        self.assertIn("lookalike", out["hint"].lower())

    def test_dismiss_error_modal_calls_ok(self):
        page = mock.MagicMock()
        page.evaluate.return_value = 1
        with mock.patch.object(mod, "_close_apollo_modals", return_value=0):
            n = mod._dismiss_apollo_errors(page)
        self.assertEqual(n, 1)
        page.evaluate.assert_called_once()


class EmailFlowTests(unittest.TestCase):
    def test_apollo_simple_person_email_success(self):
        page = mock.MagicMock()
        nav = {"ok": True, "url": "https://app.apollo.io/#/people/abc"}
        reveal = {"email": "ceo@acme.com", "clicked": True}
        with mock.patch.object(mod, "_search_person_profile_url_sync", return_value=nav), \
             mock.patch.object(mod, "_reveal_email_on_profile_sync", return_value=reveal):
            out = mod._apollo_simple_person_email(page, "Jane Doe", "Acme")
        self.assertEqual(out["email"], "ceo@acme.com")
        self.assertEqual(out["email_source"], "apollo_access_email")

    def test_find_person_email_flow_tries_app_before_extension(self):
        page = mock.MagicMock()
        calls: list[str] = []

        def fake_app(p, n, c, t=""):
            calls.append("app")
            return {"ok": False, "apollo_status": "not_found", "error": "miss"}

        def fake_ext(p, li, n=""):
            calls.append("ext")
            return {"ok": True, "email": "x@y.com", "email_source": "apollo_extension_linkedin",
                    "linkedin_url": li}

        with mock.patch.object(mod, "_apollo_simple_person_email", side_effect=fake_app), \
             mock.patch.object(mod, "_apollo_extension_email_on_linkedin", side_effect=fake_ext), \
             mock.patch.object(mod, "_with_background_page",
                               side_effect=lambda work, **kw: work(page, "cdp")), \
             mock.patch.object(mod, "_with_linkedin_extension_page",
                               side_effect=lambda work: work(page, "cdp-reuse")), \
             mock.patch("mcp_base.person_identity.confirm_identity_web",
                        return_value={"linkedin_url": "https://linkedin.com/in/x"}):
            out = mod._find_person_email_flow_sync("Jane", "Acme")
        self.assertEqual(calls, ["app", "ext"])
        self.assertEqual(out["email"], "x@y.com")
        self.assertEqual(out["path"], "linkedin_extension")

    def test_extension_scrapes_visible_email(self):
        ctx = mock.MagicMock()
        ctx.inner_text.return_value = "Contact information\nEmails\nmlechner@mit.edu\nWork"
        self.assertEqual(mod._scrape_extension_email(ctx), "mlechner@mit.edu")

    def test_extension_clicks_access_email_when_hidden(self):
        page = mock.MagicMock()
        frame = mock.MagicMock()
        frame.inner_text.side_effect = [
            "Apollo.io\nContact information\nAccess email",
            "Apollo.io\nContact information\nmlechner@mit.edu",
        ]
        btn = mock.MagicMock()
        btn.is_visible.return_value = True
        frame.get_by_role.return_value.first = btn
        with mock.patch.object(mod, "_normalize_linkedin_profile_url",
                               return_value="https://www.linkedin.com/in/mlech26l/"), \
             mock.patch.object(mod, "_wait_linkedin_profile", return_value=True), \
             mock.patch.object(mod, "wait_for_sidebar_frame", return_value=frame), \
             mock.patch.object(mod, "find_extension_sidebar_frame", return_value=frame), \
             mock.patch.object(mod, "_accept_tos_if_present", return_value=False), \
             mock.patch("time.sleep"):
            out = mod._apollo_extension_email_on_linkedin(
                page, "https://www.linkedin.com/in/mlech26l/", "Mathias Lechner",
            )
        btn.click.assert_called()
        self.assertEqual(out["email"], "mlechner@mit.edu")

    def test_extension_sidebar_not_open_returns_hint(self):
        page = mock.MagicMock()
        with mock.patch.object(mod, "_normalize_linkedin_profile_url",
                               return_value="https://www.linkedin.com/in/mlech26l/"), \
             mock.patch.object(mod, "_wait_linkedin_profile", return_value=True), \
             mock.patch.object(mod, "wait_for_sidebar_frame", return_value=None), \
             mock.patch("time.sleep"):
            out = mod._apollo_extension_email_on_linkedin(
                page, "https://www.linkedin.com/in/mlech26l/", "Mathias Lechner",
            )
        self.assertFalse(out.get("ok"))
        self.assertIn("FAB", out.get("error", ""))

    def test_linkedin_extension_page_never_closes_tab(self):
        with mock.patch.object(mod, "_ensure_ready_sync", return_value={"ready": True}), \
             mock.patch.object(mod, "with_cdp_page", return_value={"ok": True}) as cdp:
            mod._with_linkedin_extension_page(lambda p, b: {"ok": True})
        cdp.assert_called_once()
        self.assertTrue(cdp.call_args[1]["close_on_done"] is False)
        self.assertTrue(cdp.call_args[1]["reuse_tab"])
        self.assertEqual(cdp.call_args[1]["url_hint"], "linkedin.com")

    def test_find_ceo_email_uses_extension_on_app_miss(self):
        candidate = {"name": "Jane CEO", "title": "CEO", "linkedin_url": "https://linkedin.com/in/j"}
        reveal_miss = {"email": None, "no_email_on_file": True}
        with mock.patch.object(mod, "_ensure_ready_sync", return_value={"ready": True}), \
             mock.patch.object(mod, "_find_people_web_scrape_sync",
                               return_value={"people": [candidate]}), \
             mock.patch.object(mod, "_reveal_apollo_email_sync", return_value=reveal_miss), \
             mock.patch.object(mod, "_with_linkedin_extension_page",
                               return_value={"email": "j@acme.com",
                                             "email_source": "apollo_extension_linkedin",
                                             "linkedin_url": "https://linkedin.com/in/j"}):
            out = mod._find_ceo_email_sync("acme.com", "Acme", None)
        self.assertEqual(out["email"], "j@acme.com")
        self.assertEqual(out["path"], "linkedin_extension")

    def test_find_ceo_email_skips_extension_when_app_hits(self):
        candidate = {"name": "Jane CEO", "title": "CEO"}
        with mock.patch.object(mod, "_ensure_ready_sync", return_value={"ready": True}), \
             mock.patch.object(mod, "_find_people_web_scrape_sync",
                               return_value={"people": [candidate]}), \
             mock.patch.object(mod, "_reveal_apollo_email_sync",
                               return_value={"email": "j@acme.com"}), \
             mock.patch.object(mod, "_with_linkedin_extension_page") as ext:
            out = mod._find_ceo_email_sync("acme.com", "Acme", None)
        ext.assert_not_called()
        self.assertEqual(out["email"], "j@acme.com")
        self.assertEqual(out["path"], "apollo_app")

    def test_bulk_find_ceo_email_respects_max_credits(self):
        companies = [{"domain": "a.com", "company": "A"}, {"domain": "b.com", "company": "B"}]
        with mock.patch.object(mod, "_ensure_ready_sync", return_value={"ready": True}), \
             mock.patch.object(mod, "_campaign_contacted", return_value=False), \
             mock.patch.object(mod, "_find_ceo_email_sync",
                               return_value={"email": "x@a.com", "path": "apollo_app"}) as ceo:
            out = mod._bulk_find_ceo_email_sync(companies, max_credits=1)
        self.assertEqual(ceo.call_count, 1)
        self.assertEqual(out["found"], 1)
        self.assertEqual(out["stats"]["skipped"], 1)

    def test_find_person_email_flow_uses_fresh_tab(self):
        with mock.patch.object(mod, "_with_background_page", return_value={"email": "a@b.com"}) as bg:
            mod._find_person_email_flow_sync("Jane", "Acme")
        bg.assert_called_once()
        self.assertFalse(bg.call_args[1]["reuse_tab"])
        self.assertTrue(bg.call_args[1]["prefer_fresh_tab"])

    def test_no_pattern_guess_by_default(self):
        with mock.patch.object(mod, "_ensure_ready_sync", return_value={"ready": True}), \
             mock.patch.object(mod, "_find_person_email_sync",
                               return_value={"email": None, "apollo_status": "not_on_file",
                                             "path": "apollo_app", "error": "not found"}):
            out = mod.find_person_email("Jane", company="Acme")
        self.assertIsNone(out["email"])


if __name__ == "__main__":
    unittest.main()
