"""github-profile — build your GitHub profile README, pick pinned repos, draft repo descriptions
and per-repo READMEs from real repo data, and summarize language/contribution stats
(free GitHub REST API; uses your PAT for rate limits)."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import quote

from mcp_base import data_dir, get_env, http, make_server

mcp = make_server(
    "github-profile",
    instructions=("build_readme(username), suggest_pins, describe_repo, repo_readme, "
                  "language_stats, contribution_summary, profile_stats — from live GitHub data."),
)

OUT = data_dir("github-profile")
ROOT = Path(__file__).resolve().parents[2]
API = "https://api.github.com"


# GitHub usernames: alphanumeric or hyphen, 1-39 chars. Repos: name plus owner.
_USER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_MAX_BYTES = 8 * 1024 * 1024  # cap any single API response


def _valid_user(username: str) -> bool:
    return isinstance(username, str) and bool(_USER_RE.match(username))


def _valid_repo(full_name: str) -> bool:
    return isinstance(full_name, str) and bool(_REPO_RE.match(full_name))


def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json"}
    tok = get_env("GITHUB_PERSONAL_ACCESS_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _get(url, **params):
    # Uses the shared mcp_base.http helper (retry/backoff + default UA). Return shapes are
    # kept identical to the previous raw-httpx implementation: parsed JSON on 200, else an
    # {"__error__": ...} dict.
    r = http.request("GET", url, headers=_headers(), params=params or None, timeout=25)
    if not r.get("ok"):
        status = r.get("status")
        if status is None:
            return {"__error__": "request_failed", "detail": r.get("error", ""), "url": url}
        err = {"__error__": status, "url": url}
        if status == 403 and "rate limit" in (r.get("text") or "").lower():
            err["hint"] = "Rate limited. Set GITHUB_PERSONAL_ACCESS_TOKEN in .env for higher limits."
        elif status == 404:
            err["hint"] = "Not found — check the username/repo."
        return err
    if len(r.get("text") or "") > _MAX_BYTES:
        return {"__error__": "response_too_large", "url": url}
    if "json" not in r:
        return {"__error__": "invalid_json", "url": url}
    return r["json"]


def _all_repos(username: str, max_pages: int = 3) -> list | dict:
    if not _valid_user(username):
        return {"__error__": "invalid_username", "hint": "1-39 chars: letters, digits, hyphens"}
    out: list = []
    for page in range(1, max_pages + 1):
        repos = _get(f"{API}/users/{username}/repos", per_page=100, sort="updated", page=page)
        if isinstance(repos, dict):
            return repos if not out else out
        out.extend(repos)
        if len(repos) < 100:
            break
    return out


@mcp.tool
def suggest_pins(username: str, limit: int = 6) -> dict:
    """Top repos by stars for a user (good candidates to pin)."""
    repos = _all_repos(username)
    if isinstance(repos, dict):
        return repos
    ranked = sorted([r for r in repos if not r["fork"]], key=lambda r: -r["stargazers_count"])[:limit]
    return {"username": username, "pins": [{"name": r["name"], "stars": r["stargazers_count"],
            "language": r["language"], "description": r["description"]} for r in ranked]}


@mcp.tool
def describe_repo(full_name: str) -> dict:
    """Repo metadata (description, stars, language, topics) to help write a good blurb."""
    if not _valid_repo(full_name):
        return {"__error__": "invalid_repo", "hint": "expected owner/repo"}
    r = _get(f"{API}/repos/{full_name}")
    if "__error__" in r:
        return r
    return {"name": r["name"], "description": r["description"], "stars": r["stargazers_count"],
            "language": r["language"], "topics": r.get("topics", []), "homepage": r.get("homepage")}


@mcp.tool
def language_stats(username: str, top: int = 8) -> dict:
    """Aggregate primary languages across a user's non-fork repos, with percentages."""
    repos = _all_repos(username)
    if isinstance(repos, dict):
        return repos
    counts: Counter = Counter()
    for r in repos:
        if not r["fork"] and r.get("language"):
            counts[r["language"]] += 1
    total = sum(counts.values())
    if not total:
        return {"username": username, "languages": [], "note": "no language data"}
    langs = [{"language": lang, "repos": n, "pct": round(100 * n / total, 1)}
             for lang, n in counts.most_common(top)]
    return {"username": username, "total_repos_counted": total, "languages": langs}


@mcp.tool
def contribution_summary(username: str) -> dict:
    """Summarize a user's public footprint: followers, public repos, total stars, most-starred and
    recently-active repos, and primary languages (REST only, no GraphQL needed)."""
    if not _valid_user(username):
        return {"__error__": "invalid_username", "hint": "1-39 chars: letters, digits, hyphens"}
    user = _get(f"{API}/users/{username}")
    if "__error__" in user:
        return user
    repos = _all_repos(username)
    if isinstance(repos, dict):
        return repos
    own = [r for r in repos if not r["fork"]]
    total_stars = sum(r["stargazers_count"] for r in own)
    total_forks = sum(r["forks_count"] for r in own)
    most_starred = sorted(own, key=lambda r: -r["stargazers_count"])[:5]
    recent = sorted(own, key=lambda r: r["updated_at"], reverse=True)[:5]
    langs = Counter(r["language"] for r in own if r.get("language"))
    return {
        "username": username, "name": user.get("name"), "bio": user.get("bio"),
        "followers": user.get("followers"), "following": user.get("following"),
        "public_repos": user.get("public_repos"), "own_repos": len(own),
        "total_stars": total_stars, "total_forks": total_forks,
        "primary_languages": [l for l, _ in langs.most_common(5)],
        "most_starred": [{"name": r["name"], "stars": r["stargazers_count"]} for r in most_starred],
        "recently_active": [{"name": r["name"], "updated_at": r["updated_at"]} for r in recent],
    }


