# Changelog

## Unreleased — review fixes

- Apply loopback Host/Origin/Fetch-Metadata checks across APIs and require JSON mutation bodies; protect queue-draining GET requests from navigation.
- Propagate display refresh/background transport errors and reject unsigned SDK errors before committing display caches.
- Poll N4 native readiness separately from Node/Relay process liveness in launcher and tray.
- Rework the lower information strip with local time/date, four distinct knob roles and an explicit right-most ACT06 lightning touch target.
- Guide first-time reasoning-knob setup without overwriting existing user mappings.
- Refresh the clock once per minute, upload only changed display regions and keep animated WebUI render caching bounded.

## 0.1.0-rc.2 — source-only licensing and launcher design

- Original application licensed AGPL-3.0-only, copyright A1aZ; separately built driver retains MS-PL.
- Source-only public distribution policy; local EXE remains a self-build/test artifact.
- Dark native Windows launcher, clearer primary action and independent bridge/keycap app icon.

## 0.1.0-rc.1 — release preparation

- Windows portable launcher, managed child lifecycle and hardware-free self-test.
- N4 key/knob input, calibrated example, native Micro HID/RPC bridge.
- Native-size state robots, native Micro colours, conservative pulse animation.
- Memory JPEG frame cache, default SDK encoding, unsigned transport error checks.
- Input layout and static screen diagnostics; initialization gate and delayed repaint.
- Curated source export, public sample config, dependency pins, test runner and release/security documentation.

Known limitation: display stability was verified on one N4 during development; broad firmware and clean-machine compatibility is not established. Public driver signing/distribution is not complete.
