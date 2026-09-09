# Shared, read-only discovery helpers for the UMDF driver build scripts.
# Keep this file compatible with Windows PowerShell 5.1: it is also useful on
# machines that have Visual Studio Build Tools but not PowerShell 7.

function Resolve-MiraboxExistingPath {
    param([object[]]$Candidates)

    foreach ($candidate in @($Candidates)) {
        if (-not $candidate) { continue }
        try {
            $expanded = [Environment]::ExpandEnvironmentVariables([string]$candidate)
            if (Test-Path -LiteralPath $expanded) {
                return [IO.Path]::GetFullPath($expanded)
            }
        } catch {
            # A probe should continue through malformed/stale registry values.
        }
    }
    return $null
}

function Get-MiraboxVersionDirectories {
    param(
        [string]$Root,
        [string]$Pattern = '^\d+(?:\.\d+)+$'
    )

    if (-not $Root -or -not (Test-Path -LiteralPath $Root)) { return @() }

    $versions = @()
    foreach ($directory in @(Get-ChildItem -LiteralPath $Root -Directory -ErrorAction SilentlyContinue)) {
        if ($directory.Name -notmatch $Pattern) { continue }
        try {
            $versions += [pscustomobject]@{
                name = $directory.Name
                version = [version]$directory.Name
                path = $directory.FullName
            }
        } catch {
            # Ignore directory names which only superficially look versioned.
        }
    }
    return @($versions | Sort-Object version -Descending)
}

function Get-MiraboxKitBuildNumber {
    param([string]$Version)

    if ($Version -match '^10\.0\.(\d+)(?:\.\d+)?$') {
        return [int]$Matches[1]
    }
    return $null
}

function Get-MiraboxVSToolsetInfo {
    param(
        [string]$VsRoot,
        [ValidateSet('x64', 'ARM64')]
        [string]$Platform
    )

    $empty = [ordered]@{
        directory = $null
        props = $null
        targets = $null
        present = $false
    }
    if (-not $VsRoot) { return $empty }

    $platformNames = if ($Platform -eq 'ARM64') { @('ARM64', 'arm64') } else { @('x64') }
    $vcRoots = @()
    $modernVcRoot = Join-Path $VsRoot 'MSBuild\Microsoft\VC'
    foreach ($versionDirectory in @(Get-ChildItem -LiteralPath $modernVcRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^v\d+$' } |
        Sort-Object Name -Descending)) {
        $vcRoots += $versionDirectory.FullName
    }
    $vcRoots += (Join-Path $VsRoot 'Common7\IDE\VC\VCTargets')

    foreach ($vcRoot in @($vcRoots)) {
        foreach ($platformName in $platformNames) {
            $directory = Join-Path $vcRoot "Platforms\$platformName\PlatformToolsets\WindowsUserModeDriver10.0"
            if (-not (Test-Path -LiteralPath $directory)) { continue }

            $props = Resolve-MiraboxExistingPath @(
                (Join-Path $directory "Microsoft.Cpp.$platformName.WindowsUserModeDriver10.0.props"),
                (Join-Path $directory 'Toolset.props')
            )
            $targets = Resolve-MiraboxExistingPath @(
                (Join-Path $directory "Microsoft.Cpp.$platformName.WindowsUserModeDriver10.0.targets"),
                (Join-Path $directory 'Toolset.targets')
            )
            return [ordered]@{
                directory = [IO.Path]::GetFullPath($directory)
                props = $props
                targets = $targets
                present = [bool]($props -and $targets)
            }
        }
    }
    return $empty
}