@mcp.tool
def profile_stats(username: str) -> dict:
    """Combined dashboard: user basics + language_stats + top repos in one call."""
    if not _valid_user(username):
        return {"__error__": "invalid_username", "hint": "1-39 chars: letters, digits, hyphens"}
    summary = contribution_summary(username)
    if "__error__" in summary:
        return summary
    langs = language_stats(username)
    summary["language_breakdown"] = langs.get("languages", [])
    return summary


def _badge(label: str, message: str, color: str = "blue") -> str:
    enc = lambda s: quote(str(s).replace("-", "--").replace("_", "__"), safe="")
    return f"https://img.shields.io/badge/{enc(label)}-{enc(message)}-{color}"


@mcp.tool
def repo_readme(full_name: str, write: bool = False) -> dict:
    """Draft a project README.md from repo metadata (description, topics, language, homepage,
    license) with a sensible scaffold and free shields.io badges. Returns markdown; optionally
    writes <repo>_README.md to this server's data dir."""
    if not _valid_repo(full_name):
        return {"__error__": "invalid_repo", "hint": "expected owner/repo"}
    r = _get(f"{API}/repos/{full_name}")
    if "__error__" in r:
        return r
    name = Path(str(r["name"])).name or "repo"
    desc = r.get("description") or "A project."
    lang = r.get("language")
    license_name = (r.get("license") or {}).get("spdx_id") if r.get("license") else None
    homepage = r.get("homepage")
    topics = r.get("topics", [])
    badges = [f"![stars]({_badge('stars', r['stargazers_count'], 'yellow')})"]
    if lang:
        badges.append(f"![lang]({_badge('built with', lang, 'informational')})")
    if license_name and license_name != "NOASSERTION":
        badges.append(f"![license]({_badge('license', license_name, 'green')})")
    lines = [f"# {name}", "", " ".join(badges), "", f"> {desc}", ""]
    if homepage:
        lines += [f"🔗 **Live:** {homepage}", ""]
    if topics:
        lines += ["**Topics:** " + ", ".join(f"`{t}`" for t in topics), ""]
    lines += ["## Features", "", "- TODO: highlight what makes this project useful", ""]
    lines += ["## Installation", "", "```bash",
              f"git clone https://github.com/{full_name}.git",
              f"cd {name}", "# install deps", "```", ""]
    lines += ["## Usage", "", "```bash", "# TODO: show a minimal example", "```", ""]
    lines += ["## Contributing", "",
              "Issues and PRs welcome. Open an issue to discuss larger changes.", ""]
    lines += ["## License", "",
              (f"Licensed under {license_name}." if license_name and license_name != "NOASSERTION"
               else "Add a LICENSE file to clarify usage rights."), ""]
    md = "\n".join(lines)
    path = OUT / f"{name}_README.md"
    if write:
        path.write_text(md, encoding="utf-8")
    return {"path": str(path) if write else None, "markdown": md}


@mcp.tool
def build_readme(username: str, write: bool = True, include_stats: bool = True,
                 include_badges: bool = True) -> dict:
    """Draft a GitHub profile README from profile.json + your top repos. include_stats adds a
    language breakdown; include_badges adds free shields.io stat badges. Writes README.md to this
    server's data dir (you copy it to your <username>/<username> repo)."""
    if not _valid_user(username):
        return {"__error__": "invalid_username", "hint": "1-39 chars: letters, digits, hyphens"}
    profile = {}
    pf = ROOT / "profile.json"
    if pf.exists():
        try:
            loaded = json.loads(pf.read_text())
            if isinstance(loaded, dict):
                profile = loaded
        except Exception:  # noqa: BLE001 — never let a malformed profile break the README
            profile = {}
    pins = suggest_pins(username, 6)
    skills = profile.get("skills")
    if not isinstance(skills, dict):
        skills = {}
    lines = [f"## Hi, I'm {profile.get('name') or username} 👋", ""]
    if profile.get("headline"):
        lines += [f"_{profile['headline']}_", ""]
    if profile.get("summary"):
        lines += [str(profile["summary"]), ""]
    if include_badges:
        badges = [
            f"![GitHub followers](https://img.shields.io/github/followers/{username}?style=social)",
            f"![GitHub stars](https://img.shields.io/github/stars/{username}?style=social)",
        ]
        lines += [" ".join(badges), ""]
    flat = [str(s) for grp in skills.values() if isinstance(grp, (list, tuple))
            for s in grp if s]
    if flat:
        lines += ["### 🛠️ Skills", "", " · ".join(flat), ""]
    if include_stats:
        ls = language_stats(username)
        if ls.get("languages"):
            lines += ["### 📊 Top languages", "",
                      " · ".join(f"{l['language']} {l['pct']}%" for l in ls["languages"][:6]), ""]
    if isinstance(pins, dict) and pins.get("pins"):
        lines += ["### 📌 Featured projects", ""]
        for p in pins["pins"]:
            lines.append(f"- **[{p['name']}](https://github.com/{username}/{p['name']})** "
                         f"— {p['description'] or ''} ⭐{p['stars']}")
        lines.append("")
    links = profile.get("links", {})
    if not isinstance(links, dict):
        links = {}
    social = " · ".join(f"[{k}]({v})" for k, v in links.items() if v)
    if social:
        lines += ["### 🔗 Links", "", social, ""]
    md = "\n".join(lines)
    path = OUT / "README.md"
    if write:
        path.write_text(md, encoding="utf-8")
    return {"path": str(path) if write else None, "markdown": md}


if __name__ == "__main__":
    mcp.run()
