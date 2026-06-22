"""whatsapp — a robust, self-learning WhatsApp connector driven through YOUR real Chrome.

Talks to anyone by **contact name or nickname** (fuzzy "did you mean?"), in **your own per-contact
voice** (learned from your real history, so it reads as you — not an AI), and does ~everything WhatsApp
can: send/read/react, media, groups, status, calls (best-effort), search. It drives WhatsApp's own
engine (WPPConnect `window.WPP`), injected by the one-time WhatsApp WPP Bridge extension
(integrations/whatsapp-wpp-extension) — no QR, no separate profile. A persistent SQLite memory
(nicknames, learned resolutions, corrections, per-contact tone profiles, an episode log) survives
sessions and makes it smarter over time. macOS + Google Chrome only.

Run `whatsapp.diagnose()` first; if `wpp_ready` is false, load the extension (its README has the steps).
All actions are non-blocking background jobs. Single file on purpose (the suite loads servers via
spec_from_file_location, which doesn't support intra-package imports).
"""
from __future__ import annotations

import base64
import difflib
import json
import mimetypes
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from mcp_base import (BaseStore, Jobs, chrome, db_path, err, get_env, get_env_int, make_server, ok)

mcp = make_server(
    "whatsapp",
    instructions=("Robust WhatsApp via YOUR real Chrome (WPP engine; no QR). Runs fully in the BACKGROUND "
                  "— it never focuses/opens the WhatsApp window. Talk to anyone by NAME or nickname — it "
                  "fuzzy-matches your real contacts and asks 'did you mean?' when unsure, and LEARNS your "
                  "pick. To sound like YOU: call contact_style(name) or compose(name, intent), write the "
                  "message in that exact voice, then send(name, text, exact=true). For a back-and-forth: "
                  "send(), use the returned chat_id with wait_for_reply(chat_id=...) to get their reply, "
                  "repeat. AUTOPILOT: when the user's prompt contains 'autopilot', carry the whole "
                  "conversation without asking approval per message (send→wait_for_reply→reply, stopping "
                  "on a stop-word/idle/max-turns); for hands-off/session-independent use, autopilot_brief() "
                  "+ background.run(task=directive). Also react/read/media/groups/status + memory. Run "
                  "diagnose() first; load the WhatsApp WPP Bridge extension once if wpp_ready is false."),
)

WA = "web.whatsapp.com"
INLINE_WAIT = max(2, get_env_int("WHATSAPP_INLINE_WAIT", 14) or 14)
THROTTLE = max(0, get_env_int("WHATSAPP_THROTTLE_SECONDS", 3) or 3)
WPP_TIMEOUT = max(5, get_env_int("WHATSAPP_WPP_TIMEOUT", 30) or 30)
FUZZY_THRESHOLD = float(get_env("WHATSAPP_FUZZY_THRESHOLD", "0.72") or 0.72)
MAX_MEDIA = 16 * 1024 * 1024
MAX_MSG = 60000
# "autopilot" autonomous-conversation limits (the agent talks without per-message approval).
AUTOPILOT_STOPWORDS = [s.strip().lower() for s in (get_env(
    "WHATSAPP_AUTOPILOT_STOPWORDS",
    "bye,bye bye,ok bye,okay bye,goodbye,ttyl,talk later,gtg,good night,gn,stop,ruk,band karo") or "").split(",")
    if s.strip()]
AUTOPILOT_IDLE_TIMEOUT = max(30, get_env_int("WHATSAPP_AUTOPILOT_IDLE_TIMEOUT", 180) or 180)
AUTOPILOT_MAX_TURNS = max(1, get_env_int("WHATSAPP_AUTOPILOT_MAX_TURNS", 40) or 40)
JOBS = Jobs("whatsapp", max_concurrent=1, inline_wait=INLINE_WAIT)

try:
    mcp.local_provider.remove_tool("health")
except Exception:  # noqa: BLE001
    try:
        mcp.remove_tool("health")
    except Exception:  # noqa: BLE001
        pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================ persistent memory
_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts(id TEXT PRIMARY KEY, number TEXT, name TEXT, pushname TEXT,
  is_group INTEGER DEFAULT 0, is_my_contact INTEGER DEFAULT 0, updated TEXT);
