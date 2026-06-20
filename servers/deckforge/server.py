"""deckforge — build career & project-pitch presentations from a prompt, free & offline (python-pptx).

Stateful: create_presentation() returns a deck_id; add_* tools append slides; save_presentation()
writes the .pptx. Templates seed a full slide sequence the agent then fills. build_from_outline()
turns a list of slide specs into a finished deck in one call. export_pdf needs LibreOffice.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt
from mcp_base import data_dir, make_server

mcp = make_server(
    "deckforge",
    instructions=("Build .pptx decks. create_presentation(template=career|project_pitch) -> add_* "
                  "slides -> save_presentation. build_from_outline() does it in one call. "
                  "export_pdf needs LibreOffice."),
)

OUT = data_dir("deckforge") / "output"
OUT.mkdir(parents=True, exist_ok=True)
ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "profile.json"
DECKS: dict[str, Presentation] = {}


def _profile() -> dict:
    """Load the suite's shared profile.json (single source of truth), or {} if absent/invalid."""
    try:
        if PROFILE.exists():
            data = json.loads(PROFILE.read_text())
            return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — never let a malformed profile break a tool
        pass
    return {}


def _contact_line(p: dict) -> str:
    """A single 'email · phone · location · links' contact line from profile.json, or ''."""
    parts = [p.get("email"), p.get("phone"), p.get("location")]
    links = p.get("links") or {}
    if isinstance(links, dict):
        parts += [v for v in links.values() if v]
    return " · ".join(str(x).strip() for x in parts if x and str(x).strip())

THEMES = {
    "dev_dark":   {"bg": "1E1E2E", "title": "89B4FA", "body": "CDD6F4", "accent": "F38BA8", "panel": "313244", "font": "Inter"},
    "dev_light":  {"bg": "FFFFFF", "title": "1A56DB", "body": "1F2937", "accent": "0EA5E9", "panel": "F1F5F9", "font": "Inter"},
    "minimal":    {"bg": "FAFAF9", "title": "111111", "body": "333333", "accent": "777777", "panel": "EDEDEC", "font": "Helvetica"},
    "accent_blue":{"bg": "0B3D91", "title": "FFFFFF", "body": "DCE7FF", "accent": "7AA2F7", "panel": "13499C", "font": "Arial"},
    "corporate":  {"bg": "FFFFFF", "title": "0F4C81", "body": "2B2B2B", "accent": "C9A227", "panel": "EEF3F8", "font": "Calibri"},
    "sunset":     {"bg": "2B1B2F", "title": "FFB86C", "body": "F8E8EE", "accent": "FF6E9C", "panel": "47273F", "font": "Inter"},
    "mono":       {"bg": "111111", "title": "FFFFFF", "body": "C8C8C8", "accent": "9CA3AF", "panel": "1F1F1F", "font": "JetBrains Mono"},
    "forest":     {"bg": "0B1F1A", "title": "A7F3D0", "body": "E7F5EF", "accent": "34D399", "panel": "12302A", "font": "Inter"},
    "midnight":   {"bg": "0D1117", "title": "58A6FF", "body": "C9D1D9", "accent": "F778BA", "panel": "161B22", "font": "Inter"},
}
TEMPLATES = {
    "career": ["title", "About me", "Skills", "Experience", "Projects", "Education", "Contact"],
    "project_pitch": ["title", "Problem", "Solution", "How it works", "Tech stack", "Demo",
                      "Impact", "Ask / Next steps", "Contact"],
    "talk": ["title", "Agenda", "Context", "Key idea", "Deep dive", "Demo", "Takeaways", "Q&A"],
    "case_study": ["title", "Background", "Challenge", "Approach", "Results", "Lessons", "Contact"],
}
CHART_TYPES = {
    "bar": XL_CHART_TYPE.BAR_CLUSTERED,
    "column": XL_CHART_TYPE.COLUMN_CLUSTERED,
    "line": XL_CHART_TYPE.LINE_MARKERS,
    "pie": XL_CHART_TYPE.PIE,
    "area": XL_CHART_TYPE.AREA,
    "doughnut": XL_CHART_TYPE.DOUGHNUT,
}
_deck_theme: dict[str, str] = {}
_deck_slides: dict[str, list[dict]] = {}


