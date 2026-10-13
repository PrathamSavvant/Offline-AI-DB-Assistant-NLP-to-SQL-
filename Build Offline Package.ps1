[CmdletBinding()]
param(
    [string]$PackageName = "AI-Database-Assistant-Offline-Windows",
    [switch]$Resume,
    [switch]$CreateArchive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Project = Join-Path $Root "project"
$OutputRoot = Join-Path $Root "outputs"
$Package = Join-Path $OutputRoot $PackageName
$Archive = Join-Path $OutputRoot "$PackageName.tar"

if ($PackageName -notmatch '^[A-Za-z0-9][A-Za-z0-9_-]*$') {
    throw "PackageName must contain only letters, numbers, hyphens, or underscores."
}
if ($CreateArchive -and (Test-Path -LiteralPath $Archive)) {
    throw "Package archive already exists. Choose a new PackageName; existing artifacts will not be overwritten."
}
if ((Test-Path -LiteralPath $Package) -and -not $Resume) {
    throw "Package folder already exists. Choose a new PackageName or pass -Resume for an incomplete build."
}
if ($Resume -and -not (Test-Path -LiteralPath $Package -PathType Container)) {
    throw "There is no incomplete package folder to resume."
}

function Copy-Tree([string]$Source, [string]$Destination) {
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) { throw "Missing source directory: $Source" }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    & robocopy.exe $Source $Destination /E /XJ /R:1 /W:1 /NFL /NDL /NJH /NJS /NP /XD __pycache__ .pytest_cache .mypy_cache /XF *.pyc *.pyo
    if ($LASTEXITCODE -gt 7) { throw "Could not copy $Source (robocopy exit $LASTEXITCODE)." }
}

New-Item -ItemType Directory -Path $Package -Force | Out-Null
foreach ($name in @("Start AI Database Assistant.vbs", "Stop AI Database Assistant.vbs",
                    "Install Desktop Shortcuts.ps1", "README.md", "DEPLOYMENT_GUIDE.md")) {
    $source = Join-Path $Root $name
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Missing source file: $source" }
    Copy-Item -LiteralPath $source -Destination (Join-Path $Package $name)
}
Copy-Tree (Join-Path $Root "models") (Join-Path $Package "models")
Copy-Tree (Join-Path $Root "llama-b10186-bin-win-cpu-x64") (Join-Path $Package "llama-b10186-bin-win-cpu-x64")
Copy-Tree (Join-Path $Root "runtime\python") (Join-Path $Package "runtime\python")

$packagedProject = Join-Path $Package "project"
New-Item -ItemType Directory -Path $packagedProject -Force | Out-Null
foreach ($name in @(".env.example", "app.py", "main.py", "production.py", "preflight.py",
                    "offline_bootstrap.py", "offline_launcher.ps1", "offline_shutdown.ps1",
                    "startup_screen.py", "requirements.txt", "OFFLINE_DEPLOYMENT.md",
                    "PRODUCTION_CHECKLIST.md", "SCALING.md", "import_knowledge.py", "evaluate_queries.py")) {
    $source = Join-Path $Project $name
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Missing source file: $source" }
    Copy-Item -LiteralPath $source -Destination (Join-Path $packagedProject $name)
}
foreach ($name in @("agent", "config", "core", "routes", "services", "static", "templates")) {
    Copy-Tree (Join-Path $Project $name) (Join-Path $packagedProject $name)
}
Copy-Tree (Join-Path $Project ".venv\Lib\site-packages") (Join-Path $packagedProject ".venv\Lib\site-packages")
foreach ($name in @("knowledge", "logs", ".runtime")) {
    New-Item -ItemType Directory -Path (Join-Path $packagedProject $name) -Force | Out-Null
}

foreach ($private in @(".env", "knowledge", "logs", ".runtime")) {
    $target = Join-Path $packagedProject $private
    if ($private -eq ".env" -and (Test-Path -LiteralPath $target)) { throw "A private .env was copied into the package." }
    if ($private -ne ".env" -and (Get-ChildItem -LiteralPath $target -Recurse -File -Force | Select-Object -First 1)) {
        throw "Private runtime files were copied into $private."
    }
}

$model = Get-ChildItem -LiteralPath (Join-Path $Package "models") -Filter "*.gguf" -File | Sort-Object Length -Descending | Select-Object -First 1
if (-not $model) { throw "No GGUF model was included." }
$critical = @(
    $model.FullName,
    (Join-Path $Package "llama-b10186-bin-win-cpu-x64\llama.exe"),
    (Join-Path $Package "runtime\python\python.exe"),
    (Join-Path $packagedProject "production.py"),
    (Join-Path $packagedProject "offline_launcher.ps1")
)
$manifest = foreach ($file in $critical) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Package is incomplete: $file" }
    $relative = $file.Substring($Package.Length + 1)
    $hash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash
    "$hash  $relative"
}
$manifest | Set-Content -LiteralPath (Join-Path $Package "SHA256SUMS.txt") -Encoding ASCII

if ($CreateArchive) {
    & tar.exe -cf $Archive -C $OutputRoot $PackageName
    if ($LASTEXITCODE -ne 0) { throw "tar could not create the package archive." }
    $archiveHash = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash
    "$archiveHash  $PackageName.tar" | Set-Content -LiteralPath "$Archive.sha256" -Encoding ASCII
}

$files = Get-ChildItem -LiteralPath $Package -Recurse -File -Force
$bytes = ($files | Measure-Object -Property Length -Sum).Sum
Write-Host "Package folder: $Package"
if ($CreateArchive) { Write-Host "Archive: $Archive" }
Write-Host ("Files: {0}; size: {1:N2} GB" -f $files.Count, ($bytes / 1GB))
Write-Host "Private .env, logs, and database knowledge were not included."
