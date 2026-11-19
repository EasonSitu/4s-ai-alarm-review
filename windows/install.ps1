#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'
$root = (Get-Item (Join-Path $PSScriptRoot '..')).FullName
$volume = [IO.Path]::GetPathRoot($root)
if ($volume.StartsWith('\\') -or $volume.TrimEnd('\') -eq $env:SystemDrive -or (Split-Path $root -Leaf) -ne '4s-review') {
    throw 'Extract to a dedicated local data drive, e.g. D:\4s-review.'
}
foreach ($name in '4S-Review-API', '4S-Review-Gateway') {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) { throw "Task already exists: $name" }
}
foreach ($port in 8765, 8766) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) { throw "Port in use: $port" }
}
$env:FOUR_S_VOLUME = $volume
$env:FOUR_S_STATE = Join-Path $root 'var'
$env:PYTHONUTF8 = '1'
Set-Location $root
# SYSTEM, Administrators and the unprivileged Local Service identity.
& icacls.exe $root '/inheritance:r' '/grant:r' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-19:(OI)(CI)RX' | Out-Null
if ($LASTEXITCODE) { throw 'Failed to secure application directory.' }
New-Item $env:FOUR_S_STATE -ItemType Directory -Force | Out-Null
& icacls.exe $env:FOUR_S_STATE '/grant:r' '*S-1-5-19:(OI)(CI)M' | Out-Null
if ($LASTEXITCODE) { throw 'Failed to secure data directory.' }
& py.exe -3.12 -m venv .venv
if ($LASTEXITCODE) { throw 'Install Python 3.12 x64 for all users, including the Python launcher.' }
$python = Join-Path $root '.venv\Scripts\python.exe'
& $python -m pip install --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE) { throw 'Dependency installation failed.' }
& $python -c "from app.config import init_config; assert init_config()['port'] == 8765"
if ($LASTEXITCODE) { throw 'Check data drive and config port (expected 8765).' }
$identity = ([System.Security.Principal.SecurityIdentifier]'S-1-5-19').Translate([System.Security.Principal.NTAccount]).Value
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType ServiceAccount
$triggers = @(
    New-ScheduledTaskTrigger -AtStartup
    New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$created = @()
try {
    foreach ($role in 'API', 'Gateway') {
        $name = "4S-Review-$role"
        $argsLine = '-B -X utf8 "' + (Join-Path $root 'windows\run.py') + '" ' + $role.ToLowerInvariant()
        $action = New-ScheduledTaskAction -Execute $python -Argument $argsLine -WorkingDirectory $root
        Register-ScheduledTask -TaskName $name -Action $action -Principal $principal -Settings $settings -Trigger $triggers | Out-Null
        $created += $name
        Start-ScheduledTask -TaskName $name
    }
    $ready = $false
    for ($i=0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 1
        try {
            $ready = (Invoke-RestMethod 'http://127.0.0.1:8766/healthz' -TimeoutSec 2).status -eq 'ok'
        } catch { $ready = $false }
        if ($ready) { break }
    }
    if (-not $ready) { throw 'Health check failed. See var\api.log and var\gateway.log.' }
} catch {
    foreach ($name in $created) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }
    throw
}
Write-Host 'Ready. Open http://127.0.0.1:8765/admin on the server.'