def _safe_out(filename: str, default: str, suffix: str = ".pptx") -> Path:
    """Resolve a user-supplied filename to a path strictly inside OUT.

    Strips any directory components (prevents traversal/absolute paths) and
    forces the expected suffix.
    """
    name = Path(str(filename or default)).name  # drop any path separators / parents
    if not name or name in (".", ".."):
        name = default
    if not name.endswith(suffix):
        name = name + suffix
    path = (OUT / name).resolve()
    if OUT.resolve() not in path.parents and path != OUT.resolve():
        raise RuntimeError("invalid filename")
    return path


def _rgb(hexstr: str) -> RGBColor:
    return RGBColor(int(hexstr[0:2], 16), int(hexstr[2:4], 16), int(hexstr[4:6], 16))


def _bg(slide, prs, theme):
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = _rgb(THEMES[theme]["bg"])


def _style_title(tf, theme, size=40):
    for p in tf.paragraphs:
        p.font.size = Pt(size)
        p.font.bold = True
        p.font.color.rgb = _rgb(THEMES[theme]["title"])
        p.font.name = THEMES[theme]["font"]


def _style_body(tf, theme, size=20, color=None):
    for p in tf.paragraphs:
        p.font.size = Pt(size)
        p.font.color.rgb = _rgb(color or THEMES[theme]["body"])
        p.font.name = THEMES[theme]["font"]


def _panel(slide, theme, x, y, w, h, color_key="panel"):
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(THEMES[theme][color_key])
    shape.line.fill.background()
    shape.shadow.inherit = False
    return shape


def _deck(deck_id):
    if deck_id not in DECKS:
        raise RuntimeError(f"unknown deck_id {deck_id} (create_presentation first)")
    return DECKS[deck_id], _deck_theme.get(deck_id, "dev_dark")


def _record(deck_id, kind, title):
    _deck_slides.setdefault(deck_id, []).append({"type": kind, "title": title})


def _new_slide(prs, theme):
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
    _bg(slide, prs, theme)
    return slide


def _heading(slide, theme, title, size=32):
    t = slide.shapes.add_textbox(Inches(0.8), Inches(0.5), Inches(11.7), Inches(1))
    t.text_frame.text = title
    _style_title(t.text_frame, theme, size)
    return t


def _set_notes(slide, notes: str):
    if notes:
        slide.notes_slide.notes_text_frame.text = notes


@mcp.tool
def create_presentation(title: str = "", template: str = "", theme: str = "dev_dark") -> dict:
    """Start a deck. template ∈ {career, project_pitch, talk, case_study, ''}. Returns a deck_id for subsequent calls."""
    if theme not in THEMES:
        theme = "dev_dark"
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    did = secrets.token_hex(4)
    DECKS[did] = prs
    _deck_theme[did] = theme
    _deck_slides[did] = []
    # Auto-fill name/headline/contact from profile.json when building a template deck
    # and the caller didn't supply an explicit title (kept fully backwards-compatible:
    # with no profile.json present this is identical to the previous behavior).
    prof = _profile() if template in TEMPLATES else {}
    if title or template:
        slide_title = title or prof.get("name") or template.replace("_", " ").title()
        subtitle = "" if title else (prof.get("headline") or "")
        add_title_slide(did, slide_title, subtitle)
    if template in TEMPLATES:
        contact = _contact_line(prof)
        for sec in TEMPLATES[template][1:]:
            if sec.lower().startswith("contact") and contact:
                add_bullet_slide(did, sec, [contact])
            else:
                add_bullet_slide(did, sec, ["…"])
    return {"deck_id": did, "slides": len(prs.slides), "template": template, "theme": theme}