CREATE TABLE IF NOT EXISTS aliases(alias TEXT PRIMARY KEY, contact_id TEXT, label TEXT, source TEXT,
  confidence REAL DEFAULT 0.5, hits INTEGER DEFAULT 1, last_used TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS episodes(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT,
  contact_id TEXT, summary TEXT, data_json TEXT);
CREATE TABLE IF NOT EXISTS styles(contact_id TEXT PRIMARY KEY, label TEXT, profile_json TEXT,
  samples_json TEXT, n_samples INTEGER DEFAULT 0, updated TEXT);
"""
_store: BaseStore | None = None


def store() -> BaseStore:
    global _store
    if _store is None:
        _store = BaseStore(db_path("whatsapp", "memory.db"), schema=_SCHEMA)
    return _store


def _norm(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def mem_upsert_contacts(rows: list[dict]) -> int:
    s = store()
    for c in rows or []:
        cid = c.get("id")
        if not cid:
            continue
        s.execute("INSERT INTO contacts(id,number,name,pushname,is_group,is_my_contact,updated) "
                  "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET number=excluded.number,"
                  "name=excluded.name,pushname=excluded.pushname,is_group=excluded.is_group,"
                  "is_my_contact=excluded.is_my_contact,updated=excluded.updated",
                  (cid, c.get("number", ""), c.get("name") or c.get("formatted") or "",
                   c.get("pushname", ""), 1 if c.get("isGroup") else 0,
                   1 if c.get("isMyContact") else 0, _now()))
    return mem_contacts_count()


def mem_all_contacts() -> list[dict]:
    return store().query("SELECT id,number,name,pushname,is_group FROM contacts")


def mem_contacts_count() -> int:
    return (store().query_one("SELECT COUNT(*) AS n FROM contacts") or {}).get("n", 0)


def mem_resolve_alias(alias: str) -> dict | None:
    a = _norm(alias)
    row = store().query_one("SELECT * FROM aliases WHERE alias=?", (a,))
    if not row:
        return None
    store().execute("UPDATE aliases SET hits=hits+1,last_used=?,confidence=MIN(1.0,confidence+0.02) "
                    "WHERE alias=?", (_now(), a))
    return {"contact_id": row["contact_id"], "confidence": row["confidence"], "label": row["label"]}


def mem_learn_alias(alias: str, contact_id: str, label: str = "", source: str = "auto",
                    confidence: float = 0.9) -> None:
    a = _norm(alias)
    if not a or not contact_id:
        return
    if store().query_one("SELECT alias FROM aliases WHERE alias=?", (a,)):
        store().execute("UPDATE aliases SET contact_id=?,label=?,source=?,confidence=MAX(confidence,?),"
                        "hits=hits+1,last_used=? WHERE alias=?",
                        (contact_id, label, source, confidence, _now(), a))
    else:
        store().execute("INSERT INTO aliases(alias,contact_id,label,source,confidence,hits,last_used,created)"
                        " VALUES(?,?,?,?,?,?,?,?)", (a, contact_id, label, source, confidence, 1, _now(), _now()))


def mem_forget_alias(alias: str) -> bool:
    a = _norm(alias)
    found = store().query_one("SELECT alias FROM aliases WHERE alias=?", (a,))
    store().execute("DELETE FROM aliases WHERE alias=?", (a,))
    return bool(found)


def mem_list_aliases() -> list[dict]:
    return store().query("SELECT alias,contact_id,label,source,confidence,hits,last_used FROM aliases "
                         "ORDER BY hits DESC,last_used DESC")


def mem_log(kind: str, contact_id: str = "", summary: str = "", data: dict | None = None) -> None:
    store().execute("INSERT INTO episodes(ts,kind,contact_id,summary,data_json) VALUES(?,?,?,?,?)",
                    (_now(), kind, contact_id, summary, json.dumps(data or {})))


def mem_recent(limit: int = 30, contact_id: str = "") -> list[dict]:
    if contact_id:
        return store().query("SELECT ts,kind,contact_id,summary FROM episodes WHERE contact_id=? "
                             "ORDER BY id DESC LIMIT ?", (contact_id, max(1, int(limit))))
    return store().query("SELECT ts,kind,contact_id,summary FROM episodes ORDER BY id DESC LIMIT ?",
                         (max(1, int(limit)),))


def mem_save_style(contact_id: str, label: str, profile: dict, samples: list[str]) -> None:
    store().execute("INSERT INTO styles(contact_id,label,profile_json,samples_json,n_samples,updated) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(contact_id) DO UPDATE SET label=excluded.label,"
                    "profile_json=excluded.profile_json,samples_json=excluded.samples_json,"
                    "n_samples=excluded.n_samples,updated=excluded.updated",
                    (contact_id, label, json.dumps(profile), json.dumps(samples[:40]), len(samples), _now()))


def mem_get_style(contact_id: str) -> dict | None:
    row = store().query_one("SELECT * FROM styles WHERE contact_id=?", (contact_id,))
    if not row:
        return None
    return {"contact_id": contact_id, "label": row["label"], "n_samples": row["n_samples"],
            "profile": json.loads(row["profile_json"] or "{}"), "samples": json.loads(row["samples_json"] or "[]")}


def mem_digest() -> dict:
    return {"contacts_known": mem_contacts_count(), "aliases": mem_list_aliases()[:50],
            "recent_episodes": mem_recent(20),
            "styles_learned": (store().query_one("SELECT COUNT(*) AS n FROM styles") or {}).get("n", 0)}


# ============================================================ WPP bridge (window.WPP via Chrome)
_EXT_HINT = ("WPP engine not present — load the one-time 'WhatsApp WPP Bridge' extension (chrome://"
             "extensions → Developer mode → Load unpacked → integrations/whatsapp-wpp-extension/) and "
             "reload web.whatsapp.com. See its README.")


def j(x) -> str:
    return json.dumps(x, ensure_ascii=False)


def wpp_status() -> dict:
    if not chrome.chrome_running():
        return {"chrome_running": False, "ready": False, "error": f"{chrome.CHROME_APP} isn't running"}
    en, hint = chrome.js_enabled()
    if not en:
        return {"chrome_running": True, "js_from_apple_events": False, "ready": False, "error": hint}
    if not chrome.find_tab(WA):
        return {"chrome_running": True, "js_from_apple_events": True, "wpp": False, "ready": False,
                "error": "no web.whatsapp.com tab open — open it (logged in)"}
    # window.WPP / window.__WA_BRIDGE__ live in the page's MAIN world, invisible to AppleScript's
    # isolated-world JS. The extension mirrors its state to <html> attributes (shared DOM) instead.
    okj, val = chrome.run_js(WA, "(function(){var r=document.documentElement;return JSON.stringify({"
                             "bridge:r.getAttribute('data-wa-bridge'),ready:r.getAttribute('data-wa-ready'),"
                             "build:r.getAttribute('data-wa-build'),patch:r.getAttribute('data-wa-patch'),"
                             "pane:document.querySelector('#pane-side')?1:0});})()")
    if not okj:
        return {"chrome_running": True, "js_from_apple_events": True, "ready": False, "error": str(val)}
    info = val if isinstance(val, dict) else {}
    bridge = str(info.get("bridge")) == "1"
    ready = str(info.get("ready")) == "1"
    out = {"chrome_running": True, "js_from_apple_events": True, "wpp": ready, "wpp_ready": ready,
           "extension": bridge, "ready": ready, "build": info.get("build")}
    if not bridge:
        out["error"] = _EXT_HINT
    elif not ready:
        out["error"] = ("WhatsApp Web is still loading / wa-js is booting — keep the tab open and wait "
                        "~30s after the chat list appears, then try again")
    return out


def wpp_ready() -> tuple[bool, str]:
    s = wpp_status()
    return (True, "") if s.get("ready") else (False, s.get("error", "WPP not ready"))


def wpp_raw(expr: str, timeout: int | None = None) -> tuple[bool, object]:
    okr, msg = wpp_ready()
    if not okr:
        return False, msg
    # Calls run in the page's MAIN world via the extension's shared-DOM relay (window.WPP is invisible
    # to AppleScript's isolated world). `expr` evaluates to a value or Promise.
    return chrome.relay_call(WA, expr, timeout=timeout or WPP_TIMEOUT)


def wpp_get_all_contacts() -> tuple[bool, object]:
    # WPP.contact.list() returns ALL known wids (often 10k+). Keep saved address-book entries
    # (those with a name) for name/nickname resolution, and cap to bound the relay payload.
    return wpp_raw("window.WPP.contact.list().then(function(cs){return cs.filter(function(c){"
                   "return c&&(c.name||c.pushname);}).slice(0,3000).map(function(c){"
                   "return {id:(c.id&&c.id._serialized)||String(c.id),number:(c.id&&c.id.user)||'',"
                   "name:c.name||'',pushname:c.pushname||'',formatted:c.formattedName||c.verifiedName||'',"
                   "isMyContact:!!c.name};});})")


def wpp_get_all_chats() -> tuple[bool, object]:
    return wpp_raw("window.WPP.chat.list().then(function(cs){return cs.map(function(c){"
                   "return {id:(c.id&&c.id._serialized)||String(c.id),"
                   "name:c.formattedTitle||(c.contact&&(c.contact.name||c.contact.pushname))||'',"
                   "isGroup:!!c.isGroup,unread:c.unreadCount||0,t:c.t||0};});})")


def wpp_query_exists(number: str) -> tuple[bool, object]:
    return wpp_raw(f"window.WPP.contact.queryExists({j(number)}).then(function(r){{return r?"
                   "{exists:true,id:(r.wid&&r.wid._serialized)||r._serialized||String(r)}:{exists:false};})")


# Reusable JS to find a chat in ChatStore by serialized id / phone number / lid, then read its
# ALREADY-LOADED messages SYNCHRONOUSLY. We avoid WPP.chat.getMessages() — that does a server fetch
# that hangs on this build. ChatStore.get(...).msgs holds the recent messages and updates live over the
# socket, so once a chat is active (e.g. right after we send to it), reads are instant + reliable.
_FIND_CHAT = (
    "function __findChat(ref){var CS=window.WPP.whatsapp.ChatStore,c=null;"
    "try{c=CS.get(ref);}catch(e){}if(c)return c;"
    "var num=String(ref).split('@')[0].replace(/\\D/g,'');var arr=CS.getModelsArray();"
    "for(var i=0;i<arr.length;i++){var x=arr[i];var id=(x.id&&x.id._serialized)||'';"
    "var u=((x.id&&x.id.user)||'').replace(/\\D/g,'');var cu='';"
    "try{cu=((x.contact&&x.contact.id&&x.contact.id.user)||'').replace(/\\D/g,'');}catch(e){}"
    "if(id===ref||(num&&(u===num||cu===num||(num.length>=8&&(u.indexOf(num)>=0||cu.indexOf(num)>=0)))))return x;}"
    "return null;}"
)


def _msgs_js(ref_json: str, count: int) -> str:
    """A SYNC expression returning {found, chat, msgs:[{id,from_me,type,body,t}]} for a chat ref."""
    return ("(function(){" + _FIND_CHAT + "var c=__findChat(" + ref_json + ");if(!c)return {found:false};"
            "var ms=(c.msgs&&c.msgs.getModelsArray)?c.msgs.getModelsArray():[];"
            "return {found:true,chat:(c.id&&c.id._serialized)||'',msgs:ms.slice(-" + str(int(count)) +
            ").map(function(m){return {id:(m.id&&m.id._serialized)||'',from_me:!!(m.id&&m.id.fromMe),"
            "type:m.type,body:(m.body||m.caption||''),t:m.t||0};})};})()")


def wpp_chat_messages(chat_ref: str, count: int = 30) -> tuple[bool, object]:
    """Recent messages of a chat (sync ChatStore). Returns (ok, {found, chat, msgs[]})."""
    return wpp_raw(_msgs_js(j(chat_ref), count))


def wpp_get_my_messages(chat_id: str, count: int = 80) -> tuple[bool, object]:
    okr, val = wpp_chat_messages(chat_id, count)
    if not okr:
        return okr, val
    if not (isinstance(val, dict) and val.get("found")):
        return True, []
    msgs = [{"body": m["body"], "t": m["t"]} for m in val.get("msgs", [])
            if m.get("from_me") and m.get("type") == "chat" and m.get("body")]
    return True, msgs


def chat_id_from_msg_id(msg_id: str) -> str:
    """A WPP message id is '<dir>_<chatId>_<hash>[_out]' — extract the chat id (works for @c.us/@lid)."""
    parts = (msg_id or "").split("_")
    return parts[1] if len(parts) >= 2 and ("@" in parts[1]) else ""


def is_stopword(text: str) -> bool:
    """True if a message is an autopilot stop signal (bye / ttyl / stop / …)."""
    t = (text or "").strip().lower().strip(".!?…। ")
    return t in AUTOPILOT_STOPWORDS


def wpp_send_text(chat_id: str, text: str) -> tuple[bool, object]:
    # WA >= 2.3000 keys chats by LID. Resolve @c.us → actual WID (may be @lid) before sending.
    cid_js = j(chat_id)
    txt_js = j(text)
    expr = (
        "(function(){var cid=" + cid_js + ",txt=" + txt_js + ";"
        "function doSend(wid){return window.WPP.chat.sendTextMessage(wid,txt,{createChat:true})"
        ".then(function(r){return {sent:true,id:(r&&r.id)?String(r.id):''}});}"
        "if(cid.indexOf('@c.us')>=0){"
        "return window.WPP.contact.queryExists(cid.split('@')[0])"
        ".then(function(r){var wid=(r&&r.wid&&r.wid._serialized)||(r&&r._serialized)||cid;"
        "return doSend(wid);}).catch(function(){return doSend(cid);});}"
        "return doSend(cid);})()"
    )
    return wpp_raw(expr)


# ============================================================ contact resolution
def normalize_phone(query: str) -> str | None:
    c = (query or "").strip()
    digits = re.sub(r"[^\d]", "", c)
    return digits if (re.fullmatch(r"\+?[\d][\d\s\-()]{6,}", c) and 7 <= len(digits) <= 15) else None


def chat_id_for_number(digits: str) -> str:
    return f"{digits}@c.us"


def ensure_contacts(force: bool = False) -> tuple[bool, object]:
    if not force and mem_contacts_count() > 0:
        return True, mem_contacts_count()
    okc, val = wpp_get_all_contacts()
    if not okc:
        return False, val
    if isinstance(val, list):
        return True, mem_upsert_contacts(val)
    return False, "unexpected contact payload"


def _names(c: dict) -> list[str]:
    return [n for n in (c.get("name"), c.get("pushname")) if n]


def _score(query: str, name: str) -> float:
    q, n = query.lower().strip(), name.lower().strip()
    if not q or not n:
        return 0.0
    ratio = difflib.SequenceMatcher(None, q, n).ratio()
    if q == n:
        return 1.0
    if n.startswith(q) or q in n.split():
        ratio = max(ratio, 0.9)
    elif q in n:
        ratio = max(ratio, 0.82)
    qt, nt = set(q.split()), set(n.split())
    if qt & nt:
        ratio = max(ratio, 0.6 + 0.35 * (len(qt & nt) / len(qt)))
    return round(ratio, 3)


def resolve_contact_logic(query: str, auto_learn: bool = True) -> dict:
    q = (query or "").strip()
    if not q:
        return {"status": "none", "confidence": 0.0, "via": "empty", "candidates": []}
    phone = normalize_phone(q)
    if phone:
        return {"status": "phone", "via": "phone", "confidence": 1.0,
                "contact": {"id": chat_id_for_number(phone), "number": phone, "name": q}}
    al = mem_resolve_alias(q)
    if al and al.get("contact_id"):
        cid = al["contact_id"]
        row = store().query_one("SELECT id,number,name,pushname FROM contacts WHERE id=?", (cid,))
        nm = (row or {}).get("name") or (row or {}).get("pushname") or al.get("label") or q
        return {"status": "resolved", "via": "memory", "confidence": float(al.get("confidence") or 0.9),
                "contact": {"id": cid, "number": (row or {}).get("number", ""), "name": nm}}
    okc, cnt = ensure_contacts()
    if not okc:
        return {"status": "none", "via": "wpp_error", "confidence": 0.0, "candidates": [], "error": str(cnt)}
    scored = []
    for c in mem_all_contacts():
        best = max((_score(q, nm) for nm in _names(c)), default=0.0)
        if best > 0.45:
            scored.append((best, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return {"status": "none", "via": "fuzzy", "confidence": 0.0, "candidates": []}
    top_score, top = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    cand = [{"id": c["id"], "name": (c.get("name") or c.get("pushname") or ""),
             "number": c.get("number", ""), "score": round(sc, 3)} for sc, c in scored[:6]]
    if top_score >= FUZZY_THRESHOLD and (top_score - second) >= 0.08:
        nm = top.get("name") or top.get("pushname") or ""
        if auto_learn and top_score >= 0.9:
            mem_learn_alias(q, top["id"], label=nm, source="auto-fuzzy", confidence=top_score)
        return {"status": "resolved", "via": "fuzzy", "confidence": top_score,
                "contact": {"id": top["id"], "number": top.get("number", ""), "name": nm}}
    return {"status": "ambiguous", "via": "fuzzy", "confidence": top_score, "candidates": cand,
            "hint": "say which one (re-send with exact=true), or set_nickname(name, number)"}


# ============================================================ per-contact tone/style
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF❤⁉‼]")
_DEVANAGARI = re.compile("[ऀ-ॿ]")
_HINGLISH = {"hai", "nahi", "nahin", "kya", "kyu", "kyun", "bhai", "yaar", "acha", "accha", "theek",
             "thik", "kar", "raha", "rahe", "rha", "mat", "haan", "han", "nai", "abhi", "kal", "aaj",
             "bhaiya", "ji", "matlab", "chal", "chalo", "arre", "arey", "oye", "kr", "krna", "bata",
             "batao", "kaisa", "kaise", "hu", "hoon", "pohunch", "ghar", "khana", "kha", "liya", "maa"}
_SLANG = {"lol", "lmao", "haha", "hahaha", "hehe", "btw", "idk", "tbh", "fr", "ngl", "ok", "okk",
          "okay", "k", "kk", "hmm", "hmmm", "yep", "yup", "nah", "bruh", "omg", "brb", "gn", "gm"}


def analyze_style(samples: list[str]) -> dict:
    msgs = [m for m in (s.strip() for s in samples) if m]
    n = len(msgs)
    if not n:
        return {"n": 0}
    emoji_msgs = lower_msgs = no_end = devanagari = hinglish = 0
    openers, emojis, slang = Counter(), Counter(), Counter()
    for m in msgs:
        if _EMOJI.search(m):
            emoji_msgs += 1
            emojis.update(_EMOJI.findall(m))
        if [c for c in m if c.isalpha()] and m == m.lower():
            lower_msgs += 1
        if m[-1] not in ".!?":
            no_end += 1
        if _DEVANAGARI.search(m):
            devanagari += 1
        lw = re.findall(r"[a-z]+", m.lower())
        if any(w in _HINGLISH for w in lw):
            hinglish += 1
        for w in lw:
            if w in _SLANG:
                slang[w] += 1
        if m.split():
            openers[m.split()[0].lower().strip(".,!?")] += 1
    avg_words = round(sum(len(m.split()) for m in msgs) / n, 1)
    lang = ("hindi (devanagari)" if devanagari > n * 0.4 else
            "hinglish (roman)" if hinglish > n * 0.25 else "english")
    return {"n": n, "language": lang, "avg_words": avg_words,
            "avg_chars": round(sum(len(m) for m in msgs) / n, 1),
            "emoji_rate": round(emoji_msgs / n, 2), "top_emojis": [e for e, _ in emojis.most_common(8)],
            "mostly_lowercase": lower_msgs >= n * 0.5, "rarely_ends_punctuation": no_end >= n * 0.6,
            "common_openers": [w for w, _ in openers.most_common(6) if w],
            "slang": [w for w, _ in slang.most_common(10)], "short_style": avg_words <= 6}


def style_instruction(label: str, p: dict, samples: list[str]) -> str:
    if not p or not p.get("n"):
        return (f"No saved history with {label or 'this contact'} yet — write a short, natural, casual "
                "message; avoid formal/AI-sounding phrasing.")
    bits = [f"Write the next WhatsApp message AS the person who chats with {label or 'them'} — match THIS "
            "exact personal style so it reads as them, NOT an AI:",
            f"- language: {p['language']}",
            f"- length: ~{p['avg_words']} words ({'very short/casual' if p.get('short_style') else 'medium'})",
            f"- casing: {'mostly lowercase, ' if p.get('mostly_lowercase') else ''}"
            f"{'usually no ending period' if p.get('rarely_ends_punctuation') else 'normal punctuation'}",
            f"- emoji: {'uses ' + ' '.join(p['top_emojis']) if p.get('emoji_rate', 0) > 0.15 and p.get('top_emojis') else 'rarely uses emoji'}"]
    if p.get("slang"):
        bits.append(f"- words/slang they use: {', '.join(p['slang'])}")
    if p.get("common_openers"):
        bits.append(f"- often opens with: {', '.join(p['common_openers'])}")
    bits.append("- NO assistant vibe: no 'Sure!', no over-formal grammar, no sign-offs; keep their voice.")
    if samples:
        bits.append("Real examples (mimic this voice):\n" + "\n".join(f"  • {s}" for s in samples[:12]))
    return "\n".join(bits)


def style_build(contact_id: str, label: str = "", count: int = 80) -> dict:
    okm, val = wpp_get_my_messages(contact_id, count=count)
    if not okm:
        return {"ok": False, "error": str(val)}
    samples = [m.get("body", "") for m in val if isinstance(m, dict)] if isinstance(val, list) else []
    samples = [s for s in samples if s.strip()]
    profile = analyze_style(samples)
    mem_save_style(contact_id, label, profile, samples)
    return {"ok": True, "contact_id": contact_id, "label": label, "profile": profile,
            "n_samples": len(samples), "instruction": style_instruction(label, profile, samples),
            "samples": samples[:15]}


def style_get_or_build(contact_id: str, label: str = "", min_samples: int = 8) -> dict:
    cached = mem_get_style(contact_id)
    if cached and cached.get("n_samples", 0) >= min_samples:
        return {"ok": True, "contact_id": contact_id, "label": cached.get("label") or label,
                "profile": cached["profile"], "n_samples": cached["n_samples"], "samples": cached["samples"][:15],
                "instruction": style_instruction(cached.get("label") or label, cached["profile"], cached["samples"])}
    return style_build(contact_id, label)


# ============================================================ shared helpers
def _need_ready() -> dict | None:
    okr, msg = wpp_ready()
    return None if okr else err(msg, ready=False)


def _resolve(query: str, exact: bool) -> tuple[dict | None, dict | None]:
    r = resolve_contact_logic(query)
    if r["status"] in ("phone", "resolved"):
        return r["contact"], None
    if exact and r.get("candidates"):
        return r["candidates"][0], None
    if r["status"] == "ambiguous":
        return None, ok(needs_confirmation=True, candidates=r["candidates"], query=query,
                        hint="which one? re-call with exact=true after picking, or set_nickname(name, number)")
    return None, err(f"couldn't find a contact matching '{query}'",
                     hint="use a phone number with country code, or set_nickname(name, number)",
                     **({"detail": r["error"]} if r.get("error") else {}))


def _act(kind: str, fn, contact_name: str = "", contact_id: str = "") -> dict:
    def worker(job):
        try:
            okf, val = fn()
        except Exception as e:  # noqa: BLE001
            JOBS.finish(job["id"], ok_=False, error=str(e))
            return
        if okf:
            JOBS.finish(job["id"], ok_=True, result=val)
            mem_log(kind, contact_id, summary=f"{kind} {contact_name}".strip())
        else:
            JOBS.finish(job["id"], ok_=False, error=str(val))
    return JOBS.run_or_job(kind, worker, inline_wait=INLINE_WAIT, contact=contact_name or None)


# ============================================================ tools: ops
@mcp.tool
def health() -> dict:
    """Quick readiness: Chrome running, JS-from-Apple-Events on, WPP engine present & ready."""
    return ok(server="whatsapp", **wpp_status())


@mcp.tool
def diagnose() -> dict:
    """Full status: WPP engine, which feature modules are present, and what memory has learned."""
    s = wpp_status()
    modules = {}
    if s.get("ready"):
        okj, val = chrome.relay_call(WA, "(function(){var w=window.WPP;return {chat:!!(w&&w.chat),"
                                     "contact:!!(w&&w.contact),group:!!(w&&w.group),status:!!(w&&w.status),"
                                     "call:!!(w&&w.call),profile:!!(w&&w.profile)};})()", timeout=8)
        modules = val if (okj and isinstance(val, dict)) else {}
    return ok(server="whatsapp", **s, modules=modules,
              memory={"contacts": mem_contacts_count(), "aliases": len(mem_list_aliases()),
                      "styles": mem_digest().get("styles_learned", 0)},
              setup=None if s.get("ready") else
              ["load the WhatsApp WPP Bridge extension (integrations/whatsapp-wpp-extension/README.md)",
               "reload web.whatsapp.com, then diagnose() again"])


@mcp.tool
def setup() -> dict:
    """How to enable the robust WPP engine (one-time)."""
    if chrome.chrome_running() and not chrome.find_tab(WA):
        chrome.open_tab(f"https://{WA}/")
    return ok(steps=["1. chrome://extensions → Developer mode → Load unpacked → "
                     "/Users/namansharma/mcp-servers/integrations/whatsapp-wpp-extension",
                     "2. Reload web.whatsapp.com (be logged in).",
                     "3. (already done) Chrome 'Allow JavaScript from Apple Events' + Automation grant.",
                     "Then diagnose() should show wpp_ready: true."])


# ============================================================ tools: contacts / memory
@mcp.tool
def resolve_contact(query: str) -> dict:
    """Resolve a name/nickname/number to a WhatsApp contact (fuzzy; returns candidates if unsure)."""
    if not (query or "").strip():
        return err("query is required")
    return ok(**resolve_contact_logic(query))


@mcp.tool
def refresh_contacts() -> dict:
    """Re-pull your full contact list from WhatsApp into memory."""
    if (e := _need_ready()):
        return e
    okc, cnt = ensure_contacts(force=True)
    return ok(contacts=cnt) if okc else err(str(cnt))


@mcp.tool
def check_on_whatsapp(number: str) -> dict:
    """Check if a phone number (with country code) is on WhatsApp."""
    if (e := _need_ready()):
        return e
    digits = normalize_phone(number)
    if not digits:
        return err("give a phone number with country code, e.g. +14155551234")
    okq, val = wpp_query_exists(digits)
    return ok(**val) if okq and isinstance(val, dict) else err(str(val))


@mcp.tool
def set_nickname(nickname: str, target: str) -> dict:
    """Teach a nickname → person. target = a phone number or an existing contact name."""
    if not (nickname or "").strip() or not (target or "").strip():
        return err("nickname and target are required")
    contact, resp = _resolve(target, exact=False)
    if resp and not contact:
        return resp
    mem_learn_alias(nickname, contact["id"], label=contact.get("name", ""), source="user", confidence=1.0)
    return ok(learned=nickname, contact=contact)


@mcp.tool
def list_nicknames() -> dict:
    """List learned nicknames/aliases and what they resolve to."""
    return ok(aliases=mem_list_aliases())


@mcp.tool
def forget(alias: str) -> dict:
    """Forget a learned nickname/alias."""
    return ok(forgot=alias) if mem_forget_alias(alias) else err(f"no alias '{alias}'")


@mcp.tool
def contact_memory(query: str) -> dict:
    """What the connector knows about a person: resolution, tone profile, recent episodes."""
    contact, resp = _resolve(query, exact=False)
    if resp and not contact:
        return resp
    st = mem_get_style(contact["id"])
    return ok(contact=contact, style=(st.get("profile") if st else None),
              style_samples=(len(st.get("samples", [])) if st else 0),
              recent=mem_recent(15, contact_id=contact["id"]))


@mcp.tool
def memory_digest() -> dict:
    """Everything learned: known contacts, nicknames, tone profiles, recent activity."""
    return ok(**mem_digest())


# ============================================================ tools: tone / composing
@mcp.tool
def contact_style(contact: str, refresh: bool = False) -> dict:
    """Learn/return how YOU write to this person (style profile + samples + a write-as-you instruction).
    Use this, then write the message in that voice and call send(exact=true)."""
    if (e := _need_ready()):
        return e
    c, resp = _resolve(contact, exact=False)
    if resp and not c:
        return resp
    res = style_build(c["id"], c.get("name", "")) if refresh else style_get_or_build(c["id"], c.get("name", ""))
    if not res.get("ok"):
        return err(res.get("error", "could not build style"), contact=c)
    return ok(contact=c, profile=res["profile"], n_samples=res["n_samples"],
              instruction=res["instruction"], samples=res["samples"])


@mcp.tool
def compose(contact: str, intent: str) -> dict:
    """Prepare to write in YOUR voice for `contact`: returns the style instruction + samples + intent.
    (Claude: write ONE message in this exact voice, then call send(contact, text, exact=true).)"""
    if not (intent or "").strip():
        return err("intent is required (what you want to say)")
    st = contact_style(contact)
    if not st.get("ok"):
        return st
    return ok(contact=st["contact"], intent=intent, instruction=st["instruction"],
              samples=st["samples"], n_samples=st["n_samples"],
              next="write ONE message in this exact voice, then send(contact, that_text, exact=true)")


# ============================================================ tools: messaging
@mcp.tool
def send(contact: str, message: str, exact: bool = False, dry_run: bool = False) -> dict:
    """Send by name/nickname/number. Ambiguous name → returns candidates (doesn't send); pick one and
    re-call with exact=true (the pick is learned). For YOUR voice, compose() first. Background job."""
    if not (contact or "").strip():
        return err("contact is required")
    if not (message or "").strip():
        return err("message is required")
    if len(message) > MAX_MSG:
        return err(f"message too long (>{MAX_MSG})")
    if (e := _need_ready()):
        return e
    c, resp = _resolve(contact, exact)
    if resp and not c:
        return resp
    cid, name = c["id"], c.get("name", contact)
    if dry_run:
        return ok(sent=False, dry_run=True, contact=c, preview=message[:300])

    def fn():
        okr, val = wpp_send_text(cid, message)
        if okr:
            mem_learn_alias(contact, cid, label=name, source="confirmed", confidence=0.97)
            if THROTTLE:
                time.sleep(THROTTLE)
            res = val if isinstance(val, dict) else {"sent": True}
            res.setdefault("sent", True)
            # surface the resolved chat id (the @lid/@c.us the message actually landed in) so the caller
            # can read / wait_for_reply on it reliably without re-resolving.
            res["chat_id"] = chat_id_from_msg_id(res.get("id", "")) or cid
            return okr, res
        return okr, (val if isinstance(val, dict) else {"sent": True})
    return _act("send", fn, contact_name=name, contact_id=cid)


@mcp.tool
def react(contact: str, emoji: str = "👍", exact: bool = False) -> dict:
    """React with an emoji to the latest message in a chat."""
    if (e := _need_ready()):
        return e
    c, resp = _resolve(contact, exact)
    if resp and not c:
        return resp
    expr = ("(function(){" + _FIND_CHAT + f"var c=__findChat({j(c['id'])});"
            "if(!c)throw new Error('chat not found');var ms=c.msgs.getModelsArray();"
            "var last=ms[ms.length-1];if(!last)throw new Error('no messages');"
            f"return window.WPP.chat.sendReactionToMessage(last.id._serialized,{j(emoji)})"
            ".then(function(){return {reacted:true};});})()")
    return _act("react", lambda: wpp_raw(expr), contact_name=c.get("name", ""), contact_id=c["id"])


@mcp.tool
def read_chat(contact: str, limit: int = 20, exact: bool = False) -> dict:
    """Read the latest messages of a chat (by name/number)."""
    if (e := _need_ready()):
        return e
    c, resp = _resolve(contact, exact)
    if resp and not c:
        return resp
    n = max(1, min(100, int(limit)))

    def fn():
        okr, val = wpp_chat_messages(c["id"], n)
        if not okr:
            return False, val
        if not (isinstance(val, dict) and val.get("found")):
            return True, {"messages": [], "note": "chat not loaded yet — send a message or open it once"}
        return True, {"chat": val.get("chat"), "messages": val.get("msgs", [])}
    return _act("read", fn, contact_name=c.get("name", ""), contact_id=c["id"])


@mcp.tool
def wait_for_reply(contact: str = "", chat_id: str = "", after_id: str = "", timeout: int = 90,
                   from_me: bool = False) -> dict:
    """Block until the OTHER person sends a NEW message in a chat, then return it (or time out).
    The autopilot loop primitive — one call == one conversational turn. Pass chat_id (from send()'s
    result) for reliability, or a contact name/number to resolve. Reads the LIVE ChatStore (no hanging
    server fetch); works fully in the background. `from_me` defaults False (their incoming message);
    set True only for the lid 'message-yourself' chat where your own typing shows as fromMe:true."""
    if (e := _need_ready()):
        return e
    ref = (chat_id or "").strip()
    if not ref:
        if not (contact or "").strip():
            return err("contact or chat_id is required")
        c, resp = _resolve(contact, True)
        if resp and not c:
            return resp
        ref = c["id"]
    timeout = max(5, min(240, int(timeout)))
    # Baseline the current messages; we return the first NEW message from the wanted direction.
    seen: set[str] = {after_id} if after_id else set()
    okr, val = wpp_chat_messages(ref, 30)
    if okr and isinstance(val, dict) and val.get("found"):
        ref = val.get("chat") or ref
        for m in val.get("msgs", []):
            seen.add(m.get("id"))
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        okr, val = wpp_chat_messages(ref, 30)
        if not (okr and isinstance(val, dict) and val.get("found")):
            continue
        for m in val.get("msgs", []):
            mid = m.get("id")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            if bool(m.get("from_me")) is bool(from_me):
                body = m.get("body", "")
                mem_log("reply_in", ref, summary=body[:80])
                return ok(message=body, id=mid, t=m.get("t", 0), from_me=m.get("from_me"),
                          chat_id=ref, is_stop=is_stopword(body))
    return ok(timeout=True, waited=timeout, chat_id=ref)


@mcp.tool
def autopilot_brief(contact: str, goal: str = "", exact: bool = False) -> dict:
    """Build a ready-to-spawn directive for a DETACHED autopilot chat (pass to background.run(task=...)).
    Returns the resolved contact + chat_id, the per-contact style profile, the recent thread, and a
    'directive' string a headless agent can follow to carry the conversation on its own."""
    if (e := _need_ready()):
        return e
    c, resp = _resolve(contact, exact)
    if resp and not c:
        return resp
    style = style_get_or_build(c["id"])
    okr, val = wpp_chat_messages(c["id"], 15)
    recent = val.get("msgs", []) if (okr and isinstance(val, dict) and val.get("found")) else []
    stops = ", ".join(AUTOPILOT_STOPWORDS)
    directive = (
        f"Hold a live WhatsApp conversation with {c.get('name') or contact} (whatsapp chat_id {c['id']}) "
        f"using the `whatsapp` MCP tools. Goal/opener: {goal or 'just catch up naturally'}.\n"
        f"Match this person's exact texting voice (see STYLE below) — short, natural, like the user, not "
        f"like an assistant. LOOP: whatsapp.send(...) → whatsapp.wait_for_reply(chat_id=<chat_id returned "
        f"by send>) → reply in their style → repeat. STOP when they say any of [{stops}], when "
        f"wait_for_reply returns is_stop or timeout (no reply for ~{AUTOPILOT_IDLE_TIMEOUT}s), or after "
        f"{AUTOPILOT_MAX_TURNS} turns. Keep it light; never send anything risky/irreversible.\n\n"
        f"STYLE:\n{style.get('instruction', '')}\n\nRECENT (oldest→newest): "
        f"{json.dumps(recent[-10:], ensure_ascii=False)}"
    )
    return ok(contact=c, chat_id=c["id"], style=style.get("profile"), recent=recent,
              directive=directive, stopwords=AUTOPILOT_STOPWORDS,
              hint="spawn with background.run(task=<directive>) for a hands-off, session-independent chat")


@mcp.tool
def list_chats(limit: int = 30) -> dict:
    """List your chats (name, unread, group?)."""
    if (e := _need_ready()):
        return e
    okc, val = wpp_get_all_chats()
    if not okc:
        return err(str(val))
    items = sorted(val if isinstance(val, list) else [], key=lambda c: c.get("t", 0), reverse=True)
    return ok(chats=items[:max(1, int(limit))], count=len(items))


@mcp.tool
def list_unread() -> dict:
    """List chats with unread messages."""
    if (e := _need_ready()):
        return e
    okc, val = wpp_get_all_chats()
    if not okc:
        return err(str(val))
    unread = [c for c in (val or []) if c.get("unread", 0)] if isinstance(val, list) else []
    return ok(unread=unread, count=len(unread))


@mcp.tool
def mark_read(contact: str, exact: bool = False) -> dict:
    """Mark a chat as read."""
    if (e := _need_ready()):
        return e
    c, resp = _resolve(contact, exact)
    if resp and not c:
        return resp
    expr = f"window.WPP.chat.markIsRead({j(c['id'])}).then(function(){{return {{read:true}};}})"
    return _act("mark_read", lambda: wpp_raw(expr), contact_name=c.get("name", ""), contact_id=c["id"])


@mcp.tool
def send_media(contact: str, file_path: str, caption: str = "", exact: bool = False) -> dict:
    """Send an image/video/audio/document by name/number (file from disk, ≤16MB)."""
    if (e := _need_ready()):
        return e
    p = Path(str(file_path)).expanduser()
    if not p.is_file():
        return err(f"file not found: {p}")
    if p.stat().st_size > MAX_MEDIA:
        return err(f"file too large (>{MAX_MEDIA // 1024 // 1024}MB)")
    c, resp = _resolve(contact, exact)
    if resp and not c:
        return resp
    mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    data_uri = f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()
    expr = (f"window.WPP.chat.sendFileMessage({j(c['id'])},{{data:{j(data_uri)},filename:{j(p.name)},"
            f"caption:{j(caption)},type:'auto'}}).then(function(r){{return {{sent:true}};}})")
    return _act("send_media", lambda: wpp_raw(expr, timeout=120), contact_name=c.get("name", ""),
                contact_id=c["id"])


# ============================================================ tools: groups
@mcp.tool
def list_groups() -> dict:
    """List your groups."""
    if (e := _need_ready()):
        return e
    expr = ("window.WPP.group.getAllGroups().then(function(gs){return gs.map(function(g){return "
            "{id:(g.id&&g.id._serialized)||String(g.id),name:g.formattedTitle||g.name||''};});})")
    okg, val = wpp_raw(expr)
    return ok(groups=val, count=len(val) if isinstance(val, list) else 0) if okg else err(str(val))


@mcp.tool
def create_group(name: str, members: list[str]) -> dict:
    """Create a group with the given name and members (names/numbers)."""
    if not (name or "").strip() or not members:
        return err("name and at least one member are required")
    if (e := _need_ready()):
        return e
    ids = []
    for m in members:
        c, resp = _resolve(m, exact=False)
        if not c:
            return err(f"couldn't resolve member '{m}'", detail=resp)
        ids.append(c["id"])
    expr = f"window.WPP.group.create({j(name)},{j(ids)}).then(function(r){{return {{created:true}};}})"
    return _act("create_group", lambda: wpp_raw(expr), contact_name=name)


def _group_member_op(method: str, group: str, member: str) -> dict:
    if (e := _need_ready()):
        return e
    g, gr = _resolve(group, exact=False)
    if not g:
        return gr
    m, mr = _resolve(member, exact=False)
    if not m:
        return mr
    expr = f"window.WPP.group.{method}({j(g['id'])},{j([m['id']])}).then(function(){{return {{ok:true}};}})"
    return _act(method, lambda: wpp_raw(expr), contact_name=f"{member}@{group}")


@mcp.tool
def add_to_group(group: str, member: str) -> dict:
    """Add a member to a group."""
    return _group_member_op("addParticipants", group, member)


@mcp.tool
def remove_from_group(group: str, member: str) -> dict:
    """Remove a member from a group."""
    return _group_member_op("removeParticipants", group, member)


@mcp.tool
def promote_in_group(group: str, member: str) -> dict:
    """Make a member a group admin."""
    return _group_member_op("promoteParticipants", group, member)


@mcp.tool
def demote_in_group(group: str, member: str) -> dict:
    """Remove admin from a group member."""
    return _group_member_op("demoteParticipants", group, member)


# ============================================================ tools: status / calls
@mcp.tool
def post_text_status(text: str) -> dict:
    """Post a text Status/Story."""
    if not (text or "").strip():
        return err("text is required")
    if (e := _need_ready()):
        return e
    expr = f"window.WPP.status.sendTextStatus({j(text)}).then(function(){{return {{posted:true}};}})"
    return _act("post_status", lambda: wpp_raw(expr))


@mcp.tool
def start_call(contact: str, video: bool = False, exact: bool = False) -> dict:
    """Place a WhatsApp voice/video call (you talk). Best-effort — web calling may be unavailable."""
    if (e := _need_ready()):
        return e
    c, resp = _resolve(contact, exact)
    if resp and not c:
        return resp
    expr = ("(function(){if(!(window.WPP&&window.WPP.call&&window.WPP.call.offerCall))"
            "throw new Error('calls not supported by this wa-js/WhatsApp build');"
            f"return window.WPP.call.offerCall({{chatId:{j(c['id'])},isVideo:{str(bool(video)).lower()}}})"
            ".then(function(){return {calling:true};});})()")
    return _act("call", lambda: wpp_raw(expr), contact_name=c.get("name", ""), contact_id=c["id"])


@mcp.tool
def end_call() -> dict:
    """End the active WhatsApp call (best-effort)."""
    if (e := _need_ready()):
        return e
    expr = ("(function(){if(!(window.WPP&&window.WPP.call))throw new Error('call module unavailable');"
            "var f=window.WPP.call.endCall||window.WPP.call.hangUpCall||window.WPP.call.rejectCall;"
            "if(!f)throw new Error('endCall unavailable');return Promise.resolve(f.call(window.WPP.call))"
            ".then(function(){return {ended:true};});})()")
    return _act("end_call", lambda: wpp_raw(expr))


# ============================================================ tools: jobs
@mcp.tool
def job_status(job_id: str) -> dict:
    """Status/result of a background WhatsApp job."""
    return JOBS.status(job_id)


@mcp.tool
def list_jobs(limit: int = 20) -> dict:
    """Recent WhatsApp jobs."""
    return JOBS.listing(limit)


@mcp.tool
def cancel_job(job_id: str) -> dict:
    """Cancel a queued/running WhatsApp job."""
    return JOBS.cancel(job_id)


if __name__ == "__main__":
    mcp.run()
