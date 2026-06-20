"""resume-forge — tailor a résumé + cover letter to a job description and export to .docx
(PDF via LibreOffice). Pulls facts from profile.json (the suite's single source of truth);
the agent supplies the tailored wording, this server lays it out, scores it against the JD,
and interops with the free JSON Resume standard.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor
from mcp_base import data_dir, make_server

mcp = make_server(
    "resume-forge",
    instructions=("Build tailored résumés/cover letters from profile.json. read_profile -> "
                  "ats_score(jd) -> build_resume(template) / cover_letter(...) -> export. "
                  "JSON Resume import/export. PDF needs LibreOffice."),
)

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "profile.json"
OUT = data_dir("resume-forge") / "output"
OUT.mkdir(parents=True, exist_ok=True)

TEMPLATES = {
    "classic": "Single column, serif-neutral headings — the default, ATS-safe.",
    "modern": "Accent-colored headings, tighter spacing, contemporary feel.",
    "compact": "Dense single column for fitting more on one page.",
    "two_column": "Skills/contact sidebar feel via a left-aligned skills block up top.",
}

STOPWORDS = {
    "the", "and", "for", "with", "you", "your", "our", "are", "will", "have", "work", "team",
    "experience", "role", "this", "that", "who", "what", "from", "they", "their", "but", "not",
    "can", "all", "any", "has", "was", "were", "been", "being", "into", "out", "via", "per",
    "etc", "able", "such", "more", "most", "than", "then", "also", "may", "must", "should",
    "would", "could", "about", "across", "within", "using", "use", "used", "well", "good",
    "strong", "skills", "ability", "including", "include", "required", "preferred", "plus",
    "join", "looking", "seeking", "candidate", "position", "company", "years", "year",
    "seek", "want", "need", "like", "love", "build", "ensure", "help", "plus", "nice",
}
WEAK_VERBS = {
    "responsible", "helped", "worked", "assisted", "involved", "participated", "handled",
    "managed", "supported", "did", "made", "used", "tasked", "duties", "various", "stuff",
    "things", "etc",
}
STRONG_VERBS = [
    "Architected", "Built", "Launched", "Shipped", "Designed", "Engineered", "Optimized",
    "Reduced", "Increased", "Accelerated", "Automated", "Scaled", "Led", "Spearheaded",
    "Delivered", "Implemented", "Migrated", "Refactored", "Drove", "Established", "Pioneered",
]


def _safe_out(filename: str, default: str, suffixes: tuple[str, ...]) -> Path:
    """Resolve a user-supplied filename to a path strictly inside OUT.

    Strips directory components (prevents traversal/absolute paths) and forces
    one of the allowed suffixes (the first one if none match).
    """
    name = Path(str(filename or default)).name
    if not name or name in (".", ".."):
        name = default
    if not name.lower().endswith(suffixes):
        name = name + suffixes[0]
    path = (OUT / name).resolve()
    if OUT.resolve() not in path.parents and path != OUT.resolve():
        raise RuntimeError("invalid filename")
    return path


def _profile() -> dict:
    if PROFILE.exists():
        try:
            data = json.loads(PROFILE.read_text())
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001 — never let a malformed profile break a tool
            return {}
    return {}


def _have_skills(p: dict) -> set[str]:
    have: set[str] = set()
    skills = p.get("skills")
    if not isinstance(skills, dict):
        return have
    for group in skills.values():
        if isinstance(group, (list, tuple)):
            have |= {str(s).lower() for s in group if s}
    return have


def _tokens(text: str) -> list[str]:
    raw = re.findall(r"[A-Za-z][A-Za-z+.#-]{1,}", text)
    return [t for t in (w.lower().strip(".-") for w in raw) if len(t) > 1]


def _keywords(text: str, top: int = 40) -> list[tuple[str, int]]:
    counts = Counter(w for w in _tokens(text) if w not in STOPWORDS and len(w) > 2)
    # bigrams of meaningful tokens
    toks = [w for w in _tokens(text) if w not in STOPWORDS and len(w) > 2]
    bigrams = Counter(f"{a} {b}" for a, b in zip(toks, toks[1:]))
    merged = counts + Counter({k: v for k, v in bigrams.items() if v > 1})
    return merged.most_common(top)


def _as_list(v) -> list:
    """Coerce a profile field to a list of items, tolerating None/str/scalar/malformed."""
    if isinstance(v, list):
        return v
    if v in (None, ""):
        return []
    return [v]


def _profile_text(p: dict) -> str:
    parts = [p.get("summary", ""), p.get("headline", "")]
    skills = p.get("skills")
    if isinstance(skills, dict):
        for grp in skills.values():
            parts.extend(_as_list(grp))
    for key in ("experience", "projects"):
        for e in _as_list(p.get(key)):
            if not isinstance(e, dict):
                continue
            parts.append(e.get("role") or e.get("name") or "")
            parts.append(e.get("description") or e.get("tagline") or "")
            parts.extend(_as_list(e.get("highlights")))
            parts.extend(_as_list(e.get("tech")))
    return " ".join(str(x) for x in parts if x)


@mcp.tool
def read_profile() -> dict:
    """Return profile.json (name, skills, experience, projects, education) for tailoring."""
    return _profile()


@mcp.tool
def keyword_gaps(jd: str) -> dict:
    """Compare a job description's notable terms against your profile skills to spot gaps to address.
    Returns frequency-ranked JD terms, bigrams, and the gaps you don't yet cover."""
    p = _profile()
    have = _have_skills(p)
    ranked = _keywords(jd, 50)
    jd_terms = [w for w, _ in ranked]
    missing = [w for w, _ in ranked if all(part not in have for part in w.split())][:40]
    return {
        "have_skills": sorted(have),
        "jd_terms_sample": jd_terms[:40],
        "jd_terms_ranked": [{"term": w, "count": c} for w, c in ranked[:25]],
        "possible_gaps": missing,
    }


