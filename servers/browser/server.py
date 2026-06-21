"""browser — a stateful, authenticated browser agent (async Playwright).

Drives ONE real Chromium that stays open across tool calls, with a PERSISTENT profile on disk — so
cookies/logins survive restarts. It can navigate anywhere, perceive the page (elements/snapshot),
fill forms, log in / sign up, capture an SPA's API responses, manage tabs, upload/download files,
run JS, screenshot, and replay saved macros. Visible by default so you can clear a CAPTCHA / 2FA.

Needs the browser extra:  uv sync --group browser && uv run playwright install chromium
All tools are async (Playwright's async API shares the server's event loop) and never raise — on any
failure they return {ok: False, error, hint?}.

Architecture: each interactive action has a lock-free `_do_*` coroutine; the public @mcp.tool wraps it
with the single asyncio lock. `run_macro` replays a sequence of `_do_*` actions under one lock.
"""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urljoin, urlsplit

from mcp_base import data_dir, err, make_server, ok, repo_root, scrape

mcp = make_server(
    "browser",
    instructions=("Stateful browser agent. open(url) starts a real Chromium (persistent profile -> "
                  "stays logged in). Perceive: elements()/snapshot(). Act: goto/click/type/fill/"
                  "fill_by_label/fill_form/press/select_option/submit/hover/double_click/drag/scroll. "
                  "Auth: login(url,user,password); signup_autofill() from profile.json. Get results: "
                  "get_text/content/extract/contacts/data/query/links/tables/capture(API JSON)/"
                  "screenshot/save_pdf. Tabs: new_tab/list_tabs/switch_tab/close_tab. Files: upload/"
                  "download. Cookies + accept_cookies. Repeatable flows: save_macro/run_macro. "
                  "Visible by default for CAPTCHA/2FA."),
)

PROFILE_DIR = data_dir("browser") / "profile"
SESSIONS_DIR = data_dir("browser") / "sessions"
SHOTS_DIR = data_dir("browser") / "screenshots"
DOWNLOADS_DIR = data_dir("browser") / "downloads"
MACROS_DIR = data_dir("browser") / "macros"
DEFAULT_TIMEOUT = 30000  # ms

# Live browser state (created lazily, reused across calls in the server's event loop).
_pw = None
_ctx = None
_page = None
_lock = asyncio.Lock()

_USER_SELECTORS = ("input[type=email]", "input[name*=email i]", "input[id*=email i]",
                   "input[name*=user i]", "input[id*=user i]", "input[autocomplete=username]",
                   "input[name*=login i]", "input[type=text]")
_PASS_SELECTORS = ("input[type=password]", "input[name*=pass i]", "input[id*=pass i]")
_SUBMIT_SELECTORS = ("button[type=submit]", "input[type=submit]",
                     "button:has-text('Log in')", "button:has-text('Sign in')",
                     "button:has-text('Continue')", "button:has-text('Sign up')",
                     "button:has-text('Log In')", "button:has-text('Login')")
_COOKIE_SELECTORS = ("#onetrust-accept-btn-handler", "[aria-label*=accept i]",
                     "button:has-text('Accept all')", "button:has-text('Accept All')",
                     "button:has-text('Accept')", "button:has-text('I agree')",
                     "button:has-text('Agree')", "button:has-text('Allow all')",
                     "button:has-text('Got it')", "button:has-text('OK')")


def _need_playwright() -> dict:
    return err("playwright is not installed",
               hint="uv sync --group browser && uv run playwright install chromium")


async def _ensure(headless: bool = False) -> dict:
    """Start the persistent browser context if not already running. Returns {} on success or an err."""
    global _pw, _ctx, _page
    if _ctx is not None and _page is not None:
        return {}
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return _need_playwright()
    try:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        _pw = await async_playwright().start()
        _ctx = await _pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), headless=headless,
            accept_downloads=True, viewport={"width": 1366, "height": 900},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        )
        _ctx.set_default_timeout(DEFAULT_TIMEOUT)
        _page = _ctx.pages[0] if _ctx.pages else await _ctx.new_page()
        return {}
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        out = {"ok": False, "error": msg}
        if "Executable doesn't exist" in msg or "playwright install" in msg.lower():
            out["hint"] = "uv run playwright install chromium"
        await _teardown()
        return out


async def _teardown() -> None:
    global _pw, _ctx, _page
    try:
        if _ctx is not None:
            await _ctx.close()
    except Exception:
        pass
    try:
        if _pw is not None:
            await _pw.stop()
    except Exception:
        pass
    _pw = _ctx = _page = None