@mcp.tool
def add_title_slide(deck_id: str, title: str, subtitle: str = "", notes: str = "") -> dict:
    """Add a title slide (optional speaker notes)."""
    prs, theme = _deck(deck_id)
    slide = _new_slide(prs, theme)
    box = slide.shapes.add_textbox(Inches(1), Inches(2.6), Inches(11.3), Inches(2))
    box.text_frame.text = title
    _style_title(box.text_frame, theme, 48)
    if subtitle:
        sb = slide.shapes.add_textbox(Inches(1), Inches(4.4), Inches(11.3), Inches(1))
        sb.text_frame.text = subtitle
        _style_body(sb.text_frame, theme, 24)
    _set_notes(slide, notes)
    _record(deck_id, "title", title)
    return {"deck_id": deck_id, "slide_index": len(prs.slides) - 1}


@mcp.tool
def add_bullet_slide(deck_id: str, title: str, bullets: list[str], levels: list[int] | None = None,
                     notes: str = "") -> dict:
    """Add a slide with a title and bullet points (optional per-bullet indent levels, speaker notes)."""
    prs, theme = _deck(deck_id)
    slide = _new_slide(prs, theme)
    _heading(slide, theme, title)
    body = slide.shapes.add_textbox(Inches(0.9), Inches(1.7), Inches(11.5), Inches(5.2))
    tf = body.text_frame
    tf.word_wrap = True
    for i, b in enumerate(bullets):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = f"• {b}"
        if levels and i < len(levels):
            p.level = max(0, min(4, levels[i]))
    _style_body(tf, theme, 20)
    _set_notes(slide, notes)
    _record(deck_id, "bullet", title)
    return {"deck_id": deck_id, "slide_index": len(prs.slides) - 1}


@mcp.tool
def add_two_column_slide(deck_id: str, title: str, left: list[str], right: list[str],
                         notes: str = "") -> dict:
    """Add a two-column slide (e.g. skills vs experience)."""
    prs, theme = _deck(deck_id)
    slide = _new_slide(prs, theme)
    _heading(slide, theme, title)
    for col, items, x in ((0, left, 0.9), (1, right, 7.0)):
        box = slide.shapes.add_textbox(Inches(x), Inches(1.7), Inches(5.5), Inches(5.2))
        tf = box.text_frame
        tf.word_wrap = True
        for i, it in enumerate(items):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.text = f"• {it}"
        _style_body(tf, theme, 18)
    _set_notes(slide, notes)
    _record(deck_id, "two_column", title)
    return {"deck_id": deck_id, "slide_index": len(prs.slides) - 1}


@mcp.tool
def add_comparison_slide(deck_id: str, title: str, left_title: str, left: list[str],
                         right_title: str, right: list[str], notes: str = "") -> dict:
    """Add a comparison slide: two titled, panelled columns (e.g. Before vs After, Us vs Them)."""
    prs, theme = _deck(deck_id)
    slide = _new_slide(prs, theme)
    _heading(slide, theme, title)
    for (sub, items, x) in ((left_title, left, 0.8), (right_title, right, 6.95)):
        _panel(slide, theme, x, 1.7, 5.55, 5.2)
        h = slide.shapes.add_textbox(Inches(x + 0.25), Inches(1.85), Inches(5.05), Inches(0.7))
        h.text_frame.text = sub
        _style_title(h.text_frame, theme, 22)
        box = slide.shapes.add_textbox(Inches(x + 0.25), Inches(2.65), Inches(5.05), Inches(4.1))
        tf = box.text_frame
        tf.word_wrap = True
        for i, it in enumerate(items):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.text = f"• {it}"
        _style_body(tf, theme, 17)
    _set_notes(slide, notes)
    _record(deck_id, "comparison", title)
    return {"deck_id": deck_id, "slide_index": len(prs.slides) - 1}


