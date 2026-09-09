[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'codexmicro-umdf\driver\umdf2\x64\Debug\CodexMicroUm'
$signTool = 'C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe'
$inf2cat = 'C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x86\inf2cat.exe'
foreach ($file in @($signTool, $inf2cat, (Join-Path $source 'CodexMicroUm.dll'), (Join-Path $source 'CodexMicroUm.inf'))) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing prerequisite: $file" }
}
# Create a dedicated, non-exportable signing key. Only the PUBLIC certificate
# is exported to the package. This script does not establish system trust.
$certificate = New-SelfSignedCertificate -Type CodeSigningCert `
    -Subject 'CN=Mirabox N4 Codex Micro Local Development' `
    -FriendlyName 'Mirabox N4 Codex Micro - local driver testing only' `
    -CertStoreLocation 'Cert:\CurrentUser\My' -KeyAlgorithm RSA -KeyLength 3072 `
    -HashAlgorithm SHA256 -KeyExportPolicy NonExportable `
    -NotAfter (Get-Date).AddDays(90)
$package = Join-Path $PSScriptRoot ('packages\local-test-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$null = New-Item -ItemType Directory -Path $package
foreach ($name in @('CodexMicroUm.dll','CodexMicroUm.inf')) {
    Copy-Item -LiteralPath (Join-Path $source $name) -Destination (Join-Path $package $name)
}
$publicCertificate = Join-Path $package 'MiraboxLocalTest.cer'
$null = Export-Certificate -Cert $certificate -FilePath $publicCertificate
& $signTool sign /fd SHA256 /sha1 $certificate.Thumbprint /s My /d 'Mirabox N4 Codex Micro local test driver' (Join-Path $package 'CodexMicroUm.dll')
if ($LASTEXITCODE -ne 0) { throw "DLL signing failed ($LASTEXITCODE). Public certificate and key remain available for diagnosis." }
& $inf2cat "/driver:$package" /os:10_X64
if ($LASTEXITCODE -ne 0) { throw "Catalog generation failed ($LASTEXITCODE)." }
& $signTool sign /fd SHA256 /sha1 $certificate.Thumbprint /s My /d 'Mirabox N4 Codex Micro local test package' (Join-Path $package 'wudf.cat')
if ($LASTEXITCODE -ne 0) { throw "Catalog signing failed ($LASTEXITCODE)." }
$manifest = [ordered]@{
    package = $package
    subject = $certificate.Subject
    thumbprint = $certificate.Thumbprint
    expires = $certificate.NotAfter.ToString('o')
    privateKeyStore = 'CurrentUser\My (non-exportable)'
    files = @(Get-ChildItem -LiteralPath $package -File | ForEach-Object {
        @{name=$_.Name; sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash}
    })
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $package 'package-manifest.json') -Encoding UTF8
$manifest | ConvertTo-Json -Depth 5
