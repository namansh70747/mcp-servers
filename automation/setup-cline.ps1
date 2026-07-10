# Setup Cline API auth + generate Cline MCP config from .env
# Usage: powershell -ExecutionPolicy Bypass -File automation/setup-cline.ps1

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = if ($env:MCP_SUITE_ROOT) { $env:MCP_SUITE_ROOT } else { Split-Path -Parent $ScriptDir }
$EnvFile = Join-Path $Root ".env"

if (-not (Test-Path $EnvFile)) {
    Write-Error ".env not found at $EnvFile - copy .env.example first"
}

# Load .env into process environment
Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) { return }
    $i = $line.IndexOf("=")
    if ($i -lt 1) { return }
    $key = $line.Substring(0, $i).Trim()
    $val = $line.Substring($i + 1).Trim()
    if (($val.StartsWith('"') -and $val.EndsWith('"')) -or ($val.StartsWith("'") -and $val.EndsWith("'"))) {
        $val = $val.Substring(1, $val.Length - 2)
    }
    Set-Item -Path "env:$key" -Value $val
}

$apiKey = $env:CLINE_API_KEY
$model = if ($env:CLINE_MODEL) { $env:CLINE_MODEL } else { "anthropic/claude-sonnet-4-6" }

if (-not $apiKey) {
    Write-Error "CLINE_API_KEY is empty in .env - get one at https://app.cline.bot/dashboard/account?tab=api-keys"
}

Write-Host "=== Cline API setup ===" -ForegroundColor Cyan
Write-Host "Provider: cline"
Write-Host "Model:    $model"
Write-Host ""

# Ensure cline CLI
$cline = Get-Command cline -ErrorAction SilentlyContinue
if (-not $cline) {
    Write-Host "Installing cline CLI globally (npm install -g cline)..." -ForegroundColor Yellow
    npm install -g cline
    $cline = Get-Command cline -ErrorAction SilentlyContinue
    if (-not $cline) {
        Write-Error "cline CLI not found after install. Ensure Node.js/npm is on PATH."
    }
}
Write-Host "cline CLI: $($cline.Source)" -ForegroundColor Green

# Authenticate with Cline API
Write-Host "Running cline auth..." -ForegroundColor Yellow
& cline auth -p cline -k $apiKey -m $model
if ($LASTEXITCODE -ne 0) {
    Write-Warning "cline auth returned exit code $LASTEXITCODE - you may need to auth manually in VS Code Cline panel"
}

# Generate MCP config for Cline extension
Write-Host ""
Write-Host 'Generating Cline MCP config - 50 servers...' -ForegroundColor Yellow
Push-Location $Root
try {
    node mcp/generate.mjs --only=cline
    if ($LASTEXITCODE -ne 0) { Write-Error "generate.mjs failed" }
} finally {
    Pop-Location
}

$clineMcp = Join-Path $env:APPDATA "Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json"
if (Test-Path $clineMcp) {
    $cfg = Get-Content $clineMcp -Raw | ConvertFrom-Json
    $count = ($cfg.mcpServers.PSObject.Properties | Measure-Object).Count
    Write-Host ""
    Write-Host "OK - Cline MCP config: $count servers at" -ForegroundColor Green
    Write-Host "  $clineMcp"
} else {
    Write-Warning "cline_mcp_settings.json not found yet - install VS Code + Cline extension, then re-run"
}

Write-Host ""
Write-Host "=== Ollama backup (manual switch) ===" -ForegroundColor Cyan
Write-Host "  ollama serve"
Write-Host "  ollama pull qwen2.5-coder:7b"
Write-Host "  cline auth -p ollama -m qwen2.5-coder:7b"
Write-Host ""
Write-Host "Health check: powershell -File automation/check-llm.ps1" -ForegroundColor Cyan