@mcp.tool
def ats_score(jd: str, resume_text: str = "") -> dict:
    """Score a résumé against a job description (ATS-style). Uses resume_text if given, else builds
    text from profile.json. Returns 0-100 score, letter grade, matched/missing keywords, and
    concrete suggestions. Pure offline keyword analysis."""
    p = _profile()
    text = resume_text or _profile_text(p)
    res_tokens = set(_tokens(text))
    res_blob = " " + " ".join(_tokens(text)) + " "
    kws = _keywords(jd, 30)
    if not kws:
        return {"score": 0, "grade": "F", "error": "no keywords extracted from JD"}
    total_weight = sum(c for _, c in kws)
    matched, missing = [], []
    hit_weight = 0
    for term, weight in kws:
        present = all(part in res_tokens for part in term.split()) or (f" {term} " in res_blob)
        if present:
            matched.append(term)
            hit_weight += weight
        else:
            missing.append(term)
    coverage = round(100 * len(matched) / len(kws))
    weighted = round(100 * hit_weight / total_weight) if total_weight else 0
    score = round(0.5 * coverage + 0.5 * weighted)
    grade = "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 55 else "D" if score >= 40 else "F"
    suggestions = []
    if missing:
        suggestions.append(f"Add or surface these JD keywords if true: {', '.join(missing[:10])}.")
    wc = len(_tokens(text))
    if wc < 200:
        suggestions.append(f"Résumé text is short ({wc} words); expand impact bullets.")
    if not re.search(r"\d", text):
        suggestions.append("No numbers detected — quantify achievements (%, $, time saved).")
    return {
        "score": score, "grade": grade, "coverage_pct": coverage, "weighted_pct": weighted,
        "matched_keywords": matched, "missing_keywords": missing,
        "jd_top_keywords": [{"term": t, "weight": w} for t, w in kws],
        "word_count": wc, "suggestions": suggestions,
    }


@mcp.tool
def bullet_strength(bullets: list[str]) -> dict:
    """Analyze résumé bullets for impact: flags weak/passive verbs, missing metrics, over-length,
    and suggests stronger action verbs. Returns per-bullet scores and an overall rating."""
    results = []
    for b in bullets:
        words = _tokens(b)
        issues = []
        first = words[0] if words else ""
        if first in WEAK_VERBS or not first:
            issues.append("starts weak — lead with a strong action verb")
        if any(w in WEAK_VERBS for w in words[:3]):
            issues.append("contains weak phrasing")
        if not re.search(r"\d", b):
            issues.append("no quantified metric")
        if re.search(r"\b(was|were|been|by)\b", b.lower()) and re.search(r"ed by", b.lower()):
            issues.append("possibly passive voice")
        if len(words) > 32:
            issues.append("too long — tighten to one line")
        if len(words) < 4:
            issues.append("too short / vague")
        score = max(0, 100 - 22 * len(issues))
        results.append({"bullet": b, "score": score, "issues": issues})
    avg = round(sum(r["score"] for r in results) / len(results)) if results else 0
    return {"average_score": avg, "bullets": results,
            "strong_verbs": STRONG_VERBS,
            "tip": "Pattern: <Strong verb> <what> <quantified result>."}


