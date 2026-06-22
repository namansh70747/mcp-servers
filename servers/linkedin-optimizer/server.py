"""linkedin-optimizer — free, offline tooling to sharpen your LinkedIn presence (analysis & drafting
only, never posts). Generates headline variants, drafts an About section from profile.json, turns
raw notes into strong experience bullets, and audits text for recruiter keywords, length limits,
buzzwords, and readability. Pure stdlib + profile.json."""
from __future__ import annotations

import json
import re
from pathlib import Path

from mcp_base import make_server

mcp = make_server(
    "linkedin-optimizer",
    instructions=("Sharpen LinkedIn copy (draft-only). headline_variants, about_section, "
                  "experience_bullets, optimize_text, keyword_audit. Uses profile.json."),
)

ROOT = Path(__file__).resolve().parents[2]

LIMITS = {"headline": 220, "about": 2600, "post": 3000}
BUZZWORDS = {
    "synergy", "synergies", "leverage", "leveraging", "rockstar", "ninja", "guru", "wizard",
    "thought leader", "go-getter", "self-starter", "team player", "results-driven", "detail-oriented",
    "hardworking", "passionate", "motivated", "dynamic", "proactive", "best of breed", "disrupt",
    "disruptive", "game-changer", "out of the box", "value-add", "circle back", "move the needle",
    "world-class", "cutting-edge", "next-level", "10x",
}
WEAK_VERBS = {
    "responsible", "helped", "worked", "assisted", "involved", "participated", "handled",
    "managed", "supported", "did", "made", "used", "tasked",
}
STRONG_VERBS = [
    "Built", "Launched", "Shipped", "Designed", "Engineered", "Architected", "Optimized",
    "Reduced", "Increased", "Accelerated", "Automated", "Scaled", "Led", "Spearheaded",
    "Delivered", "Implemented", "Migrated", "Refactored", "Drove", "Grew", "Cut", "Improved",
]
ROLE_KEYWORDS = {
    "software engineer": ["python", "java", "javascript", "api", "testing", "ci/cd", "git",
                          "system design", "debugging", "agile"],
    "backend": ["python", "go", "rest", "graphql", "sql", "postgresql", "docker", "kubernetes",
                "microservices", "redis", "aws"],
    "frontend": ["react", "typescript", "css", "accessibility", "performance", "webpack",
                 "responsive", "ui", "state management"],
    "data scientist": ["python", "pandas", "sql", "machine learning", "statistics", "pytorch",
                       "scikit-learn", "visualization", "experimentation", "etl"],
    "data engineer": ["spark", "airflow", "sql", "etl", "kafka", "warehouse", "dbt", "python",
                      "pipelines", "aws"],
    "product manager": ["roadmap", "stakeholders", "metrics", "user research", "prioritization",
                        "a/b testing", "go-to-market", "discovery", "okrs"],
    "devops": ["kubernetes", "terraform", "ci/cd", "aws", "docker", "monitoring", "prometheus",
               "linux", "automation", "iac"],
    "ml engineer": ["pytorch", "tensorflow", "mlops", "deployment", "python", "feature engineering",
                    "inference", "model serving", "gpu"],
}


def _profile() -> dict:
    pf = ROOT / "profile.json"
    return json.loads(pf.read_text()) if pf.exists() else {}


def _flat_skills(p: dict) -> list[str]:
    return [s for grp in (p.get("skills") or {}).values() for s in grp]


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z+.#/-]*", text)]


@mcp.tool
def headline_variants(role: str = "", skills: list[str] | None = None, focus: str = "") -> dict:
    """Generate and length-check several LinkedIn headline options (limit 220 chars). Falls back to
    profile.json for role/skills when not given."""
    p = _profile()
    role = role or p.get("headline") or "Software Engineer"
    sk = skills or _flat_skills(p)[:4]
    sk_str = " · ".join(sk[:3]) if sk else ""
    focus = focus or (p.get("summary", "")[:60].strip())
    candidates = [
        role,
        f"{role} | {sk_str}" if sk_str else role,
        f"{role} — building with {sk_str}" if sk_str else role,
        f"{role} | {focus}" if focus else role,
        f"{role} | {sk_str} | {focus}" if (sk_str and focus) else role,
    ]
    seen, variants = set(), []
    for c in candidates:
        c = c.strip(" |—-·")
        if c and c.lower() not in seen:
            seen.add(c.lower())
            variants.append({"text": c, "chars": len(c),
                             "within_limit": len(c) <= LIMITS["headline"]})
    return {"role": role, "limit": LIMITS["headline"], "variants": variants}


