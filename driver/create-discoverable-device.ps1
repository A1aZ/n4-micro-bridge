[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator privileges required.' }
$package=Join-Path $PSScriptRoot 'packages\local-test-20260908-134920'
$inf=Join-Path $package 'CodexMicroUm.inf'
$thumb='6561AC493129AFA2BB3CA2923AFEBA95C99D4F0A'
$signature=Get-AuthenticodeSignature -LiteralPath (Join-Path $package 'wudf.cat')
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Thumbprint -ne $thumb) { throw 'Pinned signed package did not verify.' }
$null=Start-Transcript -Path (Join-Path $package 'discoverable-device.log') -Append
try {
    $hardware='root\MiraboxCodexMicro'
    $instance='ROOT\VID_303A&PID_8360&MI_00\MIRABOX_N4'
    $existing=Get-PnpDevice -InstanceId $instance -ErrorAction SilentlyContinue
    if (-not $existing) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
public static class MiraboxRootDevice {
    [StructLayout(LayoutKind.Sequential)]
    public struct DevInfo { public uint Size; public Guid Class; public uint DevInst; public UIntPtr Reserved; }
    [DllImport("setupapi.dll", SetLastError=true)]
    static extern IntPtr SetupDiCreateDeviceInfoList(ref Guid ClassGuid, IntPtr hwnd);
    [DllImport("setupapi.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool SetupDiCreateDeviceInfoW(IntPtr set, string name, ref Guid guid, string desc, IntPtr hwnd, uint flags, ref DevInfo info);
    [DllImport("setupapi.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool SetupDiSetDeviceRegistryPropertyW(IntPtr set, ref DevInfo info, uint property, byte[] buffer, uint size);
    [DllImport("setupapi.dll", SetLastError=true)]
    static extern bool SetupDiCallClassInstaller(uint function, IntPtr set, ref DevInfo info);
    [DllImport("setupapi.dll", SetLastError=true)]
    static extern bool SetupDiDestroyDeviceInfoList(IntPtr set);
    public static void Create(string instance, string hardware) {
        Guid guid=new Guid("745a17a0-74d3-11d0-b6fe-00a0c90f57da");
        IntPtr set=SetupDiCreateDeviceInfoList(ref guid,IntPtr.Zero);
        if(set==new IntPtr(-1))throw new Win32Exception(Marshal.GetLastWin32Error());
        try {
            DevInfo info=new DevInfo();info.Size=(uint)Marshal.SizeOf(typeof(DevInfo));
            // Full explicit instance ID, no GENERATE_ID: reruns cannot add duplicates.
            if(!SetupDiCreateDeviceInfoW(set,instance,ref guid,"Mirabox N4 Codex Micro bridge",IntPtr.Zero,0,ref info))throw new Win32Exception(Marshal.GetLastWin32Error());
            byte[] ids=Encoding.Unicode.GetBytes(hardware+"\0\0");
            if(!SetupDiSetDeviceRegistryPropertyW(set,ref info,1,ids,(uint)ids.Length))throw new Win32Exception(Marshal.GetLastWin32Error());
            if(!SetupDiCallClassInstaller(0x19,set,ref info))throw new Win32Exception(Marshal.GetLastWin32Error());
        } finally { SetupDiDestroyDeviceInfoList(set); }
    }
}
'@
        [MiraboxRootDevice]::Create($instance,$hardware)
        Write-Output "CREATED=$instance"
    } else {
        $ids=(Get-PnpDeviceProperty -InstanceId $instance -KeyName DEVPKEY_Device_HardwareIds).Data
        if ($ids -notcontains $hardware) { throw 'Existing target belongs to another device; refusing changes.' }
        Write-Output "EXISTING=$instance"
    }
    # The same signed INF already matches the hardware ID; no INF changes,
    # certificate changes or policy changes are required for a different node name.
    & "$env:SystemRoot\System32\pnputil.exe" /add-driver $inf /install
    if ($LASTEXITCODE -ne 0) { throw "Binding returned $LASTEXITCODE; no reboot will be performed." }
    $device=Get-CimInstance Win32_PnPEntity | Where-Object {$_.PNPDeviceID -eq $instance}
    $device | Select-Object Name,PNPDeviceID,Status,ConfigManagerErrorCode | Format-List
    if ($device.ConfigManagerErrorCode -ne 0) { throw 'New device is not ready. Original device left unchanged.' }
    # Preserve the old project-owned node for rollback, but prevent two active
    # identical VID/PID devices. Never disable the physical N4 or unrelated HID.
    $old='ROOT\HIDCLASS\0001'
    $oldIds=(Get-PnpDeviceProperty -InstanceId $old -KeyName DEVPKEY_Device_HardwareIds -ErrorAction SilentlyContinue).Data
    if ($oldIds -contains $hardware) {
        & "$env:SystemRoot\System32\pnputil.exe" /disable-device $old
        if ($LASTEXITCODE -ne 0) { throw 'Could not disable old virtual node; do not start a relay until ambiguity is resolved.' }
        Write-Output "ROLLBACK_NODE_DISABLED_NOT_REMOVED=$old"
    }
    Write-Output 'DISCOVERY_NODE_READY; no security settings changed; no reboot'
} catch { Write-Output "DISCOVERY_NODE_ERROR: $($_.Exception.Message)"; exit 1 }
finally { $null=Stop-Transcript }
