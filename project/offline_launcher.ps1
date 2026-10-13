<# One-click offline startup with concurrent model/application preparation. #>
[CmdletBinding()]
param([switch]$NoBrowser)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$BundleRoot = Split-Path -Parent $ProjectDir
$RuntimeDir = Join-Path $ProjectDir ".runtime"
$LogDir = Join-Path $ProjectDir "logs"
$StatePath = Join-Path $RuntimeDir "offline-processes.json"
$StatusPath = Join-Path $RuntimeDir "startup-status.json"
$AddressPath = Join-Path $RuntimeDir "startup-ui.json"
$SecretPath = Join-Path $RuntimeDir "secret.key"
$PythonPath = Join-Path $BundleRoot "runtime\python\python.exe"
$LlamaPath = Join-Path $BundleRoot "llama-b10186-bin-win-cpu-x64\llama.exe"
$SitePackages = Join-Path $ProjectDir ".venv\Lib\site-packages"
$llamaProcess = $null; $appProcess = $null; $workerProcess = $null; $screenProcess = $null
$mutex = $null; $lockHeld = $false; $screenOpened = $false
$runId = [Guid]::NewGuid().ToString("N")
$started = [DateTimeOffset]::Now
$clock = [Diagnostics.Stopwatch]::StartNew()
$steps = @{ model = "pending"; knowledge = "pending"; application = "pending" }
$owned = @{application_pid=$null; application_executable=$PythonPath; llama_pid=$null; llama_executable=$LlamaPath; startup_worker_pid=$null; startup_worker_executable=$PythonPath; startup_screen_pid=$null; startup_screen_executable=$PythonPath; launcher_pid=$PID; launcher_executable=(Get-Process -Id $PID).Path; project_dir=$ProjectDir}
$AppUrl = ""
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$timingPath = Join-Path $LogDir "startup-$stamp.log"

function Save-Json([string]$Path, $Value) {
    $temporary = "$Path.tmp"
    $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}
function Save-State {
    $owned.url = $AppUrl
    $owned.started_at = $started.ToString("o")
    Save-Json $StatePath $owned
}
function Status([string]$Message, [string]$State = "starting", [string]$Failure = "") {
    Save-Json $StatusPath @{run_id=$runId; started_at=$started.ToString("o"); state=$State; message=$Message; steps=$steps; url=$AppUrl; error=$Failure; elapsed_seconds=[Math]::Round($clock.Elapsed.TotalSeconds,2)}
    ("{0:n2}s {1}" -f $clock.Elapsed.TotalSeconds,$Message) | Add-Content -LiteralPath $timingPath -Encoding UTF8
}
function Test-Endpoint([string]$Url) {
    # Avoid expensive HTTP connection retries when the process has not bound its port.
    $uri = [Uri]$Url; $client = New-Object Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($uri.Host, $uri.Port)
        if (-not $task.Wait(180) -or -not $client.Connected) { return $false }
    } catch { return $false } finally { $client.Dispose() }
    try { return (Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2).StatusCode -eq 200 } catch { return $false }
}
function Check-Model {
    if ($steps.model -ne "done" -and (Test-Endpoint ($LlmEndpoint.TrimEnd('/') + "/health"))) {
        $steps.model = "done"; Status "Local model is ready."
    }
}
function Run-Worker([string]$Arguments, [string]$Label, [int]$TimeoutSeconds) {
    $workerOut = Join-Path $LogDir "$Label-$stamp.out.log"
    $workerErr = Join-Path $LogDir "$Label-$stamp.error.log"
    $script:workerProcess = Start-Process -FilePath $PythonPath -ArgumentList $Arguments -WorkingDirectory $ProjectDir -WindowStyle Hidden -PassThru -RedirectStandardOutput $workerOut -RedirectStandardError $workerErr
    # Retain the process handle before a short-lived bootstrap can exit.
    # Windows PowerShell otherwise sometimes returns a null ExitCode.
    $null = $workerProcess.Handle
    $owned.startup_worker_pid = $workerProcess.Id; Save-State
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while (-not $workerProcess.HasExited) {
        if ((Get-Date) -gt $deadline) { throw "$Label timed out. See $workerErr" }
        if ($llamaProcess -and $llamaProcess.HasExited) { throw "The local model stopped during startup. See $llamaErr" }
        Check-Model
        Start-Sleep -Milliseconds 400
    }
    # WaitForExit flushes the redirected handles and makes ExitCode reliable.
    $workerProcess.WaitForExit()
    if ($workerProcess.ExitCode -ne 0) { throw "$Label failed. See $workerOut and $workerErr" }
    $owned.startup_worker_pid = $null; Save-State
    $script:workerProcess = $null
}