@mcp.tool
def add_chart_slide(deck_id: str, title: str, categories: list[str], series: list[dict],
                    chart_type: str = "bar", notes: str = "") -> dict:
    """Add a native chart slide. series=[{name, values:[..]}], chart_type ∈ {bar,column,line,pie,area,doughnut}.
    Each series' values must align with categories. Charts are real, editable pptx charts."""
    prs, theme = _deck(deck_id)
    ct = CHART_TYPES.get(chart_type)
    if ct is None:
        return {"error": f"unknown chart_type {chart_type}; choose from {sorted(CHART_TYPES)}"}
    if not categories or not series:
        return {"error": "categories and series are required"}
    if not isinstance(categories, list):
        return {"error": "categories must be a list"}
    if not isinstance(series, list):
        return {"error": "series must be a list of {name, values}"}
    ncat = len(categories)
    parsed = []
    for s in series:
        if not isinstance(s, dict):
            return {"error": "each series must be an object with name/values"}
        raw = s.get("values", [])
        if not isinstance(raw, list):
            return {"error": f"series '{s.get('name', '?')}' values must be a list"}
        try:
            vals = [float(v) for v in raw]
        except (TypeError, ValueError):
            return {"error": f"series '{s.get('name', '?')}' has non-numeric values"}
        if len(vals) != ncat:
            return {"error": (f"series '{s.get('name', '?')}' has {len(vals)} values "
                              f"but there are {ncat} categories; they must match")}
        parsed.append((s.get("name", "Series"), vals))
    slide = _new_slide(prs, theme)
    _heading(slide, theme, title)
    data = CategoryChartData()
    data.categories = categories
    for name, vals in parsed:
        data.add_series(name, vals)
    gframe = slide.shapes.add_chart(ct, Inches(1.0), Inches(1.7), Inches(11.3), Inches(5.2), data)
    chart = gframe.chart
    chart.has_title = False
    if chart_type in ("pie", "doughnut") or len(series) > 1:
        chart.has_legend = True
        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
        chart.legend.include_in_layout = False
    _set_notes(slide, notes)
    _record(deck_id, "chart", title)
    return {"deck_id": deck_id, "slide_index": len(prs.slides) - 1, "chart_type": chart_type}


@mcp.tool
def add_quote_slide(deck_id: str, quote: str, attribution: str = "", notes: str = "") -> dict:
    """Add a large centered pull-quote slide with optional attribution."""
    prs, theme = _deck(deck_id)
    slide = _new_slide(prs, theme)
    box = slide.shapes.add_textbox(Inches(1.2), Inches(2.4), Inches(10.9), Inches(2.5))
    tf = box.text_frame
    tf.word_wrap = True
    tf.text = f"“{quote}”"
    for p in tf.paragraphs:
        p.alignment = PP_ALIGN.CENTER
    _style_title(tf, theme, 34)
    if attribution:
        ab = slide.shapes.add_textbox(Inches(1.2), Inches(5.0), Inches(10.9), Inches(0.8))
        ab.text_frame.text = f"— {attribution}"
        for p in ab.text_frame.paragraphs:
            p.alignment = PP_ALIGN.CENTER
        _style_body(ab.text_frame, theme, 20, color=THEMES[theme]["accent"])
    _set_notes(slide, notes)
    _record(deck_id, "quote", quote[:40])
    return {"deck_id": deck_id, "slide_index": len(prs.slides) - 1}


@mcp.tool
def add_code_slide(deck_id: str, title: str, code: str, language: str = "", notes: str = "") -> dict:
    """Add a slide with a monospace code block on a panel (whitespace preserved, no wrap)."""
    prs, theme = _deck(deck_id)
    slide = _new_slide(prs, theme)
    if title:
        _heading(slide, theme, f"{title}  ⟨{language}⟩" if language else title, 28)
    top = 1.6 if title else 0.7
    _panel(slide, theme, 0.7, top, 11.9, 7.5 - top - 0.5, "panel")
    box = slide.shapes.add_textbox(Inches(1.0), Inches(top + 0.2), Inches(11.3), Inches(7.5 - top - 0.9))
    tf = box.text_frame
    tf.word_wrap = False
    lines = code.split("\n")
    for i, ln in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = ln if ln else " "
        p.font.size = Pt(14)
        p.font.name = "Menlo"
        p.font.color.rgb = _rgb(THEMES[theme]["body"])
    _set_notes(slide, notes)
    _record(deck_id, "code", title)
    return {"deck_id": deck_id, "slide_index": len(prs.slides) - 1}


