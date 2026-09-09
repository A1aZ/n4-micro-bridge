[CmdletBinding()]
param(
    [switch]$Json,
    [ValidateSet('x64', 'ARM64')]
    [string]$Platform = 'x64',
    # Resolve the default after parameter binding. `$MyInvocation` is not
    # populated reliably inside a parameter default expression under
    # `powershell.exe -File`.
    [string]$Project,
    # Optional overrides make custom/EWDK layouts diagnosable and let the
    # discovery logic be tested without touching the installed toolchain.
    [string]$VsRoot,
    [string]$KitsRoot,
    [string]$WdkRoot
)

# This is intentionally a read-only probe. It never installs workloads,
# invokes MSBuild, changes signing policy, or touches a device.
$ErrorActionPreference = 'SilentlyContinue'

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptRoot 'driver-build-env-lib.ps1')

if (-not $Project) {
    $Project = Join-Path $scriptRoot '..\driver\codexmicro-umdf\driver\umdf2\CodexMicroUm.vcxproj'
}

$result = Get-MiraboxDriverBuildEnvironment `
    -Project $Project `
    -Platform $Platform `
    -VsRoot $VsRoot `
    -KitsRoot $KitsRoot `
    -WdkRoot $WdkRoot

if ($Json) {
    $result | ConvertTo-Json -Depth 8
} else {
    Write-Output "Project:       $($result.project.path)"
    Write-Output "Target:        $($result.project.platform)"
    Write-Output "MSBuild:       $(if ($result.visualStudio.msbuild) { $result.visualStudio.msbuild } else { 'MISSING' })"
    Write-Output "C compiler:    $(if ($result.visualStudio.compiler) { $result.visualStudio.compiler } else { 'MISSING' })"
    Write-Output "Windows SDK:   $(if ($result.windowsSdk.version) { "$($result.windowsSdk.root) ($($result.windowsSdk.version))" } else { 'MISSING' })"
    Write-Output "WDK:           $(if ($result.wdk.version) { "$($result.wdk.root) ($($result.wdk.version))" } else { 'MISSING' })"
    Write-Output "WDK VS toolset: $(if ($result.wdk.components.platformToolset) { 'READY' } else { 'MISSING' })"
    Write-Output "Buildable:      $(if ($result.canBuild) { 'YES' } else { 'NO' })"
    if ($result.missing.Count -gt 0) {
        Write-Output "Missing:        $($result.missing -join ', ')"
    }
}

if ($result.canBuild) { exit 0 }
exit 2
