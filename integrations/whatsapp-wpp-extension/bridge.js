// Runs in the page's MAIN world at document_start (FIRST content script). wa-js (window.WPP) lives in
// this same MAIN world. The local connector reaches the page via AppleScript `execute javascript`, which
// runs in an ISOLATED world and CANNOT read MAIN-world `window` globals (window.WPP, webpackChunk*) —
// only the shared DOM. So we bridge across worlds through DOM attributes on <html>:
//   data-wa-bridge : "1" once this script ran (proves the extension injected)
//   data-wa-ready  : "1" once WPP.isReady
//   data-wa-build  : JSON of WPP.conn.getBuildConstants() once available (WA Web version)
//   data-wa-patch  : JSON status written by wpp-patch.js (module-finder fix)
// and a request/response relay:
//   data-wa-cmd = JSON {id, expr}            -> we eval(expr) in MAIN world
//   data-wa-cmd = JSON {id, op, args:[...]}  -> we dispatch a named handler (CSP-safe, no eval)
//   data-wa-res = JSON {id, ok, v|e}         -> the resolved result (connector polls this)
(function () {
  var root = document.documentElement;
  function set(k, v) { try { root.setAttribute(k, v); } catch (e) {} }
  function safe(v) { try { JSON.stringify(v); return v; } catch (e) { try { return String(v); } catch (_) { return null; } } }

  set("data-wa-bridge", "1");
  set("data-wa-ready", "0");

  function poll() {
    try {
      var W = window.WPP;
      set("data-wa-ready", (W && W.isReady) ? "1" : "0");
      if (W && W.conn && typeof W.conn.getBuildConstants === "function" && !root.getAttribute("data-wa-build")) {
        try { set("data-wa-build", JSON.stringify(W.conn.getBuildConstants())); } catch (e) {}
      }
    } catch (e) {}
  }
  var n = 0, iv = setInterval(function () { poll(); if (++n > 600) clearInterval(iv); }, 1000);

  // ---- named ops (CSP-safe; extend as the connector needs) -------------------------------------
  var OPS = {
    ping: function () { return "pong"; },
    ready: function () {
      var W = window.WPP || {};
      return {
        hasWPP: !!window.WPP,
        isReady: !!W.isReady,
        keys: window.WPP ? Object.keys(window.WPP) : [],
        webpackKeys: (window.WPP && W.webpack) ? Object.keys(W.webpack) : [],
        patch: root.getAttribute("data-wa-patch") || null
      };
    },
    build: function () {
      var W = window.WPP;
      if (W && W.conn && W.conn.getBuildConstants) return W.conn.getBuildConstants();
      return null;
    }
  };

  function reply(id, o) { o.id = id; set("data-wa-res", JSON.stringify(o)); }

  // Process the current data-wa-cmd (dedup on the raw JSON so we run each command once).
  var seen = "";
  function process() {
    var cmd;
    try { cmd = root.getAttribute("data-wa-cmd"); } catch (e) { return; }
    if (!cmd || cmd === seen) return;
    seen = cmd;
    var p; try { p = JSON.parse(cmd); } catch (e) { return; }
    var id = p.id || "";
    try {
      var result;
      if (p.op) {
        var h = OPS[p.op];
        if (!h) { reply(id, { ok: false, e: "unknown op: " + p.op }); return; }
        result = h.apply(null, p.args || []);
      } else if (typeof p.expr === "string") {
        result = (0, eval)(p.expr); // primary tool; if CSP ever blocks this, named ops still work
      } else { reply(id, { ok: false, e: "no op/expr" }); return; }
      Promise.resolve(result)
        .then(function (v) { reply(id, { ok: true, v: safe(v) }); })
        .catch(function (e) { reply(id, { ok: false, e: String((e && e.message) || e) }); });
    } catch (e) { reply(id, { ok: false, e: String((e && e.message) || e) }); }
  }

  // PRIMARY trigger: a MutationObserver on the data-wa-cmd attribute. The connector sets that attribute
  // via a forced main-thread eval (`execute javascript`), which runs even when the tab is in the
  // BACKGROUND; the observer callback is delivered as a microtask, and microtasks are NOT subject to
  // Chrome's background-tab timer throttling. So the relay stays responsive without ever focusing the
  // window. (A plain setInterval here would be throttled to ~1/min in a backgrounded tab — the bug we
  // were hitting and papering over by force-focusing the tab.)
  try {
    new MutationObserver(process).observe(root, { attributes: true, attributeFilter: ["data-wa-cmd"] });
  } catch (e) {}
  // Belt-and-suspenders: a slow poll in case a mutation is ever missed (throttled when backgrounded,
  // which is fine — the observer is the real workhorse).
  setInterval(process, 1000);
  process();
})();
