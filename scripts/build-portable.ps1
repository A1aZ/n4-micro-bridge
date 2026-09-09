[CmdletBinding()]
param(
  [string]$NodePath='node.exe',
  [string]$PythonPath,
  [string]$CscPath
)
$ErrorActionPreference='Stop'
$project=Split-Path -Parent $PSScriptRoot
if (-not $PythonPath) { $PythonPath=Join-Path $project '.hidapi-venv\Scripts\python.exe' }
if (-not $CscPath) { $CscPath=Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe' }
$node=(Get-Command $NodePath -ErrorAction Stop).Source
$python=(Get-Command $PythonPath -ErrorAction Stop).Source
if (-not (Test-Path -LiteralPath $CscPath)) { throw 'C# compiler missing; supply -CscPath' }
& $python -c 'import hid,PIL,wmi,win32api; import sys; assert sys.version_info[:2]==(3,11)'
if ($LASTEXITCODE -ne 0) { throw 'Expected CPython3.11 and requirements.txt dependencies' }
$version=(Get-Content "$project\package.json" -Raw | ConvertFrom-Json).version
$output=Join-Path $project ('artifacts\portable\Mirabox-'+$version+'-'+(Get-Date -Format 'yyyyMMdd-HHmmss'))
$sdk=Join-Path $project 'upstream\StreamDock-Device-SDK\Python-SDK\src'
if (-not (Test-Path "$sdk\StreamDock\Transport\TransportDLL\transport.dll")) { throw 'Pinned SDK transport.dll is missing; see docs/release.md' }
New-Item -ItemType Directory -Path $output | Out-Null
foreach ($name in @('src','webui')) {
  New-Item -ItemType Directory -Path "$output\$name" | Out-Null
  Get-ChildItem -LiteralPath "$project\$name" -File | Where-Object { $_.Extension -in @('.py','.cjs','.html','.css','.js') } | Copy-Item -Destination "$output\$name"
}
New-Item -ItemType Directory -Path "$output\scripts","$output\data","$output\runtime","$output\upstream\StreamDock-Device-SDK\Python-SDK","$output\upstream\openmicrokbd" -Force | Out-Null
foreach ($script in @('n4-webui-bridge.py','micro-webui-relay.py','codexmicro-companion-probe.py','codexmicro-probe.py')) { Copy-Item -LiteralPath "$project\scripts\$script" -Destination "$output\scripts" }
Copy-Item -LiteralPath "$project\assets" -Destination $output -Recurse
Copy-Item -LiteralPath "$project\examples\n4-calibrated.json" -Destination "$output\data\config.json"
Copy-Item -LiteralPath $sdk -Destination "$output\upstream\StreamDock-Device-SDK\Python-SDK" -Recurse
Copy-Item -LiteralPath "$project\upstream\StreamDock-Device-SDK\LICENSE" -Destination "$output\upstream\StreamDock-Device-SDK"
Copy-Item -LiteralPath "$project\upstream\openmicrokbd\LICENSE" -Destination "$output\upstream\openmicrokbd"
$base=& $python -c 'import sys; print(sys.base_prefix)'
$site=& $python -c 'import sysconfig; print(sysconfig.get_path("purelib"))'
Copy-Item -LiteralPath $base -Destination "$output\runtime\python" -Recurse
Get-ChildItem -LiteralPath $site | Copy-Item -Destination "$output\runtime\python\Lib\site-packages" -Recurse -Force
Copy-Item -LiteralPath $node -Destination "$output\runtime\node.exe"
& $CscPath /nologo /target:winexe /platform:x64 /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /win32icon:"$project\assets\app\app.ico" /out:"$output\Mirabox.exe" "$project\scripts\MiraboxLauncher.cs"
if ($LASTEXITCODE -ne 0) { throw 'Launcher compilation failed' }
foreach ($name in @('README.md','LICENSE','COPYRIGHT','THIRD_PARTY_NOTICES.md','SECURITY.md','CHANGELOG.md','package.json')) { Copy-Item -LiteralPath "$project\$name" -Destination $output }
Copy-Item -LiteralPath "$project\docs\portable-app.md" -Destination "$output\使用说明.md"
Write-Warning 'Local release candidate only: audit runtime/SDK redistribution rights and clean-machine behavior before publishing.'
Write-Output $output
