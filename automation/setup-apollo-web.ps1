# Apollo Option B — background Chrome/Edge via CDP + manual Google login.
#
# Usage:
#   .\automation\setup-apollo-web.ps1
#
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$envFile = Join-Path $Root ".env"
$profilePath = $null
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line -match "^([^=]+)=(.*)$") {
            $name = $matches[1].Trim()
            $val = $matches[2].Trim().Trim('"').Trim("'")
            [Environment]::SetEnvironmentVariable($name, $val, "Process")
            if ($name -eq "BROWSER_USER_DATA_DIR" -and $val) { $profilePath = $val }
        }
    }
}
if (-not $profilePath) {
    $dataDir = [Environment]::GetEnvironmentVariable("MCP_DATA_DIR")
    if (-not $dataDir) { $dataDir = Join-Path $env:USERPROFILE ".mcp-suite" }
    $profilePath = Join-Path $dataDir "apollo-browser"
}

Write-Host "=== Apollo Background Browser Setup (Option B) ===" -ForegroundColor Cyan
Write-Host "Profile: $profilePath" -ForegroundColor Gray
Write-Host "IMPORTANT: Log in inside THIS browser (taskbar), NOT your daily Chrome/Edge." -ForegroundColor Yellow
Write-Host "Log into Apollo app, LinkedIn, and install+log into the Apollo Chrome extension." -ForegroundColor Yellow
Write-Host "Manual step (once per session): click Apollo FAB on LinkedIn — everything else is automated." -ForegroundColor Yellow
Write-Host "The script clicks 'Log In with Google' for you — type your email and password yourself." -ForegroundColor Yellow
Write-Host ""

Write-Host "Installing Playwright (CDP connector only)..." -ForegroundColor Yellow
uv sync --group browser
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Starting browser + ensure_ready (manual login)..." -ForegroundColor Yellow
uv run python automation/start_apollo_browser.py
$exit = $LASTEXITCODE

Write-Host ""
if ($exit -eq 0) {
    Write-Host "Apollo ready — background browser running (minimized)." -ForegroundColor Green
    Write-Host "find_people() runs silently in background CDP tabs." -ForegroundColor White
} else {
    Write-Host "Login not finished yet — restore the browser from the taskbar," -ForegroundColor Yellow
    Write-Host "complete Google sign-in there (email + password + 2FA), then minimize and run:" -ForegroundColor Yellow
    Write-Host "  uv run python automation/start_apollo_browser.py" -ForegroundColor White
}
Write-Host ""
Write-Host "Test: uv run python automation/run_cometapi_ceo.py" -ForegroundColor Gray
exit $exit
