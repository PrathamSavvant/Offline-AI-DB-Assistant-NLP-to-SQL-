[CmdletBinding()]
param([ValidateRange(0, 5)][int]$DelaySeconds = 0)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$BundleRoot = Split-Path -Parent $ProjectDir
$StatePath = Join-Path $ProjectDir ".runtime\offline-processes.json"

if ($DelaySeconds -gt 0) { Start-Sleep -Seconds $DelaySeconds }

function Stop-OwnedProcess($ProcessId, [string]$ExpectedExecutable) {
    if (-not $ProcessId) { return $true }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if (-not $process) { return $true }
    if ($process.ExecutablePath -and $process.ExecutablePath.Equals($ExpectedExecutable, [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
        $deadline = (Get-Date).AddSeconds(5)
        while ((Get-Date) -lt $deadline -and (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
            Start-Sleep -Milliseconds 200
        }
        return -not [bool](Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
    }
    return $false
}

try {
    if (Test-Path -LiteralPath $StatePath) {
        $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
        # Stop the coordinator first so it cannot create another child while
        # shutdown is stopping the model or application.
        if ($state.PSObject.Properties.Name -contains 'launcher_pid' -and $state.launcher_pid) {
            if (-not (Stop-OwnedProcess $state.launcher_pid ([string]$state.launcher_executable))) {
                throw 'The startup coordinator no longer belongs to this bundle.'
            }
            $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
        }
        $helpersStopped = $true
        foreach ($prefix in @('startup_worker', 'startup_screen')) {
            $pidField = $prefix + '_pid'; $executableField = $prefix + '_executable'
            if ($state.PSObject.Properties.Name -contains $pidField -and $state.$pidField) {
                if (-not (Stop-OwnedProcess $state.$pidField ([string]$state.$executableField))) { $helpersStopped = $false }
            }
        }
        $python = if ($state.PSObject.Properties.Name -contains "application_executable") {
            [string]$state.application_executable
        } else { Join-Path $BundleRoot "runtime\python\python.exe" }
        $llama = if ($state.PSObject.Properties.Name -contains "llama_executable" -and $state.llama_executable) {
            [string]$state.llama_executable
        } else { Join-Path $BundleRoot "llama-b10186-bin-win-cpu-x64\llama.exe" }
        $appStopped = Stop-OwnedProcess $state.application_pid $python
        $llamaStopped = Stop-OwnedProcess $state.llama_pid $llama
        if ($appStopped -and $llamaStopped -and $helpersStopped) {
            $statusPath = Join-Path $ProjectDir '.runtime\startup-status.json'
            if (Test-Path -LiteralPath $statusPath) {
                $status = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
                $status.state = 'cancelled'; $status.message = 'Application stopped.'
                $status | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
            }
            Remove-Item -LiteralPath $StatePath -Force
        } else {
            throw "A saved process ID no longer belongs to this offline bundle; it was not stopped."
        }
    }
} catch {
    $shell = New-Object -ComObject WScript.Shell
    [void]$shell.Popup("Shutdown could not complete: $($_.Exception.Message)", 0, "AI Database Assistant", 16)
    exit 1
}