@mcp.tool
def list_templates() -> dict:
    """Describe the available résumé layouts for build_resume(template=...)."""
    return dict(TEMPLATES)


def _apply_heading_style(doc, template: str):
    if template in ("modern", "two_column"):
        try:
            for lvl in (1,):
                st = doc.styles[f"Heading {lvl}"]
                st.font.color.rgb = RGBColor(0x1A, 0x56, 0xDB)
        except Exception:  # noqa: BLE001
            pass


@mcp.tool
def build_resume(headline: str = "", summary: str = "", sections: list[dict] | None = None,
                 filename: str = "resume.docx", template: str = "classic") -> dict:
    """Render a résumé .docx. `sections` = [{heading, items:[...]}, ...]; if omitted, builds from
    profile.json. template ∈ {classic, modern, compact, two_column}; classic is the default layout."""
    p = _profile()
    if template not in TEMPLATES:
        template = "classic"
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(9.5 if template == "compact" else 10.5)
    _apply_heading_style(doc, template)

    name = str(p.get("name") or "Your Name")
    h = doc.add_heading(name, level=0)
    if template == "modern":
        for run in h.runs:
            run.font.color.rgb = RGBColor(0x0F, 0x4C, 0x81)
    contact = " · ".join(x for x in [p.get("email"), p.get("phone"), p.get("location"),
                                     (p.get("links") or {}).get("github"),
                                     (p.get("links") or {}).get("linkedin")] if x)
    if headline or p.get("headline"):
        doc.add_paragraph(str(headline or p.get("headline")))
    if contact:
        cp = doc.add_paragraph(contact)
        if template in ("modern", "two_column"):
            cp.alignment = WD_ALIGN_PARAGRAPH.LEFT
    if summary or p.get("summary"):
        doc.add_heading("Summary", level=1)
        doc.add_paragraph(str(summary or p.get("summary")))

    if sections:
        for sec in sections:
            doc.add_heading(sec.get("heading", ""), level=1)
            for item in sec.get("items", []):
                doc.add_paragraph(str(item), style="List Bullet")
    else:
        skills = p.get("skills")
        if not isinstance(skills, dict):
            skills = {}
        if any(skills.values()):
            doc.add_heading("Skills", level=1)
            for k, v in skills.items():
                vals = [str(s) for s in _as_list(v) if s]
                if vals:
                    doc.add_paragraph(f"{str(k).title()}: {', '.join(vals)}")
        for label, key in (("Experience", "experience"), ("Projects", "projects"), ("Education", "education")):
            entries = [e for e in _as_list(p.get(key))
                       if isinstance(e, dict) and any(e.values())]
            if entries:
                doc.add_heading(label, level=1)
                for e in entries:
                    title = " — ".join(str(x) for x in [e.get("role") or e.get("name") or e.get("degree"),
                                                        e.get("company") or e.get("school")] if x)
                    if title:
                        doc.add_paragraph(title).runs[0].bold = True
                    for hl in _as_list(e.get("highlights")):
                        if hl:
                            doc.add_paragraph(str(hl), style="List Bullet")
                    if e.get("description"):
                        doc.add_paragraph(str(e["description"]))

    path = _safe_out(filename, "resume.docx", (".docx",))
    doc.save(str(path))
    return {"path": str(path), "template": template}


