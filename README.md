# N4 Bridge · N4 键桥

Windows x64 社区项目 · 0.1.0-rc.2 · A1aZ。把 N4 按键、旋钮和屏幕接入 Micro HID 协议。

**非 OpenAI、Work Louder 或 Mirabox 官方产品。** 基于一台 N4 实测，不保证所有固件或未来主机版本兼容。

## 功能

- 10个屏幕按键、4个旋钮、长条信息屏；可视化输入与纯色诊断。
- 虚拟HID主/Companion双collection，Micro RPC与原生状态颜色，不另外读取任务API。
- 原生尺寸状态机器人、内存JPEG帧缓存、官方默认编码。
- 便携启动器管理WebUI、N4连接和Relay。× 隐藏到系统托盘，— 最小化到任务栏；托盘“退出”才清理自己启动的进程。

## 当前边界

- 虚拟驱动在开发机器上安装识别成功；公开生产签名和干净机器安装尚未完成，不是通用即装即用正式产品。
- 旋钮按压仅按下包，合成释放不能表示真实长按时长。
- 固件输入码可能不同。示例上排01–05、下排06–0A，需用按键对照台验证。
- 推理强度需先在主机Micro设置绑定左右方向，再开启配置确认项。
- 曾有扫描线异常，恢复官方默认JPEG并断电后正常。动效长期稳定性仍需实机验证，SDK成功不等于面板显示成功。

## 便携版使用

保留完整目录，双击 Mirabox.exe → 启动全部。控制页默认 http://127.0.0.1:18792/real-n4 。

配置在 data/config.json，日志在 logs/。不要将它们公开上传。不要同时用StreamDock、WebHID和原生桥接打开N4。首次使用仍需独立安装虚拟驱动；日常启动不会安装证书或驱动。

## 开发和测试

准备 Node.js 22+、CPython 3.11、Windows x64。

```powershell
python -m venv .hidapi-venv
.\.hidapi-venv\Scripts\python.exe -m pip install -r requirements.txt
powershell -File scripts/test.ps1 -PythonPath .\.hidapi-venv\Scripts\python.exe
```

测试不需要USB，也不安装驱动。Node无运行时npm依赖；重建图标时额外准备Sharp，运行 build-theme-assets.cjs 和 build-status-robots.cjs。日常运行使用已生成资源。

首次创建配置并启动WebUI：

```powershell
New-Item -ItemType Directory data -Force
Copy-Item examples/n4-calibrated.json data/config.json
powershell -File scripts/start-webui.ps1 -Port 18792
```

实体N4需要上游transport二进制，参见[发布流程](docs/release.md)。源码导出不包含二进制运行时。Micro Relay另外需要已安装的虚拟驱动。

## 结构

- src/：协议、映射、渲染、传输。
- webui/：控制、配置、诊断页面。
- driver/：MS-PL虚拟HID驱动源码与显式安装工具。
- scripts/：测试、构建、导出。
- examples/：无个人信息的示例配置。
- upstream/：第三方源代码及原始许可证。

## 发布与许可

用 `node scripts/export-source.cjs` 导出允许清单内的源码及SHA-256清单，**不要直接压缩整个开发目录**。

原创应用代码与原创图标采用 AGPL-3.0-only，Copyright © 2026 A1aZ；Microsoft派生驱动独立保留MS-PL，不能把整个目录一概重授权为AGPL。第三方依赖保留各自许可。详见[第三方声明](THIRD_PARTY_NOTICES.md)、[安全说明](SECURITY.md)、[贡献指南](CONTRIBUTING.md)、[发布流程](docs/release.md)。

对外仅发布源码，用户自行获取依赖、编译应用及驱动。本地EXE用于开发验证，不作为公开Release附件。修改后通过网络提供服务时，应遵守AGPL第13节的对应源码提供义务。

当前为发布准备阶段，不自动创建远程仓库或上传文件。
