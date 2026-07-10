# Verify LLM provider chain (Cline -> NVIDIA -> Grok -> Gemini -> Ollama)
# Usage: powershell -ExecutionPolicy Bypass -File automation/check-llm.ps1

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = if ($env:MCP_SUITE_ROOT) { $env:MCP_SUITE_ROOT } else { Split-Path -Parent $ScriptDir }
$EnvFile = Join-Path $Root ".env"

if (Test-Path $EnvFile) {
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
}

$chainRaw = if ($env:LLM_PROVIDER_CHAIN) { $env:LLM_PROVIDER_CHAIN } else { "cline,nvidia,grok,gemini,ollama" }
$chain = @($chainRaw -split "," | ForEach-Object { $_.Trim().ToLower() } | Where-Object { $_ })

Write-Host "=== LLM provider chain health check ===" -ForegroundColor Cyan
Write-Host "Chain order: $($chain -join ' -> ')"
Write-Host ""

function Test-ChatProvider {
    param(
        [string]$Label,
        [string]$BaseUrl,
        [string]$ApiKey,
        [string]$Model
    )
    $base = $BaseUrl.TrimEnd("/")
    Write-Host "$Label ($base)" -ForegroundColor Yellow
    if (-not $ApiKey -and $Label -ne "ollama") {
        Write-Host "  SKIP  API key not set" -ForegroundColor DarkGray
        return $false
    }
    try {
        $body = @{
            model = $Model
            messages = @(@{ role = "user"; content = "ping" })
            max_tokens = 5
        } | ConvertTo-Json -Depth 5
        $headers = @{
            Authorization = "Bearer $ApiKey"
            "Content-Type" = "application/json"
        }
        $resp = Invoke-RestMethod -Uri "$base/chat/completions" -Method Post -Headers $headers -Body $body -TimeoutSec 30
        $reply = $resp.choices[0].message.content
        Write-Host "  OK    responded (model: $($resp.model))" -ForegroundColor Green
        if ($reply) { Write-Host "        reply: $($reply.Substring(0, [Math]::Min(60, $reply.Length)))" }
        return $true
    } catch {
        $status = $_.Exception.Response.StatusCode.value__
        Write-Host "  FAIL  ($status): $($_.Exception.Message)" -ForegroundColor Red
        return $false
    }
}

$providerConfigs = @{
    cline  = @{
        base  = if ($env:CLINE_API_BASE_URL) { $env:CLINE_API_BASE_URL } else { "https://api.cline.bot/api/v1" }
        key   = $env:CLINE_API_KEY
        model = if ($env:CLINE_MODEL) { $env:CLINE_MODEL } else { "anthropic/claude-sonnet-4-6" }
    }
    nvidia = @{
        base  = if ($env:NVIDIA_API_BASE_URL) { $env:NVIDIA_API_BASE_URL } else { "https://integrate.api.nvidia.com/v1" }
        key   = $env:NVIDIA_API_KEY
        model = if ($env:NVIDIA_MODEL) { $env:NVIDIA_MODEL } else { "moonshotai/kimi-k2-instruct" }
    }
    grok   = @{
        base  = if ($env:GROK_API_BASE_URL) { $env:GROK_API_BASE_URL } else { "https://api.x.ai/v1" }
        key   = if ($env:GROK_API_KEY) { $env:GROK_API_KEY } else { $env:XAI_API_KEY }
        model = if ($env:GROK_MODEL) { $env:GROK_MODEL } else { "grok-4-fast" }
    }
    gemini = @{
        base  = if ($env:GEMINI_API_BASE_URL) { $env:GEMINI_API_BASE_URL } else { "https://generativelanguage.googleapis.com/v1beta/openai" }
        key   = $env:GEMINI_API_KEY
        model = if ($env:GEMINI_MODEL) { $env:GEMINI_MODEL } else { "gemini-2.0-flash" }
    }
    ollama = @{
        base  = if ($env:OLLAMA_BASE_URL) { $env:OLLAMA_BASE_URL } else { "http://localhost:11434/v1" }
        key   = if ($env:OLLAMA_API_KEY) { $env:OLLAMA_API_KEY } else { "ollama" }
        model = if ($env:OLLAMA_MODEL) { $env:OLLAMA_MODEL } else { "qwen2.5-coder:7b" }
    }
}

$firstOk = $null
$readyCount = 0
foreach ($id in $chain) {
    if (-not $providerConfigs.ContainsKey($id)) {
        Write-Host "Unknown provider in chain: $id" -ForegroundColor Red
        continue
    }
    $cfg = $providerConfigs[$id]
    if ($id -ne "ollama" -and -not $cfg.key) {
        Write-Host "$id" -ForegroundColor Yellow
        Write-Host "  SKIP  API key not set" -ForegroundColor DarkGray
        Write-Host ""
        continue
    }
    $ok = Test-ChatProvider -Label $id -BaseUrl $cfg.base -ApiKey $cfg.key -Model $cfg.model
    if ($ok) {
        $readyCount++
        if (-not $firstOk) { $firstOk = $id }
    }
    Write-Host ""
}

if ($firstOk) {
    Write-Host "Apollo would use first healthy provider: $firstOk ($readyCount of $($chain.Count) in chain ready)" -ForegroundColor Green
} else {
    Write-Host "No LLM providers ready — Apollo falls back to rule-based matching only" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "When Cline expires, set in .env:" -ForegroundColor Cyan
Write-Host "  LLM_PROVIDER_CHAIN=nvidia,grok,gemini,ollama"
Write-Host ""
Write-Host "MCP doctor: node mcp/generate.mjs --check" -ForegroundColor Cyan