@mcp.tool
def add_agenda_slide(deck_id: str, items: list[str], title: str = "Agenda", notes: str = "") -> dict:
    """Add a numbered agenda / outline slide."""
    prs, theme = _deck(deck_id)
    slide = _new_slide(prs, theme)
    _heading(slide, theme, title)
    box = slide.shapes.add_textbox(Inches(1.0), Inches(1.8), Inches(11.3), Inches(5.2))
    tf = box.text_frame
    tf.word_wrap = True
    for i, it in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = f"{i + 1}.  {it}"
        p.space_after = Pt(10)
    _style_body(tf, theme, 24)
    _set_notes(slide, notes)
    _record(deck_id, "agenda", title)
    return {"deck_id": deck_id, "slide_index": len(prs.slides) - 1}


@mcp.tool
def add_timeline_slide(deck_id: str, title: str, milestones: list[dict], notes: str = "") -> dict:
    """Add a horizontal timeline. milestones=[{when, what}] rendered as connected panels."""
    prs, theme = _deck(deck_id)
    slide = _new_slide(prs, theme)
    _heading(slide, theme, title)
    ms = milestones[:5] if milestones else []
    if not ms:
        return {"error": "milestones required: [{when, what}]"}
    n = len(ms)
    gap = 0.4
    total = 12.0
    w = (total - gap * (n - 1)) / n
    x = 0.7
    for m in ms:
        _panel(slide, theme, x, 2.4, w, 2.6, "panel")
        wb = slide.shapes.add_textbox(Inches(x + 0.15), Inches(2.55), Inches(w - 0.3), Inches(0.7))
        wb.text_frame.text = str(m.get("when", ""))
        _style_title(wb.text_frame, theme, 18)
        for p in wb.text_frame.paragraphs:
            p.font.color.rgb = _rgb(THEMES[theme]["accent"])
        tb = slide.shapes.add_textbox(Inches(x + 0.15), Inches(3.3), Inches(w - 0.3), Inches(1.6))
        tb.text_frame.text = str(m.get("what", ""))
        tb.text_frame.word_wrap = True
        _style_body(tb.text_frame, theme, 14)
        x += w + gap
    _set_notes(slide, notes)
    _record(deck_id, "timeline", title)
    return {"deck_id": deck_id, "slide_index": len(prs.slides) - 1}


@mcp.tool
def add_metrics_slide(deck_id: str, title: str, metrics: list[dict], notes: str = "") -> dict:
    """Add a KPI / big-number slide. metrics=[{value, label}] rendered as cards (up to 4)."""
    prs, theme = _deck(deck_id)
    slide = _new_slide(prs, theme)
    _heading(slide, theme, title)
    ms = metrics[:4] if metrics else []
    if not ms:
        return {"error": "metrics required: [{value, label}]"}
    n = len(ms)
    gap = 0.4
    total = 12.0
    w = (total - gap * (n - 1)) / n
    x = 0.7
    for m in ms:
        _panel(slide, theme, x, 2.3, w, 3.0, "panel")
        vb = slide.shapes.add_textbox(Inches(x + 0.1), Inches(2.7), Inches(w - 0.2), Inches(1.4))
        vb.text_frame.text = str(m.get("value", ""))
        for p in vb.text_frame.paragraphs:
            p.alignment = PP_ALIGN.CENTER
        _style_title(vb.text_frame, theme, 44)
        for p in vb.text_frame.paragraphs:
            p.font.color.rgb = _rgb(THEMES[theme]["accent"])
        lb = slide.shapes.add_textbox(Inches(x + 0.1), Inches(4.2), Inches(w - 0.2), Inches(0.9))
        lb.text_frame.text = str(m.get("label", ""))
        lb.text_frame.word_wrap = True
        for p in lb.text_frame.paragraphs:
            p.alignment = PP_ALIGN.CENTER
        _style_body(lb.text_frame, theme, 16)
        x += w + gap
    _set_notes(slide, notes)
    _record(deck_id, "metrics", title)
    return {"deck_id": deck_id, "slide_index": len(prs.slides) - 1}


