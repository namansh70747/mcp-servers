#!/usr/bin/env node
// Registry-driven MCP config generator.
//
// Reads ONE canonical source (servers.base.json + auto-discovered servers/*/server.py)
// and writes each client's native config: right top-level key, right schema family,
// right secret handling, absolute command paths where needed. Re-run after any change.
//
// Usage:  node mcp/generate.mjs            (write all clients)
//         node mcp/generate.mjs --dry      (print, don't write)
//         node mcp/generate.mjs --only=claude-code,cursor
//
// Zero dependencies (Node 18+). Custom servers self-load the repo .env, so only the
// ready-made servers carry secrets here.

import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const HOME = os.homedir();

const argv = process.argv.slice(2);
const DRY = argv.includes("--dry");
const CHECK = argv.includes("--check");
const ONLY = (argv.find((a) => a.startsWith("--only=")) || "").replace("--only=", "");
const onlySet = ONLY ? new Set(ONLY.split(",").map((s) => s.trim())) : null;

// ---------- helpers ----------
const exists = (p) => { try { fs.accessSync(p); return true; } catch { return false; } };
const firstExisting = (cands, fallback) => cands.find(exists) || fallback;
const expandTilde = (p) => (p.startsWith("~") ? path.join(HOME, p.slice(1)) : p);

function parseEnv(file) {
  const out = {};
  if (!exists(file)) return out;
  for (const line of fs.readFileSync(file, "utf8").split("\n")) {
    const t = line.trim();
    if (!t || t.startsWith("#")) continue;
    const i = t.indexOf("=");
    if (i < 0) continue;
    let v = t.slice(i + 1).trim();
    if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) v = v.slice(1, -1);
    out[t.slice(0, i).trim()] = v;
  }
  return out;
}

function readJsonIfExists(file) {
  if (!exists(file)) return {};
  try { return JSON.parse(fs.readFileSync(file, "utf8")); } catch { return {}; }
}

// Careful reader for CLIENT files: distinguishes "absent" from "present but unparseable"
// (e.g. JSONC with comments) so we never overwrite a config we can't safely round-trip.
function readClient(file) {
  if (!exists(file)) return { present: false, parsed: true, data: {} };
  const raw = fs.readFileSync(file, "utf8");
  if (!raw.trim()) return { present: true, parsed: true, data: {} };
  try { return { present: true, parsed: true, data: JSON.parse(raw) }; }
  catch { return { present: true, parsed: false, data: {} }; }
}

// ---------- placeholders ----------
const env = parseEnv(path.join(ROOT, ".env"));
const UV = firstExisting([path.join(HOME, ".local/bin/uv"), "/opt/homebrew/bin/uv"], "uv");
const UVX = firstExisting([path.join(HOME, ".local/bin/uvx"), "/opt/homebrew/bin/uvx"], "uvx");
const GITHUB_BIN = path.join(ROOT, "bin", "github-mcp-server");
const PROJECT_DIR = env.MCP_PROJECT_DIR || HOME;

const PLACEHOLDERS = {
  "{{ROOT}}": ROOT,
  "{{HOME}}": HOME,
  "{{UV}}": UV,
  "{{UVX}}": UVX,
  "{{GITHUB_BIN}}": GITHUB_BIN,
  "{{PROJECT_DIR}}": PROJECT_DIR,
};
const subst = (s) =>
  typeof s === "string"
    ? Object.entries(PLACEHOLDERS).reduce((acc, [k, v]) => acc.split(k).join(v), s)
    : s;

// ---------- build the neutral server list ----------
const base = readJsonIfExists(path.join(ROOT, "mcp", "servers.base.json"));
const servers = [];
const warnings = [];

for (const [name, def] of Object.entries(base.ready || {})) {
  servers.push({
    name,
    command: subst(def.command),
    args: (def.args || []).map(subst),
    env: def.env || {},
    exclude: def.exclude || [],
  });
}

// Auto-discover custom Python servers (servers/<name>/server.py). They self-load .env,
// so no secrets are needed in their config.
const serversDir = path.join(ROOT, "servers");
if (exists(serversDir)) {
  for (const d of fs.readdirSync(serversDir).sort()) {
    const sp = path.join(serversDir, d, "server.py");
    if (!exists(sp)) continue;
    servers.push({
      name: d,
      command: UV,
      args: ["run", "--project", ROOT, "python", sp],
      env: {},
      exclude: [],
    });
  }
}

