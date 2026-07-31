<#
  start.ps1 - local dev launcher for the screener stack (Windows / PowerShell 5.1+).

  Brings up three supervised windows, each teeing to logs\<svc>_<date>.log:
    1. API       - scripts\serve_api.py            (http://127.0.0.1:8001)
    2. Pipeline  - scripts\run_pipeline.py --loop  (ingest -> enrich -> score -> signal)
    3. Frontend  - npm run dev                     (http://localhost:3000)

  The paper-trading DRIVER is OFF by default. Opt in with -Trader ONLY if this
  machine's Alpaca keys point at an account NO OTHER driver is trading (the cloud
  driver on Railway is live for the shared paper account -- running a second driver
  on the SAME account double-places orders). See the one-driver rule below.

  Usage:
    .\start.ps1                 # API + pipeline (lexicon sentiment) + frontend
    .\start.ps1 -FinBERT        # score with FinBERT instead of the LM lexicon
    .\start.ps1 -Trader         # ALSO launch the paper driver (read the warning!)
    .\start.ps1 -Stop           # stop anything this script started, then exit

  ASCII-only on purpose: em-dashes / unicode corrupt under cp1252 on PS 5.1.
#>

param(
    [switch]$Trader,             # opt-in: launch the paper-trading driver (run_trader.py)
    [switch]$FinBERT,            # opt-in: FinBERT sentiment instead of the LM lexicon
    [switch]$Stop,               # stop the stack (frees ports 8001/3000, kills workers)
    [string]$BindHost = "127.0.0.1"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $root

# --- -Stop: free the ports + kill our python/node workers, then exit ----------
if ($Stop) {
    Write-Host "Stopping screener stack..." -ForegroundColor Yellow
    foreach ($port in 8001, 3000) {
        $owners = (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).OwningProcess
        foreach ($procId in @($owners) | Where-Object { $_ } | Select-Object -Unique) {
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            Write-Host ('  freed port ' + $port + ' (pid ' + $procId + ')') -ForegroundColor DarkGray
        }
    }
    foreach ($needle in 'serve_api', 'run_pipeline', 'run_trader') {
        $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like ('*' + $needle + '*') }
        foreach ($p in $procs) {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host ('  stopped ' + $needle + ' (pid ' + $p.ProcessId + ')') -ForegroundColor DarkGray
        }
    }
    Write-Host "Stopped. (Close any leftover windows manually if they were opened by hand.)" -ForegroundColor Green
    return
}

# --- Preflight ----------------------------------------------------------------
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "ERROR: venv Python not found at $venvPy" -ForegroundColor Red
    Write-Host "Create it first (from the repo root):" -ForegroundColor Red
    Write-Host "  python -m venv .venv" -ForegroundColor Red
    Write-Host "  .\.venv\Scripts\python -m pip install -r requirements.txt" -ForegroundColor Red
    exit 1
}

$nodeModules = Join-Path $root "frontend\node_modules"
if (-not (Test-Path $nodeModules)) {
    Write-Host "ERROR: frontend deps not installed ($nodeModules missing)" -ForegroundColor Red
    Write-Host "Install them first:" -ForegroundColor Red
    Write-Host "  cd frontend; npm install; cd .." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Host "NOTE: no .env found -- running with defaults (SQLite at data\pipeline.db," -ForegroundColor Yellow
    Write-Host "      lexicon sentiment, no Alpaca/LLM). Copy the template from docs\ENVIRONMENT.md" -ForegroundColor Yellow
    Write-Host "      to .env to switch features on. The stack runs fine without it." -ForegroundColor Yellow
}

# Warn (do not kill) if the ports are already taken -- use -Stop to reclaim them.
foreach ($port in 8001, 3000) {
    $busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($busy) {
        Write-Host ('WARNING: port ' + $port + ' is already in use (pid ' +
            $busy.OwningProcess + '). Run  .\start.ps1 -Stop  to reclaim it.') -ForegroundColor Yellow
    }
}

# Per-service logs (gitignored). Supervised windows tee stdout+stderr here.
$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

$srcPath = Join-Path $root "src"   # the `pipeline` package lives under src/ (not pip-installed locally)

Write-Host ""
Write-Host "Starting screener stack (host $BindHost)" -ForegroundColor Cyan
Write-Host ("  logs -> " + $logDir) -ForegroundColor DarkGray
Write-Host ""

# --- 1. API (serve_api.py, :8001) --------------------------------------------
Write-Host "Launching API window (:8001)..." -ForegroundColor Blue
Start-Process powershell -ArgumentList "-NoExit", "-Command", @"
    `$Host.UI.RawUI.WindowTitle = 'screener API :8001'
    Set-Location '$root'
    `$env:PYTHONPATH = '$srcPath'
    `$env:PYTHONUNBUFFERED = '1'; `$env:PYTHONIOENCODING = 'utf-8'
    Write-Host '=== screener API (serve_api.py :8001) ===' -ForegroundColor Blue
    while (`$true) {
        `$log = '$logDir\api_' + (Get-Date -Format 'yyyyMMdd') + '.log'
        & cmd /c '"$venvPy" scripts\serve_api.py --host $BindHost --port 8001 2>&1' | ForEach-Object { `$_; Add-Content -LiteralPath `$log -Value `$_ -Encoding UTF8 }
        Write-Host ('[start.ps1] API exited (code ' + `$LASTEXITCODE + ') -- restarting in 3s. Ctrl+C to stop.') -ForegroundColor Red
        Start-Sleep -Seconds 3
    }
