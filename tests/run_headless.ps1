# Run the headless tests in Blender and print the report.
# Usage: powershell -File tests/run_headless.ps1 [-Blender <path to blender.exe>]
param([string]$Blender = "")
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "headless_tests.py"
$log = Join-Path $here "headless_tests.log"
if ($Blender -eq "") {
  $cmd = Get-Command blender -ErrorAction SilentlyContinue
  if ($cmd) { $Blender = $cmd.Source }
  else { $Blender = "$env:LOCALAPPDATA\Microsoft\WindowsApps\blender-launcher.exe" }  # Blender from the Microsoft Store
}
if (Test-Path $log) { Clear-Content $log }
$p = Start-Process -FilePath $Blender -ArgumentList @("--background", "--factory-startup", "--python", ('"' + $script + '"')) -Wait -PassThru
$i = 0
while (-not (Test-Path $log) -and $i -lt 240) { Start-Sleep -Milliseconds 250; $i++ }
if (Test-Path $log) { Get-Content $log -Encoding utf8 } else { Write-Output "No log produced ($log)" }
