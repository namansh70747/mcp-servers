"""portfolio-site — generate a clean single-page portfolio website from profile.json, ready to
host free on GitHub Pages. Offline (Jinja2). Can also pull projects from GitHub, emits SEO meta,
an inline emoji favicon, and a gh-pages deploy helper."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import quote

import httpx
from jinja2 import Template
from mcp_base import data_dir, get_env, make_server

# GitHub usernames: alphanumeric or single hyphens, 1-39 chars.
_USER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")

mcp = make_server(
    "portfolio-site",
    instructions=("build_site(theme, github_username, out_dir) from profile.json; set_theme; "
                  "list_themes; projects_from_github; deploy_instructions."),
)

ROOT = Path(__file__).resolve().parents[2]
OUT = data_dir("portfolio-site") / "site"

THEMES = {
    "dark":     {"bg": "#0f1115", "fg": "#e6e6e6", "accent": "#7aa2f7", "card": "#171a21"},
    "light":    {"bg": "#ffffff", "fg": "#1f2937", "accent": "#1a56db", "card": "#f5f6f8"},
    "mint":     {"bg": "#0b1f1a", "fg": "#e7f5ef", "accent": "#4ade80", "card": "#12302a"},
    "paper":    {"bg": "#faf6ef", "fg": "#2d2a26", "accent": "#b45309", "card": "#f1e9da"},
    "nord":     {"bg": "#2e3440", "fg": "#eceff4", "accent": "#88c0d0", "card": "#3b4252"},
    "rose":     {"bg": "#1a0f14", "fg": "#fce7f0", "accent": "#fb7185", "card": "#2a1620"},
    "terminal": {"bg": "#0a0a0a", "fg": "#33ff66", "accent": "#33ff66", "card": "#0f1a0f"},
}

PAGE = Template("""<!doctype html>{% autoescape true %}
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ p.name }} — Portfolio</title>
<meta name="description" content="{{ meta.description }}">
{% if meta.keywords %}<meta name="keywords" content="{{ meta.keywords }}">{% endif %}
<link rel="canonical" href="{{ meta.url }}">
<meta property="og:type" content="website">
<meta property="og:title" content="{{ p.name }} — Portfolio">
<meta property="og:description" content="{{ meta.description }}">
{% if meta.url %}<meta property="og:url" content="{{ meta.url }}">{% endif %}
{% if meta.og_image %}<meta property="og:image" content="{{ meta.og_image }}">{% endif %}
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{{ p.name }} — Portfolio">
<meta name="twitter:description" content="{{ meta.description }}">
<link rel="icon" href="{{ meta.favicon }}">
<script type="application/ld+json">{{ meta.jsonld | safe }}</script>
<style>
:root{--bg:{{t.bg}};--fg:{{t.fg}};--accent:{{t.accent}};--card:{{t.card}}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.6}
.wrap{max-width:52rem;margin:0 auto;padding:3rem 1.25rem}
h1{font-size:2.4rem;margin:0}h2{margin-top:2.5rem;border-bottom:1px solid #ffffff22;padding-bottom:.3rem}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}.muted{opacity:.75}
.chips span{display:inline-block;background:var(--card);padding:.25rem .6rem;border-radius:1rem;margin:.2rem;font-size:.85rem}
.card{background:var(--card);padding:1rem 1.2rem;border-radius:.8rem;margin:.8rem 0}
.card .tech span{display:inline-block;background:#ffffff14;padding:.1rem .5rem;border-radius:.5rem;margin:.15rem;font-size:.78rem}
.stars{float:right;opacity:.8;font-size:.85rem}
</style></head><body><div class="wrap">
<h1>{{ p.name }}</h1>
<p class="muted">{{ p.headline }}</p>
<p>{% for k,v in (p.links or {}).items() %}{% if v %}<a href="{{v}}">{{k}}</a> · {% endif %}{% endfor %}</p>
{% if p.summary %}<p>{{ p.summary }}</p>{% endif %}
{% set sk = [] %}{% for grp in (p.skills or {}).values() %}{% for s in grp %}{% set _=sk.append(s) %}{% endfor %}{% endfor %}
{% if sk %}<h2>Skills</h2><div class="chips">{% for s in sk %}<span>{{s}}</span>{% endfor %}</div>{% endif %}
{% if projects and projects[0].name %}<h2>Projects</h2>
{% for pr in projects %}{% if pr.name %}<div class="card">{% if pr.stars is not none %}<span class="stars">⭐ {{pr.stars}}</span>{% endif %}<strong>{{pr.name}}</strong>
{% if pr.tagline %}— {{pr.tagline}}{% endif %}<div>{{pr.description}}</div>
{% if pr.tech %}<div class="tech">{% for tt in pr.tech %}<span>{{tt}}</span>{% endfor %}</div>{% endif %}
{% if pr.link %}<a href="{{pr.link}}">{{pr.link}}</a>{% endif %}</div>{% endif %}{% endfor %}{% endif %}
{% if p.experience and p.experience[0].company %}<h2>Experience</h2>
{% for e in p.experience %}{% if e.company %}<div class="card"><strong>{{e.role}}</strong>, {{e.company}}
<span class="muted">{{e.start}}–{{e.end}}</span>
{% if e.highlights %}<ul>{% for h in e.highlights %}<li>{{h}}</li>{% endfor %}</ul>{% endif %}</div>{% endif %}{% endfor %}{% endif %}
{% if p.education and p.education[0].school %}<h2>Education</h2>
{% for ed in p.education %}{% if ed.school %}<div class="card"><strong>{{ed.degree}}</strong>, {{ed.school}}
<span class="muted">{{ed.start}}–{{ed.end}}</span>{% if ed.details %}<div>{{ed.details}}</div>{% endif %}</div>{% endif %}{% endfor %}{% endif %}
{% if p.email %}<h2>Contact</h2><p><a href="mailto:{{p.email}}">{{p.email}}</a></p>{% endif %}
<p class="muted" style="margin-top:3rem">Built with portfolio-site · host free on GitHub Pages.</p>
</div></body></html>{% endautoescape %}""")


def _profile() -> dict:
    pf = ROOT / "profile.json"
    if pf.exists():
        try:
            data = json.loads(pf.read_text())
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001 — never let a malformed profile break a tool
            return {}
    return {}


def _favicon(name: str) -> str:
    emoji = "🚀"
    letters = "".join(c for c in name if c.isalpha())
    if letters:
        emoji = letters[0].upper()
    svg = (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
           f"<text y='52' font-size='52'>{emoji}</text></svg>")
    return "data:image/svg+xml," + quote(svg)


def _flat_skills(p: dict) -> list[str]:
    """Flatten profile skills to a list of strings, tolerating None/list/non-dict/non-list groups."""
    skills = p.get("skills")
    if not isinstance(skills, dict):
        return []
    out: list[str] = []
    for grp in skills.values():
        if isinstance(grp, (list, tuple)):
            out += [str(s) for s in grp if s]
    return out


def _safe_links(p: dict) -> dict:
    links = p.get("links")
    return links if isinstance(links, dict) else {}


def _normalize_profile(p: dict) -> dict:
    """Coerce a (possibly malformed) profile into types the template/_meta expect, without
    dropping good data: skills->dict-of-lists, links->dict, list sections->list-of-dicts."""
    p = dict(p) if isinstance(p, dict) else {}
    skills = p.get("skills")
    if isinstance(skills, dict):
        p["skills"] = {k: (v if isinstance(v, (list, tuple)) else ([v] if v else []))
                       for k, v in skills.items()}
    else:
        p["skills"] = {}
    p["links"] = _safe_links(p)

    def _listify(v):
        return list(v) if isinstance(v, (list, tuple)) else ([] if v in (None, "") else [v])

    for key in ("projects", "experience", "education"):
        val = p.get(key)
        entries = [dict(e) for e in val if isinstance(e, dict)] if isinstance(val, list) else []
        for e in entries:
            # Fields the template iterates over must be lists.
            for lk in ("tech", "highlights"):
                if lk in e:
                    e[lk] = _listify(e[lk])
        p[key] = entries
    return p


def _meta(p: dict, url: str, og_image: str, description: str) -> dict:
    desc = description or p.get("summary") or p.get("headline") or f"Portfolio of {p.get('name','')}"
    desc = str(desc)[:200]
    sk = _flat_skills(p)
    jsonld = json.dumps({
        "@context": "https://schema.org", "@type": "Person",
        "name": str(p.get("name") or ""), "description": desc,
        "url": url or _safe_links(p).get("website", ""),
        "knowsAbout": sk[:20],
        "sameAs": [v for v in _safe_links(p).values() if v],
    })
    return {"description": desc, "keywords": ", ".join(sk[:15]),
            "url": url or _safe_links(p).get("website", ""),
            "og_image": og_image, "favicon": _favicon(str(p.get("name") or "")), "jsonld": jsonld}


def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json"}
    tok = get_env("GITHUB_PERSONAL_ACCESS_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


@mcp.tool
def list_themes() -> list[str]:
    """Available site themes."""
    return list(THEMES)


@mcp.tool
def projects_from_github(username: str, limit: int = 6) -> dict:
    """Fetch a user's top non-fork repos as portfolio project dicts (name, tagline, link, tech,
    stars). Free GitHub REST; uses PAT if set. Returns an error dict offline/rate-limited."""
    if not isinstance(username, str) or not _USER_RE.match(username):
        return {"error": "invalid username (1-39 chars: letters, digits, hyphens)"}
    try:
        r = httpx.get(f"https://api.github.com/users/{username}/repos",
                      headers=_gh_headers(), params={"per_page": 100, "sort": "updated"}, timeout=25)
    except Exception as e:  # noqa: BLE001
        return {"error": f"request failed: {e}"}
    if r.status_code != 200:
        return {"error": f"github {r.status_code}", "hint": "set GITHUB_PERSONAL_ACCESS_TOKEN for limits"}
    if len(r.content) > 8 * 1024 * 1024:
        return {"error": "response too large"}
    try:
        payload = r.json()
    except Exception:  # noqa: BLE001
        return {"error": "invalid json from github"}
    if not isinstance(payload, list):
        return {"error": "unexpected github response"}
    repos = sorted([x for x in payload if not x.get("fork")],
                   key=lambda x: -(x.get("stargazers_count") or 0))[:limit]
    projects = [{"name": x.get("name", ""), "tagline": "", "description": x.get("description") or "",
                 "link": x.get("html_url", ""), "tech": [x["language"]] if x.get("language") else [],
                 "stars": x.get("stargazers_count") or 0} for x in repos]
    return {"username": username, "projects": projects}


@mcp.tool
def build_site(theme: str = "dark", out_dir: str = "", github_username: str = "",
               merge_github: bool = False, url: str = "", og_image: str = "",
               description: str = "") -> dict:
    """Generate index.html from profile.json. Optionally pull projects from GitHub (github_username;
    merge_github=True merges with profile projects, else GitHub fills in when profile has none).
    Adds SEO meta, Open Graph/Twitter cards, JSON-LD, and an inline favicon. Returns the path;
    push it to a gh-pages repo to host free."""
    t = THEMES.get(theme, THEMES["dark"])
    p = _normalize_profile(_profile())
    projects = [pr for pr in p.get("projects", []) if pr.get("name")]
    if github_username:
        gh = projects_from_github(github_username)
        if isinstance(gh, dict) and "projects" in gh:
            if merge_github or not projects:
                seen = {str(pr["name"]).lower() for pr in projects}
                projects += [g for g in gh["projects"] if str(g["name"]).lower() not in seen]
    for pr in projects:
        pr.setdefault("stars", None)
    meta = _meta(p, url, og_image, description)
    html = PAGE.render(p=p, t=t, projects=projects, meta=meta)
    out = Path(out_dir).expanduser() if out_dir else OUT
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(html, encoding="utf-8")
    # .nojekyll lets GitHub Pages serve files as-is
    (out / ".nojekyll").write_text("", encoding="utf-8")
    return {"path": str(out / "index.html"), "theme": theme, "projects": len(projects),
            "bytes": len(html),
            "deploy": "Push to a <user>.github.io repo or enable Pages on any repo's /docs or root."}


@mcp.tool
def deploy_instructions(repo: str = "", branch: str = "gh-pages") -> dict:
    """Return step-by-step free GitHub Pages deploy commands for the generated site."""
    site = str(OUT)
    steps = [
        f"cd {site}",
        "git init -b main" if not repo else f"git init -b main && git remote add origin {repo}",
        "git add -A && git commit -m 'Deploy portfolio'",
    ]
    if repo:
        steps += [
            f"git push -u origin main",
            f"# Then enable Pages: Settings → Pages → Source = '{branch}' or '/ (root)' on main",
        ]
    else:
        steps += ["# Create a repo named <username>.github.io for a user site, or push to any repo",
                  "git remote add origin https://github.com/<you>/<repo>.git",
                  "git push -u origin main"]
    return {"site_dir": site, "branch": branch, "steps": steps,
            "note": "A .nojekyll file is written so Pages serves the static HTML as-is."}


if __name__ == "__main__":
    mcp.run()
