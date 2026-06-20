"""api-tester — a local Postman-lite: save HTTP requests into collections and run them (httpx).

All offline/local; no account needed. Supports environment variables ({{VAR}} substitution),
auth helpers (bearer/basic/apikey), response assertions, run history, and curl/OpenAPI import."""
from __future__ import annotations

import base64
import json
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path

import httpx
from mcp_base import BaseStore, db_path, http, make_server

mcp = make_server(
    "api-tester",
    instructions=("Save & run HTTP requests. add_request, run, run_collection, list_requests, "
                  "set_env, set_auth, set_assertions, import_curl, import_openapi, history, curl_for."),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests(
  id INTEGER PRIMARY KEY, name TEXT, collection TEXT DEFAULT 'default', method TEXT DEFAULT 'GET',
  url TEXT, headers TEXT DEFAULT '{}', body TEXT DEFAULT '', created_at TEXT
);
CREATE TABLE IF NOT EXISTS env_vars(
  id INTEGER PRIMARY KEY, env TEXT DEFAULT 'default', key TEXT, value TEXT DEFAULT '',
  UNIQUE(env, key)
);
CREATE TABLE IF NOT EXISTS history(
  id INTEGER PRIMARY KEY, request_id INTEGER, name TEXT, method TEXT, url TEXT,
  status INTEGER, ok INTEGER, elapsed_ms INTEGER, passed INTEGER, body_preview TEXT, ran_at TEXT
);
"""
store = BaseStore(db_path("api-tester"), schema=SCHEMA)


def _ensure_columns(table: str, cols: dict[str, str]) -> None:
    have = {r["name"] for r in store.query(f"PRAGMA table_info({table})")}
    for name, decl in cols.items():
        if name not in have:
            store.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


_ensure_columns("requests", {
    "auth_type": "TEXT DEFAULT ''",
    "auth_value": "TEXT DEFAULT ''",
    "assertions": "TEXT DEFAULT '[]'",
    "updated_at": "TEXT",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_MAX_LIMIT = 500  # clamp for unbounded list/history queries


def _clamp_limit(limit: int, default: int = 50) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return min(n, _MAX_LIMIT)


_VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")


def _env_map(env: str) -> dict[str, str]:
    return {r["key"]: r["value"] for r in
            store.query("SELECT key,value FROM env_vars WHERE env=?", (env,))}


def _subst(text: str, env: dict[str, str]) -> str:
    if not text or "{{" not in text:
        return text
    return _VAR_RE.sub(lambda m: env.get(m.group(1), m.group(0)), text)


def _apply_auth(headers: dict, auth_type: str, auth_value: str, env: dict) -> dict:
    """Inject auth into headers. auth_type: bearer | basic (user:pass) | apikey (Header:value)."""
    headers = dict(headers)
    auth_value = _subst(auth_value or "", env)
    t = (auth_type or "").lower()
    if t == "bearer" and auth_value:
        headers["Authorization"] = f"Bearer {auth_value}"
    elif t == "basic" and ":" in auth_value:
        token = base64.b64encode(auth_value.encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    elif t == "apikey" and ":" in auth_value:
        k, v = auth_value.split(":", 1)
        headers[k.strip()] = v.strip()
    return headers


def _check_assertions(specs: list, resp: dict) -> list[dict]:
    """Evaluate assertion specs against a response summary dict."""
    results = []
    body = resp.get("body_preview", "") or ""
    parsed = None
    for spec in specs or []:
        t = spec.get("type")
        ok, detail = False, ""
        try:
            if t == "status":
                ok = resp.get("status") == spec.get("equals")
                detail = f"status={resp.get('status')} expected={spec.get('equals')}"
            elif t == "contains":
                ok = spec.get("value", "") in body
                detail = f"body contains {spec.get('value')!r}"
            elif t == "not_contains":
                ok = spec.get("value", "") not in body
                detail = f"body not contains {spec.get('value')!r}"
            elif t == "latency":
                ok = resp.get("elapsed_ms", 1e9) <= spec.get("max_ms", 0)
                detail = f"elapsed={resp.get('elapsed_ms')}ms max={spec.get('max_ms')}ms"
            elif t == "json":
                if parsed is None:
                    parsed = json.loads(body)
                val = parsed
                for part in str(spec.get("path", "")).split("."):
                    if part == "":
                        continue
                    if isinstance(val, list):
                        val = val[int(part)]
                    else:
                        val = val[part]
                ok = val == spec.get("equals")
                detail = f"{spec.get('path')}={val!r} expected={spec.get('equals')!r}"
            else:
                detail = f"unknown assertion type {t!r}"
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"error: {e}"
        results.append({"type": t, "ok": ok, "detail": detail})
    return results


_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def _do(method, url, headers, body, *, env=None, auth_type="", auth_value="",
        assertions=None, timeout=30) -> dict:
    env = env or {}
    if (method or "").upper() not in _ALLOWED_METHODS:
        return {"error": f"unsupported HTTP method: {method!r}"}
    timeout = max(1, min(int(timeout or 30), 120))
    url = _subst(url, env)
    if not (url or "").strip():
        return {"error": "url is required"}
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"error": "url must start with http:// or https://"}
    headers = {k: _subst(str(v), env) for k, v in (headers or {}).items()}
    headers = _apply_auth(headers, auth_type, auth_value, env)
    body = _subst(body, env) if body else body
    try:
        r = httpx.request(method.upper(), url, headers=headers or None,
                          content=body if body else None, timeout=timeout)
        ct = r.headers.get("content-type", "")
        out = {"status": r.status_code, "ok": r.is_success, "content_type": ct,
               "elapsed_ms": int(r.elapsed.total_seconds() * 1000), "body_preview": r.text[:2000],
               "request_url": str(r.request.url)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    if assertions:
        results = _check_assertions(assertions, out)
        out["assertions"] = results
        out["assertions_passed"] = all(a["ok"] for a in results)
    return out


def _record(req_id, name, method, url, resp) -> None:
    passed = resp.get("assertions_passed")
    store.execute(
        "INSERT INTO history(request_id,name,method,url,status,ok,elapsed_ms,passed,body_preview,ran_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (req_id, name, method, url, resp.get("status"), 1 if resp.get("ok") else 0,
         resp.get("elapsed_ms"), None if passed is None else (1 if passed else 0),
         (resp.get("body_preview") or "")[:500], _now()))


def _add_request(name: str, url: str, method: str = "GET", headers: dict | None = None,
                 body: str = "", collection: str = "default", auth_type: str = "",
                 auth_value: str = "", assertions: list | None = None) -> dict:
    if not (url or "").strip():
        return {"error": "url is required"}
    if (method or "GET").upper() not in _ALLOWED_METHODS:
        return {"error": f"unsupported HTTP method: {method!r}"}
    rid = store.execute(
        "INSERT INTO requests(name,collection,method,url,headers,body,auth_type,auth_value,"
        "assertions,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (name, collection, method, url, json.dumps(headers or {}), body, auth_type, auth_value,
         json.dumps(assertions or []), _now(), _now()))
    return {"id": rid, "name": name, "collection": collection}


@mcp.tool
def add_request(name: str, url: str, method: str = "GET", headers: dict | None = None,
                body: str = "", collection: str = "default", auth_type: str = "",
                auth_value: str = "", assertions: list | None = None) -> dict:
    """Save a request into a collection. Optional auth (bearer/basic/apikey) and assertions."""
    return _add_request(name, url, method, headers, body, collection, auth_type,
                        auth_value, assertions)


@mcp.tool
def run(request_id: int, env: str = "default") -> dict:
    """Run a saved request (with env-var substitution, auth, and assertions) and record history."""
    r = store.query_one("SELECT * FROM requests WHERE id=?", (request_id,))
    if not r:
        return {"error": "not found"}
    resp = _do(r["method"], r["url"], json.loads(r["headers"] or "{}"), r["body"],
               env=_env_map(env), auth_type=r.get("auth_type", ""), auth_value=r.get("auth_value", ""),
               assertions=json.loads(r.get("assertions") or "[]"))
    if "error" not in resp:
        _record(request_id, r["name"], r["method"], resp.get("request_url", r["url"]), resp)
    return {"request": r["name"], **resp}


@mcp.tool
def run_inline(url: str, method: str = "GET", headers: dict | None = None, body: str = "",
               env: str = "default", auth_type: str = "", auth_value: str = "",
               assertions: list | None = None) -> dict:
    """Run an ad-hoc request without saving it (supports env/auth/assertions)."""
    return _do(method, url, headers or {}, body, env=_env_map(env), auth_type=auth_type,
               auth_value=auth_value, assertions=assertions)


@mcp.tool
def run_collection(collection: str = "default", env: str = "default") -> list[dict]:
    """Run every request in a collection (with the given environment)."""
    em = _env_map(env)
    out = []
    for r in store.query("SELECT * FROM requests WHERE collection=? ORDER BY id", (collection,)):
        resp = _do(r["method"], r["url"], json.loads(r["headers"] or "{}"), r["body"], env=em,
                   auth_type=r.get("auth_type", ""), auth_value=r.get("auth_value", ""),
                   assertions=json.loads(r.get("assertions") or "[]"))
        if "error" not in resp:
            _record(r["id"], r["name"], r["method"], resp.get("request_url", r["url"]), resp)
        out.append({"request": r["name"], **resp})
    return out


@mcp.tool
def list_requests(collection: str = "") -> list[dict]:
    """List saved requests."""
    if collection:
        return store.query("SELECT id,name,method,url,collection,auth_type FROM requests WHERE collection=?", (collection,))
    return store.query("SELECT id,name,method,url,collection,auth_type FROM requests ORDER BY collection, id")


@mcp.tool
def delete_request(request_id: int) -> dict:
    """Delete a saved request."""
    if not store.query_one("SELECT id FROM requests WHERE id=?", (request_id,)):
        return {"error": "not found"}
    store.execute("DELETE FROM requests WHERE id=?", (request_id,))
    return {"ok": True, "deleted": request_id}


@mcp.tool
def set_env(env: str, key: str, value: str) -> dict:
    """Set an environment variable for {{KEY}} substitution. Stored locally in plaintext SQLite."""
    store.execute("INSERT INTO env_vars(env,key,value) VALUES(?,?,?) "
                  "ON CONFLICT(env,key) DO UPDATE SET value=excluded.value", (env, key, value))
    return {"ok": True, "env": env, "key": key}


@mcp.tool
def get_env(env: str = "default") -> dict:
    """List variables for an environment (values returned as-is; treat as local secrets)."""
    return {"env": env, "vars": _env_map(env)}


@mcp.tool
def list_envs() -> list[str]:
    """List all environment names that have variables."""
    return [r["env"] for r in store.query("SELECT DISTINCT env FROM env_vars ORDER BY env")]


@mcp.tool
def delete_env(env: str, key: str = "") -> dict:
    """Delete one variable, or the whole environment when key is empty."""
    if key:
        store.execute("DELETE FROM env_vars WHERE env=? AND key=?", (env, key))
    else:
        store.execute("DELETE FROM env_vars WHERE env=?", (env,))
    return {"ok": True, "env": env, "key": key or "*"}


@mcp.tool
def set_auth(request_id: int, auth_type: str, auth_value: str) -> dict:
    """Attach auth to a saved request. auth_type: bearer | basic (user:pass) | apikey (Header:value).

    auth_value may contain {{VAR}} references resolved from the active environment at run time."""
    if not store.query_one("SELECT id FROM requests WHERE id=?", (request_id,)):
        return {"error": "not found"}
    store.execute("UPDATE requests SET auth_type=?, auth_value=?, updated_at=? WHERE id=?",
                  (auth_type, auth_value, _now(), request_id))
    return {"ok": True, "id": request_id, "auth_type": auth_type}


@mcp.tool
def set_assertions(request_id: int, assertions: list) -> dict:
    """Attach response assertions to a saved request.

    Each assertion is a dict, e.g. {"type":"status","equals":200},
    {"type":"contains","value":"ok"}, {"type":"not_contains","value":"error"},
    {"type":"json","path":"data.0.id","equals":5}, {"type":"latency","max_ms":500}."""
    if not store.query_one("SELECT id FROM requests WHERE id=?", (request_id,)):
        return {"error": "not found"}
    store.execute("UPDATE requests SET assertions=?, updated_at=? WHERE id=?",
                  (json.dumps(assertions), _now(), request_id))
    return {"ok": True, "id": request_id, "count": len(assertions)}


def _parse_curl(command: str) -> dict:
    """Parse a curl command string into method/url/headers/body."""
    toks = shlex.split(command)
    if toks and toks[0] == "curl":
        toks = toks[1:]
    method, url, body, headers = "GET", "", "", {}
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in ("-X", "--request") and i + 1 < len(toks):
            method = toks[i + 1].upper(); i += 2; continue
        if t in ("-H", "--header") and i + 1 < len(toks):
            h = toks[i + 1]
            if ":" in h:
                k, v = h.split(":", 1)
                headers[k.strip()] = v.strip()
            i += 2; continue
        if t in ("-d", "--data", "--data-raw", "--data-binary") and i + 1 < len(toks):
            body = toks[i + 1]
            if method == "GET":
                method = "POST"
            i += 2; continue
        if t in ("-u", "--user") and i + 1 < len(toks):
            tok = base64.b64encode(toks[i + 1].encode()).decode()
            headers["Authorization"] = f"Basic {tok}"
            i += 2; continue
        if t.startswith("-"):
            # skip flags that take no value we model (e.g. -s, -L) or unknown valued flags
            i += 1; continue
        if not url and (t.startswith("http://") or t.startswith("https://") or "." in t):
            url = t
        i += 1
    return {"method": method, "url": url, "headers": headers, "body": body}


@mcp.tool
def import_curl(command: str, name: str = "", collection: str = "default", save: bool = True) -> dict:
    """Parse a curl command into a request. Saves it by default; set save=False to just preview."""
    parsed = _parse_curl(command)
    if not parsed["url"]:
        return {"error": "could not find a URL in the curl command"}
    if not save:
        return {"saved": False, **parsed}
    res = _add_request(name or parsed["url"], parsed["url"], parsed["method"],
                         parsed["headers"], parsed["body"], collection)
    return {"saved": True, **res, **parsed}


@mcp.tool
def curl_for(request_id: int) -> dict:
    """Emit an equivalent curl command for a saved request (round-trips with import_curl)."""
    r = store.query_one("SELECT * FROM requests WHERE id=?", (request_id,))
    if not r:
        return {"error": "not found"}
    parts = ["curl", "-X", r["method"], shlex.quote(r["url"])]
    headers = json.loads(r["headers"] or "{}")
    headers = _apply_auth(headers, r.get("auth_type", ""), r.get("auth_value", ""), {})
    for k, v in headers.items():
        parts += ["-H", shlex.quote(f"{k}: {v}")]
    if r["body"]:
        parts += ["-d", shlex.quote(r["body"])]
    return {"id": request_id, "curl": " ".join(parts)}


@mcp.tool
def import_openapi(spec: str, collection: str = "", limit: int = 100) -> dict:
    """Import an OpenAPI/Swagger JSON spec (file path or URL) — creates a saved request per operation.

    JSON specs only (no YAML). Builds full URLs from the first server/host entry."""
    raw = ""
    if spec.startswith("http://") or spec.startswith("https://"):
        resp = http.request("GET", spec, timeout=30)
        if not resp.get("ok"):
            return {"error": f"fetch failed: {resp.get('error') or ('HTTP ' + str(resp.get('status')))}"}
        raw = resp.get("text", "")
    else:
        p = Path(spec).expanduser()
        if not p.is_file():
            return {"error": f"no such file: {spec}"}
        raw = p.read_text(encoding="utf-8")
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"error": f"not valid JSON (YAML is unsupported): {e}"}

    base = ""
    if doc.get("servers"):
        base = (doc["servers"][0] or {}).get("url", "").rstrip("/")
    elif doc.get("host"):
        scheme = (doc.get("schemes") or ["https"])[0]
        base = f"{scheme}://{doc['host']}{doc.get('basePath', '').rstrip('/')}"
    coll = collection or (doc.get("info", {}).get("title") or "openapi").strip() or "openapi"

    created, methods = [], {"get", "post", "put", "patch", "delete", "head", "options"}
    for path, ops in (doc.get("paths") or {}).items():
        if not isinstance(ops, dict):
            continue
        for method, op in ops.items():
            if method.lower() not in methods:
                continue
            if len(created) >= limit:
                break
            op = op or {}
            name = op.get("operationId") or f"{method.upper()} {path}"
            url = f"{base}{path}" if base else path
            res = _add_request(name, url, method.upper(), {}, "", coll)
            if "id" not in res:
                continue
            created.append(res["id"])
    return {"ok": True, "collection": coll, "imported": len(created), "ids": created, "base_url": base}


@mcp.tool
def history(request_id: int = 0, limit: int = 50) -> list[dict]:
    """Recent run history — all requests, or just one when request_id is given."""
    limit = _clamp_limit(limit)
    if request_id:
        return store.query(
            "SELECT id,name,method,url,status,ok,elapsed_ms,passed,ran_at FROM history "
            "WHERE request_id=? ORDER BY id DESC LIMIT ?", (request_id, limit))
    return store.query(
        "SELECT id,request_id,name,method,url,status,ok,elapsed_ms,passed,ran_at FROM history "
        "ORDER BY id DESC LIMIT ?", (limit,))


@mcp.tool
def save_response(request_id: int, path: str, env: str = "default") -> dict:
    """Run a saved request and write its full response body to a file."""
    r = store.query_one("SELECT * FROM requests WHERE id=?", (request_id,))
    if not r:
        return {"error": "not found"}
    em = _env_map(env)
    url = _subst(r["url"], em)
    headers = {k: _subst(str(v), em) for k, v in json.loads(r["headers"] or "{}").items()}
    headers = _apply_auth(headers, r.get("auth_type", ""), r.get("auth_value", ""), em)
    body = _subst(r["body"], em) if r["body"] else r["body"]
    if (r["method"] or "GET").upper() not in _ALLOWED_METHODS:
        return {"error": f"unsupported HTTP method: {r['method']!r}"}
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"error": "url must start with http:// or https://"}
    try:
        resp = httpx.request(r["method"].upper(), url, headers=headers or None,
                             content=body if body else None, timeout=30)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(resp.text, encoding="utf-8")
    return {"ok": True, "path": str(out), "status": resp.status_code, "bytes": len(resp.text)}


if __name__ == "__main__":
    mcp.run()
