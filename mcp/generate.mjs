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
//         node mcp/generate.mjs --check    (doctor)
//
// Zero dependencies (Node 18+). Custom servers self-load the repo .env, so only the
// ready-made servers carry secrets here.

import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const HOME = os.homedir();
const PLATFORM = process.platform;
const IS_DARWIN = PLATFORM === "darwin";
const IS_WIN = PLATFORM === "win32";

const argv = process.argv.slice(2);
const DRY = argv.includes("--dry");
const CHECK = argv.includes("--check");
const ONLY = (argv.find((a) => a.startsWith("--only=")) || "").replace("--only=", "");
const onlySet = ONLY ? new Set(ONLY.split(",").map((s) => s.trim())) : null;

// ---------- helpers ----------
const exists = (p) => { try { fs.accessSync(p); return true; } catch { return false; } };
const firstExisting = (cands, fallback) => cands.find(exists) || fallback;

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

function readClient(file) {
  if (!exists(file)) return { present: false, parsed: true, data: {} };
  const raw = fs.readFileSync(file, "utf8");
  if (!raw.trim()) return { present: true, parsed: true, data: {} };
  try { return { present: true, parsed: true, data: JSON.parse(raw) }; }
  catch { return { present: true, parsed: false, data: {} }; }
}

function findTool(name) {
  const ext = IS_WIN ? ".exe" : "";
  const local = path.join(HOME, ".local", "bin", name + ext);
  const cands = [local];
  if (IS_DARWIN) {
    cands.push(`/opt/homebrew/bin/${name}`, `/usr/local/bin/${name}`);
  }
  if (IS_WIN) {
    const winget = path.join(
      process.env.LOCALAPPDATA || path.join(HOME, "AppData", "Local"),
      "Microsoft", "WinGet", "Packages"
    );
    if (exists(winget)) {
      try {
        for (const pkg of fs.readdirSync(winget)) {
          if (pkg.toLowerCase().includes("astral-sh.uv")) {
            const p = path.join(winget, pkg, name + ext);
            if (exists(p)) cands.push(p);
          }
        }
      } catch { /* ignore */ }
    }
  }
  return firstExisting(cands, name);
}

function githubBinPath() {
  const base = path.join(ROOT, "bin", "github-mcp-server");
  if (IS_WIN) {
    const exe = base + ".exe";
    return exists(exe) ? exe : base;
  }
  return exists(base) ? base : base;
}

function clientConfigPath(id) {
  const appData = process.env.APPDATA || path.join(HOME, "AppData", "Roaming");
  const localAppData = process.env.LOCALAPPDATA || path.join(HOME, "AppData", "Local");

  const paths = {
    "claude-code": path.join(ROOT, ".mcp.json"),
    "cursor": path.join(HOME, ".cursor", "mcp.json"),
    "windsurf": path.join(HOME, ".codeium", "windsurf", "mcp_config.json"),
    "qwen": path.join(HOME, ".qwen", "settings.json"),
    "kimi": path.join(HOME, ".kimi", "mcp.json"),
    "gemini": path.join(HOME, ".gemini", "settings.json"),
    "zed": path.join(HOME, ".config", "zed", "settings.json"),
  };

  if (IS_DARWIN) {
    Object.assign(paths, {
      "claude-desktop": path.join(HOME, "Library", "Application Support", "Claude", "claude_desktop_config.json"),
      "vscode": path.join(HOME, "Library", "Application Support", "Code", "User", "mcp.json"),
      "cline": path.join(HOME, "Library", "Application Support", "Code", "User", "globalStorage",
        "saoudrizwan.claude-dev", "settings", "cline_mcp_settings.json"),
      "roo": path.join(HOME, "Library", "Application Support", "Code", "User", "globalStorage",
        "rooveterinaryinc.roo-cline", "settings", "mcp_settings.json"),
    });
  } else if (IS_WIN) {
    Object.assign(paths, {
      "claude-desktop": path.join(appData, "Claude", "claude_desktop_config.json"),
      "vscode": path.join(appData, "Code", "User", "mcp.json"),
      "cline": path.join(appData, "Code", "User", "globalStorage",
        "saoudrizwan.claude-dev", "settings", "cline_mcp_settings.json"),
      "roo": path.join(appData, "Code", "User", "globalStorage",
        "rooveterinaryinc.roo-cline", "settings", "mcp_settings.json"),
    });
  } else {
    Object.assign(paths, {
      "claude-desktop": path.join(HOME, ".config", "Claude", "claude_desktop_config.json"),
      "vscode": path.join(HOME, ".config", "Code", "User", "mcp.json"),
      "cline": path.join(HOME, ".config", "Code", "User", "globalStorage",
        "saoudrizwan.claude-dev", "settings", "cline_mcp_settings.json"),
      "roo": path.join(HOME, ".config", "Code", "User", "globalStorage",
        "rooveterinaryinc.roo-cline", "settings", "mcp_settings.json"),
    });
  }
  return paths[id];
}