// ---------- Qwen hubs ----------
// Qwen caps MCP entries (~5 servers). So Qwen ALONE gets the 5 aggregated hubs instead of the
// individual servers — each hub is one process (runner/run-mcp-hub <name>) that mounts many
// servers and re-exposes their tools under <server>_<tool> names. Every server stays reachable;
// the hub membership lives in runner/mcp_runner/hubs.py. Every OTHER client keeps individual servers.
const QWEN_HUBS = ["dev", "outreach", "career", "prod", "system"];
// `--refresh-package` forces uvx to rebuild the local runner wheel from source on every launch
// (it's tiny, so this is cheap) while keeping the heavy deps cached. Without it, uvx serves a
// stale cached runner and edits to hubs.py (hub membership) silently don't take effect.
const qwenHubEntry = (hub) => ({
  command: UVX,
  args: ["--refresh-package", "mcp-suite-runner", "--from", path.join(ROOT, "runner"), "run-mcp-hub", hub],
  env: { MCP_SUITE_ROOT: ROOT },
});

// ---------- client registry ----------
// secret: how this client expresses secrets
//   ref-dollar -> ${VAR} | ref-env -> ${env:VAR} | inline -> literal from .env | vscode-input -> inputs[]
// schema: mcpServers | vscode | zed
const CLIENTS = [
  { id: "claude-code",    file: path.join(ROOT, ".mcp.json"),                                                                 key: "mcpServers",      secret: "ref-dollar",   schema: "mcpServers" },
  { id: "claude-desktop", file: path.join(HOME, "Library/Application Support/Claude/claude_desktop_config.json"),             key: "mcpServers",      secret: "inline",       schema: "mcpServers" },
  { id: "cursor",         file: path.join(HOME, ".cursor/mcp.json"),                                                          key: "mcpServers",      secret: "ref-env",      schema: "mcpServers" },
  { id: "windsurf",       file: path.join(HOME, ".codeium/windsurf/mcp_config.json"),                                        key: "mcpServers",      secret: "ref-env",      schema: "mcpServers" },
  { id: "qwen",           file: path.join(HOME, ".qwen/settings.json"),                                                       key: "mcpServers",      secret: "ref-dollar",   schema: "mcpServers" },
  { id: "qwen-desktop",  file: path.join(HOME, "Library/Application Support/Qwen/settings.json"),                              key: "mcp_config",      secret: "ref-dollar",   schema: "mcpServers" },
  { id: "kimi",           file: path.join(HOME, ".kimi/mcp.json"),                                                            key: "mcpServers",      secret: "inline",       schema: "mcpServers" },
  { id: "vscode",         file: path.join(HOME, "Library/Application Support/Code/User/mcp.json"),                            key: "servers",         secret: "vscode-input", schema: "vscode" },
  { id: "gemini",         file: path.join(HOME, ".gemini/settings.json"),                                                     key: "mcpServers",      secret: "ref-dollar",   schema: "mcpServers" },
  { id: "cline",          file: path.join(HOME, "Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json"), key: "mcpServers", secret: "inline", schema: "mcpServers" },
  { id: "roo",            file: path.join(HOME, "Library/Application Support/Code/User/globalStorage/rooveterinaryinc.roo-cline/settings/mcp_settings.json"),   key: "mcpServers", secret: "inline", schema: "mcpServers" },
  { id: "zed",            file: path.join(HOME, ".config/zed/settings.json"),                                                 key: "context_servers", secret: "inline",       schema: "zed" },
];

// ---------- doctor (`--check`): validate toolchain, secrets, and emitted configs ----------
if (CHECK) {
  console.log(`MCP suite doctor — ${servers.length} servers discovered\n\ntoolchain:`);
  const tool = (label, p) => console.log(`  ${exists(p) ? "✓" : "✗"} ${label.padEnd(14)} ${p}`);
  tool("uv", UV); tool("uvx", UVX); tool("github binary", GITHUB_BIN);
  console.log(`  ${exists(path.join(ROOT, ".env")) ? "✓" : "✗"} .env present`);

  console.log("\nsecrets (.env):");
  for (const s of ["GITHUB_PERSONAL_ACCESS_TOKEN", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
    "SEC_USER_AGENT", "REOON_API_KEY", "HUNTER_API_KEY", "APOLLO_API_KEY"])
    console.log(`  ${env[s] ? "✓ set  " : "· empty"} ${s}`);

  console.log("\nclient configs:");
  for (const c of CLIENTS) {
    const cur = readClient(c.file);
    if (!cur.present) { console.log(`  · ${c.id.padEnd(15)} (not generated yet)`); continue; }
    if (!cur.parsed) { console.log(`  ✗ ${c.id.padEnd(15)} UNPARSEABLE (would be skipped)`); continue; }
    const n = Object.keys(cur.data[c.key] || {}).length;
    console.log(`  ✓ ${c.id.padEnd(15)} ${n} servers`);
  }
  process.exit(0);
}