try {
    New-Item -ItemType Directory -Force -Path $RuntimeDir, $LogDir | Out-Null
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $key = ([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($ProjectDir.ToLowerInvariant())))).Replace("-","").Substring(0,20) } finally { $hash.Dispose() }
    $mutex = New-Object Threading.Mutex($false, "Local\AIDatabaseStartup-$key")
    try { $lockHeld = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $lockHeld = $true }
    if (-not $lockHeld) {
        if (-not $NoBrowser -and (Test-Path -LiteralPath $AddressPath)) {
            $address = Get-Content -LiteralPath $AddressPath -Raw | ConvertFrom-Json
            Start-Process ([string]$address.url)
        }
        exit 0
    }
    $config = @{}
    Get-Content -LiteralPath (Join-Path $ProjectDir ".env") | ForEach-Object {
        if ($_ -match '^\s*([A-Z_0-9]+)\s*=(.*)$') { $config[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'") }
    }
    $ServerPort = if ($config.ContainsKey("SERVER_PORT")) { [int]$config.SERVER_PORT } else { 5000 }
    $LlmEndpoint = if ($config.ContainsKey("LLM_ENDPOINT")) { $config.LLM_ENDPOINT } else { "http://localhost:8080" }
    $LlmUri = [Uri]$LlmEndpoint
    if ($LlmUri.Host -in @('localhost', '::1')) {
        # The bundled server binds IPv4. Avoid localhost resolving to IPv6
        # and spending the readiness timeout probing an unbound address.
        $localUri = New-Object UriBuilder($LlmUri)
        $localUri.Host = '127.0.0.1'
        $LlmEndpoint = $localUri.Uri.AbsoluteUri.TrimEnd('/')
        $LlmUri = [Uri]$LlmEndpoint
    }
    $AppUrl = "http://127.0.0.1:$ServerPort"
    if ((Test-Endpoint "$AppUrl/readyz") -and (Test-Endpoint ($LlmEndpoint.TrimEnd('/') + "/health"))) {
        if (-not $NoBrowser) { Start-Process $AppUrl }
        exit 0
    }
    # Retain ownership of an already-running component when restarting the other.
    if (Test-Path -LiteralPath $StatePath) {
        $previous = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
        foreach ($field in @("application_pid","application_executable","llama_pid","llama_executable")) {
            if ($previous.PSObject.Properties.Name -contains $field) { $owned[$field] = $previous.$field }
        }
    }
    Status "Preparing your local workspace."
    foreach ($required in @($PythonPath, $SitePackages, $LlamaPath, (Join-Path $ProjectDir "startup_screen.py"))) {
        if (-not (Test-Path -LiteralPath $required)) { throw "Offline package is incomplete. Missing: $required" }
    }
    $env:PYTHONPATH = "$ProjectDir;$SitePackages"
    $env:PYTHONNOUSERSITE = "1"
    $env:PYTHONUTF8 = "1"
    $env:ENVIRONMENT = "production"
    $env:FLASK_ENV = "production"
    $env:SERVER_HOST = "127.0.0.1"
    $env:SERVER_PORT = [string]$ServerPort
    $env:CORS_ORIGINS = $AppUrl
    $env:LLM_ENDPOINT = $LlmEndpoint
    # The launcher checks model readiness concurrently; avoid a redundant startup probe.
    $env:OFFLINE_LAUNCHER = "1"
    if (-not $NoBrowser) {
        $screenArgs = 'startup_screen.py --status "' + $StatusPath + '" --address "' + $AddressPath + '" --run-id ' + $runId
        $screenProcess = Start-Process -FilePath $PythonPath -ArgumentList $screenArgs -WorkingDirectory $ProjectDir -WindowStyle Hidden -PassThru -RedirectStandardError (Join-Path $LogDir "startup-screen-$stamp.error.log")
        $owned.startup_screen_pid = $screenProcess.Id; Save-State
        $deadline = (Get-Date).AddSeconds(8)
        while ((Get-Date) -lt $deadline -and -not $screenProcess.HasExited) {
            if (Test-Path -LiteralPath $AddressPath) {
                $address = Get-Content -LiteralPath $AddressPath -Raw | ConvertFrom-Json
                if ($address.run_id -eq $runId) { Start-Process ([string]$address.url); $screenOpened = $true; break }
            }
            Start-Sleep -Milliseconds 60
        }
    }
    if (-not (Test-Path -LiteralPath $SecretPath)) {
        $bytes = New-Object byte[] 48; $random = New-Object Security.Cryptography.RNGCryptoServiceProvider
        try { $random.GetBytes($bytes) } finally { $random.Dispose() }
        [Convert]::ToBase64String($bytes) | Set-Content -LiteralPath $SecretPath -NoNewline -Encoding ASCII
    }
    $env:SECRET_KEY = Get-Content -LiteralPath $SecretPath -Raw
    $steps.model = "running"; Status "Loading the local model and preparing database knowledge."
    if (Test-Endpoint ($LlmEndpoint.TrimEnd('/') + "/health")) { $steps.model = "done" }
    else {
        if ($LlmUri.Host -notin @("localhost","127.0.0.1","::1")) { throw "The configured model endpoint is unavailable: $LlmEndpoint" }
        $ModelPath = Get-ChildItem -LiteralPath (Join-Path $BundleRoot "models") -Filter "*.gguf" -File | Sort-Object Length -Descending | Select-Object -First 1 -ExpandProperty FullName
        if (-not $ModelPath) { throw "No bundled GGUF model was found." }
        $llamaOut = Join-Path $LogDir "llama-$stamp.out.log"; $llamaErr = Join-Path $LogDir "llama-$stamp.error.log"
        $llamaArgs = 'serve -m "' + $ModelPath + '" --host 127.0.0.1 --port ' + $LlmUri.Port + ' -c 4096 -ngl 0'
        $llamaProcess = Start-Process -FilePath $LlamaPath -ArgumentList $llamaArgs -WorkingDirectory (Split-Path -Parent $LlamaPath) -WindowStyle Hidden -PassThru -RedirectStandardOutput $llamaOut -RedirectStandardError $llamaErr
        $owned.llama_pid = $llamaProcess.Id; $owned.llama_executable = $LlamaPath; Save-State
    }
    if (-not (Test-Endpoint "$AppUrl/readyz")) {
        $steps.knowledge = "running"; Status "Checking database knowledge. A first scan may take several minutes."
        Run-Worker "offline_bootstrap.py" "bootstrap" 1200
        $steps.knowledge = "done"; $steps.application = "running"; Status "Starting your workspace and checking the database connection."
        Run-Worker "preflight.py --quiet --skip-llm" "preflight" 90
        $appOut = Join-Path $LogDir "application-$stamp.out.log"; $appErr = Join-Path $LogDir "application-$stamp.error.log"
        $appArgs = 'production.py --host 127.0.0.1 --port ' + $ServerPort + ' --threads 8 --skip-preflight'
        $appProcess = Start-Process -FilePath $PythonPath -ArgumentList $appArgs -WorkingDirectory $ProjectDir -WindowStyle Hidden -PassThru -RedirectStandardOutput $appOut -RedirectStandardError $appErr
        $owned.application_pid = $appProcess.Id; $owned.application_executable = $PythonPath; Save-State
    } else { $steps.knowledge = "done"; $steps.application = "done" }
    $deadline = (Get-Date).AddSeconds(180)
    while ((Get-Date) -lt $deadline) {
        if ($appProcess -and $appProcess.HasExited) { throw "The application stopped during startup. See $appErr" }
        if ($llamaProcess -and $llamaProcess.HasExited) { throw "The model stopped during startup. See $llamaErr" }
        Check-Model
        if ($steps.application -ne "done" -and (Test-Endpoint "$AppUrl/readyz")) { $steps.application = "done"; Status "Workspace ready. Finishing model startup." }
        if ($steps.application -eq "done" -and $steps.model -eq "done") { break }
        Start-Sleep -Milliseconds 400
    }
    if ($steps.application -ne "done" -or $steps.model -ne "done") { throw "Startup timed out. Check the application and model logs in $LogDir" }
    $owned.launcher_pid = $null; Save-State
    Status "Your workspace is ready." "ready"
    if (-not $NoBrowser -and -not $screenOpened) { Start-Process $AppUrl }
} catch {
    $failure = $_.Exception.Message
    foreach ($child in @($workerProcess,$appProcess,$llamaProcess)) {
        if ($child -and -not $child.HasExited) { Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue }
    }
    if ($appProcess) { $owned.application_pid = $null }
    if ($llamaProcess) { $owned.llama_pid = $null }
    $owned.startup_worker_pid = $null; $owned.launcher_pid = $null
    if ($lockHeld) {
        Status "Startup could not complete." "failed" ($failure + "`n`nLogs: " + $LogDir + "`nUse the desktop icon to try again.")
        Save-State
    }
    if (-not $screenOpened -and -not $NoBrowser) {
        $shell = New-Object -ComObject WScript.Shell
        [void]$shell.Popup("Startup failed: $failure",0,"AI Database Assistant",16)
    }
    Write-Error $failure -ErrorAction Continue
    exit 1
} finally {
    if ($lockHeld -and $mutex) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
}
