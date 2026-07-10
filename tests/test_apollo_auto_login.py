"""Offline tests for Apollo Google auto-login (mocked Playwright page — never hits Google)."""
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
    "apollo_srv", ROOT / "servers" / "apollo" / "server.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["apollo_srv"] = mod
spec.loader.exec_module(mod)


class ApolloAutoLoginTests(unittest.TestCase):
    def test_already_logged_in_skips_flow(self):
        page = mock.MagicMock()
        page.url = "https://app.apollo.io/#/people?finderViewId=1"
        page.content.return_value = "<html>people finder prospect data-cy=nav</html>"
        out = mod._auto_login_google_on_page(page, "user@gmail.com", "secret")
        self.assertTrue(out.get("logged_in"))
        self.assertEqual(out.get("skipped"), "already_logged_in")

    def test_missing_creds_returns_hint(self):
        with mock.patch.object(mod, "_google_creds", return_value=(None, None)):
            out = mod._auto_login_google_sync()
        self.assertFalse(out.get("logged_in"))
        self.assertIn("APOLLO_GOOGLE_EMAIL", out.get("hint", ""))

    def test_google_creds_strip_quotes(self):
        with mock.patch.object(mod, "get_env", side_effect=lambda k, d=None: {
            "APOLLO_GOOGLE_EMAIL": '"user@test.com"',
            "APOLLO_GOOGLE_PASSWORD": "' pass '",
        }.get(k, d)):
            email, password = mod._google_creds()
        self.assertEqual(email, "user@test.com")
        self.assertEqual(password, "pass")

    def test_classify_wrong_password(self):
        page = mock.MagicMock()
        page.inner_text.return_value = "Wrong password. Try again."
        code, hint = mod._classify_google_error(page)
        self.assertEqual(code, "wrong_password")
        self.assertIn("apollo profile", hint.lower())

    def test_type_human_uses_press_sequentially(self):
        page = mock.MagicMock()
        loc = mock.MagicMock()
        loc.is_visible.return_value = True
        page.locator.return_value.first = loc
        ok = mod._type_human(page, ("input[name=Passwd]",), "secret")
        self.assertTrue(ok)
        loc.press_sequentially.assert_called_once()

    def test_ensure_ready_hybrid_skips_auto_when_logged_in(self):
        with mock.patch.object(mod, "_cdp_alive", return_value=True), \
             mock.patch.object(mod, "_login_mode", return_value="hybrid"), \
             mock.patch.object(mod, "_session_check_sync",
                               return_value={"logged_in": True}), \
             mock.patch.object(mod, "_auto_login_google_sync") as login:
            out = mod._ensure_ready_sync()
        login.assert_not_called()
        self.assertTrue(out.get("ready"))

    def test_login_mode_defaults_manual(self):
        with mock.patch.object(mod, "get_env", return_value=None):
            self.assertEqual(mod._login_mode(), "manual")

    def test_ensure_ready_manual_waits(self):
        with mock.patch.object(mod, "_cdp_alive", return_value=True), \
             mock.patch.object(mod, "_login_mode", return_value="manual"), \
             mock.patch.object(mod, "_session_check_sync",
                               return_value={"logged_in": False}), \
             mock.patch.object(mod, "_prep_manual_login_sync",
                               return_value={"ok": True, "clicked_google": True}), \
             mock.patch.object(mod, "_wait_for_manual_login_sync",
                               return_value={"logged_in": True, "waited_seconds": 3}), \
             mock.patch.object(mod, "_auto_login_google_on_page") as auto_page, \
             mock.patch.object(mod, "_type_human") as type_human:
            out = mod._ensure_ready_sync()
        self.assertTrue(out.get("ready"))
        auto_page.assert_not_called()
        type_human.assert_not_called()

    def test_prep_manual_login_clicks_google_on_login_page(self):
        page = mock.MagicMock()
        page.url = "https://app.apollo.io/#/login"
        page.content.return_value = '<html>log in sign in type="password"</html>'

        def work(fn, **kwargs):
            self.assertTrue(kwargs.get("reuse_tab"))
            self.assertFalse(kwargs.get("prefer_fresh_tab"))
            return fn(page, "cdp")

        with mock.patch.object(mod, "_cdp_alive", return_value=True), \
             mock.patch.object(mod, "_with_background_page", side_effect=work), \
             mock.patch.object(mod, "_click_google_sso", return_value=True) as click, \
             mock.patch.object(mod, "_restore_browser_window") as restore:
            out = mod._prep_manual_login_sync()
        click.assert_called_once_with(page)
        restore.assert_called_once()
        self.assertTrue(out.get("clicked_google"))

    def test_company_keyword_from_domain(self):
        self.assertEqual(mod._company_keyword_from_domain("cometapi.com"), "CometAPI")
        self.assertEqual(mod._company_keyword_from_domain("stripe.com"), "Stripe")

    def test_is_login_page_logged_in_shell(self):
        self.assertFalse(mod._is_login_page(
            "https://app.apollo.io/#/people",
            "<html>people finder data-cy=sidebar</html>",
        ))

    def test_is_login_page_root_with_login_form(self):
        self.assertTrue(mod._is_login_page(
            "https://app.apollo.io/#/",
            '<html>log in sign in type="password"</html>',
        ))


if __name__ == "__main__":
    unittest.main()