@mcp.tool
def add_section(deck_id: str, section_title: str, notes: str = "") -> dict:
    """Add a section-divider slide."""
    prs, theme = _deck(deck_id)
    slide = _new_slide(prs, theme)
    box = slide.shapes.add_textbox(Inches(1), Inches(3), Inches(11.3), Inches(1.5))
    box.text_frame.text = section_title
    _style_title(box.text_frame, theme, 40)
    _set_notes(slide, notes)
    _record(deck_id, "section", section_title)
    return {"deck_id": deck_id, "slide_index": len(prs.slides) - 1}


@mcp.tool
def add_image_slide(deck_id: str, image_path: str, title: str = "", caption: str = "",
                    notes: str = "") -> dict:
    """Add a slide with an image (PNG/JPEG). Validates the file exists."""
    prs, theme = _deck(deck_id)
    img = Path(str(image_path)).expanduser()
    if not img.is_file():
        return {"error": f"image not found: {image_path}"}
    if img.suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".bmp"):
        return {"error": "image must be PNG/JPEG/GIF/BMP"}
    slide = _new_slide(prs, theme)
    top = Inches(1.4) if title else Inches(0.7)
    if title:
        _heading(slide, theme, title, 30)
    slide.shapes.add_picture(str(img), Inches(2.5), top, height=Inches(4.8))
    if caption:
        cb = slide.shapes.add_textbox(Inches(0.8), Inches(6.6), Inches(11.7), Inches(0.7))
        cb.text_frame.text = caption
        _style_body(cb.text_frame, theme, 14)
    _set_notes(slide, notes)
    _record(deck_id, "image", title)
    return {"deck_id": deck_id, "slide_index": len(prs.slides) - 1}


@mcp.tool
def set_speaker_notes(deck_id: str, slide_index: int, notes: str) -> dict:
    """Set (or replace) the speaker notes on an existing slide by index."""
    prs, _ = _deck(deck_id)
    if slide_index < 0 or slide_index >= len(prs.slides):
        return {"error": f"slide_index out of range (0..{len(prs.slides) - 1})"}
    prs.slides[slide_index].notes_slide.notes_text_frame.text = notes
    return {"ok": True, "deck_id": deck_id, "slide_index": slide_index}


_OUTLINE_DISPATCH = {
    "title": lambda d, s: add_title_slide(d, s.get("title", ""), s.get("subtitle", ""), s.get("notes", "")),
    "bullet": lambda d, s: add_bullet_slide(d, s.get("title", ""), s.get("bullets", []), s.get("levels"), s.get("notes", "")),
    "two_column": lambda d, s: add_two_column_slide(d, s.get("title", ""), s.get("left", []), s.get("right", []), s.get("notes", "")),
    "comparison": lambda d, s: add_comparison_slide(d, s.get("title", ""), s.get("left_title", ""), s.get("left", []), s.get("right_title", ""), s.get("right", []), s.get("notes", "")),
    "chart": lambda d, s: add_chart_slide(d, s.get("title", ""), s.get("categories", []), s.get("series", []), s.get("chart_type", "bar"), s.get("notes", "")),
    "quote": lambda d, s: add_quote_slide(d, s.get("quote", ""), s.get("attribution", ""), s.get("notes", "")),
    "code": lambda d, s: add_code_slide(d, s.get("title", ""), s.get("code", ""), s.get("language", ""), s.get("notes", "")),
    "agenda": lambda d, s: add_agenda_slide(d, s.get("items", []), s.get("title", "Agenda"), s.get("notes", "")),
    "timeline": lambda d, s: add_timeline_slide(d, s.get("title", ""), s.get("milestones", []), s.get("notes", "")),
    "metrics": lambda d, s: add_metrics_slide(d, s.get("title", ""), s.get("metrics", []), s.get("notes", "")),
    "section": lambda d, s: add_section(d, s.get("title", "") or s.get("section_title", ""), s.get("notes", "")),
    "image": lambda d, s: add_image_slide(d, s.get("image_path", ""), s.get("title", ""), s.get("caption", ""), s.get("notes", "")),
}


