# Keep Windows from sleeping and keep the screen on, until this process is killed.
# Does not change any power settings. Stop LINK.bat kills it.
Add-Type -Name P -Namespace W -MemberDefinition '[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint f);'
# ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
while ($true) { [W.P]::SetThreadExecutionState(0x80000003) | Out-Null; Start-Sleep 30 }
