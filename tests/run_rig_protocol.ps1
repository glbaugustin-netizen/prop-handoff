# Replay the specification protocol on a real rig: a .blend (Auto-Rig Pro by
# default) or a Rigify human generated on the fly (-Rigify).
# Usage: powershell -File tests/run_rig_protocol.ps1 -Blend <file.blend> [-Blender <blender.exe>]
#        powershell -File tests/run_rig_protocol.ps1 -Rigify [-Blender <blender.exe>]
param([string]$Blend = "", [switch]$Rigify, [string]$Blender = "")
if ($Blend -eq "" -and -not $Rigify) { Write-Output "Pass -Blend <file.blend> or -Rigify"; exit 1 }
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "rig_protocol.py"
$log = Join-Path $here "rig_protocol.log"
if ($Blender -eq "") {
  $cmd = Get-Command blender -ErrorAction SilentlyContinue
  if ($cmd) { $Blender = $cmd.Source }
  else { $Blender = "$env:LOCALAPPDATA\Microsoft\WindowsApps\blender-launcher.exe" }  # Blender from the Microsoft Store
}
if (Test-Path $log) { Clear-Content $log }
$args = @()
if ($Blend -ne "") { $args += ('"' + $Blend + '"') }
$args += @("--background", "--factory-startup", "--python", ('"' + $script + '"'))
if ($Rigify) { $env:PH_RIGIFY = "1" } else { Remove-Item Env:PH_RIGIFY -ErrorAction SilentlyContinue }
$p = Start-Process -FilePath $Blender -ArgumentList $args -Wait -PassThru
$i = 0
while (-not (Test-Path $log) -and $i -lt 240) { Start-Sleep -Milliseconds 250; $i++ }
if (Test-Path $log) { Get-Content $log -Encoding utf8 } else { Write-Output "No log produced ($log)" }