@mcp.tool
def cover_letter(company: str, role: str, body: str, recipient: str = "Hiring Team",
                 filename: str = "") -> dict:
    """Render a cover letter .docx. `body` is the tailored letter text (agent-written)."""
    p = _profile()
    doc = Document()
    name = str(p.get("name") or "")
    doc.add_paragraph(name)
    if p.get("email"):
        doc.add_paragraph(str(p["email"]))
    doc.add_paragraph("")
    doc.add_paragraph(f"Dear {recipient},")
    for para in str(body or "").split("\n\n"):
        doc.add_paragraph(para)
    doc.add_paragraph("")
    doc.add_paragraph("Sincerely,")
    doc.add_paragraph(name)
    default = f"cover_{company.lower().replace(' ', '_')}_{role.lower().replace(' ', '_')}.docx"
    path = _safe_out(filename, default, (".docx",))
    doc.save(str(path))
    return {"path": str(path), "company": company, "role": role}


@mcp.tool
def export_markdown(headline: str = "", summary: str = "", filename: str = "resume.md") -> dict:
    """Export a plain-markdown résumé from profile.json (great for ATS paste + ats_score input)."""
    p = _profile()
    lines = [f"# {p.get('name') or 'Your Name'}"]
    if headline or p.get("headline"):
        lines.append(f"_{headline or p.get('headline')}_")
    links = p.get("links") if isinstance(p.get("links"), dict) else {}
    contact = " · ".join(str(x) for x in [p.get("email"), p.get("phone"), p.get("location"),
                                          links.get("github"),
                                          links.get("linkedin")] if x)
    if contact:
        lines += ["", contact]
    if summary or p.get("summary"):
        lines += ["", "## Summary", "", str(summary or p.get("summary"))]
    skills = p.get("skills")
    if not isinstance(skills, dict):
        skills = {}
    if any(skills.values()):
        lines += ["", "## Skills"]
        for k, v in skills.items():
            vals = [str(s) for s in _as_list(v) if s]
            if vals:
                lines.append(f"- **{str(k).title()}:** {', '.join(vals)}")
    for label, key in (("Experience", "experience"), ("Projects", "projects"), ("Education", "education")):
        entries = [e for e in _as_list(p.get(key))
                   if isinstance(e, dict) and any(e.values())]
        if entries:
            lines += ["", f"## {label}"]
            for e in entries:
                title = " — ".join(str(x) for x in [e.get("role") or e.get("name") or e.get("degree"),
                                                    e.get("company") or e.get("school")] if x)
                dates = " ".join(str(x) for x in [e.get("start"), e.get("end")] if x)
                if title:
                    lines.append(f"### {title}" + (f" ({dates})" if dates else ""))
                for hl in _as_list(e.get("highlights")):
                    if hl:
                        lines.append(f"- {hl}")
                if e.get("description"):
                    lines.append(str(e["description"]))
    md = "\n".join(str(x) for x in lines) + "\n"
    path = _safe_out(filename, "resume.md", (".md",))
    path.write_text(md, encoding="utf-8")
    return {"path": str(path), "markdown": md}


def _to_json_resume(p: dict) -> dict:
    links = p.get("links") if isinstance(p.get("links"), dict) else {}
    profiles = [{"network": k, "url": v} for k, v in links.items() if v and k not in ("website",)]

    def _entries(key):
        return [e for e in _as_list(p.get(key)) if isinstance(e, dict) and any(e.values())]

    skills = p.get("skills") if isinstance(p.get("skills"), dict) else {}
    return {
        "$schema": "https://raw.githubusercontent.com/jsonresume/resume-schema/v1.0.0/schema.json",
        "basics": {
            "name": p.get("name", ""),
            "label": p.get("headline", ""),
            "email": p.get("email", ""),
            "phone": p.get("phone", ""),
            "url": links.get("website", ""),
            "summary": p.get("summary", ""),
            "location": {"address": p.get("location", "")},
            "profiles": profiles,
        },
        "work": [{"name": e.get("company", ""), "position": e.get("role", ""),
                  "startDate": e.get("start", ""), "endDate": e.get("end", ""),
                  "highlights": _as_list(e.get("highlights"))} for e in _entries("experience")],
        "projects": [{"name": e.get("name", ""), "description": e.get("description", ""),
                      "url": e.get("link", ""), "keywords": _as_list(e.get("tech")),
                      "highlights": _as_list(e.get("highlights"))} for e in _entries("projects")],
        "education": [{"institution": e.get("school", ""), "studyType": e.get("degree", ""),
                       "startDate": e.get("start", ""), "endDate": e.get("end", "")}
                      for e in _entries("education")],
        "skills": [{"name": k, "keywords": _as_list(v)} for k, v in skills.items() if v],
    }


