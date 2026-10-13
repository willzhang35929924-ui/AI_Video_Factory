param([string]$Config = "")
$ErrorActionPreference = "Stop"
$FactoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$FactoryPython = Join-Path $FactoryRoot "backend/.venv/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $FactoryPython -PathType Leaf)) {
    Write-Error "缺少 $FactoryPython；没有关闭任何进程。"
    exit 1
}
$LauncherArgs = @((Join-Path $FactoryRoot "scripts/factory_launcher.py"), "stop")
if ($Config) { $LauncherArgs += @("--config", $Config) }
& $FactoryPython @LauncherArgs
exit $LASTEXITCODE
