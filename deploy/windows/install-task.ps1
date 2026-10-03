# Run Switchboard at logon as a hidden Scheduled Task for the current user
# (no admin rights needed).
# Usage:  powershell -ExecutionPolicy Bypass -File deploy\windows\install-task.ps1
# Remove: powershell -ExecutionPolicy Bypass -File deploy\windows\install-task.ps1 -Remove
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$TaskName = "Switchboard LLM router"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "removed"; exit 0
}

$Pythonw = Join-Path $Repo ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Pythonw)) { throw "Create the venv first (see README)." }
if (-not (Test-Path (Join-Path $Repo "config.yaml"))) { throw "Create config.yaml first (see README)." }

# pythonw.exe = no console window; output goes to logs\server.log.
$Action = New-ScheduledTaskAction -Execute $Pythonw `
    -Argument "-m switchboard --config config.yaml serve --host 127.0.0.1 --port 8787" `
    -WorkingDirectory $Repo
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Principal $Principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "running. logs: $Repo\logs\server.log"