def _from_json_resume(j: dict) -> dict:
    b = j.get("basics") or {}
    loc = b.get("location") or {}
    links = {"website": b.get("url", "")}
    for pr in b.get("profiles") or []:
        if pr.get("network") and pr.get("url"):
            links[pr["network"].lower()] = pr["url"]
    return {
        "name": b.get("name", ""), "email": b.get("email", ""), "phone": b.get("phone", ""),
        "location": loc.get("address", "") or loc.get("city", ""), "headline": b.get("label", ""),
        "summary": b.get("summary", ""), "links": links,
        "skills": {s.get("name", f"group{i}"): s.get("keywords", [])
                   for i, s in enumerate(j.get("skills") or [])},
        "experience": [{"company": w.get("name", ""), "role": w.get("position", ""),
                        "start": w.get("startDate", ""), "end": w.get("endDate", ""),
                        "highlights": w.get("highlights", [])} for w in j.get("work") or []],
        "projects": [{"name": pr.get("name", ""), "description": pr.get("description", ""),
                      "link": pr.get("url", ""), "tech": pr.get("keywords", []),
                      "highlights": pr.get("highlights", [])} for pr in j.get("projects") or []],
        "education": [{"school": e.get("institution", ""), "degree": e.get("studyType", ""),
                       "start": e.get("startDate", ""), "end": e.get("endDate", "")}
                      for e in j.get("education") or []],
    }


@mcp.tool
def export_json_resume(filename: str = "resume.json") -> dict:
    """Export profile.json to the free JSON Resume standard (jsonresume.org). Enables any free JSON
    Resume theme/renderer. Writes to the output dir."""
    data = _to_json_resume(_profile())
    path = _safe_out(filename, "resume.json", (".json",))
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {"path": str(path), "resume": data}


@mcp.tool
def import_json_resume(path_or_json: str, write_to: str = "") -> dict:
    """Parse a JSON Resume (file path or raw JSON string) into the suite's profile.json shape.
    Returns the mapped dict. If write_to is given, writes it there (never silently clobbers the repo
    profile.json)."""
    if not isinstance(path_or_json, str) or not path_or_json.strip():
        return {"error": "path_or_json must be a non-empty file path or JSON string"}
    raw = path_or_json
    _MAX = 5 * 1024 * 1024  # 5 MB cap
    try:
        candidate = Path(path_or_json).expanduser()
        is_file = len(path_or_json) < 4096 and candidate.is_file()
    except (OSError, ValueError):
        is_file = False
    if is_file:
        if candidate.stat().st_size > _MAX:
            return {"error": "file too large (max 5 MB)"}
        raw = candidate.read_text()
    elif len(raw) > _MAX:
        return {"error": "input too large (max 5 MB)"}
    try:
        j = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"error": f"invalid JSON: {e}"}
    if not isinstance(j, dict):
        return {"error": "JSON Resume must be an object"}
    mapped = _from_json_resume(j)
    out = {"profile": mapped}
    if write_to:
        dest = Path(write_to).expanduser()
        dest.write_text(json.dumps(mapped, indent=2), encoding="utf-8")
        out["path"] = str(dest)
    return out


@mcp.tool
def export_pdf(docx_path: str) -> dict:
    """Convert a .docx to PDF via LibreOffice (free)."""
    import shutil
    import subprocess
    if not isinstance(docx_path, str) or not docx_path.strip():
        return {"ok": False, "error": "docx_path is required"}
    src = Path(docx_path).expanduser()
    if src.suffix.lower() != ".docx":
        return {"ok": False, "error": "docx_path must be a .docx file"}
    try:
        src = src.resolve()
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "invalid docx_path"}
    if not src.is_file():
        return {"ok": False, "error": f"docx not found: {docx_path}"}
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return {"ok": False, "error": "LibreOffice not installed (brew install --cask libreoffice)"}
    try:
        subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(OUT), str(src)],
                       check=True, capture_output=True, timeout=120)
        return {"ok": True, "path": str(OUT / (src.stem + ".pdf"))}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@mcp.tool
def list_versions() -> list[str]:
    """List generated résumé/cover-letter files."""
    return [str(p) for p in sorted(OUT.glob("*.docx"))]


if __name__ == "__main__":
    mcp.run()
