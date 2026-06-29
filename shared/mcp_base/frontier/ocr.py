"""F1 — OCR email-in-image extraction. Optional (pytesseract/opencv); no-op if absent.

Many sites render the email as an image/SVG/canvas to beat regex. This OCRs candidate images
(and Playwright screenshots), preprocesses for contrast, then regex+deobfuscates the text.
"""
from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def available() -> bool:
    try:
        import pytesseract  # noqa: F401
        return True
    except Exception:
        return False


def _preprocess(img_bytes: bytes):
    """Return a contrast-enhanced grayscale image array, or None if opencv/numpy absent."""
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(img_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        # upscale small images, CLAHE contrast, Otsu binarize
        if max(img.shape) < 800:
            img = cv2.resize(img, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        img = clahe.apply(img)
        _, img = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return img
    except Exception:
        return None


def ocr_emails(image_bytes: bytes) -> list[str]:
    """OCR one image's bytes → list of emails found (deobfuscated). [] if OCR unavailable/none."""
    try:
        import pytesseract
    except Exception:
        return []
    text = ""
    img = _preprocess(image_bytes)
    try:
        if img is not None:
            text = pytesseract.image_to_string(img)
        else:
            # fall back to PIL if opencv missing
            from io import BytesIO
            from PIL import Image
            text = pytesseract.image_to_string(Image.open(BytesIO(image_bytes)))
    except Exception:
        return []
    found: list[str] = []
    try:
        from ..email_extract import deobfuscate_text
        cand = deobfuscate_text(text)
    except Exception:
        cand = [m.lower() for m in _EMAIL_RE.findall(text)]
    for e in cand:
        e = e.lower()
        if e not in found:
            found.append(e)
    return found


def harvest_image_emails(page_html: str, base_url: str, max_images: int = 8) -> list[str]:
    """Find <img> sources on a page that look like email images, fetch + OCR them."""
    if not available():
        return []
    import re as _re
    from urllib.parse import urljoin
    from ..fetch import fetch  # noqa: F401
    import httpx

    srcs = _re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', page_html or "", _re.I)
    # prioritize images whose name/alt hints at contact/email
    hinted = [s for s in srcs if _re.search(r"(email|mail|contact)", s, _re.I)]
    ordered = (hinted + [s for s in srcs if s not in hinted])[:max_images]
    out: list[str] = []
    for src in ordered:
        try:
            url = urljoin(base_url, src)
            r = httpx.get(url, timeout=10, follow_redirects=True)
            if r.is_success and r.content:
                for e in ocr_emails(r.content):
                    if e not in out:
                        out.append(e)
        except Exception:
            continue
    return out
