param([ValidateSet('Check', 'Start', 'Stop')][string]$Action = 'Check', [string]$Config = '')
$ErrorActionPreference = 'Stop'
$FactoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
if (-not $Config) { $Config = Join-Path $FactoryRoot 'config/launcher.local.json' }
if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) { throw 'Missing reviewed local launcher configuration.' }
$Settings = Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
$Port = 3000
if ($Settings.PSObject.Properties.Name -contains 'frontend_port') { $Port = $Settings.frontend_port }
if ($Port -isnot [int] -and $Port -isnot [long]) { throw 'frontend_port must be an integer.' }
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Invalid frontend_port.' }
$Target = "http://127.0.0.1:$Port"
$TailscaleCmd = Get-Command tailscale.exe -ErrorAction SilentlyContinue
$Tailscale = if ($TailscaleCmd) { $TailscaleCmd.Source } else { Join-Path $env:ProgramFiles 'Tailscale/tailscale.exe' }
if (-not (Test-Path -LiteralPath $Tailscale -PathType Leaf)) { throw 'Tailscale executable missing. Install/login with the official client first.' }
function Invoke-Tailscale([string[]]$Arguments) {
    $Output = & $Tailscale @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Tailscale command failed (exit $LASTEXITCODE); no retry or reset performed." }
    return ($Output -join "`n")
}
function Properties($Value) {
    if ($null -eq $Value) { return @() }
    return @($Value.PSObject.Properties)
}
function Serve-Fingerprint($Value) {
    $Bytes = [Text.Encoding]::UTF8.GetBytes(($Value | ConvertTo-Json -Depth 40 -Compress))
    $Sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($Sha.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $Sha.Dispose() }
}
function Assert-Only-Factory-Serve($Value) {
    foreach ($Funnel in (Properties $Value.AllowFunnel)) {
        if ($Funnel.Value -eq $true) { throw 'Funnel is enabled. Refusing to alter public exposure; review it manually.' }
    }
    foreach ($Field in (Properties $Value)) {
        if ($Field.Name -notin @('TCP', 'Web', 'AllowFunnel')) { throw 'Unknown/additional Serve configuration preserved. Review it manually.' }
    }
    $Tcp = @(Properties $Value.TCP)
    $Web = @(Properties $Value.Web)
    if ($Tcp.Count -eq 0 -and $Web.Count -eq 0) { return $false }
    if ($Tcp.Count -ne 1 -or $Tcp[0].Name -ne '443' -or $Tcp[0].Value.HTTPS -ne $true -or $Web.Count -ne 1) {
        throw 'Existing Serve ports/services do not exclusively match Factory; nothing overwritten.'
    }
    $Handlers = @(Properties $Web[0].Value.Handlers)
    if ($Handlers.Count -ne 1 -or $Handlers[0].Name -ne '/' -or $Handlers[0].Value.Proxy -ne $Target) {
        throw 'Existing Serve handler does not exclusively proxy this Factory frontend; preserved.'
    }
    return $true
}
Write-Output (Invoke-Tailscale -Arguments @('version'))
$HelpText = Invoke-Tailscale -Arguments @('serve', '--help')
if ($HelpText -notmatch '--bg' -or $HelpText -notmatch '--https') { throw 'Installed CLI lacks expected Serve flags; use its actual --help instead.' }
$NodeState = (Invoke-Tailscale -Arguments @('status', '--json')) | ConvertFrom-Json
if ($NodeState.BackendState -ne 'Running') { throw 'WAITING_MANUAL: sign in to the intended tailnet. Script does not log in or change VPN.' }
$Serve = (Invoke-Tailscale -Arguments @('serve', 'status', '--json')) | ConvertFrom-Json
if ($null -eq $Serve) { $Serve = [pscustomobject]@{} }
$Existing = Assert-Only-Factory-Serve $Serve
$OwnedFile = Join-Path $FactoryRoot "logs/launcher/tailscale-owned-$Port.json"
if ($Action -eq 'Stop') {
    if (-not (Test-Path -LiteralPath $OwnedFile -PathType Leaf)) { throw 'No owned Serve record. User-managed sharing preserved.' }
    $Owned = Get-Content -LiteralPath $OwnedFile -Raw | ConvertFrom-Json
    if ($Owned.root -ne $FactoryRoot -or $Owned.target -ne $Target -or $Owned.fingerprint -ne (Serve-Fingerprint $Serve) -or -not $Existing) {
        throw 'Serve ownership/configuration changed. Nothing stopped or reset.'
    }
    Write-Output (Invoke-Tailscale -Arguments @('serve', '--https=443', 'off'))
    Remove-Item -LiteralPath $OwnedFile
    Write-Output 'Only the owned Factory HTTPS handler stopped. Factory and Tailscale services remain running.'
    exit 0
}
$Health = Invoke-RestMethod -Uri "$Target/factory-api/api/health" -TimeoutSec 10
$Launcher = Invoke-RestMethod -Uri "$Target/api/launcher" -TimeoutSec 10
if ($Health.status -ne 'healthy' -or $Launcher.application -ne 'AI Video Factory' -or $Launcher.api_transport -ne 'same-origin' -or $Launcher.api_base_path -ne '/factory-api') {
    throw 'Local same-origin frontend/API health not verified. Serve not started.'
}
if ($Action -eq 'Start' -and -not $Existing) {
    Write-Output (Invoke-Tailscale -Arguments @('serve', '--bg', '--https=443', $Target))
    $Serve = (Invoke-Tailscale -Arguments @('serve', 'status', '--json')) | ConvertFrom-Json
    if (-not (Assert-Only-Factory-Serve $Serve)) { throw 'Serve receipt does not match the requested frontend. Inspect actual status; no blind retry.' }
    $Owned = @{root=$FactoryRoot; target=$Target; fingerprint=(Serve-Fingerprint $Serve); created_at=(Get-Date).ToUniversalTime().ToString('o')}
    $LogDir = Split-Path $OwnedFile -Parent
    if (-not (Test-Path -LiteralPath $LogDir)) { [void](New-Item -ItemType Directory -Path $LogDir) }
    [IO.File]::WriteAllText($OwnedFile, ($Owned | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
} elseif ($Action -eq 'Start') {
    Write-Output 'Matching existing Serve reused; no overwrite, no extra service, no queue action.'
}
Write-Output (Invoke-Tailscale -Arguments @('serve', 'status'))
Write-Output 'Verify the actual HTTPS URL from an authorized second tailnet computer. No Windows/remote acceptance is implied.'