async def _state() -> dict:
    """Current url + title (best-effort)."""
    try:
        return {"url": _page.url, "title": (await _page.title()) or ""}
    except Exception:
        return {"url": None, "title": ""}


async def _find(selectors) -> str | None:
    """First selector that matches a visible element, else None."""
    for sel in selectors:
        try:
            loc = _page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                return sel
        except Exception:
            continue
    return None


def _safe(name: str, default: str = "out") -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name) if name else default


# ---------- lock-free action impls (assume _page is set; used by tools + macros) ----------
async def _do_goto(url: str) -> dict:
    full = url if url.startswith(("http://", "https://")) else f"https://{url}"
    await _page.goto(full, wait_until="domcontentloaded")
    return ok(**await _state())


async def _do_click(selector: str = "", text: str = "") -> dict:
    if text and not selector:
        await _page.get_by_text(text, exact=False).first.click()
    else:
        await _page.locator(selector).first.click()
    await _page.wait_for_load_state("domcontentloaded")
    return ok(clicked=selector or text, **await _state())


async def _do_type(selector: str, text: str, clear: bool = True) -> dict:
    loc = _page.locator(selector).first
    await (loc.fill(text) if clear else loc.type(text))
    return ok(typed_into=selector)


async def _do_fill(selector: str, value: str) -> dict:
    await _page.locator(selector).first.fill(value)
    return ok(filled=selector)


async def _do_fill_by_label(label: str, value: str) -> dict:
    try:
        await _page.get_by_label(label, exact=False).first.fill(value)
        return ok(filled_label=label)
    except Exception:
        # fallback: <label>text</label> -> for=id
        lid = await _page.evaluate(
            """(t)=>{const ls=[...document.querySelectorAll('label')];
                 const m=ls.find(l=>l.innerText.toLowerCase().includes(t.toLowerCase()));
                 if(!m) return null; if(m.htmlFor) return '#'+m.htmlFor;
                 const i=m.querySelector('input,textarea,select'); return i&&i.id?'#'+i.id:null;}""",
            label)
        if not lid:
            raise
        await _page.locator(lid).first.fill(value)
        return ok(filled_label=label, selector=lid)


async def _do_press(key: str) -> dict:
    await _page.keyboard.press(key)
    return ok(pressed=key)


async def _do_select(selector: str, value: str) -> dict:
    try:
        await _page.locator(selector).first.select_option(value=value)
    except Exception:
        await _page.locator(selector).first.select_option(label=value)
    return ok(selected=value, selector=selector)


async def _do_submit(selector: str = "") -> dict:
    target = selector or await _find(_SUBMIT_SELECTORS)
    if target:
        await _page.locator(target).first.click()
    else:
        await _page.keyboard.press("Enter")
    await _page.wait_for_load_state("domcontentloaded")
    return ok(submitted=target or "Enter", **await _state())


async def _do_hover(selector: str) -> dict:
    await _page.locator(selector).first.hover()
    return ok(hovered=selector)


async def _do_double_click(selector: str) -> dict:
    await _page.locator(selector).first.dblclick()
    return ok(double_clicked=selector)


async def _do_drag(source: str, target: str) -> dict:
    await _page.locator(source).first.drag_to(_page.locator(target).first)
    return ok(dragged=source, to=target)


async def _do_scroll(to: str = "bottom") -> dict:
    if to == "bottom":
        await _page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    elif to == "top":
        await _page.evaluate("window.scrollTo(0, 0)")
    else:
        await _page.evaluate(f"window.scrollBy(0, {int(re.sub(r'[^0-9]', '', to) or 0)})")
    return ok(scrolled=to)


async def _do_wait_for(selector: str, timeout: int = 15000) -> dict:
    await _page.locator(selector).first.wait_for(timeout=timeout)
    return ok(appeared=selector, **await _state())


async def _do_wait_for_text(text: str, timeout: int = 15000) -> dict:
    await _page.get_by_text(text, exact=False).first.wait_for(timeout=timeout)
    return ok(found_text=text, **await _state())


async def _do_wait_for_url(pattern: str, timeout: int = 15000) -> dict:
    pat = pattern if any(c in pattern for c in "*?") else f"**{pattern}**"
    await _page.wait_for_url(pat, timeout=timeout)
    return ok(**await _state())


async def _do_accept_cookies() -> dict:
    sel = await _find(_COOKIE_SELECTORS)
    if not sel:
        return ok(accepted=False, note="no cookie banner found")
    await _page.locator(sel).first.click()
    return ok(accepted=True, via=sel)