@mcp.tool
def build_from_outline(outline: list[dict], theme: str = "dev_dark", title: str = "",
                       filename: str = "") -> dict:
    """One-shot deck build. outline=[{type, ...}] where type ∈ {title, bullet, two_column,
    comparison, chart, quote, code, agenda, timeline, metrics, section, image}. Each item carries
    the same fields the matching add_* tool takes. If filename is given, also saves the .pptx.
    Returns deck_id, per-slide results, and (if saved) the path."""
    if theme not in THEMES:
        theme = "dev_dark"
    created = create_presentation(title=title, theme=theme)
    did = created["deck_id"]
    results = []
    for i, spec in enumerate(outline or []):
        kind = (spec.get("type") or "bullet").lower()
        fn = _OUTLINE_DISPATCH.get(kind)
        if fn is None:
            results.append({"index": i, "error": f"unknown slide type {kind}"})
            continue
        try:
            results.append({"index": i, "type": kind, **fn(did, spec)})
        except Exception as e:  # noqa: BLE001
            results.append({"index": i, "type": kind, "error": str(e)})
    out = {"deck_id": did, "theme": theme, "slides": len(DECKS[did].slides), "results": results}
    if filename:
        out["path"] = save_presentation(did, filename)["path"]
    return out


@mcp.tool
def deck_info(deck_id: str) -> dict:
    """Summarize a deck: slide count and per-slide type/title."""
    prs, theme = _deck(deck_id)
    return {"deck_id": deck_id, "theme": theme, "slides": len(prs.slides),
            "outline": _deck_slides.get(deck_id, [])}


@mcp.tool
def list_decks() -> list[dict]:
    """List in-memory decks (id, theme, slide count)."""
    return [{"deck_id": d, "theme": _deck_theme.get(d, "dev_dark"), "slides": len(p.slides)}
            for d, p in DECKS.items()]


@mcp.tool
def delete_deck(deck_id: str) -> dict:
    """Drop an in-memory deck."""
    DECKS.pop(deck_id, None)
    _deck_theme.pop(deck_id, None)
    _deck_slides.pop(deck_id, None)
    return {"ok": True, "deck_id": deck_id}


@mcp.tool
def list_templates() -> dict:
    """Describe the built-in deck blueprints."""
    return {k: v for k, v in TEMPLATES.items()}


@mcp.tool
def list_themes() -> list[str]:
    """List available color themes."""
    return list(THEMES)


@mcp.tool
def list_slide_types() -> list[str]:
    """List slide types usable in build_from_outline / add_* tools."""
    return sorted(_OUTLINE_DISPATCH)


@mcp.tool
def save_presentation(deck_id: str, filename: str = "") -> dict:
    """Write the deck to a .pptx file and return its absolute path."""
    prs, _ = _deck(deck_id)
    path = _safe_out(filename, f"deck_{deck_id}.pptx", ".pptx")
    prs.save(str(path))
    return {"path": str(path), "slides": len(prs.slides)}


@mcp.tool
def export_pdf(pptx_path: str = "", deck_id: str = "") -> dict:
    """Convert a .pptx to PDF via LibreOffice (free). Pass a saved pptx_path, OR a deck_id (saved
    first automatically). Returns ok=False with a hint if LibreOffice is absent."""
    import shutil
    import subprocess
    if not pptx_path and deck_id:
        if deck_id not in DECKS:
            return {"ok": False, "error": f"unknown deck_id {deck_id}"}
        pptx_path = save_presentation(deck_id)["path"]
    if not pptx_path:
        return {"ok": False, "error": "provide pptx_path or deck_id"}
    src = Path(pptx_path).expanduser()
    if src.suffix.lower() != ".pptx":
        return {"ok": False, "error": "pptx_path must be a .pptx file"}
    try:
        src = src.resolve()
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "invalid pptx_path"}
    if not src.is_file():
        return {"ok": False, "error": f"pptx not found: {pptx_path}"}
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return {"ok": False, "error": "LibreOffice not installed (brew install --cask libreoffice)"}
    try:
        subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(OUT), str(src)],
                       check=True, capture_output=True, timeout=120)
        return {"ok": True, "path": str(OUT / (src.stem + ".pdf"))}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    mcp.run()
