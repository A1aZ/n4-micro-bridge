# CodexMicroUm scaffold

This directory is a UMDF 2 HID-minidriver starting point for the Mirabox N4
bridge. It is intentionally not an installer and does not contain a signed
binary. See [`../README.md`](../README.md) for prerequisites and the safe build
boundary.

The source is adapted from Microsoft's `hid/vhidmini2` sample under the
Microsoft Public License; the retained notices and full license are in
[`MS-PL.txt`](MS-PL.txt). The original
sample's test timer is disabled; reports are supplied through the custom
sideband interface declared in `protocol/codexmicro_protocol.h`.

Important implementation boundary:

* HID class requests are handled by `driver/codexmicro.c`.
* The native N4 bridge uses `IOCTL_MIRABOX_CODEX_PUSH_INPUT` to complete a
  pending `IOCTL_HID_READ_REPORT`, or queues the report until Codex reads it.
* Codex output reports are queued by `SetOutputReport`/`WriteReport` and can be
  drained with `IOCTL_MIRABOX_CODEX_READ_OUTPUT`.

The sideband queue is deliberately small (32 reports in each direction) and
drops the oldest entry on overflow. The production bridge should monitor the
queue and reconnect cleanly after a device reset.
