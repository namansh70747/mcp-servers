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
  var seen = "";
  setInterval(function () {
    var cmd = root.getAttribute("data-wa-cmd");
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
        result = (0, eval)(p.expr); // primary RE tool; if CSP blocks this, we use named ops instead
      } else { reply(id, { ok: false, e: "no op/expr" }); return; }
      Promise.resolve(result)
        .then(function (v) { reply(id, { ok: true, v: safe(v) }); })
        .catch(function (e) { reply(id, { ok: false, e: String((e && e.message) || e) }); });
    } catch (e) { reply(id, { ok: false, e: String((e && e.message) || e) }); }
  }, 200);
})();
