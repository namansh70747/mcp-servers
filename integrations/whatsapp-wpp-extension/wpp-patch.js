// MAIN world, document_start, AFTER wa-loader.js (which defines window.__bootWA but does NOT run wa-js).
//
// Root cause of wppconnect-team/wa-js#3419 on WhatsApp Web >= 2.3000: wa-js runs its module-finders once,
// right after injection. At document_start WhatsApp's (Metro) modules aren't registered yet, so the
// finders miss (ChatStore / getIsMyContact / getMentionName / getNotifyName / isAuthenticated "not found")
// and wa-js never retries -> WPP.isReady stays false and even sendTextMessage throws (ChatStore is undefined).
//
// Fix: DEFER booting wa-js until WhatsApp's app has loaded (its modules are registered), then boot once.
// wa-js's own finders then bind everything correctly. We additionally patch the one genuine rename
// (isAuthenticated -> isLoggedIn, moved to a separate module) if wa-js still can't bind it.
//
// Status is mirrored to the shared DOM attribute data-wa-patch so the connector can read it across the
// isolated world. bridge.js separately mirrors WPP.isReady to data-wa-ready.
(function () {
  var root = document.documentElement;
  function setPatch(o) { try { root.setAttribute("data-wa-patch", JSON.stringify(o)); } catch (e) {} }

  function appLoaded() {
    return typeof self.require === "function" && !!document.querySelector("#pane-side");
  }

  var booted = false, tries = 0;
  var iv = setInterval(function () {
    tries++;
    if (!booted) {
      var ready = appLoaded();
      if (ready || tries > 45) { // boot when modules are present, or force after ~45s as a fallback
        try {
          if (typeof window.__bootWA === "function") {
            window.__bootWA();
            booted = true;
            setPatch({ stage: "booted", tries: tries, forced: !ready });
          } else {
            setPatch({ stage: "no-bootWA", tries: tries });
          }
        } catch (e) {
          setPatch({ stage: "boot-error", err: String((e && e.message) || e), tries: tries });
        }
      } else {
        setPatch({ stage: "waiting", tries: tries, hasRequire: typeof self.require === "function", pane: !!document.querySelector("#pane-side") });
      }
      return;
    }

    // Post-boot: report binding/readiness; apply the isAuthenticated rename patch if still unbound.
    var W = window.WPP;
    if (!W) { setPatch({ stage: "booted-no-WPP", tries: tries }); return; }
    var st = { stage: "post-boot", tries: tries, isReady: !!W.isReady };
    try {
      var wa = W.whatsapp, L = W.loader;
      st.bound = {
        ChatStore: typeof wa.ChatStore,
        getIsMyContact: typeof wa.getIsMyContact,
        getMentionName: typeof wa.getMentionName,
        getNotifyName: typeof wa.getNotifyName,
        isAuthenticated: typeof wa.isAuthenticated
      };
      if (typeof wa.isAuthenticated !== "function") {
        var am = null; try { am = L.search(function (m) { return m.isLoggedIn; }); } catch (e) {}
        if (am && typeof am.isLoggedIn === "function") {
          try { wa.isAuthenticated = am.isLoggedIn; st.authPatched = true; } catch (e) { st.authErr = String(e); }
        }
      }
      // Patch CallStore.assertGet (removed in WA Web >= 2.3000.x, wa-js 4.3.1 still calls it).
      // New CallStore: call offer is async — the call object may not be in the store yet when
      // assertGet is called. Return a proxy so offer() can continue; wa-js sets up listeners on it.
      if (wa.CallStore && typeof wa.CallStore.assertGet !== "function" && typeof wa.CallStore.get === "function") {
        wa.CallStore.assertGet = function (id) {
          var r = wa.CallStore.get(id);
          if (r) return r;
          // Call not in store yet — return a lightweight proxy so wa-js doesn't throw
          return {
            id: id, isConnected: false, peerJid: null,
            on: function() { return this; }, off: function() { return this; },
            once: function() { return this; }, toString: function() { return id; }
          };
        };
        st.callstorePatched = true;
      }
    } catch (e) { st.err = String((e && e.message) || e); }
    setPatch(st);
    if (W.isReady || tries > 90) clearInterval(iv);
  }, 1000);
})();
