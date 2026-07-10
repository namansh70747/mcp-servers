# Regenerate MCP configs for all agent clients (Cursor, Qwen, Kimi, Cline, VS Code, etc.)
# Usage: powershell -ExecutionPolicy Bypass -File automation\refresh-all-mcp.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "=== MCP Suite - refresh all agent configs ===" -ForegroundColor Cyan

Write-Host "Syncing Python deps..." -ForegroundColor Yellow
uv sync
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Generating all client MCP configs..." -ForegroundColor Yellow
node mcp/generate.mjs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Running MCP doctor..." -ForegroundColor Yellow
node mcp/generate.mjs --check
$checkExit = $LASTEXITCODE

Write-Host ""
Write-Host "Configs written for Claude Code, Cursor, Windsurf, Qwen, Kimi, Gemini, Zed," -ForegroundColor Green
Write-Host "Claude Desktop, VS Code, Cline, Roo" -ForegroundColor Green
Write-Host ""
Write-Host "Restart each client to load new MCP servers." -ForegroundColor Yellow
Write-Host "Apollo browser: run automation/setup-apollo-web.ps1 if CDP is not running." -ForegroundColor Gray

exit $checkExit
