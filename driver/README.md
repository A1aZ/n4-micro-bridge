# Mirabox N4 → Codex Micro：Windows 虚拟 HID 试验驱动

这里是一个**未安装、未签名**的 UMDF 2 HID minidriver scaffold。它基于微软
`Windows-driver-samples/hid/vhidmini2` 的公开样例结构，目的是给后续的 N4
native bridge 提供一个可编译的起点；不会修改系统，也不会自动注册设备。

> 架构边界（必须先读）：微软的 `vhidmini2` HID minidriver 样例明确指出，
> 普通 custom IOCTL/WMI 不是 HID minidriver 的可靠用户态通信通道。因此本目录
> 保留的 `GUID_DEVINTERFACE_MIRABOX_CODEX_MICRO_BRIDGE` 和
> `IOCTL_MIRABOX_CODEX_*` 只能作为**诊断 scaffold/合同占位**，不能据此声称
> 安装后旧版 sideband 客户端一定可达 `codexmicro.c`。
>
> 当前 scaffold 是同一个虚拟 HID descriptor 中的两个 top-level collections：
> Codex 主 collection（Usage Page `0xFF00`、Report ID `6`）和 companion
> collection（Usage Page `0xFF70`、Report ID `7`）。两者共享一个 HID
> `IOCTL_HID_READ_REPORT` 输入队列；驱动按报文首字节的 Report ID 路由报告。
> 这使 native N4 bridge 可以尝试用 hidapi 打开 FF70，但它**不是**
> `openmicrokbd` 参考固件的独立第五 USB HID interface 拓扑。参考固件把 Codex
> interface 放在 interface 0，后面另有 keyboard/consumer/mouse/raw interfaces；
> 当前 dual-TLC 方案必须在 WDK 构建并安装后实际检查 Windows collection path、
> Codex 主 collection 独占打开以及 FF70 companion 的读写，不能预先宣称等价。

## 为什么选 `vhidmini2`，而不是直接调用 VHF

VHF（Virtual HID Framework）可以创建虚拟 HID，但普通 Node/Python 进程不能
直接调用它；需要先安装一个 UMDF 客户端驱动并由该驱动调用 VHF。更重要的是，
VHF 路径不适合本项目的完整兼容目标：Codex Micro 需要 vendor HID 的 output
reports 和 manufacturer/product strings。UMDF HID minidriver 能处理这些请求，
所以这里先沿 `vhidmini2` 方向搭建。

## 目标 HID 身份（与 openmicrokbd/Codex Micro 兼容）

| 字段 | 值 |
| --- | --- |
| VID / PID | `0x303A` / `0x8360` |
| Manufacturer | `Work Louder` |
| Product | `Codex Micro` |
| bcdDevice | `0x0100` |
| 主 Usage Page / Usage | `0xFF00` / `1` |
| 主 Report ID | `6` |
| Input / Output | 63-byte body（带 report ID 后各 64 bytes）|
| Companion Usage Page / Usage | `0xFF70` / `1`（当前 dual-TLC scaffold） |
| Companion Report ID | `7` |

主 Codex collection 的 descriptor（与 openmicrokbd 一致）是：

```text
06 00 FF 09 01 A1 01 85 06 15 00 26 FF 00
75 08 95 3F 09 01 81 02 95 3F 09 02 91 02 C0
```

当前驱动还追加了一个同样 64-byte framing 的 companion collection：

```text
06 70 FF 09 01 A1 01 85 07 15 00 26 FF 00
75 08 95 3F 09 01 81 02 95 3F 09 02 91 02 C0
```

两段拼接后的 active descriptor 长度为 58 bytes。主 collection 在线上使用
`[06 02 len payload padding...]`；companion 使用 Report ID 7 携带同形状的
不透明 body，由驱动在 N4 bridge 与 Codex collection 之间只转换 Report ID，
不改变 JSON payload。每个 report 的 body 最多 61 个 UTF-8 字节。描述、常量
和转换合同集中在 `protocol/codexmicro_protocol.h`。

## 当前状态

已包含：

- `driver/umdf2/CodexMicroUm.vcxproj` / `.inx`：UMDF2 项目和 root-enumerated INF；
- `driver/codexmicro.c` / `.h`：来自 vhidmini2 的 HID descriptor、字符串、
  read/write/feature 请求骨架，当前为 dual-TLC、64-byte Codex framing；
- `protocol/codexmicro_protocol.h`：记录主/companion descriptor、Report ID 转换、
  报告结构，以及保留的 `PUSH_INPUT`/`READ_OUTPUT`/`GET_INFO`/`RESET` 诊断合同；
- `driver/bridge.c`：32-entry 双向 ring 和共享 HID input queue；N4 bridge 发来的
  ID 7 报文转成主路径的 ID 6，Codex 的 ID 6 output 转成 companion 可读的 ID 7。
  旧 custom IOCTL 队列仍只用于诊断，不是 companion 的必需路径；
