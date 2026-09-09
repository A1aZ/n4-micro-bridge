# Contributing

Start with Windows x64, Node 22+ and CPython 3.11. Install requirements.txt into a dedicated virtual environment. Run scripts/test.ps1 before submitting changes; its tests do not require HID access.

Keep HID input, host protocol, visual rendering and transport separate. A UI status must distinguish process running, device ready, SDK upload success and actual panel visibility. Never treat an SDK success code as proof of display presentation.

Changes to JPEG settings, device mapping or timing require a hardware smoke test. Record device model/firmware, test steps, results and whether a power cycle was needed. Do not silently change more than one of these variables when diagnosing display corruption.

Preserve default JPEG encoding, native pixel dimensions and the separation between input codes and image indices. Do not add alternate task-reading APIs to infer native Micro states without explicit design approval.

Original application contributions are under AGPL-3.0-only, copyright A1aZ and their respective contributors; preserve third-party MS-PL/MIT/OFL notices. Do not import MS-PL driver implementation into application code. Use the source-export script to prepare a reviewable source release; never zip the full working directory. A modified network-served version must provide the corresponding source offer required by AGPL section 13.
