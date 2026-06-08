<#
.SYNOPSIS
    Start Ollama on the Windows host with the parallel-classification
    settings from the README (see "Parallel classification").

.DESCRIPTION
    Stops any running Ollama instance — including the system-tray app, which
    would otherwise respawn the server with default settings — then launches
    `ollama serve` in the foreground with:

        OLLAMA_MAX_LOADED_MODELS = 1
        OLLAMA_CONTEXT_LENGTH    = 4096        (default ctx; the classifier
                                                client also pins num_ctx=4096)
        OLLAMA_NUM_PARALLEL      = -Parallel    (default 4)
        OLLAMA_FLASH_ATTENTION   = 1
        OLLAMA_KV_CACHE_TYPE     = -KvCacheType (default q8_0, halves KV-cache VRAM)

    KV cache is allocated as num_ctx x num_parallel up front. A q8_0 weight
    quant (e.g. gemma e4b at q8_0) loads at roughly double the Q4 size, so on
    a 16 GB card most of the VRAM is weights — raise -Parallel only as far as
    `ollama ps` still shows 100% GPU.

    The variables are set for this process only; nothing is written to your
    system environment. Ctrl+C stops the server — relaunch the Ollama desktop
    app afterwards if you want the tray icon / default behavior back.

    Pair -Parallel with [ollama].concurrency in config.toml (or
    `classify --concurrency N`). After the first classify batch, run
    `ollama ps` — if the model shows anything other than 100% GPU, lower
    -Parallel: spilling weights to CPU costs far more than parallelism gains.

.EXAMPLE
    .\scripts\start-ollama.ps1

.EXAMPLE
    .\scripts\start-ollama.ps1 -Parallel 16
#>
[CmdletBinding()]
param(
    [int]$Parallel = 4,
    [string]$KvCacheType = "q8_0"
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Write-Error "ollama not found on PATH - install it from https://ollama.com/download/windows"
}

# The tray app ("ollama app") supervises the server: stop it first, or it
# immediately respawns ollama.exe with default settings.
foreach ($name in "ollama app", "ollama") {
    Get-Process -Name $name -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host "Stopping $($_.ProcessName) (pid $($_.Id)) ..."
        Stop-Process -Id $_.Id -Force
    }
}

# Wait (up to ~5s) for the previous server to release port 11434.
for ($i = 0; $i -lt 20; $i++) {
    $listening = Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue
    if (-not $listening) { break }
    Start-Sleep -Milliseconds 250
}
if ($listening) {
    Write-Error "Port 11434 is still in use - is another Ollama instance running?"
}

$env:OLLAMA_MAX_LOADED_MODELS = 1
$env:OLLAMA_CONTEXT_LENGTH = 4096
$env:OLLAMA_NUM_PARALLEL = "$Parallel"
$env:OLLAMA_FLASH_ATTENTION = "1"
$env:OLLAMA_KV_CACHE_TYPE = $KvCacheType

Write-Host ""
Write-Host "Starting ollama serve with:"
Write-Host "  OLLAMA_NUM_PARALLEL    = $env:OLLAMA_NUM_PARALLEL"
Write-Host "  OLLAMA_FLASH_ATTENTION = $env:OLLAMA_FLASH_ATTENTION"
Write-Host "  OLLAMA_KV_CACHE_TYPE   = $env:OLLAMA_KV_CACHE_TYPE"
Write-Host ""
Write-Host "After the first classify batch, check 'ollama ps' - the model must show"
Write-Host "100% GPU; if it doesn't, restart with a lower -Parallel."
Write-Host "Ctrl+C stops the server."
Write-Host ""

ollama serve