function renderSecret(client, varName, inputs) {
  switch (client.secret) {
    case "ref-dollar": return "${" + varName + "}";
    case "ref-env":    return "${env:" + varName + "}";
    case "inline": {
      const v = env[varName];
      if (!v) warnings.push(`[${client.id}] secret ${varName} is empty in .env -> inlined as ""`);
      return v || "";
    }
    case "vscode-input": {
      const id = varName.toLowerCase().replace(/[^a-z0-9]+/g, "-");
      if (!inputs.find((x) => x.id === id))
        inputs.push({ type: "promptString", id, description: varName, password: true });
      return "${input:" + id + "}";
    }
    default: return env[varName] || "";
  }
}

function buildEntry(client, srv, inputs) {
  const entry = {};
  if (client.schema === "vscode") entry.type = "stdio";
  entry.command = srv.command;
  entry.args = srv.args;
  const envOut = {};
  for (const [k, v] of Object.entries(srv.env)) {
    envOut[k] = typeof v === "string" && v.startsWith("@secret:")
      ? renderSecret(client, v.slice("@secret:".length), inputs)
      : subst(v);
  }
  if (Object.keys(envOut).length) entry.env = envOut;
  return entry;
}

// ---------- emit ----------
let written = 0;
let skipped = 0;
for (const client of CLIENTS) {
  if (onlySet && !onlySet.has(client.id)) continue;

  const inputs = [];
  const block = {};
  let count = 0;
  if (client.id === "qwen-desktop") {
    // Qwen Desktop caps MCP entries (~5). Write the 5 aggregated hubs.
    // --refresh-package forces uvx to rebuild the local runner wheel from source on every launch
    // so edits to hubs.py take effect immediately without manual reinstall.
    // Qwen Desktop spawns MCP servers with a MINIMAL environment, so we must set HOME (uvx cache),
    // USER, and a full PATH (node/npx for ready-made servers, uv/uvx/git) explicitly — otherwise
    // uvx can't build/run the hub and it silently exposes nothing.
    const nodeDir = path.dirname(firstExisting([
      path.join(HOME, ".local/bin/node"), "/opt/homebrew/bin/node", "/usr/local/bin/node",
    ], "/opt/homebrew/bin/node"));
    const QWEN_PATH = [
      path.join(HOME, ".local/bin"), nodeDir, "/opt/homebrew/bin",
      "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    ].filter((p, i, a) => a.indexOf(p) === i).join(":");
    const hubEnv = { HOME, USER: env.USER || os.userInfo().username, PATH: QWEN_PATH, MCP_SUITE_ROOT: ROOT };
    for (const hub of QWEN_HUBS) {
      const hubName = `${hub}-hub`;
      block[hubName] = {
        name: hubName,
        command: UVX,
        args: ["--refresh-package", "mcp-suite-runner", "--from", path.join(ROOT, "runner"), "run-mcp-hub", hub],
        env: hubEnv,
      };
      count++;
    }
  } else {
    // Every other client (incl. Qwen Code CLI — which has NO 5-server limit) gets all servers
    // wired individually. Qwen CLI entries are marked trust:true so it doesn't prompt per-server.
    for (const srv of servers) {
      if (srv.exclude.includes(client.id)) continue;
      const e = buildEntry(client, srv, inputs);
      if (client.id === "qwen") e.trust = true;
      block[srv.name] = e;
      count++;
    }
  }

  // merge into existing file (preserve unrelated settings)
  const cur = readClient(client.file);
  if (cur.present && !cur.parsed) {
    warnings.push(`[${client.id}] ${client.file} exists but isn't plain JSON (comments?) — SKIPPED to avoid wiping your settings. Add the servers manually or convert the file to JSON, then re-run.`);
    skipped++;
    continue;
  }
  const config = cur.data;
  config[client.key] = block;
  if (client.schema === "vscode" && inputs.length) {
    const prev = Array.isArray(config.inputs) ? config.inputs.filter((x) => !inputs.find((y) => y.id === x.id)) : [];
    config.inputs = [...prev, ...inputs];
  }

  const json = JSON.stringify(config, null, 2) + "\n";
  if (DRY) {
    console.log(`\n# ${client.id} -> ${client.file}  (${count} servers)`);
    console.log(json);
  } else {
    fs.mkdirSync(path.dirname(client.file), { recursive: true });
    const bak = client.file + ".orig-mcp.bak";
    if (cur.present && !exists(bak)) fs.copyFileSync(client.file, bak); // one-time backup of the original
    fs.writeFileSync(client.file, json);
    written++;
    console.log(`✓ ${client.id.padEnd(15)} ${count} servers -> ${client.file}`);
  }
}

if (warnings.length) {
  console.log("\nwarnings:");
  for (const w of [...new Set(warnings)]) console.log("  - " + w);
}
console.log(`\n${DRY ? "(dry run) " : ""}${servers.length} servers across ${onlySet ? onlySet.size : CLIENTS.length} clients${DRY ? "" : `, ${written} files written`}.`);