function Get-MiraboxCompilerInfo {
    param(
        [string]$VsRoot,
        [ValidateSet('x64', 'ARM64')]
        [string]$Platform
    )

    $empty = [ordered]@{
        version = $null
        x64 = $null
        arm64 = $null
        selected = $null
    }
    if (-not $VsRoot) { return $empty }

    $toolRoot = Join-Path $VsRoot 'VC\Tools\MSVC'
    foreach ($toolDirectory in @(Get-MiraboxVersionDirectories -Root $toolRoot)) {
        $x64Compiler = Resolve-MiraboxExistingPath @(
            (Join-Path $toolDirectory.path 'bin\Hostx64\x64\cl.exe'),
            (Join-Path $toolDirectory.path 'bin\Hostarm64\x64\cl.exe'),
            (Join-Path $toolDirectory.path 'bin\Hostx86\x64\cl.exe')
        )
        $arm64Compiler = Resolve-MiraboxExistingPath @(
            (Join-Path $toolDirectory.path 'bin\Hostx64\arm64\cl.exe'),
            (Join-Path $toolDirectory.path 'bin\Hostarm64\arm64\cl.exe'),
            (Join-Path $toolDirectory.path 'bin\Hostx86\arm64\cl.exe')
        )
        $selected = if ($Platform -eq 'ARM64') { $arm64Compiler } else { $x64Compiler }
        if ($selected) {
            return [ordered]@{
                version = $toolDirectory.name
                x64 = $x64Compiler
                arm64 = $arm64Compiler
                selected = $selected
            }
        }
    }
    return $empty
}

