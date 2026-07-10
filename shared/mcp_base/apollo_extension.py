"""Apollo Chrome extension side-panel helpers (LinkedIn Path 2)."""
from __future__ import annotations

import re
import time

APOLLO_EXT_ID = "alhgpfoeiimagjlnfekdhkjlkiomcapa"
_SIDEBAR_MARKER = re.compile(r"contact information", re.I)


def _frame_has_sidebar(frame) -> bool:
    try:
        text = frame.inner_text("body")[:8000]
    except Exception:
        return False
    return bool(_SIDEBAR_MARKER.search(text))


def find_extension_sidebar_frame(browser_context):
    """Return the frame inside Apollo's Chrome side-panel that holds the sidebar UI."""
    for page in browser_context.pages:
        url = (page.url or "").lower()
        if APOLLO_EXT_ID not in url or "side-panel" not in url:
            continue
        for frame in page.frames:
            if _frame_has_sidebar(frame):
                return frame
    return None


def wait_for_sidebar_frame(
    browser_context,
    *,
    name_hint: str = "",
    timeout_s: int = 90,
    poll_s: float = 2.0,
):
    """Poll until the extension side-panel shows Contact information (and optional name)."""
    first = (name_hint or "").strip().split()[0].lower() if name_hint else ""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        frame = find_extension_sidebar_frame(browser_context)
        if frame:
            if not first:
                return frame
            try:
                if first in frame.inner_text("body")[:5000].lower():
                    return frame
            except Exception:
                return frame
        time.sleep(poll_s)
    return find_extension_sidebar_frame(browser_context)
