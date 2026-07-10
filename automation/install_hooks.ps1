# Install a git post-commit hook that auto-reindexes with codeindex and refreshes context files.
# Usage: .\automation\install_hooks.ps1 [repo_path]
param([string]$Target = (Get-Location).Path)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Suite = if ($env:MCP_SUITE_ROOT) { $env:MCP_SUITE_ROOT } else { Split-Path -Parent $ScriptDir }

Push-Location $Target
try {
    $Repo = (git rev-parse --show-toplevel 2>$null)
    if (-not $Repo) { throw "Not a git repo: $Target" }
} finally {
    Pop-Location
}

$Python = Join-Path $Suite ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

$HookDir = Join-Path $Repo ".git\hooks"
$Hook = Join-Path $HookDir "post-commit"
New-Item -ItemType Directory -Path $HookDir -Force | Out-Null

$HookContent = @"
#!/bin/sh
# codeindex auto-prime (installed by mcp-suite)
"$Python" "$Suite\automation\reindex_hook.py" "$Repo" >/dev/null 2>&1 || true
"@
Set-Content -Path $Hook -Value $HookContent -NoNewline

Write-Host "post-commit reindex hook installed in $Repo"
& $Python "$Suite\automation\reindex_hook.py" $Repo
Write-Host "$Repo is primed - CLAUDE.md + AGENTS.md refreshed"