# ---------- lifecycle ----------
@mcp.tool
async def open(url: str = "", headless: bool = False) -> dict:
    """Start the browser (persistent profile -> stays logged in) and optionally navigate to url.
    Visible by default so you can solve CAPTCHA / 2FA. Returns current url + title."""
    async with _lock:
        e = await _ensure(headless)
        if e:
            return e
        if url:
            try:
                await _do_goto(url)
            except Exception as ex:  # noqa: BLE001
                return err(f"opened browser but navigation failed: {ex}", **await _state())
        return ok(**await _state())


@mcp.tool
async def close() -> dict:
    """Close the browser (the persistent profile keeps you logged in next time)."""
    async with _lock:
        await _teardown()
        return ok(closed=True)


@mcp.tool
async def status() -> dict:
    """Is the browser running? Current url + title."""
    if _page is None:
        return ok(running=False)
    return ok(running=True, **await _state())


# ---------- navigation ----------
@mcp.tool
async def goto(url: str) -> dict:
    """Navigate to a URL in the current browser."""
    async with _lock:
        e = await _ensure()
        if e:
            return e
        try:
            return await _do_goto(url)
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def back() -> dict:
    """Go back one page."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            await _page.go_back(wait_until="domcontentloaded")
            return ok(**await _state())
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def reload() -> dict:
    """Reload the current page."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            await _page.reload(wait_until="domcontentloaded")
            return ok(**await _state())
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


