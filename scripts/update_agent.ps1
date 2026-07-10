# Update the Data Intake Agent on this workstation from a dist share.
#
#   .\update_agent.ps1 -SourceDir "\\UGREEN\Software\DataIntakeAgent"
#
# Stops the scheduled task, copies the new EXE, restarts the task. Config and
# work root are untouched. If a job is running, stopping the agent is SAFE:
# the payload keeps running and the state file re-adopts it on restart.

param(
    [Parameter(Mandatory = $true)] [string]$SourceDir,
    [string]$InstallDir = "C:\Program Files\DataIntakeAgent",
    [string]$TaskName   = "Data Intake Agent"
)

$ErrorActionPreference = "Stop"
$exeName = "DataIntakeAgent.exe"

Write-Host "Stopping '$TaskName'..."
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
Get-Process -Name ($exeName -replace '\.exe$','') -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

Write-Host "Copying new agent from $SourceDir"
Copy-Item -Force (Join-Path $SourceDir $exeName) $InstallDir

Write-Host "Starting '$TaskName'..."
Start-ScheduledTask -TaskName $TaskName
Write-Host "Done — the dashboard's agent_version column confirms the update."