function serverSupported(platform) {
  const p = platform || "any";
  if (p === "any") return true;
  if (p === "macos") return IS_DARWIN;
  if (p === "windows") return IS_WIN;
  if (p === "linux") return PLATFORM === "linux";
  return true;
}

// ---------- placeholders ----------
const env = parseEnv(path.join(ROOT, ".env"));
const UV = findTool("uv");
const UVX = findTool("uvx");
const GITHUB_BIN = githubBinPath();
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

const customPlatforms = readJsonIfExists(path.join(ROOT, "mcp", "custom_platforms.json"));
const skippedServers = [];

// ---------- build the neutral server list ----------
const base = readJsonIfExists(path.join(ROOT, "mcp", "servers.base.json"));
const servers = [];

for (const [name, def] of Object.entries(base.ready || {})) {
  if (!serverSupported(def.platform)) {
    skippedServers.push({ name, platform: def.platform || "macos", reason: "platform" });
    continue;
  }
  servers.push({
    name,
    command: subst(def.command),
    args: (def.args || []).map(subst),
    env: def.env || {},
    exclude: def.exclude || [],
  });
}

const serversDir = path.join(ROOT, "servers");
if (exists(serversDir)) {
  for (const d of fs.readdirSync(serversDir).sort()) {
    const sp = path.join(serversDir, d, "server.py");
    if (!exists(sp)) continue;
    const plat = customPlatforms[d] || "any";
    if (!serverSupported(plat)) {
      skippedServers.push({ name: d, platform: plat, reason: "platform" });
      continue;
    }
    servers.push({
      name: d,
      command: UV,
      args: ["run", "--project", ROOT, "python", sp],
      env: {},
      exclude: [],
    });
  }
}

// ---------- client registry ----------
const CLIENTS = [
  { id: "claude-code", secret: "ref-dollar",   schema: "mcpServers", key: "mcpServers" },
  { id: "claude-desktop", secret: "inline",       schema: "mcpServers", key: "mcpServers" },
  { id: "cursor",         secret: "ref-env",      schema: "mcpServers", key: "mcpServers" },
  { id: "windsurf",       secret: "ref-env",      schema: "mcpServers", key: "mcpServers" },
  { id: "qwen",           secret: "ref-dollar",   schema: "mcpServers", key: "mcpServers" },
  { id: "kimi",           secret: "inline",       schema: "mcpServers", key: "mcpServers" },
  { id: "vscode",         secret: "vscode-input", schema: "vscode",     key: "servers" },
  { id: "gemini",         secret: "ref-dollar",   schema: "mcpServers", key: "mcpServers" },
  { id: "cline",          secret: "inline",       schema: "mcpServers", key: "mcpServers" },
  { id: "roo",            secret: "inline",       schema: "mcpServers", key: "mcpServers" },
  { id: "zed",            secret: "inline",       schema: "zed",          key: "context_servers" },
].map((c) => ({ ...c, file: clientConfigPath(c.id) }));

const warnings = [];

function writeQwenHubs() {
  const hubNames = ["dev-hub", "outreach-hub", "career-hub", "prod-hub", "system-hub"];
  const hubIds = ["dev", "outreach", "career", "prod", "system"];
  const block = {};
  for (let i = 0; i < hubNames.length; i++) {
    block[hubNames[i]] = {
      command: UVX,
      args: ["--from", path.join(ROOT, "runner"), "run-mcp-hub", hubIds[i]],
      env: { MCP_SUITE_ROOT: ROOT },
    };
  }
  const out = { mcpServers: block };
  const dest = path.join(ROOT, "mcp", "qwen-hubs.json");
  if (!DRY) {
    fs.writeFileSync(dest, JSON.stringify(out, null, 2) + "\n");
    console.log(`✓ qwen-hubs        ${hubNames.length} hubs -> ${dest}`);
  }
}

