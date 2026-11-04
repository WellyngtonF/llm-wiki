# Registers the logon task that runs the one shared LLM-Wiki MCP server (ADR 0008).
#
# One task, LLMWiki-McpHttp: at logon it starts `scripts/mcp_http.py --ensure`
# with the vault's console-less pythonw, so no window opens. `--ensure` exits 0
# when a server already holds the port, so the task and a by-hand start never
# make two. The task has no time limit: the server lives for the logon session
# and the nightly pass restarts it after an update (`mcp_endpoint`).
#
# Opt-in and explicit. Registering it changes nothing in any agent's MCP
# configuration; `scripts/mcp_http.py --client-config` prints the entries.
#
# Usage:
#   scripts\install-mcp-http-task.ps1 -VaultRoot C:\path\to\vault            # install and start
#   scripts\install-mcp-http-task.ps1 -VaultRoot C:\path\to\vault -Status    # check state
#   scripts\install-mcp-http-task.ps1 -VaultRoot C:\path\to\vault -Uninstall # stop and remove

param(
    [Parameter(Mandatory = $true)][string]$VaultRoot,
    [int]$Port = 47615,
    [switch]$Uninstall,
    [switch]$Status,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$taskName = "LLMWiki-McpHttp"
$VaultRoot = [System.IO.Path]::GetFullPath($VaultRoot)

if ($Status) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "${taskName}: NOT INSTALLED" -ForegroundColor Yellow
        exit 1
    }
    $info = Get-ScheduledTaskInfo -TaskName $taskName
    Write-Host "${taskName}: $($task.State)"
    Write-Host "  Action:      $($task.Actions[0].Execute) $($task.Actions[0].Arguments)"
    Write-Host "  Last run:    $($info.LastRunTime) (result $($info.LastTaskResult))"
    exit 0
}

if ($Uninstall) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $task) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "${taskName}: removed" -ForegroundColor Green
    } else {
        Write-Host "${taskName}: not installed"
    }
    exit 0
}

$pythonw = Join-Path $VaultRoot ".venv\Scripts\pythonw.exe"
$script = Join-Path $VaultRoot "scripts\mcp_http.py"
foreach ($required in @($pythonw, $script)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "missing: $required" }
}

$action = New-ScheduledTaskAction `
    -Execute $pythonw `
    -Argument "`"$script`" --ensure --port $Port" `
    -WorkingDirectory $VaultRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# ExecutionTimeLimit zero is "no limit": the server lives for the logon session.
# IgnoreNew keeps a second start from even reaching `--ensure`.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "LLM-wiki: one shared MCP server over loopback HTTP for every agent (ADR 0008)." `
    -Force | Out-Null
Write-Host "${taskName}: registered" -ForegroundColor Green

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $taskName
    Write-Host "${taskName}: started; the server accepts calls once warm (~30 s)" -ForegroundColor Green
}
