"""Shared HTML extraction helpers — one source of truth for the scraper + browser servers.

All functions are pure, lazy-import bs4/trafilatura, and never raise (degrade to fallbacks/empties).
"""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlsplit

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
# phone: optional +cc then 2-5 grouped digit runs; validated to 7-15 digits downstream
PHONE_RE = re.compile(r"(?:(?:\+|00)\d{1,3}[\s.\-]?)?(?:\(?\d{2,4}\)?[\s.\-]?){2,5}\d{2,4}")
SOCIAL_HOSTS = {
    "github.com": "github", "linkedin.com": "linkedin", "twitter.com": "twitter", "x.com": "twitter",
    "instagram.com": "instagram", "youtube.com": "youtube", "facebook.com": "facebook",
    "t.me": "telegram", "medium.com": "medium", "dribbble.com": "dribbble", "behance.net": "behance",
    "mastodon.social": "mastodon", "bsky.app": "bluesky", "tiktok.com": "tiktok",
}


def soup(html: str):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html or "", "html.parser")


def bs4_main_and_text(html: str) -> tuple[str, str]:
    """(main_content_html, plain_text) via the readability heuristic."""
    try:
        s = soup(html)
        for tag in s(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
            tag.decompose()
        main = s.find("article") or s.find("main") or s.body or s
        text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
        return str(main), text
    except Exception:
        return "", ""


def main_content(html: str, url: str, fmt: str = "markdown") -> str:
    """Clean main content as markdown|text|html. trafilatura -> markdownify -> BeautifulSoup."""
    fmt = (fmt or "markdown").lower()
    if fmt not in ("markdown", "text", "html"):
        fmt = "markdown"
    if fmt in ("markdown", "text"):
        try:
            import trafilatura
            out = trafilatura.extract(html, output_format=("markdown" if fmt == "markdown" else "txt"),
                                      include_links=(fmt == "markdown"), include_tables=True, url=url)
            if out and out.strip():
                return out.strip()
        except Exception:
            pass
    main_html, text = bs4_main_and_text(html)
    if fmt == "html":
        return main_html
    if fmt == "text":
        return text
    try:
        from markdownify import markdownify as md
        out = md(main_html)
        if out and out.strip():
            return re.sub(r"\n{3,}", "\n\n", out).strip()
    except Exception:
        pass
    return text


def title(html: str) -> str:
    try:
        s = soup(html)
        if s.title and s.title.string:
            return s.title.string.strip()[:500]
        og = s.find("meta", property="og:title")
        if og and og.get("content"):
            return og["content"].strip()[:500]
    except Exception:
        pass
    return ""


def structured(html: str, url: str) -> dict:
    out: dict = {"title": None, "description": None, "canonical": None, "lang": None,
                 "opengraph": {}, "twitter": {}, "meta": {}, "jsonld": [],
                 "headings": {"h1": [], "h2": []}}
    try:
        s = soup(html)
        if s.title and s.title.string:
            out["title"] = s.title.string.strip()[:500]
        htmltag = s.find("html")
        if htmltag and htmltag.get("lang"):
            out["lang"] = htmltag.get("lang")
        for m in s.find_all("meta"):
            prop = (m.get("property") or "").lower()
            name = (m.get("name") or "").lower()
            content = m.get("content")
            if not content:
                continue
            if prop.startswith("og:"):
                out["opengraph"][prop[3:]] = content
            elif name.startswith("twitter:"):
                out["twitter"][name[8:]] = content
            elif name in ("description", "keywords", "author"):
                out["meta"][name] = content
        out["description"] = out["opengraph"].get("description") or out["meta"].get("description")
        can = s.find("link", rel=lambda v: v and "canonical" in v)
        if can and can.get("href"):
            out["canonical"] = urljoin(url, can["href"])
        for sc in s.find_all("script", attrs={"type": "application/ld+json"}):
            txt = sc.string or sc.get_text()
            if not txt:
                continue
            try:
                out["jsonld"].append(json.loads(txt))
            except Exception:
                continue
        out["headings"]["h1"] = [h.get_text(" ", strip=True) for h in s.find_all("h1")][:10]
        out["headings"]["h2"] = [h.get_text(" ", strip=True) for h in s.find_all("h2")][:20]
    except Exception:
        pass
    return out


def emails_from_html(html: str) -> dict:
    """{email: weight} — mailto links (weight 2) + plain-text matches (weight 1)."""
    found: dict[str, int] = {}
    if not html:
        return found
    text = html
    try:
        s = soup(html)
        for a in s.select("a[href^=mailto]"):
            em = a.get("href", "")[7:].split("?")[0].strip().lower()
            if em:
                found[em] = found.get(em, 0) + 2
        text = s.get_text(" ")
    except Exception:
        pass
    for em in EMAIL_RE.findall(text):
        found[em.lower()] = found.get(em.lower(), 0) + 1
    return found


def socials_from_html(html: str) -> dict:
    socials: dict[str, str] = {}
    try:
        for a in soup(html).find_all("a", href=True):
            href = a["href"]
            host = (urlsplit(href if href.startswith("http") else "https://" + href).hostname or "")
            host = host.lower()
            if host.startswith("www."):
                host = host[4:]
            key = SOCIAL_HOSTS.get(host)
            if key:
                socials.setdefault(key, href)
    except Exception:
        pass
    return socials


def phones(text: str) -> list:
    out: list = []
    for m in PHONE_RE.findall(text or ""):
        digits = re.sub(r"\D", "", m)
        if 7 <= len(digits) <= 15 and m.strip() not in out:
            out.append(m.strip())
        if len(out) >= 10:
            break
    return out


def contacts(html: str) -> dict:
    emails = emails_from_html(html)
    text = ""
    try:
        text = soup(html).get_text(" ")
    except Exception:
        pass
    return {"emails": [e for e, _ in sorted(emails.items(), key=lambda kv: -kv[1])],
            "phones": phones(text), "socials": socials_from_html(html)}


def links(html: str, base: str) -> dict:
    internal, external, seen = [], [], set()
    try:
        base_host = (urlsplit(base).hostname or "").lower()
        for a in soup(html).find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            absu = urljoin(base, href)
            if absu in seen:
                continue
            seen.add(absu)
            host = (urlsplit(absu).hostname or "").lower()
            (internal if host == base_host else external).append(absu)
    except Exception:
        pass
    return {"internal": internal[:500], "external": external[:500]}


def chunks(markdown: str, max_chars: int = 1200) -> list:
    """Split markdown into [(heading, text)] passages: segment by headings, then pack paragraphs to
    ~max_chars so each chunk is a coherent, embeddable unit tagged with its nearest heading."""
    md = (markdown or "").strip()
    if not md:
        return []
    # segment into (heading, body) by markdown headings
    segments: list[tuple[str, list[str]]] = []
    heading = ""
    buf: list[str] = []
    for line in md.splitlines():
        m = re.match(r"^#{1,6}\s+(.*)", line)
        if m:
            if buf:
                segments.append((heading, buf))
            heading = m.group(1).strip()[:200]
            buf = []
        else:
            buf.append(line)
    if buf:
        segments.append((heading, buf))
    if not segments:
        segments = [("", md.splitlines())]
    # pack each segment's paragraphs into <= max_chars chunks
    out: list[tuple[str, str]] = []
    for head, lines in segments:
        paras = re.split(r"\n\s*\n", "\n".join(lines).strip())
        cur = ""
        for para in paras:
            para = para.strip()
            if not para:
                continue
            if len(para) > max_chars:  # very long block: hard-split
                if cur:
                    out.append((head, cur.strip()))
                    cur = ""
                for i in range(0, len(para), max_chars):
                    out.append((head, para[i:i + max_chars].strip()))
                continue
            if len(cur) + len(para) + 2 > max_chars and cur:
                out.append((head, cur.strip()))
                cur = para
            else:
                cur = f"{cur}\n\n{para}" if cur else para
        if cur.strip():
            out.append((head, cur.strip()))
    return [(h, t) for h, t in out if t]


def tables(html: str) -> list:
    out = []
    try:
        for t in soup(html).find_all("table"):
            rows = t.find_all("tr")
            if not rows:
                continue
            headers = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
            recs = []
            for tr in rows[1:]:
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                if not cells:
                    continue
                recs.append(dict(zip(headers, cells)) if headers and len(headers) == len(cells)
                            else cells)
            if recs:
                out.append({"headers": headers, "rows": recs[:200]})
    except Exception:
        pass
    return out
