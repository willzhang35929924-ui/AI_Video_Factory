param([string]$Config = "", [switch]$NoBrowser)
$ErrorActionPreference = "Stop"
$FactoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$FactoryPython = Join-Path $FactoryRoot "backend/.venv/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $FactoryPython -PathType Leaf)) {
    Write-Error "缺少 $FactoryPython；请按 docs/operations/one_click_start_windows.md 创建 Windows 虚拟环境。"
    exit 1
}
$LauncherArgs = @((Join-Path $FactoryRoot "scripts/factory_launcher.py"), "start")
if ($Config) { $LauncherArgs += @("--config", $Config) }
if ($NoBrowser) { $LauncherArgs += "--no-browser" }
& $FactoryPython @LauncherArgs
exit $LASTEXITCODE
