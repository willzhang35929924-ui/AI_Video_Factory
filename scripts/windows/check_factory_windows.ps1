param([string]$Config = "")
$ErrorActionPreference = "Stop"
$FactoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$FactoryPython = Join-Path $FactoryRoot "backend/.venv/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $FactoryPython -PathType Leaf)) {
    throw "Project interpreter missing: $FactoryPython. No services started."
}
$LauncherArgs = @((Join-Path $FactoryRoot "scripts/factory_launcher.py"), "check")
if ($Config) { $LauncherArgs += @("--config", $Config) }
& $FactoryPython @LauncherArgs
exit $LASTEXITCODE