- `driver/umdf2/util.c`：UMDF HID transfer packet 的安全解包辅助函数。
- `src/codexmicro_companion.py` / `scripts/codexmicro-companion-probe.py`：
  hidapi 的 lazy companion transport 和只读枚举探测。

样例中的 timer callback 代码仍保留以方便调试，但默认不会启动；N4/native
bridge 是唯一的输入来源。驱动安装后，应先运行只读 companion probe，确认
FF70/Usage 1 collection 和可用 path，再确认 FF00/Usage 1 主 collection 能被
Codex Desktop 发现并独占打开，随后才启动 `--companion` bridge；在 WDK 构建、
签名、安装和实机验证完成前，只能运行离线 contract tests、环境探测和 probe。

## 构建（不会安装）

需要 Visual Studio 2022 的 C++ 工具链以及与当前 SDK **build number 匹配**的
Windows Driver Kit（本机检查到 Windows SDK `10.0.26100.0`，但尚未安装 WDK）。
例如 `26100` SDK 应搭配 `26100` WDK；末尾的 QFE 数字可以不同。安装 WDK 后，
在“Developer PowerShell for VS 2022”执行：

```powershell
msbuild .\driver\codexmicro-umdf\driver\umdf2\CodexMicroUm.vcxproj `
  /p:Configuration=Debug /p:Platform=x64 /p:SignMode=Off /p:EnableTestSign=false
```

2026-09-08 已安装 WDK 26100.6584、VS WDK Build Tools 组件及 x64/x86 Spectre 库，
并完成 x64 Debug 不签名构建。产物在 `driver/umdf2/x64/Debug/CodexMicroUm/`：
`CodexMicroUm.dll`、INF 和 CAT；DLL 为 `NotSigned`。尚未安装或验证 Codex 实机接入。
Node/WebUI 测试与驱动构建相互独立。这个 scaffold 没有调用 VHF，故不要求 `VhfUm.lib`。

在执行构建前，可以运行只读环境探测（不会调用 MSBuild，也不会修改系统）：

```powershell
& .\scripts\check-driver-build-env.ps1
& .\scripts\check-driver-build-env.ps1 -Json
& .\scripts\check-driver-build-env.ps1 -Platform ARM64 -Json
```

探测会通过 `vswhere`、注册表和常见安装目录寻找 Visual Studio、Windows SDK、
WDK 及 WDK VSIX；也支持 `-VsRoot`、`-KitsRoot`、`-WdkRoot` 覆盖 EWDK 或自定义
安装位置。它会按目标架构选择 MSVC，并优先选择 SDK/WDK build number 相匹配的
版本。只有项目、目标架构编译器、SDK、WDK headers/libraries、WDK build targets
和 VS platform toolset 都齐全时才报告 `Buildable: YES`。`build-driver.ps1
-CheckOnly` 使用同一套探测并在缺少组件时直接停止，不会调用安装器。

WDK 的传统安装布局中，探测的关键文件大致是：

- `Include\\wdf\\umdf\\<umdf-version>\\wdf.h`
- `Lib\\wdf\\umdf\\<x64|arm64>\\<umdf-version>\\WdfDriverStubUm.lib`
- `Include\\<sdk-version>\\km\\hidport.h`
- `build\\<sdk-version>\\<arch>\\WindowsUserModeDriver\\WDK.*.props`
- `MSBuild\\Microsoft\\VC\\v170\\Platforms\\<arch>\\PlatformToolsets\\WindowsUserModeDriver10.0\\Microsoft.Cpp.*.props/.targets`

不同版本的 WDK 可能使用 `Toolset.props`/`Toolset.targets` 或 `Common7\\IDE\\VC\\VCTargets`
的旧位置；探测器会兼容这些变体。

## 安装与签名（明确不会由本项目自动执行）

UMDF root device 需要管理员权限和有效签名。开发机通常要启用测试签名并使用
WDK 的 `Inf2Cat`/`signtool`，或者使用企业/发行证书。仅在明确确认后再执行，
例如：

```powershell
devcon install .\CodexMicroUm.inf root\MiraboxCodexMicro
```

卸载、测试签名、重启和 Codex 独占 HID 接口都属于系统状态变更，本 scaffold
不会替用户做这些操作。

## 与现有 WebUI 的关系

当前 WebUI 已能读取真实 N4 并通过 HTTP/SSE 验证映射与合成释放；浏览器不能
同时持有 N4 和 native hidapi 句柄。安装并验证双 TLC 驱动后，生产 companion
流程是：

```text
N4 hidapi reader
    → MicroBridge / JSON framing
    → companion HID output (ID 7)
    → UMDF shared HID input queue
    → ID 6 primary collection
    → Codex Desktop

Codex Desktop output report
    → UMDF SetOutputReport
    → companion HID input (ID 7)
    → MicroBridge / N4 lighting writer
```

`protocol/codexmicro_protocol.h` 还保留旧 custom IOCTL 的边界定义，便于诊断和
离线合同检查；它不会加载或复制 Codex 应用内部代码。WebHID 页面与 hidapi
bridge 不能同时独占同一个实体 N4，切换前请先断开并关闭页面。
