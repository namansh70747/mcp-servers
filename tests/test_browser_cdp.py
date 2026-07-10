"""Tests for browser_cdp pick_cdp_page behavior."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))

from mcp_base.browser_cdp import pick_cdp_page  # noqa: E402


class PickCdpPageTests(unittest.TestCase):
    def test_opens_new_page_when_hint_not_matched(self):
        apollo_page = mock.MagicMock()
        apollo_page.url = "https://app.apollo.io/#/home"
        ctx = mock.MagicMock()
        ctx.pages = [apollo_page]
        new_page = mock.MagicMock()
        ctx.new_page.return_value = new_page
        page, backend = pick_cdp_page(ctx, url_hint="linkedin.com")
        self.assertIs(page, new_page)
        self.assertEqual(backend, "cdp-new")
        ctx.new_page.assert_called_once()

    def test_reuses_matching_hint_tab(self):
        li_page = mock.MagicMock()
        li_page.url = "https://www.linkedin.com/in/test/"
        ctx = mock.MagicMock()
        ctx.pages = [mock.MagicMock(), li_page]
        page, backend = pick_cdp_page(ctx, url_hint="linkedin.com")
        self.assertIs(page, li_page)
        self.assertEqual(backend, "cdp-reuse")


if __name__ == "__main__":
    unittest.main()