# ---------- interaction ----------
@mcp.tool
async def click(selector: str = "", text: str = "") -> dict:
    """Click an element by CSS `selector`, or by visible `text` (e.g. a button/link label)."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            return await _do_click(selector, text)
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def type(selector: str, text: str, clear: bool = True) -> dict:
    """Type `text` into the element matched by `selector` (clears it first by default)."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            return await _do_type(selector, text, clear)
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def fill(selector: str, value: str) -> dict:
    """Set the value of an input/textarea matched by `selector`."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            return await _do_fill(selector, value)
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def fill_by_label(label: str, value: str) -> dict:
    """Fill the field whose visible LABEL matches `label` (robust when you don't know the selector)."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            return await _do_fill_by_label(label, value)
        except Exception as ex:  # noqa: BLE001
            return err(f"no field for label '{label}': {ex}")


@mcp.tool
async def fill_form(fields: dict, submit: bool = False) -> dict:
    """Fill several fields at once: {css_selector: value, ...}. Optionally submit afterward."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        filled, errors = [], {}
        for sel, val in (fields or {}).items():
            try:
                await _page.locator(sel).first.fill(str(val))
                filled.append(sel)
            except Exception as ex:  # noqa: BLE001
                errors[sel] = str(ex)
        if submit:
            try:
                await _do_submit()
            except Exception as ex:  # noqa: BLE001
                errors["__submit__"] = str(ex)
        return ok(filled=filled, errors=errors, submitted=submit, **await _state())


@mcp.tool
async def press(key: str) -> dict:
    """Press a keyboard key (e.g. 'Enter', 'Tab', 'Escape', 'Control+A')."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            return await _do_press(key)
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def select_option(selector: str, value: str) -> dict:
    """Choose an option in a <select> by value or visible label."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            return await _do_select(selector, value)
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def submit(selector: str = "") -> dict:
    """Submit the current form: click `selector`, else auto-detect a submit button, else press Enter."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            return await _do_submit(selector)
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def hover(selector: str) -> dict:
    """Hover the mouse over an element (reveals menus/tooltips)."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            return await _do_hover(selector)
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def double_click(selector: str) -> dict:
    """Double-click an element."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            return await _do_double_click(selector)
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def drag(source: str, target: str) -> dict:
    """Drag the `source` element onto the `target` element."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            return await _do_drag(source, target)
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def accept_cookies() -> dict:
    """Find and click a cookie/consent 'Accept' button (common selectors + button text)."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            return await _do_accept_cookies()
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


# ---------- login / signup ----------
@mcp.tool
async def login(url: str, username: str, password: str, user_selector: str = "",
                pass_selector: str = "", submit_selector: str = "") -> dict:
    """Navigate to a sign-in page and log in. Auto-detects the username/password/submit fields when
    selectors aren't given. The persistent profile keeps you logged in for future runs. If a CAPTCHA /
    2FA appears, finish it in the visible window — the session is saved either way."""
    async with _lock:
        e = await _ensure(headless=False)  # login needs a visible window for CAPTCHA/2FA
        if e:
            return e
        try:
            await _do_goto(url)
            usel = user_selector or await _find(_USER_SELECTORS)
            if not usel:
                return err("could not find a username/email field", hint="pass user_selector",
                           **await _state())
            await _page.locator(usel).first.fill(username)
            psel = pass_selector or await _find(_PASS_SELECTORS)
            if not psel:  # two-step flows reveal password after submitting the username
                nxt = await _find(_SUBMIT_SELECTORS)
                if nxt:
                    await _page.locator(nxt).first.click()
                    await _page.wait_for_load_state("domcontentloaded")
                    psel = pass_selector or await _find(_PASS_SELECTORS)
            if not psel:
                return err("found username but no password field", hint="pass pass_selector",
                           **await _state())
            await _page.locator(psel).first.fill(password)
            ssel = submit_selector or await _find(_SUBMIT_SELECTORS)
            if ssel:
                await _page.locator(ssel).first.click()
            else:
                await _page.keyboard.press("Enter")
            try:
                await _page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            logged_in = await _find(_PASS_SELECTORS) is None
            return ok(logged_in=logged_in,
                      note="if a CAPTCHA/2FA is showing, complete it in the window; session is saved",
                      **await _state())
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def signup_autofill(url: str = "", submit: bool = False, overrides: dict | None = None) -> dict:
    """Fill a signup/contact form from profile.json (name/email/phone/location), with `overrides`
    winning per field (e.g. {'password': '...'}). Never invents a password. Returns which fields were
    filled. Optionally submit."""
    async with _lock:
        e = await _ensure(headless=False)
        if e:
            return e
        try:
            if url:
                await _do_goto(url)
            prof = {}
            try:
                prof = json.loads((repo_root() / "profile.json").read_text())
            except Exception:
                pass
            ov = overrides or {}
            full = ov.get("name") or prof.get("name") or ""
            first = ov.get("first_name") or (full.split()[0] if full else "")
            last = ov.get("last_name") or (full.split()[-1] if len(full.split()) > 1 else "")
            values = {
                "email": ov.get("email") or prof.get("email") or "",
                "first": first, "last": last, "name": full,
                "phone": ov.get("phone") or prof.get("phone") or "",
                "username": ov.get("username") or "",
                "location": ov.get("location") or prof.get("location") or "",
                "password": ov.get("password") or "",
            }
            # field -> selectors to try (by type/name/id/placeholder/autocomplete)
            field_sel = {
                "email": ["input[type=email]", "input[name*=email i]", "input[id*=email i]"],
                "first": ["input[name*=first i]", "input[id*=first i]", "input[autocomplete=given-name]"],
                "last": ["input[name*=last i]", "input[id*=last i]", "input[autocomplete=family-name]"],
                "name": ["input[name*=name i]", "input[id*=name i]", "input[autocomplete=name]"],
                "phone": ["input[type=tel]", "input[name*=phone i]", "input[id*=phone i]"],
                "username": ["input[name*=user i]", "input[id*=user i]"],
                "location": ["input[name*=city i]", "input[name*=location i]", "input[name*=address i]"],
                "password": ["input[type=password]"],
            }
            filled = {}
            for field, sels in field_sel.items():
                val = values.get(field)
                if not val:
                    continue
                sel = await _find(sels)
                if sel:
                    try:
                        await _page.locator(sel).first.fill(str(val))
                        filled[field] = sel
                    except Exception:
                        pass
            submitted = False
            if submit:
                try:
                    await _do_submit()
                    submitted = True
                except Exception:
                    pass
            note = None
            if "password" in field_sel and not values["password"] and await _find(_PASS_SELECTORS):
                note = "a password field exists but no password was given — pass overrides={'password': ...}"
            return ok(filled=list(filled), field_selectors=filled, submitted=submitted,
                      note=note, **await _state())
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


# ---------- perception ----------
_ELEMENTS_JS = r"""
(limit) => {
  const out = [];
  const sels = 'a,button,input,select,textarea,[role=button],[role=link],[role=tab],[onclick],' +
               'h1,h2,h3,nav,main,aside,[role=main],[role=navigation],[role=search],[role=dialog]';
  const seen = new Set();
  const css = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
    const t = el.getAttribute('data-testid'); if (t) return '[data-testid="' + t + '"]';
    const tag = el.tagName.toLowerCase();
    const sib = [...document.querySelectorAll(tag)].indexOf(el);
    return tag + ':nth-of-type(' + (sib + 1) + ')';
  };
  const ctx = (el) => {
    const a = el.closest('[role=dialog],dialog,[role=navigation],nav,main,[role=main],aside,section,form,header,footer');
    if (!a || a === el) return undefined;
    return a.getAttribute('role') || a.tagName.toLowerCase();
  };
  const LM = new Set(['nav','main','aside']);
  for (const el of document.querySelectorAll(sels)) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || tag;
    let kind = 'interactive';
    if (/^h[1-3]$/.test(tag)) kind = 'heading';
    else if (LM.has(tag) || ['main','navigation','search','dialog'].includes(role)) kind = 'landmark';
    const label = (el.innerText || el.value || el.placeholder ||
                   el.getAttribute('aria-label') || el.name || '').trim().slice(0, 100);
    if (kind !== 'interactive' && !label) continue;
    const sel = css(el);
    if (seen.has(sel)) continue; seen.add(sel);
    out.push({kind, role, text: label, type: el.getAttribute('type') || undefined,
              name: el.name || undefined, selector: sel, context: ctx(el)});
    if (out.length >= limit) break;
  }
  return out;
}
"""


@mcp.tool
async def elements(limit: int = 120) -> dict:
    """Perceive the page: interactive elements (links/buttons/inputs) PLUS headings & landmarks
    (nav/main/dialog) with ready-to-use selectors, role, visible text, and a `context` hint (the nearest
    section/dialog/nav) — so you can act and understand structure without guessing selectors."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            items = await _page.evaluate(_ELEMENTS_JS, max(1, min(int(limit), 400)))
            kinds = {}
            for it in items:
                kinds[it.get("kind", "?")] = kinds.get(it.get("kind", "?"), 0) + 1
            return ok(count=len(items), kinds=kinds, elements=items, **await _state())
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def snapshot() -> dict:
    """Accessibility-tree snapshot of the page (YAML a11y outline — roles + names, what a screen reader
    sees). Great for deciding what to act on."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            tree = await _page.locator("body").aria_snapshot()
            lines = tree.splitlines()
            return ok(outline="\n".join(lines[:600]), nodes=len(lines), truncated=len(lines) > 600)
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


# ---------- extraction ----------
@mcp.tool
async def get_text(selector: str = "") -> dict:
    """Visible text of the element matched by `selector`, or the whole page when omitted."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            txt = (await _page.locator(selector).first.inner_text()) if selector \
                else (await _page.inner_text("body"))
            return ok(text=txt[:200000], chars=len(txt), **await _state())
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def content(format: str = "markdown") -> dict:
    """Clean main content of the current page as markdown|text|html (trafilatura -> BeautifulSoup)."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            html, base = await _page.content(), _page.url
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))
    out = scrape.main_content(html, base, format)
    return ok(format=format, chars=len(out or ""), content=out, url=base)


