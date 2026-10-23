# Register Watcher AI to start at logon (no console, below-normal priority) and add Start Menu + Desktop shortcuts.
# Usage:  .\install-autostart.ps1            .\install-autostart.ps1 -Remove
param([switch]$Remove)
$name = "Strux"
# migrate from the old names (Watcher AI, then Halo)
foreach ($old in @(@("WatcherAI", "Watcher AI"), @("Halo", "Halo"))) {
  Unregister-ScheduledTask -TaskName $old[0] -Confirm:$false -ErrorAction SilentlyContinue
  Remove-Item (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$($old[1]).lnk"), (Join-Path ([Environment]::GetFolderPath("Desktop")) "$($old[1]).lnk") -ErrorAction SilentlyContinue
}
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Strux.lnk"
$desktop = Join-Path ([Environment]::GetFolderPath("Desktop")) "Strux.lnk"
if ($Remove) {
  Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
  Remove-Item $startMenu, $desktop -ErrorAction SilentlyContinue
  Write-Host "removed task + shortcuts"; exit
}
$pyw = Join-Path $root ".venv\Scripts\pythonw.exe"
$icon = Join-Path $root "assets\strux.ico"

# 1) logon task (hidden, starts minimised to tray via --hidden)
$action = New-ScheduledTaskAction -Execute $pyw -Argument "-m watcher run --hidden" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT20S"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -Priority 7 -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

# 2) shortcuts (double-click = open the dashboard; if already running it just shows the window)
$ws = New-Object -ComObject WScript.Shell
foreach ($path in @($startMenu, $desktop)) {
  $s = $ws.CreateShortcut($path)
  $s.TargetPath = $pyw
  $s.Arguments = "-m watcher run"
  $s.WorkingDirectory = $root
  $s.IconLocation = $icon
  $s.Description = "Strux - your local desktop assistant"
  $s.Save()
}
Write-Host "registered task '$name' (runs at logon) and shortcuts:"
Write-Host "  $startMenu"
Write-Host "  $desktop"
Write-Host "Start now:  Start-ScheduledTask -TaskName $name"