"@

# --- 2. Pipeline loop (run_pipeline.py) ---------------------------------------
# Default is the zero-dependency LM lexicon (fast, low-RAM, no ML stack needed).
# -FinBERT drops --no-finbert so it scores with FinBERT (torch, ~440MB) or, if
# SENTIMENT_MODE=onnx is set in .env, the in-process int8 ONNX model.
$pipeArgs = if ($FinBERT) { "--interval 300" } else { "--interval 300 --no-finbert" }
$sentLabel = if ($FinBERT) { "FinBERT" } else { "lexicon" }
Write-Host ("Launching Pipeline window (" + $sentLabel + " sentiment)...") -ForegroundColor Blue
Start-Process powershell -ArgumentList "-NoExit", "-Command", @"
    `$Host.UI.RawUI.WindowTitle = 'screener pipeline ($sentLabel)'
    Set-Location '$root'
    `$env:PYTHONPATH = '$srcPath'
    `$env:PYTHONUNBUFFERED = '1'; `$env:PYTHONIOENCODING = 'utf-8'
    Write-Host '=== screener pipeline (run_pipeline.py $pipeArgs) ===' -ForegroundColor Blue
    while (`$true) {
        `$log = '$logDir\pipeline_' + (Get-Date -Format 'yyyyMMdd') + '.log'
        & cmd /c '"$venvPy" scripts\run_pipeline.py $pipeArgs 2>&1' | ForEach-Object { `$_; Add-Content -LiteralPath `$log -Value `$_ -Encoding UTF8 }
        Write-Host ('[start.ps1] Pipeline exited (code ' + `$LASTEXITCODE + ') -- restarting in 10s. Ctrl+C to stop.') -ForegroundColor Red
        Start-Sleep -Seconds 10
    }
"@

# --- 3. Frontend (npm run dev, :3000) ----------------------------------------
# Defaults to the localhost:8001 API with NO extra env (see frontend/src/lib/config.ts).
Write-Host "Launching Frontend window (:3000)..." -ForegroundColor Blue
Start-Process powershell -ArgumentList "-NoExit", "-Command", @"
    `$Host.UI.RawUI.WindowTitle = 'screener frontend :3000'
    Set-Location '$root\frontend'
    Write-Host '=== screener frontend (npm run dev :3000) ===' -ForegroundColor Blue
    while (`$true) {
        `$log = '$logDir\frontend_' + (Get-Date -Format 'yyyyMMdd') + '.log'
        & cmd /c 'npm run dev 2>&1' | ForEach-Object { `$_; Add-Content -LiteralPath `$log -Value `$_ -Encoding UTF8 }
        Write-Host ('[start.ps1] Frontend exited (code ' + `$LASTEXITCODE + ') -- restarting in 3s. Ctrl+C to stop.') -ForegroundColor Red
        Start-Sleep -Seconds 3
    }
"@

# --- 4. Paper-trading driver (OPT-IN: -Trader) -------------------------------
if ($Trader) {
    Write-Host ""
    Write-Host "############################################################" -ForegroundColor Red
    Write-Host "# -Trader: launching the LOCAL paper driver (run_trader.py) #" -ForegroundColor Red
    Write-Host "# ONE driver per Alpaca account. The CLOUD driver is LIVE.  #" -ForegroundColor Red
    Write-Host "# Only proceed if THIS machine's ALPACA_* keys point at an  #" -ForegroundColor Red
    Write-Host "# account no other driver is trading, or you WILL double-   #" -ForegroundColor Red
    Write-Host "# place orders on the shared paper book.                    #" -ForegroundColor Red
    Write-Host "############################################################" -ForegroundColor Red
    Start-Process powershell -ArgumentList "-NoExit", "-Command", @"
        `$Host.UI.RawUI.WindowTitle = 'screener TRADER driver'
        Set-Location '$root'
        `$env:PYTHONPATH = '$srcPath'
        `$env:PYTHONUNBUFFERED = '1'; `$env:PYTHONIOENCODING = 'utf-8'
        `$env:TRADER_DRIVER_ENABLED = 'true'   # this window only; not persisted
        Write-Host '=== screener TRADER driver (run_trader.py) ===' -ForegroundColor Magenta
        while (`$true) {
            `$log = '$logDir\trader_' + (Get-Date -Format 'yyyyMMdd') + '.log'
            & cmd /c '"$venvPy" scripts\run_trader.py 2>&1' | ForEach-Object { `$_; Add-Content -LiteralPath `$log -Value `$_ -Encoding UTF8 }
            Write-Host ('[start.ps1] Trader driver exited (code ' + `$LASTEXITCODE + ') -- restarting in 30s. Ctrl+C to stop.') -ForegroundColor Red
            Start-Sleep -Seconds 30
        }
"@
} else {
    Write-Host "Trader driver: OFF (pass -Trader to opt in; read the one-driver-per-account rule first)." -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "Up. Open:" -ForegroundColor Green
Write-Host "  http://localhost:3000        (dashboard)" -ForegroundColor Green
Write-Host "  http://127.0.0.1:8001/health (API health: scoring / mount / driver)" -ForegroundColor Green
Write-Host "  http://127.0.0.1:8001/docs   (API docs)" -ForegroundColor Green
Write-Host ""
Write-Host "Each service runs in its own window (restart-on-crash) and tees to logs\." -ForegroundColor DarkGray
Write-Host "Stop everything with  .\start.ps1 -Stop  (or just close the windows)." -ForegroundColor DarkGray