@mcp.tool
async def extract() -> dict:
    """Structured data of the CURRENT (possibly logged-in) page: JSON-LD, OpenGraph, Twitter, meta,
    canonical, lang, h1/h2 outline."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            html, base = await _page.content(), _page.url
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))
    return ok(url=base, **scrape.structured(html, base))


@mcp.tool
async def contacts() -> dict:
    """Emails, phones, and social-profile links found on the CURRENT page (works behind a login)."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            html = await _page.content()
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))
    return ok(**scrape.contacts(html), url=_page.url)


@mcp.tool
async def data() -> dict:
    """One-shot page intelligence: title + structured data + contacts + link counts for the current page."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            html, base = await _page.content(), _page.url
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))
    st = scrape.structured(html, base)
    lk = scrape.links(html, base)
    return ok(url=base, title=st.get("title"), description=st.get("description"),
              structured=st, contacts=scrape.contacts(html),
              links={"internal": len(lk["internal"]), "external": len(lk["external"])})


@mcp.tool
async def query(selector: str, attr: str = "", all: bool = True) -> dict:
    """Extract text (or an attribute) from elements matched by `selector`. all=True returns every match."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            loc = _page.locator(selector)
            n = await loc.count()
            out = []
            for i in (range(n) if all else range(min(n, 1))):
                el = loc.nth(i)
                out.append((await el.get_attribute(attr)) if attr else (await el.inner_text()))
            return ok(selector=selector, count=n, results=[r for r in out if r is not None][:500])
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def links() -> dict:
    """All hyperlinks on the current page, absolute-resolved, split internal vs external."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            html, base = await _page.content(), _page.url
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))
    return ok(url=base, **scrape.links(html, base))


@mcp.tool
async def tables() -> dict:
    """Extract every HTML <table> on the current page as JSON records."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            html = await _page.content()
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))
    out = scrape.tables(html)
    return ok(table_count=len(out), tables=out)


