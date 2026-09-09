# 发布候选流程

## 依赖来源

- SDK: https://github.com/MiraboxSpace/StreamDock-Device-SDK ，提交 df53672a0c484bf6e679728a7a529f80df3cbd0d。
- Micro参考: https://github.com/conol-ai/openmicrokbd ，提交 0f4de97f18c303a90822848a89e8d0a8a6c80aae。
- Python直接依赖见requirements.txt。这不是包含所有传递依赖哈希的供应链锁文件。

源码导出不含SDK二进制、字体、开发环境或签名驱动。运行N4前，从上述固定SDK提交取出 Python-SDK/src/StreamDock/Transport/TransportDLL，放回同名相对目录。不覆盖本项目的Python源文件，不从不明附件获取DLL。Windows字体可作为缺少Noto时的回退。

## 便携构建

在干净Windows x64环境准备Node22+、CPython3.11及requirements、.NET Framework C#编译器和SDK DLL。

1. 执行 scripts/test.ps1；失败停止构建。
2. 执行 scripts/build-portable.ps1 -NodePath <node.exe> -PythonPath <venv-python.exe>。只使用公开示例，不读取个人配置。
3. 新目录执行 N4Bridge.exe --self-test，检查 logs/selftest-result.txt。
4. 在无开发环境的机器测试启动/退出、端口冲突、驱动和设备连接。

这是可重复的构建步骤，不宣称字节级可复现。Python运行时从显式指定的环境复制，必须使用干净构建环境。不能把本地候选包未经审计就公开。

## 发布闸门

- [x] 原创应用采用AGPL-3.0-only，署名A1aZ；只发布源码，用户自行编译。
- [ ] 独立MS-PL驱动与应用的许可边界、第三方源代码分发条件最终复核。
- [ ] 配置私密安全报告联系人。
- [ ] 干净机器安装、重复启动、停止和退出验证。
- [ ] N4冷插拔、睡眠唤醒、两排输入校准、连续动效30分钟验证。
- [ ] Micro完成未读/等待审批真实信号验证，不用演示数据代替。
- [ ] 驱动生产签名、安装、升级、卸载回滚方案。
- [ ] 候选包无个人配置、日志、证书、私钥；审核source-manifest.json。
- [ ] 附版本、SHA-256、运行时版本和已知问题。

驱动曾在开发机器可用，不代表可公开安装。不要分发本机开发证书、私钥或开发签名包，不自动更改Windows安全设置。闸门未满足前只称候选版。发布脚本不上传、不创建远程仓库。