// ---------- doctor (`--check`) ----------
if (CHECK) {
  console.log(`MCP suite doctor — ${servers.length} servers (${PLATFORM})\n`);
  if (skippedServers.length) {
    console.log(`skipped ${skippedServers.length} platform-incompatible servers:`);
    for (const s of skippedServers) console.log(`  · ${s.name} (${s.platform})`);
    console.log();
  }
  console.log("toolchain:");
  const tool = (label, p) => console.log(`  ${exists(p) || p === UV || p === UVX ? "✓" : "✗"} ${label.padEnd(14)} ${p}`);
  tool("uv", UV);
  tool("uvx", UVX);
  tool("github binary", GITHUB_BIN);
  console.log(`  ${exists(path.join(ROOT, ".env")) ? "✓" : "✗"} .env present`);

  console.log("\nsecrets (.env):");
  for (const s of ["GITHUB_PERSONAL_ACCESS_TOKEN", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
    "SEC_USER_AGENT", "REOON_API_KEY", "HUNTER_API_KEY", "APOLLO_API_KEY", "CLINE_API_KEY"])
    console.log(`  ${env[s] ? "✓ set  " : "· empty"} ${s}`);
  const clineModel = env.CLINE_MODEL || "anthropic/claude-sonnet-4-6";
  console.log(`  ${env.CLINE_MODEL ? "✓ set  " : "· default"} CLINE_MODEL (${clineModel})`);
  console.log(`  ${env.LLM_PROVIDER ? "✓ set  " : "· empty"} LLM_PROVIDER (${env.LLM_PROVIDER || "—"})`);

  const llmChain = (env.LLM_PROVIDER_CHAIN || "cline,nvidia,grok,gemini,ollama")
    .split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
  const llmKeyFor = {
    cline: env.CLINE_API_KEY,
    nvidia: env.NVIDIA_API_KEY,
    grok: env.GROK_API_KEY || env.XAI_API_KEY,
    gemini: env.GEMINI_API_KEY,
    ollama: env.OLLAMA_BASE_URL || "http://localhost:11434/v1",
  };
  const llmReady = llmChain.filter((id) => {
    if (id === "ollama") return true;
    return Boolean(llmKeyFor[id]);
  });
  const chainStatus = llmChain.map((id) => {
    const ready = id === "ollama" ? "ollama" : (llmKeyFor[id] ? "✓" : "·");
    return `${id} ${ready}`;
  }).join(" | ");
  console.log(`  ${env.LLM_PROVIDER_CHAIN ? "✓ set  " : "· default"} LLM_PROVIDER_CHAIN`);
  console.log(`                  chain: ${chainStatus}`);
  if (llmReady.length) {
    console.log(`                  Apollo tiebreak: ${llmReady.length} of ${llmChain.length} providers ready (first: ${llmReady[0]})`);
  } else {
    console.log("                  Apollo tiebreak: LLM disabled — rule-based matching only");
  }

  console.log("\napollo web mode (Option B):");
  const apolloMode = env.APOLLO_MODE || "web";
  const dataDir = env.MCP_DATA_DIR || path.join(HOME, ".mcp-suite");
  const browserProfile = env.BROWSER_USER_DATA_DIR || path.join(dataDir, "apollo-browser");
  const profileExists = exists(browserProfile) && fs.readdirSync(browserProfile).length > 0;
  const cdpUrl = (env.BROWSER_CDP_URL || "http://127.0.0.1:9222").replace(/\/$/, "");
  let cdpAlive = false;
  try {
    const cdpProbe = spawnSync(
      process.execPath,
      ["-e", `fetch("${cdpUrl}/json/version").then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))`],
      { cwd: ROOT, encoding: "utf8", timeout: 5000 },
    );
    cdpAlive = cdpProbe.status === 0;
  } catch { /* ignore */ }
  let loggedIn = null;
  try {
    const venvPy = path.join(ROOT, ".venv", IS_WIN ? "Scripts" : "bin", IS_WIN ? "python.exe" : "python");
    const pyCmd = exists(venvPy) ? venvPy : UV;
    const probeScript = [
      "import importlib.util, json, sys",
      `ROOT = ${JSON.stringify(ROOT)}`,
      "spec = importlib.util.spec_from_file_location('apollo_srv', ROOT + '/servers/apollo/server.py')",
      "mod = importlib.util.module_from_spec(spec)",
      "spec.loader.exec_module(mod)",
      "print(json.dumps(mod._session_check_sync()))",
    ].join("; ");
    const pyArgs = exists(venvPy)
      ? ["-c", probeScript]
      : ["run", "python", "-c", probeScript];
    const r = spawnSync(pyCmd, pyArgs, { cwd: ROOT, encoding: "utf8", timeout: 45000 });
    if (r.status === 0 && r.stdout) {
      const sess = JSON.parse(String(r.stdout).trim().split("\n").pop());
      loggedIn = sess.logged_in === true;
    }
  } catch { /* ignore */ }
  console.log(`  ${apolloMode === "web" ? "✓ web  " : "· api  "} APOLLO_MODE (${apolloMode})`);
  console.log(`  ${env.APOLLO_API_KEY ? "✓ set  " : "· empty"} APOLLO_API_KEY (api mode only)`);
  console.log(`  ${env.APOLLO_AUTO_LOGIN !== "0" && env.APOLLO_AUTO_LOGIN !== "false" ? "✓ on   " : "· off  "} APOLLO_AUTO_LOGIN (${env.APOLLO_AUTO_LOGIN || "1"})`);
  const loginMode = env.APOLLO_LOGIN_MODE || "manual";
  const loginLabel = loginMode === "manual" ? "✓ manual" : loginMode === "hybrid" ? "· hybrid" : "· auto  ";
  console.log(`  ${loginLabel} APOLLO_LOGIN_MODE (${loginMode})`);
  if (loginMode !== "manual") {
    console.log("                  manual recommended — Google blocks automated password entry via CDP");
  }
  console.log(`  ${env.APOLLO_GOOGLE_EMAIL ? "✓ set  " : "· empty"} APOLLO_GOOGLE_EMAIL`);
  console.log(`  ${env.APOLLO_GOOGLE_PASSWORD ? "✓ set  " : "· empty"} APOLLO_GOOGLE_PASSWORD`);
  console.log(`  ${cdpAlive ? "✓ alive" : "· down "} CDP (${cdpUrl})`);
  console.log(`  ${loggedIn === true ? "✓ yes  " : loggedIn === false ? "· no   " : "?      "} apollo logged_in`);
  console.log(`  ${profileExists ? "✓ exists" : "· empty"} apollo profile (${browserProfile})`);
  if (cdpAlive && loggedIn === false) {
    console.log("                  Login must happen in the apollo profile browser above — NOT daily Chrome.");
  }
  let pwOk = false;
  try {
    const venvPy = path.join(ROOT, ".venv", IS_WIN ? "Scripts" : "bin", IS_WIN ? "python.exe" : "python");
    const pyCmd = exists(venvPy) ? venvPy : UV;
    const pyArgs = exists(venvPy)
      ? ["-c", "import playwright; print('ok')"]
      : ["run", "python", "-c", "import playwright; print('ok')"];
    const r = spawnSync(pyCmd, pyArgs, { cwd: ROOT, encoding: "utf8", timeout: 30000 });
    pwOk = r.status === 0 && String(r.stdout || "").trim().includes("ok");
  } catch { /* ignore */ }
  console.log(`  ${pwOk ? "✓ installed" : "✗ missing "} playwright (uv sync --group browser)`);
  if (!pwOk) console.log("                  uv run playwright install chromium");

  console.log("\nclient config paths:");
  for (const c of CLIENTS) {
    const cur = readClient(c.file);
    const status = !cur.present ? "· not generated" : !cur.parsed ? "✗ UNPARSEABLE" :
      `✓ ${Object.keys(cur.data[c.key] || {}).length} servers`;
    console.log(`  ${c.id.padEnd(15)} ${status}`);
    console.log(`                  ${c.file}`);
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
  for (const srv of servers) {
    if (srv.exclude.includes(client.id)) continue;
    block[srv.name] = buildEntry(client, srv, inputs);
    count++;
  }

  const cur = readClient(client.file);
  if (cur.present && !cur.parsed) {
    warnings.push(`[${client.id}] ${client.file} exists but isn't plain JSON — SKIPPED.`);
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
    if (cur.present && !exists(bak)) fs.copyFileSync(client.file, bak);
    fs.writeFileSync(client.file, json);
    written++;
    console.log(`✓ ${client.id.padEnd(15)} ${count} servers -> ${client.file}`);
  }
}

if (!onlySet || onlySet.has("qwen")) writeQwenHubs();

if (skippedServers.length) {
  console.log(`\nplatform: skipped ${skippedServers.length} server(s) on ${PLATFORM}:`);
  for (const s of skippedServers) console.log(`  - ${s.name} (${s.platform})`);
}
if (warnings.length) {
  console.log("\nwarnings:");
  for (const w of [...new Set(warnings)]) console.log("  - " + w);
}
console.log(`\n${DRY ? "(dry run) " : ""}${servers.length} servers across ${onlySet ? onlySet.size : CLIENTS.length} clients${DRY ? "" : `, ${written} files written`}.`);