@mcp.tool
async def capture(url: str = "", url_filter: str = "", wait_ms: int = 6000) -> dict:
    """Capture the JSON/XHR responses the page fetches (the reliable way to scrape data-heavy SPAs).
    Navigates to `url` (or reloads), records JSON responses whose URL contains `url_filter`, and
    returns [{url, status, json}]. Great when the visible HTML has no data but an API populates it."""
    async with _lock:
        e = await _ensure()
        if e:
            return e
        captured: list = []

        def on_response(resp):
            try:
                ct = resp.headers.get("content-type", "")
                if "json" in ct and (not url_filter or url_filter in resp.url):
                    captured.append(resp)
            except Exception:
                pass

        _page.on("response", on_response)
        try:
            if url:
                await _do_goto(url)
            else:
                await _page.reload(wait_until="domcontentloaded")
            await _page.wait_for_timeout(max(0, min(int(wait_ms), 30000)))
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))
        finally:
            try:
                _page.remove_listener("response", on_response)
            except Exception:
                pass
        out, seen = [], set()
        for resp in captured:
            key = (resp.url, resp.status)
            if key in seen:
                continue
            seen.add(key)
            try:
                out.append({"url": resp.url, "status": resp.status, "json": await resp.json()})
            except Exception:
                continue
            if len(out) >= 40:
                break
        return ok(url=_page.url, captured=len(out), responses=out)


# ---------- collecting (pagination / infinite scroll) ----------
@mcp.tool
async def collect(item_selector: str, max_items: int = 100, strategy: str = "scroll",
                  next_selector: str = "", attr: str = "") -> dict:
    """Accumulate items across infinite scroll or pagination. strategy='scroll' scrolls until no new
    items (or max), 'next' clicks `next_selector` between pages. Returns each item's text (or `attr`)."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            max_items = max(1, min(int(max_items), 1000))
        except (TypeError, ValueError):
            max_items = 100
        seen, items = set(), []

        async def harvest():
            loc = _page.locator(item_selector)
            for i in range(await loc.count()):
                if len(items) >= max_items:
                    break
                el = loc.nth(i)
                try:
                    v = (await el.get_attribute(attr)) if attr else (await el.inner_text())
                except Exception:
                    v = None
                if v and v not in seen:
                    seen.add(v)
                    items.append(v)

        try:
            for _ in range(40):
                await harvest()
                if len(items) >= max_items:
                    break
                if strategy == "next":
                    nxt = next_selector or "a[rel=next], a:has-text('Next'), button:has-text('Next')"
                    loc = _page.locator(nxt).first
                    if not await loc.count():
                        break
                    await loc.click()
                    await _page.wait_for_load_state("domcontentloaded")
                else:
                    before = len(seen)
                    await _page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await _page.wait_for_timeout(1200)
                    await harvest()
                    if len(seen) == before:
                        break
            return ok(item_selector=item_selector, count=len(items), items=items[:max_items])
        except Exception as ex:  # noqa: BLE001
            return err(str(ex), count=len(items), items=items)


# ---------- screenshot / pdf ----------
@mcp.tool
async def screenshot(name: str = "", full_page: bool = True) -> dict:
    """Save a PNG screenshot of the current page. Returns the file path."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            SHOTS_DIR.mkdir(parents=True, exist_ok=True)
            path = SHOTS_DIR / f"{_safe(name, 'shot')}.png"
            await _page.screenshot(path=str(path), full_page=full_page)
            return ok(path=str(path), **await _state())
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def save_pdf(name: str = "") -> dict:
    """Save the current page as a PDF (Chromium supports this in headless mode). Falls back to a
    full-page screenshot with a note if the browser is running visibly."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        SHOTS_DIR.mkdir(parents=True, exist_ok=True)
        path = SHOTS_DIR / f"{_safe(name, 'page')}.pdf"
        try:
            await _page.pdf(path=str(path))
            return ok(path=str(path), **await _state())
        except Exception as ex:  # noqa: BLE001
            try:
                png = SHOTS_DIR / f"{_safe(name, 'page')}.png"
                await _page.screenshot(path=str(png), full_page=True)
                return ok(path=str(png), note=f"PDF needs headless mode ({ex}); saved a PNG instead",
                          **await _state())
            except Exception as ex2:  # noqa: BLE001
                return err(str(ex2))


# ---------- tabs ----------
@mcp.tool
async def new_tab(url: str = "") -> dict:
    """Open a new tab (and optionally navigate). It becomes the active tab."""
    global _page
    async with _lock:
        e = await _ensure()
        if e:
            return e
        try:
            _page = await _ctx.new_page()
            if url:
                await _do_goto(url)
            return ok(tab_index=_ctx.pages.index(_page), **await _state())
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def list_tabs() -> dict:
    """List open tabs with their index, url, and title; marks the active one."""
    async with _lock:
        if _ctx is None:
            return err("browser not open")
        out = []
        for i, p in enumerate(_ctx.pages):
            try:
                out.append({"index": i, "url": p.url, "title": await p.title(),
                            "active": p is _page})
            except Exception:
                out.append({"index": i, "url": getattr(p, "url", None), "active": p is _page})
        return ok(count=len(out), tabs=out)


@mcp.tool
async def switch_tab(index: int) -> dict:
    """Make tab `index` the active tab (and bring it to front)."""
    global _page
    async with _lock:
        if _ctx is None:
            return err("browser not open")
        try:
            _page = _ctx.pages[int(index)]
            await _page.bring_to_front()
            return ok(tab_index=int(index), **await _state())
        except Exception as ex:  # noqa: BLE001
            return err(f"no tab {index}: {ex}")


@mcp.tool
async def close_tab(index: int) -> dict:
    """Close tab `index`. The active tab falls back to the first remaining one."""
    global _page
    async with _lock:
        if _ctx is None:
            return err("browser not open")
        try:
            p = _ctx.pages[int(index)]
            await p.close()
            _page = _ctx.pages[0] if _ctx.pages else None
            return ok(closed_index=int(index), remaining=len(_ctx.pages))
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


# ---------- files ----------
@mcp.tool
async def upload(selector: str, path: str) -> dict:
    """Upload a local file into the file input matched by `selector`."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            await _page.locator(selector).first.set_input_files(path)
            return ok(uploaded=path, selector=selector)
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def download(trigger_selector: str = "", url: str = "", name: str = "") -> dict:
    """Download a file by clicking `trigger_selector` (or navigating to `url`). Saves under the
    browser data dir and returns the path."""
    async with _lock:
        if _page is None and not url:
            return err("browser not open")
        e = await _ensure()
        if e:
            return e
        try:
            DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
            async with _page.expect_download(timeout=60000) as dl:
                if trigger_selector:
                    await _page.locator(trigger_selector).first.click()
                elif url:
                    await _page.goto(url if url.startswith("http") else f"https://{url}")
                else:
                    return err("pass trigger_selector or url")
            d = await dl.value
            path = DOWNLOADS_DIR / (_safe(name) + "_" + d.suggested_filename if name else d.suggested_filename)
            await d.save_as(str(path))
            return ok(path=str(path), filename=d.suggested_filename)
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


