[CmdletBinding()]
param(
  [ValidateRange(1, 65535)]
  [int]$Port = 8787,
  [string]$PythonPath
)

$ErrorActionPreference = 'Stop'

if (-not $PSBoundParameters.ContainsKey('Port') -and
    -not [string]::IsNullOrWhiteSpace($env:MIRABOX_WEBUI_PORT)) {
  $environmentPort = 0
  if (-not [int]::TryParse($env:MIRABOX_WEBUI_PORT, [ref]$environmentPort) -or
      $environmentPort -lt 1 -or $environmentPort -gt 65535) {
    throw "MIRABOX_WEBUI_PORT must be an integer between 1 and 65535"
  }
  $Port = $environmentPort
}

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

# Fail before Node starts when the requested port already belongs to another
# WebUI process.  This is especially important during development because an
# old Node process serves the newest HTML files from disk while retaining its
# old in-memory API routes, which otherwise looks like a partially upgraded
# server in the browser.
$listeners = @(
  Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalAddress -in @('127.0.0.1', '0.0.0.0', '::', '::1') }
)
if ($listeners.Count -gt 0) {
  $owners = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
  $ownerLabels = foreach ($ownerPid in $owners) {
    $owner = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
    if ($owner) { "PID $ownerPid ($($owner.ProcessName))" } else { "PID $ownerPid" }
  }
  $ownerText = if ($ownerLabels.Count) { $ownerLabels -join ', ' } else { 'an unknown process' }
  throw "Port $Port is already in use by $ownerText. Existing URL: http://127.0.0.1:$Port. Stop/restart that process to load the current backend, or choose a free port with -Port <port>."
}

# The WebUI's read-only Companion HID probe launches Python on demand.  Prefer
# the project-local environment, when present, so the probe sees the optional
# `hid` module installed for this checkout instead of whichever global Python
# happens to be first on PATH.  An explicit -PythonPath or MIRABOX_PYTHON wins.
$projectPython = Join-Path $projectRoot '.hidapi-venv\Scripts\python.exe'
if ($PythonPath) {
  $resolvedPython = if ([IO.Path]::IsPathRooted($PythonPath)) {
    $PythonPath
  } else {
    Join-Path $projectRoot $PythonPath
  }
  if (-not (Test-Path -LiteralPath $resolvedPython -PathType Leaf)) {
    throw "Python executable does not exist: $resolvedPython"
  }
  $env:MIRABOX_PYTHON = (Resolve-Path -LiteralPath $resolvedPython).Path
} elseif ([string]::IsNullOrWhiteSpace($env:MIRABOX_PYTHON) -and
          (Test-Path -LiteralPath $projectPython -PathType Leaf)) {
  $env:MIRABOX_PYTHON = (Resolve-Path -LiteralPath $projectPython).Path
}

$env:MIRABOX_WEBUI_PORT = [string]$Port
$pythonNotice = if ($env:MIRABOX_PYTHON) {
  "Python probe: $env:MIRABOX_PYTHON"
} else {
  'Python probe: python from PATH (project .hidapi-venv was not found)'
}
Write-Host "Starting Mirabox Codex Micro WebUI at http://127.0.0.1:$Port"
Write-Host $pythonNotice
& node (Join-Path $projectRoot 'webui\server.cjs')
exit $LASTEXITCODE