@mcp.tool
def about_section(profile_path: str = "", focus: str = "") -> dict:
    """Draft a LinkedIn About/summary from profile.json (or a given profile path): a hook, what you
    do, proof points from experience/projects, skills, and a soft CTA. Returns the text + char count
    (limit 2600)."""
    p = _profile()
    if profile_path:
        try:
            cand = Path(str(profile_path)).expanduser()
            if cand.is_file() and cand.stat().st_size <= 5 * 1024 * 1024:
                loaded = json.loads(cand.read_text())
                if isinstance(loaded, dict):
                    p = loaded
        except (OSError, ValueError):
            pass  # fall back to repo profile.json on any read/parse error
    name = p.get("name", "")
    headline = p.get("headline", "")
    summary = p.get("summary", "")
    sk = _flat_skills(p)
    parts = []
    hook = summary.split(".")[0].strip() if summary else (focus or headline)
    if hook:
        parts.append(hook + ("." if not hook.endswith(".") else ""))
    if headline and headline.lower() not in (hook or "").lower():
        parts.append(f"I'm {name + ', ' if name else ''}{headline}.".strip())
    proofs = []
    for e in (p.get("experience") or []):
        for h in e.get("highlights", [])[:1]:
            proofs.append(h)
    for pr in (p.get("projects") or []):
        if pr.get("name") and pr.get("tagline"):
            proofs.append(f"{pr['name']} — {pr['tagline']}")
    if proofs:
        parts.append("A few things I've worked on:\n" + "\n".join(f"• {x}" for x in proofs[:4]))
    if sk:
        parts.append("Core skills: " + ", ".join(sk[:12]) + ".")
    parts.append("Open to connecting — feel free to reach out.")
    text = "\n\n".join(x for x in parts if x)
    return {"about": text, "chars": len(text), "limit": LIMITS["about"],
            "within_limit": len(text) <= LIMITS["about"]}


@mcp.tool
def experience_bullets(role: str, company: str, raw: str) -> dict:
    """Turn raw notes (free text, one idea per line or comma-separated) into polished, strong-verb
    bullet suggestions for a role. Flags lines lacking metrics."""
    items = [x.strip() for x in re.split(r"[\n;]|,(?=\s*[A-Z])", raw) if x.strip()]
    bullets = []
    vi = 0
    for it in items:
        words = it.split()
        first = words[0].lower() if words else ""
        suggestion = it
        if first in WEAK_VERBS or not first or first.endswith("ing"):
            verb = STRONG_VERBS[vi % len(STRONG_VERBS)]
            vi += 1
            rest = " ".join(words[1:]) if first in WEAK_VERBS else it
            suggestion = f"{verb} {rest}".strip()
        has_metric = bool(re.search(r"\d", it))
        bullets.append({"original": it, "suggestion": suggestion[0].upper() + suggestion[1:],
                        "has_metric": has_metric,
                        "tip": None if has_metric else "Add a number (%, time, scale, $)."})
    return {"role": role, "company": company, "bullets": bullets,
            "pattern": "<Strong verb> <what> <quantified result>"}


