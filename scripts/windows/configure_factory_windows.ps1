param()
$ErrorActionPreference = "Stop"
$FactoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$LauncherPath = Join-Path $FactoryRoot "config/launcher.local.json"
$PublishingPath = Join-Path $FactoryRoot "config/publishing.windows.local.yaml"
if ((Test-Path -LiteralPath $LauncherPath) -or (Test-Path -LiteralPath $PublishingPath)) {
    throw "Existing local configuration preserved. Review it manually; this script never overwrites it."
}
$Launcher = Get-Content -LiteralPath (Join-Path $FactoryRoot "config/windows.launcher.example.json") -Raw | ConvertFrom-Json
$WindowsMedia = ($FactoryRoot.Replace('\', '/') + '/storage/windows-acceptance')
$Publishing = Get-Content -LiteralPath (Join-Path $FactoryRoot "config/publishing.windows.example.yaml") -Raw
$Publishing = $Publishing.Replace('D:/AI_Video_Factory/storage', $WindowsMedia)
[System.IO.File]::WriteAllText($PublishingPath, $Publishing, [System.Text.UTF8Encoding]::new($false))
[System.IO.File]::WriteAllText($LauncherPath, ($Launcher | ConvertTo-Json -Depth 6), [System.Text.UTF8Encoding]::new($false))
Write-Output "Acceptance-only local configuration created. No database, media, credentials or queue were changed."
Write-Output "DB: backend/factory.windows-acceptance.db; media: storage/windows-acceptance"
Write-Output "Verify the migrated backup and manifest before running start_factory_windows.bat."
