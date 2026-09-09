[CmdletBinding()]
param([string]$NodePath='node.exe',[string]$PythonPath='python.exe')
$ErrorActionPreference='Stop'
$project=Split-Path -Parent $PSScriptRoot
$nodeResolved=(Get-Command $NodePath -ErrorAction Stop).Source
$pythonResolved=(Get-Command $PythonPath -ErrorAction Stop).Source
$previousNode=$env:MIRABOX_TEST_NODE
Push-Location $project
try {
  $env:MIRABOX_TEST_NODE=$nodeResolved
  $tests=@(Get-ChildItem -LiteralPath tests -Filter '*.test.cjs' | ForEach-Object FullName)
  & $nodeResolved --test @tests
  if($LASTEXITCODE -ne 0){throw 'Node tests failed'}
  & $pythonResolved -m unittest discover -s tests -p 'test_*.py'
  if($LASTEXITCODE -ne 0){throw 'Python tests failed'}
  & $pythonResolved scripts/check-sideband-contract.py
  if($LASTEXITCODE -ne 0){throw 'Driver contract tests failed'}
} finally { $env:MIRABOX_TEST_NODE=$previousNode; Pop-Location }