# ---------- cookies ----------
@mcp.tool
async def get_cookies() -> dict:
    """List cookies for the current context."""
    async with _lock:
        if _ctx is None:
            return err("browser not open")
        try:
            cookies = await _ctx.cookies()
            return ok(count=len(cookies), cookies=cookies[:200])
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def set_cookie(name: str, value: str, domain: str = "", path: str = "/") -> dict:
    """Add a cookie. Domain defaults to the current page's host."""
    async with _lock:
        if _ctx is None:
            return err("browser not open")
        try:
            dom = domain or (urlsplit(_page.url).hostname if _page else "")
            await _ctx.add_cookies([{"name": name, "value": value, "domain": dom, "path": path}])
            return ok(set=name, domain=dom)
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def clear_cookies() -> dict:
    """Clear all cookies in the current context."""
    async with _lock:
        if _ctx is None:
            return err("browser not open")
        try:
            await _ctx.clear_cookies()
            return ok(cleared=True)
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


# ---------- control ----------
@mcp.tool
async def wait_for(selector: str, timeout: int = 15000) -> dict:
    """Wait until an element matching `selector` appears (timeout in ms)."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            return await _do_wait_for(selector, timeout)
        except Exception as ex:  # noqa: BLE001
            return err(f"'{selector}' did not appear: {ex}")


@mcp.tool
async def wait_for_text(text: str, timeout: int = 15000) -> dict:
    """Wait until `text` appears anywhere on the page."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            return await _do_wait_for_text(text, timeout)
        except Exception as ex:  # noqa: BLE001
            return err(f"text '{text}' did not appear: {ex}")