function Get-MiraboxVisualStudioInfo {
    param(
        [ValidateSet('x64', 'ARM64')]
        [string]$Platform,
        [string]$VsRootOverride
    )

    $programFilesX86 = [Environment]::GetEnvironmentVariable('ProgramFiles(x86)')
    $programFiles = [Environment]::GetEnvironmentVariable('ProgramFiles')
    $roots = @()

    if ($VsRootOverride) { $roots += $VsRootOverride }
    if ($env:VSINSTALLDIR) { $roots += $env:VSINSTALLDIR }

    $vswhere = Resolve-MiraboxExistingPath @(
        $(if ($programFilesX86) { Join-Path $programFilesX86 'Microsoft Visual Studio\Installer\vswhere.exe' }),
        $(if ($programFiles) { Join-Path $programFiles 'Microsoft Visual Studio\Installer\vswhere.exe' }),
        $((Get-Command vswhere.exe -ErrorAction SilentlyContinue).Source)
    )
    if ($vswhere) {
        try {
            $discovered = @(& $vswhere -all -products * -requires Microsoft.Component.MSBuild -property installationPath 2>$null)
            $roots += @($discovered | Where-Object { $_ })
        } catch {
            # Standard-location fallback below still works if vswhere is stale.
        }
    }

    foreach ($base in @(
        $(if ($programFilesX86) { Join-Path $programFilesX86 'Microsoft Visual Studio\2022' }),
        $(if ($programFiles) { Join-Path $programFiles 'Microsoft Visual Studio\2022' })
    )) {
        if (-not $base -or -not (Test-Path -LiteralPath $base)) { continue }
        $roots += @(Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty FullName)
    }

    $seen = @{}
    $instances = @()
    foreach ($candidate in @($roots)) {
        $root = Resolve-MiraboxExistingPath @($candidate)
        if (-not $root) { continue }
        $key = $root.TrimEnd('\').ToLowerInvariant()
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true

        $msbuild = Resolve-MiraboxExistingPath @(
            (Join-Path $root 'MSBuild\Current\Bin\amd64\MSBuild.exe'),
            (Join-Path $root 'MSBuild\Current\Bin\MSBuild.exe'),
            (Join-Path $root 'MSBuild\17.0\Bin\amd64\MSBuild.exe'),
            (Join-Path $root 'MSBuild\17.0\Bin\MSBuild.exe')
        )
        $compiler = Get-MiraboxCompilerInfo -VsRoot $root -Platform $Platform
        $toolset = Get-MiraboxVSToolsetInfo -VsRoot $root -Platform $Platform
        $score = 0
        if ($msbuild) { $score += 4 }
        if ($compiler.selected) { $score += 4 }
        if ($toolset.present) { $score += 2 }
        $instances += [pscustomobject]@{
            root = $root
            msbuild = $msbuild
            compiler = $compiler
            toolset = $toolset
            score = $score
        }
    }

    $selected = $instances | Sort-Object score -Descending | Select-Object -First 1
    if (-not $selected) {
        return [ordered]@{
            root = $null
            msbuild = $null
            compiler = $null
            compilerX64 = $null
            compilerArm64 = $null
            compilerVersion = $null
            platformToolsetDirectory = $null
            platformToolsetProps = $null
            platformToolsetTargets = $null
            platformToolsetPresent = $false
            present = $false
        }
    }

    return [ordered]@{
        root = $selected.root
        msbuild = $selected.msbuild
        compiler = $selected.compiler.selected
        compilerX64 = $selected.compiler.x64
        compilerArm64 = $selected.compiler.arm64
        compilerVersion = $selected.compiler.version
        platformToolsetDirectory = $selected.toolset.directory
        platformToolsetProps = $selected.toolset.props
        platformToolsetTargets = $selected.toolset.targets
        platformToolsetPresent = [bool]$selected.toolset.present
        present = [bool]($selected.msbuild -and $selected.compiler.selected)
    }
}

function Resolve-MiraboxKitsRoot {
    param([string]$Override)

    if ($Override) {
        return Resolve-MiraboxExistingPath @($Override)
    }

    $programFilesX86 = [Environment]::GetEnvironmentVariable('ProgramFiles(x86)')
    $programFiles = [Environment]::GetEnvironmentVariable('ProgramFiles')
    $candidates = @($env:WindowsSdkDir, $env:WindowsSdkDir_10)
    foreach ($key in @(
        'HKLM:\SOFTWARE\Microsoft\Windows Kits\Installed Roots',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows Kits\Installed Roots'
    )) {
        try {
            $value = (Get-ItemProperty -LiteralPath $key -Name KitsRoot10 -ErrorAction SilentlyContinue).KitsRoot10
            if ($value) { $candidates += $value }
        } catch {
        }
    }
    $candidates += @(
        $(if ($programFilesX86) { Join-Path $programFilesX86 'Windows Kits\10' }),
        $(if ($programFiles) { Join-Path $programFiles 'Windows Kits\10' })
    )
    return Resolve-MiraboxExistingPath $candidates
}

function Get-MiraboxWdfInfo {
    param(
        [string]$WdkRoot,
        [ValidateSet('x64', 'ARM64')]
        [string]$Platform
    )

    $arch = if ($Platform -eq 'ARM64') { 'arm64' } else { 'x64' }
    $includeRoot = if ($WdkRoot) { Join-Path $WdkRoot 'Include\wdf\umdf' } else { $null }
    $libRoot = if ($WdkRoot) { Join-Path $WdkRoot "Lib\wdf\umdf\$arch" } else { $null }
    $headerFallback = $null
    $libraryFallback = $null

    foreach ($headerVersion in @(Get-MiraboxVersionDirectories -Root $includeRoot -Pattern '^2\.\d+$')) {
        $header = Resolve-MiraboxExistingPath @((Join-Path $headerVersion.path 'wdf.h'))
        if (-not $header) { continue }
        if (-not $headerFallback) { $headerFallback = $header }
        # UMDF links WdfDriverStubUm.lib; WdfDriverEntry.lib belongs to KMDF.
        $library = Resolve-MiraboxExistingPath @((Join-Path $libRoot "$($headerVersion.name)\WdfDriverStubUm.lib"))
        if ($library) {
            return [ordered]@{
                version = $headerVersion.name
                header = $header
                library = $library
                present = $true
            }
        }
    }

    foreach ($libraryVersion in @(Get-MiraboxVersionDirectories -Root $libRoot -Pattern '^2\.\d+$')) {
        $libraryFallback = Resolve-MiraboxExistingPath @((Join-Path $libraryVersion.path 'WdfDriverStubUm.lib'))
        if ($libraryFallback) { break }
    }
    return [ordered]@{
        version = $null
        header = $headerFallback
        library = $libraryFallback
        present = $false
    }
}

function Get-MiraboxDriverBuildEnvironment {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Project,
        [ValidateSet('x64', 'ARM64')]
        [string]$Platform = 'x64',
        [string]$VsRoot,
        [string]$KitsRoot,
        [string]$WdkRoot
    )

    $projectPath = [IO.Path]::GetFullPath($Project)
    $vs = Get-MiraboxVisualStudioInfo -Platform $Platform -VsRootOverride $VsRoot
    $sdkRoot = Resolve-MiraboxKitsRoot -Override $KitsRoot
    $resolvedWdkRoot = if ($WdkRoot) {
        Resolve-MiraboxExistingPath @($WdkRoot)
    } elseif ($env:WDKContentRoot) {
        Resolve-MiraboxExistingPath @($env:WDKContentRoot, $sdkRoot)
    } else {
        $sdkRoot
    }

    $sdkIncludeRoot = if ($sdkRoot) { Join-Path $sdkRoot 'Include' } else { $null }
    $sdkLibRoot = if ($sdkRoot) { Join-Path $sdkRoot 'Lib' } else { $null }
    $sdkVersions = @(Get-MiraboxVersionDirectories -Root $sdkIncludeRoot -Pattern '^10\.0\.\d+\.\d+$')
    $arch = if ($Platform -eq 'ARM64') { 'arm64' } else { 'x64' }
    $usableSdkVersions = @()
    foreach ($sdk in $sdkVersions) {
        $windowsHeader = Resolve-MiraboxExistingPath @((Join-Path $sdk.path 'um\Windows.h'))
        $sdkLibrary = Resolve-MiraboxExistingPath @((Join-Path $sdkLibRoot "$($sdk.name)\um\$arch\kernel32.lib"))
        if ($windowsHeader -and $sdkLibrary) {
            $usableSdkVersions += $sdk
        }
    }
    $sdk = $usableSdkVersions | Select-Object -First 1

    $wdkBuildRoot = if ($resolvedWdkRoot) { Join-Path $resolvedWdkRoot 'build' } else { $null }
    $wdkVersions = @(Get-MiraboxVersionDirectories -Root $wdkBuildRoot -Pattern '^10\.0\.\d+\.\d+$')
    $matchingSdk = $null
    $wdk = $null
    foreach ($sdkCandidate in $usableSdkVersions) {
        $sdkBuild = Get-MiraboxKitBuildNumber -Version $sdkCandidate.name
        $wdkCandidate = $wdkVersions | Where-Object {
            (Get-MiraboxKitBuildNumber -Version $_.name) -eq $sdkBuild
        } | Select-Object -First 1
        if ($wdkCandidate) {
            $matchingSdk = $sdkCandidate
            $wdk = $wdkCandidate
            break
        }
    }
    if ($matchingSdk) { $sdk = $matchingSdk }
    if (-not $wdk) { $wdk = $wdkVersions | Select-Object -First 1 }

    $sdkVersion = if ($sdk) { $sdk.name } else { $null }
    $wdkVersion = if ($wdk) { $wdk.name } else { $null }
    $sdkBuildNumber = Get-MiraboxKitBuildNumber -Version $sdkVersion
    $wdkBuildNumber = Get-MiraboxKitBuildNumber -Version $wdkVersion
    $buildNumbersMatch = [bool]($sdkBuildNumber -and $wdkBuildNumber -and $sdkBuildNumber -eq $wdkBuildNumber)

    $wdkBuildDirectory = if ($wdk) { $wdk.path } else { $wdkBuildRoot }
    $wdkCommonProps = if ($wdk) { Resolve-MiraboxExistingPath @((Join-Path $wdk.path 'WindowsDriver.Common.props')) } else { $null }
    $wdkCommonTargets = if ($wdk) { Resolve-MiraboxExistingPath @((Join-Path $wdk.path 'WindowsDriver.Common.targets')) } else { $null }
    $wdkPlatformProps = if ($wdk) {
        Resolve-MiraboxExistingPath @(
            (Join-Path $wdk.path "$Platform\WindowsUserModeDriver\WDK.$Platform.WindowsUserModeDriver.props"),
            (Join-Path $wdk.path "$arch\WindowsUserModeDriver\WDK.$arch.WindowsUserModeDriver.props")
        )
    } else { $null }
    $hidportHeader = if ($sdkVersion -and $resolvedWdkRoot) {
        Resolve-MiraboxExistingPath @(
            (Join-Path $resolvedWdkRoot "Include\$sdkVersion\km\hidport.h"),
            $(if ($sdkRoot -ne $resolvedWdkRoot) { Join-Path $sdkRoot "Include\$sdkVersion\km\hidport.h" })
        )
    } else { $null }
    $wdf = Get-MiraboxWdfInfo -WdkRoot $resolvedWdkRoot -Platform $Platform

    $hostToolArchitectures = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
        @('arm64', 'x64', 'x86')
    } else {
        @('x64', 'x86', 'arm64')
    }
    $infVerifCandidates = @()
    foreach ($toolArch in $hostToolArchitectures) {
        if (-not $resolvedWdkRoot) { continue }
        if ($wdkVersion) { $infVerifCandidates += (Join-Path $resolvedWdkRoot "bin\$wdkVersion\$toolArch\InfVerif.exe") }
        if ($sdkVersion -and $sdkVersion -ne $wdkVersion) { $infVerifCandidates += (Join-Path $resolvedWdkRoot "bin\$sdkVersion\$toolArch\InfVerif.exe") }
        $infVerifCandidates += (Join-Path $resolvedWdkRoot "bin\$toolArch\InfVerif.exe")
    }
    $infVerif = Resolve-MiraboxExistingPath $infVerifCandidates

    $buildFilesPresent = [bool]($wdkCommonProps -and $wdkCommonTargets -and $wdkPlatformProps)
    $wdkPresent = [bool](
        $buildNumbersMatch -and
        $buildFilesPresent -and
        $wdf.present -and
        $hidportHeader -and
        $vs.platformToolsetPresent
    )

    $result = [ordered]@{
        project = [ordered]@{
            path = $projectPath
            exists = [bool](Test-Path -LiteralPath $projectPath)
            platform = $Platform
            platformToolset = 'WindowsUserModeDriver10.0'
        }
        visualStudio = $vs
        windowsSdk = [ordered]@{
            root = $sdkRoot
            version = $sdkVersion
            buildNumber = $sdkBuildNumber
            includeRoot = $sdkIncludeRoot
            libRoot = $sdkLibRoot
            present = [bool]($sdkRoot -and $sdk)
        }
        wdk = [ordered]@{
            root = $resolvedWdkRoot
            version = $wdkVersion
            buildNumber = $wdkBuildNumber
            buildNumbersMatch = $buildNumbersMatch
            buildDirectory = $wdkBuildDirectory
            commonProps = $wdkCommonProps
            commonTargets = $wdkCommonTargets
            platformProps = $wdkPlatformProps
            wdfVersion = $wdf.version
            wdfHeader = $wdf.header
            hidportHeader = $hidportHeader
            wdfLibrary = $wdf.library
            infverif = $infVerif
            platformToolsetDirectory = $vs.platformToolsetDirectory
            platformToolsetProps = $vs.platformToolsetProps
            platformToolsetTargets = $vs.platformToolsetTargets
            components = [ordered]@{
                buildDirectory = [bool]($wdkBuildDirectory -and (Test-Path -LiteralPath $wdkBuildDirectory))
                buildFiles = $buildFilesPresent
                wdfHeader = [bool]$wdf.header
                hidportHeader = [bool]$hidportHeader
                wdfLibrary = [bool]$wdf.library
                infverif = [bool]$infVerif
                platformToolset = [bool]$vs.platformToolsetPresent
                matchingKitBuild = $buildNumbersMatch
            }
            present = $wdkPresent
        }
    }
    $result['canBuild'] = [bool](
        $result.project.exists -and
        $result.visualStudio.present -and
        $result.windowsSdk.present -and
        $result.wdk.present
    )
    $result['missing'] = @()
    if (-not $result.project.exists) { $result.missing += 'project' }
    if (-not $result.visualStudio.present) { $result.missing += "Visual Studio C++ Build Tools/MSBuild for $Platform" }
    if (-not $result.windowsSdk.present) { $result.missing += "Windows SDK headers/libraries for $Platform" }
    if (-not $result.wdk.components.buildFiles) { $result.missing += "WDK UMDF build files for $Platform" }
    if (-not $result.wdk.components.wdfHeader) { $result.missing += 'WDK UMDF WDF headers' }
    if (-not $result.wdk.components.hidportHeader) { $result.missing += 'WDK hidport.h' }
    if (-not $result.wdk.components.wdfLibrary) { $result.missing += "WDK UMDF WdfDriverStubUm.lib for $Platform" }
    if (-not $result.wdk.components.platformToolset) { $result.missing += "Visual Studio WDK platform toolset for $Platform" }
    if ($result.windowsSdk.present -and $wdkVersion -and -not $buildNumbersMatch) {
        $result.missing += 'matching Windows SDK/WDK build numbers'
    }
    return $result
}