@mcp.tool
def optimize_text(text: str, kind: str = "about") -> dict:
    """Audit any LinkedIn text (kind ∈ {headline, about, post}) for length vs limit, buzzwords/clichés,
    weak verbs, readability, and emoji/hashtag usage. Returns issues + a 0-100 score."""
    limit = LIMITS.get(kind, LIMITS["about"])
    low = text.lower()
    found_buzz = sorted({b for b in BUZZWORDS if b in low})
    weak = sorted({w for w in WEAK_VERBS if re.search(rf"\b{re.escape(w)}\b", low)})
    words = _tokens(text)
    wc = len(words)
    sentences = [s for s in re.split(r"(?<=[.!?])\s", text) if s.strip()]
    avg_sentence = round(wc / len(sentences), 1) if sentences else 0
    hashtags = re.findall(r"#\w+", text)
    issues = []
    if len(text) > limit:
        issues.append(f"Over the {kind} limit ({len(text)}/{limit} chars).")
    if found_buzz:
        issues.append("Buzzwords/clichés to cut: " + ", ".join(found_buzz))
    if weak:
        issues.append("Weak verbs to replace: " + ", ".join(weak))
    if avg_sentence > 25:
        issues.append(f"Long sentences (avg {avg_sentence} words) — break them up.")
    if kind == "post" and not hashtags:
        issues.append("No hashtags — add 3–5 relevant ones for reach.")
    if kind == "about" and len(text) < 200:
        issues.append("About is short — aim for a few short paragraphs.")
    score = max(0, 100 - 12 * len(issues))
    return {"kind": kind, "chars": len(text), "limit": limit, "words": wc,
            "avg_sentence_words": avg_sentence, "buzzwords": found_buzz, "weak_verbs": weak,
            "hashtags": hashtags, "issues": issues, "score": score}


@mcp.tool
def keyword_audit(text: str, target_role: str) -> dict:
    """Check how well text covers recruiter keywords for a target role (e.g. 'backend',
    'data scientist'). Returns matched/missing keywords and a coverage score. Free curated lists."""
    key = target_role.lower().strip()
    kws = ROLE_KEYWORDS.get(key)
    if not kws:
        # fuzzy: pick the role whose name shares the most words
        best, best_overlap = None, 0
        tset = set(key.split())
        for r, v in ROLE_KEYWORDS.items():
            ov = len(tset & set(r.split()))
            if ov > best_overlap:
                best, best_overlap = r, ov
        if best:
            kws = ROLE_KEYWORDS[best]
            key = best
    if not kws:
        return {"error": f"no keyword list for '{target_role}'",
                "available_roles": sorted(ROLE_KEYWORDS)}
    low = " " + text.lower() + " "
    matched = [k for k in kws if k in low]
    missing = [k for k in kws if k not in low]
    coverage = round(100 * len(matched) / len(kws))
    return {"target_role": key, "coverage_pct": coverage, "matched": matched, "missing": missing,
            "suggestion": ("Weave in (if true): " + ", ".join(missing)) if missing else "Strong coverage."}


@mcp.tool
def ab_variants(text: str, kind: str = "headline", n: int = 3) -> dict:
    """Generate N labeled A/B-test variants of a headline/about/bio, each with a distinct angle
    (impact / keyword-rich / personable / outcome), with a length check so you can pick the best
    performer. Draft-only — never posts."""
    text = (text or "").strip()
    if not text:
        return {"error": "text is required", "hint": "pass the headline/about copy to vary"}
    n = max(2, min(int(n) if str(n).isdigit() else 3, 5))
    limit = LIMITS.get(kind, 220)
    core = re.split(r"[|—·\n]", text)[0].strip() or text
    toks = _tokens(text)[:4]
    kw = " · ".join(toks[:3])
    angles = [
        ("impact", core),
        ("keyword-rich", f"{core} | {kw}" if kw else core),
        ("personable", f"{core} — {('passionate about ' + toks[0]) if toks else 'always learning'}"),
        ("outcome", f"{core} | helping teams ship {toks[0] if toks else 'great products'}"),
        ("concise", core[:80]),
    ]
    seen, variants = set(), []
    for label, v in angles:
        v = v.strip(" |—-·")
        if v and v.lower() not in seen:
            seen.add(v.lower())
            variants.append({"label": label, "text": v, "chars": len(v), "within_limit": len(v) <= limit})
        if len(variants) >= n:
            break
    return {"kind": kind, "limit": limit, "variants": variants}


@mcp.tool
def list_target_roles() -> list[str]:
    """List target roles with built-in recruiter keyword sets for keyword_audit."""
    return sorted(ROLE_KEYWORDS)


if __name__ == "__main__":
    mcp.run()
