[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$PackagePath,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9A-Fa-f]{40}$')][string]$ExpectedThumbprint
)
$ErrorActionPreference = 'Stop'
$package = (Resolve-Path -LiteralPath $PackagePath).Path
$allowedRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'packages')) + [IO.Path]::DirectorySeparatorChar
if (-not $package.StartsWith($allowedRoot,[StringComparison]::OrdinalIgnoreCase)) { throw 'Package must be inside this project driver/packages directory.' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator privileges are required.' }
$log = Join-Path $package 'install.log'
$null = Start-Transcript -Path $log -Append
$resultCode = 1
try {
    $certFile = Join-Path $package 'MiraboxLocalTest.cer'
    $certificate = New-Object Security.Cryptography.X509Certificates.X509Certificate2($certFile)
    if ($certificate.Thumbprint -ne $ExpectedThumbprint) { throw 'Certificate fingerprint mismatch.' }
    if ($certificate.NotAfter -lt (Get-Date)) { throw 'Signing certificate has expired.' }
    foreach ($file in @('CodexMicroUm.dll','wudf.cat')) {
        $signature = Get-AuthenticodeSignature -LiteralPath (Join-Path $package $file)
        if ($signature.SignerCertificate.Thumbprint -ne $ExpectedThumbprint) { throw "Unexpected signer: $file" }
    }
    # Authorized project-specific code-signing trust only. No BCD, HVCI,
    # Secure Boot, integrity-check policy or automatic reboot changes.
    foreach ($store in @('Cert:\LocalMachine\Root','Cert:\LocalMachine\TrustedPublisher')) {
        if (-not (Test-Path -LiteralPath "$store\$ExpectedThumbprint")) {
            $null = Import-Certificate -FilePath $certFile -CertStoreLocation $store
        }
        Write-Output "TRUSTED: $store\$ExpectedThumbprint"
    }
    $signtool = 'C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe'
    $catalog = Join-Path $package 'wudf.cat'
    foreach ($name in @('CodexMicroUm.dll','CodexMicroUm.inf')) {
        & $signtool verify /pa /v /c $catalog (Join-Path $package $name)
        if ($LASTEXITCODE -ne 0) { throw "Package integrity verification failed: $name" }
    }
    $inf = Join-Path $package 'CodexMicroUm.inf'
    & "$env:SystemRoot\System32\pnputil.exe" /add-driver $inf
    if ($LASTEXITCODE -notin @(0,3010)) { throw "Driver store staging failed: $LASTEXITCODE. No device node was created." }
    if ($LASTEXITCODE -eq 3010) { $resultCode=3010; throw 'Driver staging requests a reboot. No reboot was performed.' }
    $hardwareId = 'root\MiraboxCodexMicro'
    $existing = @(Get-CimInstance Win32_PnPEntity -Filter "PNPDeviceID LIKE 'ROOT%'" | Where-Object { $_.HardwareID -contains $hardwareId })
    if ($existing.Count -gt 1) { throw 'Multiple matching virtual devices exist. Refusing to create or update ambiguous targets.' }
    $devcon = 'C:\Program Files (x86)\Windows Kits\10\Tools\10.0.26100.0\x64\devcon.exe'
    # install creates a node every time; update the exact hardware ID on retries.
    if ($existing.Count -eq 0) { & $devcon install $inf $hardwareId }
    else { & $devcon update $inf $hardwareId }
    $devconCode = $LASTEXITCODE
    Write-Output "DEVCON_EXIT=$devconCode"
    if ($devconCode -notin @(0,1)) { throw "Device installation failed: $devconCode" }
    $resultCode = if ($devconCode -eq 1) { 3010 } else { 0 }
    Get-CimInstance Win32_PnPEntity -Filter "PNPDeviceID LIKE 'ROOT%'" | Where-Object { $_.HardwareID -contains $hardwareId } |
        Select-Object Name,PNPDeviceID,ConfigManagerErrorCode,Status | Format-List
    Write-Output "INSTALL_RESULT=$resultCode; no reboot requested by this script"
} catch {
    Write-Output "INSTALL_ERROR: $($_.Exception.Message)"
} finally { $null = Stop-Transcript }
exit $resultCode
