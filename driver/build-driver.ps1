[CmdletBinding()]
param(
    [ValidateSet('Debug', 'Release')]
    [string]$Configuration = 'Debug',

    [ValidateSet('x64', 'ARM64')]
    [string]$Platform = 'x64',

    [switch]$CheckOnly,

    # Optional overrides support EWDK/custom installations without changing
    # the machine-wide SDK/WDK registration.
    [string]$VsRoot,
    [string]$KitsRoot,
    [string]$WdkRoot
)

$ErrorActionPreference = 'Stop'
$driverRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$project = Join-Path $driverRoot 'codexmicro-umdf\driver\umdf2\CodexMicroUm.vcxproj'
. (Join-Path $driverRoot '..\scripts\driver-build-env-lib.ps1')

$environment = Get-MiraboxDriverBuildEnvironment `
    -Project $project `
    -Platform $Platform `
    -VsRoot $VsRoot `
    -KitsRoot $KitsRoot `
    -WdkRoot $WdkRoot

if (-not $environment.project.exists) {
    throw "Driver project was not found at '$project'."
}
if (-not $environment.visualStudio.msbuild) {
    throw "MSBuild for Visual Studio 2022 was not found. Install the C++ Build Tools workload first."
}
if (-not $environment.visualStudio.compiler) {
    throw "The MSVC compiler for target platform '$Platform' was not found in '$($environment.visualStudio.root)'. Install the matching C++ tools workload."
}
if (-not $environment.windowsSdk.present) {
    throw 'A usable Windows SDK (headers and target-platform libraries) was not found.'
}
if (-not $environment.wdk.components.buildDirectory -or -not $environment.wdk.components.buildFiles) {
    throw "WDK build files were not found under '$($environment.wdk.root)'. Install a WDK matching SDK build $($environment.windowsSdk.buildNumber); this script never installs components automatically."
}
if (-not $environment.wdk.components.matchingKitBuild) {
    throw "Windows SDK build $($environment.windowsSdk.version) does not match WDK build $($environment.wdk.version). Install matching SDK/WDK build numbers or pass -KitsRoot/-WdkRoot overrides."
}
if (-not $environment.wdk.components.wdfHeader -or -not $environment.wdk.components.wdfLibrary) {
    throw "UMDF WDF headers/library are incomplete (header='$($environment.wdk.wdfHeader)', library='$($environment.wdk.wdfLibrary)'). Repair or install the WDK before building."
}
if (-not $environment.wdk.components.hidportHeader) {
    throw "WDK hidport.h was not found for SDK '$($environment.windowsSdk.version)'. Repair or install the matching WDK."
}
if (-not $environment.wdk.components.platformToolset) {
    throw "Visual Studio WDK platform toolset WindowsUserModeDriver10.0 for '$Platform' was not found. In Visual Studio Installer, add the Windows Driver Kit component."
}

Write-Host "MSBuild: $($environment.visualStudio.msbuild)"
Write-Host "MSVC:    $($environment.visualStudio.compiler)"
Write-Host "SDK:     $($environment.windowsSdk.root) ($($environment.windowsSdk.version))"
Write-Host "WDK:     $($environment.wdk.root) ($($environment.wdk.version))"
Write-Host "Project: $project"
if ($CheckOnly) {
    Write-Host 'Toolchain check passed; no build was started.'
    exit 0
}

& $environment.visualStudio.msbuild $project /m /t:Build `
    "/p:Configuration=$Configuration" `
    "/p:Platform=$Platform" `
    "/p:WindowsTargetPlatformVersion=$($environment.windowsSdk.version)" `
    '/p:SignMode=Off' '/p:EnableTestSign=false'
if ($LASTEXITCODE -ne 0) {
    throw "Driver build failed with exit code $LASTEXITCODE."
}
