"""blog-drafter — draft dev-blog / LinkedIn posts from your projects (DRAFT ONLY — no auto-posting).
Stores drafts in SQLite and exports markdown. The agent writes the prose; this keeps & exports it,
formats per platform (dev.to/medium/linkedin/hashnode), adds frontmatter, runs SEO checks, and
turns an outline into a draft scaffold."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from mcp_base import BaseStore, data_dir, db_path, make_server

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "profile.json"


def _profile() -> dict:
    """Load the suite's shared profile.json (single source of truth), or {} if absent/invalid."""
    try:
        if PROFILE.exists():
            data = json.loads(PROFILE.read_text())
            return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — never let a malformed profile break a tool
        pass
    return {}


def _author_bio(p: dict) -> str:
    """Build a short author bio line from profile.json (name + headline), or ''."""
    name = str(p.get("name") or "").strip()
    headline = str(p.get("headline") or "").strip()
    if name and headline:
        return f"{name} — {headline}"
    return name or headline


def _slug(title: str, n: int = 40) -> str:
    """Filesystem-safe slug from a draft title (no path separators / traversal)."""
    s = re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")
    return (s or "untitled")[:n]

mcp = make_server(
    "blog-drafter",
    instructions=("Draft posts (draft-only). new_draft/save_draft/list/get/export_md. "
                  "outline() & outline_to_draft() scaffold; export_platform() formats for "
                  "dev.to/medium/linkedin/hashnode; set_meta/seo_check/list_series."),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts(
  id INTEGER PRIMARY KEY, title TEXT, kind TEXT DEFAULT 'blog', topic TEXT DEFAULT '',
  body TEXT DEFAULT '', status TEXT DEFAULT 'draft', updated_at TEXT, created_at TEXT
);
"""
store = BaseStore(db_path("blog-drafter"), schema=SCHEMA)
OUT = data_dir("blog-drafter") / "output"
OUT.mkdir(parents=True, exist_ok=True)

# Additive migration: add metadata columns to existing DBs without data loss.
_EXTRA_COLS = {
    "tags": "TEXT DEFAULT ''", "series": "TEXT DEFAULT ''", "series_part": "INTEGER",
    "canonical_url": "TEXT DEFAULT ''", "cover_image": "TEXT DEFAULT ''",
    "platform": "TEXT DEFAULT ''", "author": "TEXT DEFAULT ''",
}


def _migrate():
    existing = {r["name"] for r in store.query("PRAGMA table_info(drafts)")}
    for col, decl in _EXTRA_COLS.items():
        if col not in existing:
            store.execute(f"ALTER TABLE drafts ADD COLUMN {col} {decl}")


_migrate()

OUTLINES = {
    "linkedin": ["Hook (1 line)", "The problem", "What you built/learned", "1-2 concrete takeaways",
                 "Soft CTA / question", "3-5 hashtags"],
    "blog": ["Title", "TL;DR", "Context / problem", "Approach", "Key code or decision", "Results",
             "What's next", "Closing"],
    "tutorial": ["Title", "What you'll build", "Prerequisites", "Step 1", "Step 2", "Step 3",
                 "Common pitfalls", "Wrap-up + repo link"],
    "thread": ["Hook tweet", "Context tweet", "3-5 insight tweets", "Example/code tweet",
               "Takeaway tweet", "CTA tweet"],
    "newsletter": ["Subject line", "Intro/personal note", "Main story", "Quick links",
                   "One takeaway", "Sign-off"],
    "release_notes": ["Version + date", "Highlights", "New features", "Fixes", "Breaking changes",
                      "Upgrade notes"],
}
WORDS = {
    "blog": {"TL;DR": 40, "Context / problem": 150, "Approach": 250, "Results": 150, "Closing": 80},
    "tutorial": {"What you'll build": 100, "Step 1": 200, "Step 2": 200, "Step 3": 200},
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _parse_tags(tags) -> list[str]:
    if isinstance(tags, list):
        return [str(t).strip() for t in tags if str(t).strip()]
    return [t.strip() for t in re.split(r"[,\n]", str(tags or "")) if t.strip()]


@mcp.tool
def outline(topic: str, kind: str = "blog") -> dict:
    """Suggest a post structure to fill in. kind ∈ {blog, linkedin, tutorial, thread, newsletter,
    release_notes}. Returns the section list plus rough word counts (blog/tutorial) and SEO tips."""
    struct = OUTLINES.get(kind, OUTLINES["blog"])
    out = {"topic": topic, "kind": kind, "outline": struct}
    if kind in WORDS:
        out["target_words"] = WORDS[kind]
    if kind == "blog":
        out["seo_tips"] = ["Put the keyword in the title and first paragraph",
                           "Use H2/H3 subheadings", "Add a code block or image",
                           "End with a clear takeaway and a link"]
    return out


@mcp.tool
def outline_to_draft(topic: str, kind: str = "blog", title: str = "", author: str = "") -> dict:
    """Turn the outline() structure into a draft body scaffold (section headers as markdown), create
    the draft, and return its id + body. author defaults to your name/headline from profile.json
    when omitted (used as the byline in exports); pass author to override."""
    struct = OUTLINES.get(kind, OUTLINES["blog"])
    t = title or topic.title()
    byline = author or _author_bio(_profile())
    if kind in ("blog", "tutorial"):
        body = "\n\n".join(f"## {s}\n\n_TODO_" for s in struct if s.lower() != "title")
    else:
        body = "\n\n".join(f"**{s}**\n\n_TODO_" for s in struct)
    did = store.execute(
        "INSERT INTO drafts(title,kind,topic,body,author,updated_at,created_at) VALUES(?,?,?,?,?,?,?)",
        (t, kind, topic, body, byline, _now(), _now()))
    return {"id": did, "title": t, "kind": kind, "body": body, "author": byline}


@mcp.tool
def new_draft(title: str, body: str = "", kind: str = "blog", topic: str = "", author: str = "") -> dict:
    """Create a draft. author defaults to your name/headline from profile.json when omitted
    (used as the byline in exports); pass author to override or "-" to leave it blank."""
    byline = author or _author_bio(_profile())
    did = store.execute(
        "INSERT INTO drafts(title,kind,topic,body,author,updated_at,created_at) VALUES(?,?,?,?,?,?,?)",
        (title, kind, topic, body, byline, _now(), _now()))
    return {"id": did, "title": title}


@mcp.tool
def save_draft(draft_id: int, body: str, status: str = "draft") -> dict:
    """Update a draft's body/status."""
    store.execute("UPDATE drafts SET body=?, status=?, updated_at=? WHERE id=?", (body, status, _now(), draft_id))
    return {"ok": True, "id": draft_id}


@mcp.tool
def set_meta(draft_id: int, tags: list[str] | None = None, series: str = "",
             series_part: int | None = None, canonical_url: str = "", cover_image: str = "",
             platform: str = "") -> dict:
    """Set a draft's publishing metadata (tags, series + part, canonical URL, cover image, platform).
    Only provided fields are updated."""
    if not store.query_one("SELECT id FROM drafts WHERE id=?", (draft_id,)):
        return {"error": "not found"}
    updates, params = [], []
    if tags is not None:
        updates.append("tags=?"); params.append(", ".join(_parse_tags(tags)))
    if series:
        updates.append("series=?"); params.append(series)
    if series_part is not None:
        updates.append("series_part=?"); params.append(series_part)
    if canonical_url:
        updates.append("canonical_url=?"); params.append(canonical_url)
    if cover_image:
        updates.append("cover_image=?"); params.append(cover_image)
    if platform:
        updates.append("platform=?"); params.append(platform)
    if not updates:
        return {"ok": True, "id": draft_id, "note": "nothing to update"}
    updates.append("updated_at=?"); params.append(_now())
    params.append(draft_id)
    store.execute(f"UPDATE drafts SET {', '.join(updates)} WHERE id=?", params)
    return {"ok": True, "id": draft_id}


@mcp.tool
def list_drafts(kind: str = "", limit: int = 50) -> list[dict]:
    """List drafts."""
    if kind:
        return store.query("SELECT id,title,kind,status,updated_at FROM drafts WHERE kind=? ORDER BY updated_at DESC LIMIT ?",
                           (kind, limit))
    return store.query("SELECT id,title,kind,status,updated_at FROM drafts ORDER BY updated_at DESC LIMIT ?", (limit,))


@mcp.tool
def get(draft_id: int) -> dict:
    """Get a full draft."""
    return store.query_one("SELECT * FROM drafts WHERE id=?", (draft_id,)) or {"error": "not found"}


@mcp.tool
def export_md(draft_id: int, with_frontmatter: bool = False) -> dict:
    """Write a draft to a .md file (you publish manually). with_frontmatter=True prepends a YAML
    block with any title/tags/series/canonical metadata; default keeps the simple `# title` output."""
    d = store.query_one("SELECT * FROM drafts WHERE id=?", (draft_id,))
    if not d:
        return {"error": "not found"}
    path = OUT / f"{int(draft_id)}_{_slug(d['title'])}.md"
    if with_frontmatter:
        fm = _frontmatter(d)
        path.write_text(f"{fm}\n# {d['title']}\n\n{d['body']}\n", encoding="utf-8")
    else:
        path.write_text(f"# {d['title']}\n\n{d['body']}\n", encoding="utf-8")
    return {"path": str(path)}


def _frontmatter(d: dict) -> str:
    lines = ["---", f"title: {d['title']}", f"date: {(d.get('created_at') or '')[:10]}"]
    author = (d.get("author") or "").strip()
    if author and author != "-":
        lines.append(f"author: {author}")
    tags = _parse_tags(d.get("tags"))
    if tags:
        lines.append("tags: [" + ", ".join(tags) + "]")
    if d.get("series"):
        lines.append(f"series: {d['series']}")
    if d.get("canonical_url"):
        lines.append(f"canonical_url: {d['canonical_url']}")
    if d.get("cover_image"):
        lines.append(f"cover_image: {d['cover_image']}")
    lines.append("---\n")
    return "\n".join(lines)


def _strip_md(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    return text.strip()


@mcp.tool
def export_platform(draft_id: int, platform: str) -> dict:
    """Format and export a draft for a publishing platform ∈ {devto, hashnode, medium, linkedin}.
    devto/hashnode get YAML frontmatter; medium gets clean markdown; linkedin gets plain text with
    hashtags. Returns the path and the formatted string. Draft-only — never posts."""
    d = store.query_one("SELECT * FROM drafts WHERE id=?", (draft_id,))
    if not d:
        return {"error": "not found"}
    platform = re.sub(r"[^a-z0-9]", "", str(platform).lower())
    tags = _parse_tags(d.get("tags"))
    slug = _slug(d["title"])
    if platform in ("devto", "hashnode"):
        fm = ["---", f"title: {d['title']}", "published: false"]
        if tags:
            fm.append("tags: " + ", ".join(tags[:4]))
        if d.get("canonical_url"):
            fm.append(f"canonical_url: {d['canonical_url']}")
        if d.get("cover_image"):
            fm.append(f"cover_image: {d['cover_image']}")
        if d.get("series"):
            fm.append(f"series: {d['series']}")
        fm.append("---\n")
        content = "\n".join(fm) + d["body"] + "\n"
        ext = "md"
    elif platform == "medium":
        content = f"# {d['title']}\n\n{d['body']}\n"
        ext = "md"
    elif platform == "linkedin":
        body = _strip_md(d["body"])
        tagline = " ".join("#" + re.sub(r"[^a-z0-9]", "", t.lower()) for t in tags[:5])
        content = f"{d['title']}\n\n{body}" + (f"\n\n{tagline}" if tagline else "") + "\n"
        ext = "txt"
    else:
        return {"error": f"unknown platform {platform}; choose devto, hashnode, medium, linkedin"}
    path = OUT / f"{int(draft_id)}_{slug}_{platform}.{ext}"
    path.write_text(content, encoding="utf-8")
    return {"path": str(path), "platform": platform, "content": content}


@mcp.tool
def seo_check(draft_id: int) -> dict:
    """Analyze a draft for SEO/readability: title length, headings, word count, reading time,
    top terms (tag suggestions), and a meta-description suggestion."""
    d = store.query_one("SELECT * FROM drafts WHERE id=?", (draft_id,))
    if not d:
        return {"error": "not found"}
    body = d["body"] or ""
    plain = _strip_md(body)
    words = re.findall(r"[A-Za-z][A-Za-z'-]+", plain)
    wc = len(words)
    headings = len(re.findall(r"^#{2,3}\s", body, flags=re.M))
    issues = []
    tl = len(d["title"])
    if tl < 20 or tl > 65:
        issues.append(f"Title is {tl} chars; aim for 20–65 for search snippets.")
    if headings == 0:
        issues.append("No H2/H3 subheadings — add structure.")
    if wc < 300:
        issues.append(f"Only {wc} words; long-form (700+) tends to rank better.")
    if "```" not in body and "![" not in body:
        issues.append("No code block or image — add a visual.")
    stop = {"the", "and", "for", "with", "you", "your", "this", "that", "are", "was", "but",
            "not", "from", "have", "has", "can", "will", "out", "into", "use", "using"}
    freq = {}
    for w in (x.lower() for x in words if len(x) > 3 and x.lower() not in stop):
        freq[w] = freq.get(w, 0) + 1
    top = sorted(freq.items(), key=lambda kv: -kv[1])[:8]
    first_sentence = re.split(r"(?<=[.!?])\s", plain.strip())[0] if plain.strip() else ""
    return {
        "title_len": tl, "word_count": wc, "headings": headings,
        "reading_time_min": max(1, round(wc / 200)),
        "tag_suggestions": [w for w, _ in top],
        "meta_description": first_sentence[:155],
        "issues": issues, "score": max(0, 100 - 18 * len(issues)),
    }


@mcp.tool
def list_series() -> list[dict]:
    """List drafts grouped by series, parts ordered."""
    rows = store.query("SELECT id,title,series,series_part FROM drafts WHERE series!='' AND series IS NOT NULL")
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["series"], []).append(r)
    return [{"series": s,
             "parts": sorted(items, key=lambda r: (r["series_part"] is None, r["series_part"] or 0))}
            for s, items in groups.items()]


@mcp.tool
def export_md_all(kind: str = "") -> dict:
    """Export every draft (optionally filtered by kind) to .md files; returns the paths."""
    rows = store.query("SELECT id FROM drafts" + (" WHERE kind=?" if kind else ""),
                       (kind,) if kind else ())
    paths = [export_md(r["id"])["path"] for r in rows]
    return {"count": len(paths), "paths": paths}


if __name__ == "__main__":
    mcp.run()
