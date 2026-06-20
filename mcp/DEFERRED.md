# Deferred clients: ChatGPT & Perplexity

Both are intentionally **not** wired by `generate.mjs` because, as of mid-2026, they cannot use your
local servers for free:

| Client | Why excluded |
|---|---|
| **ChatGPT** | Custom MCP connectors require a **paid plan** (Plus/Pro/Business/Enterprise) AND only accept **remote HTTPS** servers — it cannot launch local stdio servers at all. |
| **Perplexity** | Custom/local MCP connectors are **Pro/Max/Enterprise only** (launched Mar 2026). Free tier gets prebuilt connectors only. |

## If you upgrade later and want them anyway

Both need your servers reachable as a **remote HTTPS endpoint**, so:

1. **Expose a server over HTTP.** FastMCP servers can run HTTP instead of stdio:
   `mcp.run(transport="http", host="127.0.0.1", port=9000)`.
2. **Tunnel it for free** with `cloudflared`:
   `brew install cloudflared && cloudflared tunnel --url http://127.0.0.1:9000`
   → gives a public `https://...trycloudflare.com` URL.
3. **Register the URL** in the client:
   - **ChatGPT** (paid): Settings → Apps & Connectors → Developer mode → Create → paste the HTTPS URL.
     Deep Research connectors also require the server to expose `search` + `fetch` tools.
   - **Perplexity** (paid): Settings → Connectors → Add Connector → paste the HTTPS URL.

Keep the tunnel running while you use them; the free `trycloudflare.com` URL changes each run (a named
Cloudflare tunnel gives a stable URL, also free).
