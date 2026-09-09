# N4 Bridge · N4 键桥

由 **A1aZ** 开发的 Windows x64 社区工具，让 Mirabox N4 的按键、旋钮和屏幕接入 Codex Micro 工作流。

> 非 OpenAI、Work Louder 或 Mirabox 官方产品。目前仅支持 N4，项目处于测试阶段。

## 功能

- 使用屏幕按键切换会话，查看主机提供的任务状态。
- 使用旋钮进行导航、聊天滚动、推理强度调整和设备亮度控制。
- 状态机器人、原生状态色带和选中脉动。
- 本地控制页提供配置、按键对照和显示诊断。
- Windows 启动器支持系统托盘运行，不自动打开网页。

## 开始使用

**本仓库只发布源码，不提供预编译 EXE 或驱动。** 请先阅读[构建与发布说明](docs/release.md)，自行准备依赖并构建。构建出的启动器为 `Mirabox.exe`；运行后点击“启动全部”，需要配置时手动选择“打开控制页”。

控制页默认地址：http://127.0.0.1:18792/real-n4 。

实际连接需要 N4 SDK 的传输库和单独构建、安装的虚拟驱动。不要同时用 StreamDock、WebHID 页面和本工具打开同一台 N4。

## 环境与本地测试

- Windows x64
- Node.js 22 或更新版本
- CPython 3.11
- 实体连接和驱动构建所需依赖见[构建说明](docs/release.md)

```powershell
python -m venv .hidapi-venv
.\.hidapi-venv\Scripts\python.exe -m pip install -r requirements.txt
powershell -File scripts/test.ps1 -PythonPath .\.hidapi-venv\Scripts\python.exe
```

这些测试不访问 USB，也不安装驱动。Node 服务无 npm 运行时依赖，图标资源已包含在仓库中。

仅启动控制页进行软件侧检查：

```powershell
New-Item -ItemType Directory data -Force
Copy-Item examples/n4-calibrated.json data/config.json
powershell -File scripts/start-webui.ps1 -Port 18792
```

复制示例配置仅用于首次初始化，请勿覆盖已有配置。

## 限制与注意事项

- 当前基于有限实机测试，不保证所有 N4 固件及未来主机版本兼容。
- 正式驱动签名及干净系统安装验证尚未完成，需要开发者自行评估安装方案。
- 旋钮按压无法可靠提供真实长按时长；部分功能需要先在主机 Micro 设置中配置。
- 不同固件可能有不同按键编号，首次连接请使用按键对照台校准。
- 部分固件的显示稳定性仍待验证；遇到异常请停止服务，并参考[问题排查](docs/troubleshooting.md)。
- 服务仅供本机使用，不应暴露到公网。分享问题记录前请检查并脱敏配置、日志及设备信息。

## 文档

- [构建与发布](docs/release.md)
- [启动器使用](docs/portable-app.md)
- [Micro 原生状态](docs/micro-native-status.md)
- [问题排查](docs/troubleshooting.md)
- [贡献指南](CONTRIBUTING.md)
- [安全说明](SECURITY.md)

## 许可证

原创应用代码及原创资源采用 **AGPL-3.0-only**，Copyright © 2026 **A1aZ**。

独立构建的 Microsoft 派生驱动保留 **MS-PL**，其他第三方组件保留各自许可；本仓库不是全部采用同一种许可证。详见 [LICENSE](LICENSE)、[COPYRIGHT](COPYRIGHT) 和[第三方声明](THIRD_PARTY_NOTICES.md)。
