"""Tests for Apollo Chrome extension side-panel helpers."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))

from mcp_base import apollo_extension as ext  # noqa: E402


def _side_panel_context(sidebar_text: str):
    """Mock browser context: side-panel page + inner frame with sidebar text."""
    frame = mock.MagicMock()
    frame.inner_text.return_value = sidebar_text
    shell_frame = mock.MagicMock()
    shell_frame.inner_text.return_value = ""
    side_page = mock.MagicMock()
    side_page.url = (
        "chrome-extension://alhgpfoeiimagjlnfekdhkjlkiomcapa/6whwx_rNdM_side-panelz3zbm.html"
    )
    side_page.frames = [shell_frame, frame]
    li_page = mock.MagicMock()
    li_page.url = "https://www.linkedin.com/in/willahmed/"
    ctx = mock.MagicMock()
    ctx.pages = [li_page, side_page]
    return ctx, frame


class ApolloExtensionFrameTests(unittest.TestCase):
    def test_find_extension_sidebar_frame_returns_inner_frame(self):
        ctx, frame = _side_panel_context(
            "Will Ahmed\nContact information\nAccess email"
        )
        found = ext.find_extension_sidebar_frame(ctx)
        self.assertIs(found, frame)

    def test_find_extension_sidebar_frame_skips_linkedin_only(self):
        li_page = mock.MagicMock()
        li_page.url = "https://www.linkedin.com/in/willahmed/"
        li_page.frames = [mock.MagicMock()]
        li_page.frames[0].inner_text.return_value = "Will Ahmed profile"
        ctx = mock.MagicMock()
        ctx.pages = [li_page]
        self.assertIsNone(ext.find_extension_sidebar_frame(ctx))

    def test_wait_for_sidebar_frame_matches_name(self):
        ctx, frame = _side_panel_context(
            "Will Ahmed\nFounder & CEO\nContact information\nAccess email"
        )
        with mock.patch.object(ext, "find_extension_sidebar_frame", return_value=frame):
            found = ext.wait_for_sidebar_frame(ctx, name_hint="Will Ahmed", timeout_s=1)
        self.assertIs(found, frame)

    def test_wait_for_sidebar_frame_times_out(self):
        ctx = mock.MagicMock()
        ctx.pages = []
        with mock.patch.object(ext, "find_extension_sidebar_frame", return_value=None):
            found = ext.wait_for_sidebar_frame(ctx, timeout_s=0)
        self.assertIsNone(found)


if __name__ == "__main__":
    unittest.main()
