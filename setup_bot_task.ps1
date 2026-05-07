$ErrorActionPreference = "Stop"

$taskName = "Diablo4DiscordBot"
$projectDir = "C:\Users\waylo\OneDrive\Desktop\Diablo 4 Discord Bot\Diablo4DiscordBot"
$launcher = Join-Path $projectDir "run_bot_forever.ps1"

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""

$triggerAtStartup = New-ScheduledTaskTrigger -AtStartup
$triggerAtLogon = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1)

# Re-create task cleanly if it already exists
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

try {
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger @($triggerAtStartup, $triggerAtLogon) `
        -Settings $settings `
        -Description "Keeps Diablo 4 Discord bot running whenever the PC is on" `
        -User $env:USERNAME

    Write-Host "Scheduled task '$taskName' created."
    Write-Host "Start it now with: Start-ScheduledTask -TaskName '$taskName'"
}
catch {
    Write-Error "Failed to create scheduled task. Run PowerShell as Administrator, then run this script again."
    throw
}
