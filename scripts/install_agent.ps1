# Install the Data Intake Agent on a processing workstation.
#
# Run as the PROCESSING user (the account that stays logged in and runs the
# GUI apps), from an elevated PowerShell if the install dir needs it:
#
#   .\install_agent.ps1 -NodeName TERRA-01 -Token "<token from POST /api/v1/nodes>"
#
# What it does:
#   1. Copies DataIntakeAgent.exe (+ agent.yaml if present) from -SourceDir
#      into -InstallDir.
#   2. Creates the work root (logs / jobs / state).
#   3. Stores the node token as a user environment variable
#      (DATA_INTAKE_NODE_TOKEN) for the processing account.
#   4. Registers a Task Scheduler task: at logon of this user, run only when
#      logged on (Session 0 cannot drive GUI apps), 30s delay, restart on
#      failure — then starts it.

param(
    [Parameter(Mandatory = $true)]  [string]$NodeName,
    [Parameter(Mandatory = $false)] [string]$Token = "",
    [string]$SourceDir  = $PSScriptRoot,
    [string]$InstallDir = "C:\Program Files\DataIntakeAgent",
    [string]$WorkRoot   = "C:\ProgramData\DataIntakeAgent",
    [string]$TaskName   = "Data Intake Agent"
)

$ErrorActionPreference = "Stop"
$exeName = "DataIntakeAgent.exe"

Write-Host "[1/4] Copying agent to $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -Force (Join-Path $SourceDir $exeName) $InstallDir
$configSource = Join-Path $SourceDir "agent.yaml"
$configDest   = Join-Path $InstallDir "agent.yaml"
if (Test-Path $configSource) {
    Copy-Item -Force $configSource $configDest
} elseif (-not (Test-Path $configDest)) {
    Write-Warning "No agent.yaml found — copy config/agent.example.yaml to $configDest and edit it before starting."
}

Write-Host "[2/4] Creating work root $WorkRoot"
foreach ($sub in @("logs", "jobs", "state")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $WorkRoot $sub) | Out-Null
}

if ($Token -ne "") {
    Write-Host "[3/4] Storing node token in user environment (DATA_INTAKE_NODE_TOKEN)"
    [Environment]::SetEnvironmentVariable("DATA_INTAKE_NODE_TOKEN", $Token, "User")
} else {
    Write-Host "[3/4] No -Token given — assuming DATA_INTAKE_NODE_TOKEN is already set"
}

Write-Host "[4/4] Registering scheduled task '$TaskName'"
$action  = New-ScheduledTaskAction -Execute (Join-Path $InstallDir $exeName) `
    -Argument "--config `"$configDest`"" -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT30S"
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 100 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
# "Run only when user is logged on": achieved with an Interactive logon type.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "Done. Agent '$NodeName' installed and started."
Write-Host "Check the coordinator dashboard — the node should appear within ~15s."
Write-Host "Logs: $WorkRoot\logs\agent.log"
