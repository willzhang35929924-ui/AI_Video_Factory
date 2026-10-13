param([switch]$Remove)
$ErrorActionPreference = "Stop"
$FactoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$StartScript = Join-Path $PSScriptRoot "start_factory_windows.ps1"
$Startup = [Environment]::GetFolderPath('Startup')
if (-not $Startup) { throw "Current user Startup folder unavailable; nothing changed." }
$ShortcutPath = Join-Path $Startup 'AI Video Factory (Web only).lnk'
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$ExpectedArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`" -NoBrowser"
$PowerShellExe = (Get-Command powershell.exe -ErrorAction Stop).Source
if (Test-Path -LiteralPath $ShortcutPath) {
    if ($Shortcut.TargetPath -ne $PowerShellExe -or $Shortcut.Arguments -ne $ExpectedArgs) {
        throw "An unrelated Startup shortcut already uses this name. It was not overwritten or removed."
    }
    if ($Remove) {
        Remove-Item -LiteralPath $ShortcutPath
        Write-Output "Only this project's web-only Startup shortcut removed. Existing services remain running."
    } else {
        Write-Output "Existing identical web-only Startup shortcut reused."
    }
    exit 0
}
if ($Remove) { Write-Output "No owned Startup shortcut to remove."; exit 0 }
if (-not (Test-Path -LiteralPath (Join-Path $FactoryRoot 'config/launcher.local.json'))) {
    throw "Prepare and validate launcher.local.json before enabling login startup."
}
$Shortcut.TargetPath = $PowerShellExe
$Shortcut.Arguments = $ExpectedArgs
$Shortcut.WorkingDirectory = $FactoryRoot
$Shortcut.Description = 'AI Video Factory web only; production and external queue recovery disabled'
$Shortcut.WindowStyle = 7
$Shortcut.Save()
Write-Output "Web-only startup installed for this Windows user after login, not before login."
Write-Output "No current services started; no credentials or queue recovery enabled."