@mcp.tool
async def wait_for_url(pattern: str, timeout: int = 15000) -> dict:
    """Wait until the URL matches `pattern` (substring or glob like '**/dashboard')."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            return await _do_wait_for_url(pattern, timeout)
        except Exception as ex:  # noqa: BLE001
            return err(f"url did not match '{pattern}': {ex}")


@mcp.tool
async def evaluate(js: str) -> dict:
    """Run JavaScript in the page and return the (JSON-serializable) result."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            result = await _page.evaluate(js)
            try:
                json.dumps(result)
            except Exception:
                result = str(result)
            return ok(result=result)
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def scroll(to: str = "bottom") -> dict:
    """Scroll the page: 'bottom', 'top', or a pixel amount like '800'."""
    async with _lock:
        if _page is None:
            return err("browser not open")
        try:
            return await _do_scroll(to)
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


# ---------- sessions ----------
@mcp.tool
async def save_session(name: str = "default") -> dict:
    """Snapshot current cookies/localStorage to a named session file (multi-account support)."""
    async with _lock:
        if _ctx is None:
            return err("browser not open")
        try:
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            path = SESSIONS_DIR / f"{_safe(name, 'default')}.json"
            await _ctx.storage_state(path=str(path))
            return ok(saved=str(path), session=_safe(name, "default"))
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def list_sessions() -> dict:
    """List saved named sessions."""
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        return ok(sessions=sorted(p.stem for p in SESSIONS_DIR.glob("*.json")))
    except Exception as ex:  # noqa: BLE001
        return err(str(ex))


# ---------- macros (record & replay flows) ----------
_ACTIONS = {
    "goto": _do_goto, "click": _do_click, "type": _do_type, "fill": _do_fill,
    "fill_by_label": _do_fill_by_label, "press": _do_press, "select_option": _do_select,
    "submit": _do_submit, "hover": _do_hover, "double_click": _do_double_click, "drag": _do_drag,
    "scroll": _do_scroll, "wait_for": _do_wait_for, "wait_for_text": _do_wait_for_text,
    "wait_for_url": _do_wait_for_url, "accept_cookies": _do_accept_cookies,
}


@mcp.tool
async def save_macro(name: str, steps: list) -> dict:
    """Save a reusable flow. steps = [{"action": ..., "args": {...}}, ...]. Valid actions: goto, click,
    type, fill, fill_by_label, press, select_option, submit, hover, double_click, drag, scroll,
    wait_for, wait_for_text, wait_for_url, accept_cookies."""
    try:
        bad = [s.get("action") for s in (steps or []) if s.get("action") not in _ACTIONS]
        if bad:
            return err(f"unknown actions: {bad}", valid=sorted(_ACTIONS))
        MACROS_DIR.mkdir(parents=True, exist_ok=True)
        path = MACROS_DIR / f"{_safe(name, 'macro')}.json"
        path.write_text(json.dumps({"name": name, "steps": steps}, indent=2))
        return ok(saved=str(path), name=name, steps=len(steps or []))
    except Exception as ex:  # noqa: BLE001
        return err(str(ex))


@mcp.tool
async def list_macros() -> dict:
    """List saved macros."""
    try:
        MACROS_DIR.mkdir(parents=True, exist_ok=True)
        return ok(macros=sorted(p.stem for p in MACROS_DIR.glob("*.json")))
    except Exception as ex:  # noqa: BLE001
        return err(str(ex))


@mcp.tool
async def run_macro(name: str) -> dict:
    """Replay a saved macro: runs each step in order in the live browser. Stops at the first failure
    and reports how far it got."""
    async with _lock:
        e = await _ensure()
        if e:
            return e
        path = MACROS_DIR / f"{_safe(name, 'macro')}.json"
        if not path.exists():
            return err(f"no macro '{name}'", hint="save_macro(name, steps) first")
        try:
            steps = json.loads(path.read_text()).get("steps", [])
        except Exception as ex:  # noqa: BLE001
            return err(f"could not read macro: {ex}")
        ran = []
        for i, step in enumerate(steps):
            fn = _ACTIONS.get(step.get("action"))
            if not fn:
                return err(f"step {i}: unknown action {step.get('action')!r}", ran=ran)
            try:
                await fn(**(step.get("args") or {}))
                ran.append(step.get("action"))
            except Exception as ex:  # noqa: BLE001
                return err(f"step {i} ({step.get('action')}) failed: {ex}", ran=ran, **await _state())
        return ok(macro=name, steps_run=len(ran), actions=ran, **await _state())


if __name__ == "__main__":
    mcp.run()
