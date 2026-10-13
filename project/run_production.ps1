<#
.SYNOPSIS
    Starts the AI Database Assistant in production mode on Windows.
.DESCRIPTION
    Validates virtual environment, installs missing dependencies,
    executes preflight checks, and starts Waitress WSGI.
#>
[CmdletBinding()]
param (
    [string]$HostAddress = "0.0.0.0",
    [int]$Port = 5000,
    [int]$Threads = 8,
    [switch]$SkipPreflight
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

# 1. Resolve Python executable
$PythonPath = Join-Path $ScriptDir ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonPath)) {
    Write-Host "[!] Virtual environment not found at .venv. Falling back to system python..." -ForegroundColor Yellow
    $PythonPath = "python"
}

# 2. Check for .env
$EnvPath = Join-Path $ScriptDir ".env"
if (-not (Test-Path $EnvPath)) {
    $EnvExample = Join-Path $ScriptDir ".env.example"
    if (Test-Path $EnvExample) {
        Write-Host "[i] No .env found. Copying .env.example -> .env..." -ForegroundColor Cyan
        Copy-Item $EnvExample $EnvPath
    }
}

# 3. Preflight check
if (-not $SkipPreflight) {
    Write-Host "[*] Running deployment preflight check..." -ForegroundColor Cyan
    & $PythonPath preflight.py --skip-db --skip-llm
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Preflight check failed. Resolve errors before running production server."
        exit $LASTEXITCODE
    }
}

# 4. Launch Production WSGI Server
Write-Host "[*] Launching Waitress WSGI Server on $HostAddress`:$Port with $Threads worker threads..." -ForegroundColor Green
& $PythonPath production.py --host $HostAddress --port $Port --threads $Threads
