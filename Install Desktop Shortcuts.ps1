[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Desktop = [Environment]::GetFolderPath("Desktop")
$Shell = New-Object -ComObject WScript.Shell

function New-AssistantShortcut([string]$Name, [string]$ScriptName, [int]$IconIndex) {
    $shortcut = $Shell.CreateShortcut((Join-Path $Desktop "$Name.lnk"))
    $shortcut.TargetPath = (Join-Path $env:WINDIR "System32\wscript.exe")
    $shortcut.Arguments = '"' + (Join-Path $Root $ScriptName) + '"'
    $shortcut.WorkingDirectory = $Root
    $shortcut.IconLocation = (Join-Path $env:WINDIR "System32\shell32.dll") + ",$IconIndex"
    $shortcut.Description = $Name
    $shortcut.Save()
}

New-AssistantShortcut "AI Database Assistant" "Start AI Database Assistant.vbs" 13
New-AssistantShortcut "Stop AI Database Assistant" "Stop AI Database Assistant.vbs" 27
Write-Host "Desktop shortcuts installed."
