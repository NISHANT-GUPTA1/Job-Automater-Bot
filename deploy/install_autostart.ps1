# Runs Job Applier in the background on this laptop, always:
#  - starts hidden when you log in to Windows (no VS Code / terminal needed)
#  - checked every 5 minutes and restarted if it ever stopped
#  - keeps running with the lid closed while plugged in (on battery the laptop sleeps as usual)
# Install:    powershell -ExecutionPolicy Bypass -File deploy\install_autostart.ps1
# Uninstall:  powershell -ExecutionPolicy Bypass -File deploy\install_autostart.ps1 -Uninstall
param([switch]$Uninstall)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$task = "JobApplier"

if ($Uninstall) {
    Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
    powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 1   # lid closed on charger -> sleep again
    powercfg /setactive SCHEME_CURRENT
    Write-Host "Removed. The bot no longer starts by itself." -ForegroundColor Yellow
    exit
}

# 1) Background task (pythonw = no console window)
$action = New-ScheduledTaskAction -Execute "$root\.venv\Scripts\pythonw.exe" -Argument "main.py" -WorkingDirectory $root
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $task -Action $action -Trigger $atLogon, $watchdog -Settings $settings `
    -Principal $principal -Description "Job Applier Telegram bot (hidden, auto-restart)" -Force | Out-Null

# 2) Power: on the charger never sleep, and closing the lid does nothing (screen still turns off)
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /setactive SCHEME_CURRENT

# 3) Start now
Start-ScheduledTask -TaskName $task
Write-Host "Installed. Job Applier now runs in the background and starts by itself after every login." -ForegroundColor Green
