# Build the configured Ibasho viewer (after setup.sh and configure.sh).
#
#   powershell -ExecutionPolicy Bypass -File build.ps1 [-Jobs 4] [-Projects 1] [-Priority BelowNormal] [-MinFreeGB 3] [-Src TREE]
#
# Output: <tree>\build-vc170-64\newview\Release\IbashoViewer.exe (the folder is the runnable viewer).
# Jobs = compiler processes per project, Projects = projects built at once; each cl.exe can take 1-2 GB.
# Priority: BelowNormal or Idle keeps the machine usable. The build stops if free RAM stays under
# MinFreeGB for 30 s. Logs: <work>\build-logs (build.log, build.err, mem.log). Don't tail -F the logs
# while it runs: a lock on mem.log stops the guard writing.
param([int]$Jobs = 4, [int]$Projects = 1, [string]$Priority = 'BelowNormal', [double]$MinFreeGB = 3, [string]$Src = '')

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$work = if ($env:IBASHO_VIEWER_WORK) { $env:IBASHO_VIEWER_WORK } else { Join-Path $here 'work' }
if (-not $Src) { $Src = Join-Path $work 'viewer-src' }
$bld = Join-Path $Src 'build-vc170-64'
if (-not (Test-Path $bld)) { throw "no build tree at ${bld}: run configure.sh first" }

$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vs = (& $vswhere -products * -latest -property installationPath).Trim()
$cmake = Join-Path $vs 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'

# A CMake re-run during the build reads these, as configure.sh set them.
$base = (Get-Content (Join-Path $here 'BASE') -Raw).Trim()
$revision = (git -C $Src rev-list --count "$base..HEAD").Trim()
$env:revision = $revision
$env:AUTOBUILD_VSVER = '170'; $env:AUTOBUILD_ADDRSIZE = '64'
$env:AUTOBUILD_INSTALLABLE_CACHE = Join-Path $work 'autobuild-cache'
$env:AUTOBUILD_VARIABLES_FILE = Join-Path $work 'build-variables\variables'
$env:PATH = (Join-Path $work 'ab-venv\Scripts') + ';' + $env:PATH

$configured = Join-Path $bld 'ibasho-revision.txt'
if ((Test-Path $configured) -and ((Get-Content $configured -Raw).Trim() -ne $revision)) {
  Write-Warning "configured for build $((Get-Content $configured -Raw).Trim()), the tree is now at ${revision}: run configure.sh for the right version number"
}

$logs = Join-Path $work 'build-logs'
$null = New-Item -ItemType Directory -Force $logs
$memlog = Join-Path $logs 'mem.log'
$buildArgs = @('--build', $bld, '--config', 'Release', '--', "/m:$Projects", "/p:CL_MPCount=$Jobs", '/nr:false', '/v:minimal', '/nologo')
$p = Start-Process -FilePath $cmake -ArgumentList $buildArgs -PassThru -NoNewWindow `
  -RedirectStandardOutput (Join-Path $logs 'build.log') -RedirectStandardError (Join-Path $logs 'build.err')
$null = $p.Handle
$p.PriorityClass = $Priority
$p.Id | Out-File -Encoding ascii (Join-Path $logs 'build.pid')
$t0 = Get-Date
$low = 0
while (-not $p.HasExited) {
  Start-Sleep 15
  $free = [math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1MB, 2)
  $cl = @(Get-Process cl -ErrorAction SilentlyContinue).Count
  "{0:HH:mm:ss} free={1}GB cl={2}" -f (Get-Date), $free, $cl | Add-Content $memlog
  if ($free -lt $MinFreeGB) { $low++ } else { $low = 0 }
  if ($low -ge 2) {
    "{0:HH:mm:ss} LOW MEMORY - stopping build tree pid {1}" -f (Get-Date), $p.Id | Add-Content $memlog
    taskkill /T /F /PID $p.Id | Out-Null
    break
  }
}
$p.WaitForExit()
"exit=$($p.ExitCode) elapsed=$((Get-Date) - $t0)" | Add-Content $memlog
Write-Output "exit=$($p.ExitCode) elapsed=$((Get-Date) - $t0)"
exit $p.ExitCode
