# Third-party notices

Copyright (C) 2026 A1aZ. The root AGPL-3.0-only license applies to original application code and artwork, not a relicensing of dependencies.

The Microsoft-derived driver is a separately built MS-PL component, not AGPL. MS-PL is not GPL-compatible; do not copy its implementation into the AGPL application or describe the whole tree as solely AGPL. Whether a distribution is an aggregate depends on its actual integration, not merely directory names. This release provides source only; users obtain dependencies and build their own programs. Public combined-distribution licensing still requires review.

| Component | Provenance | License / packaging rule |
|---|---|---|
| StreamDock Device SDK | MiraboxSpace/StreamDock-Device-SDK, df53672a0c484bf6e679728a7a529f80df3cbd0d | MIT; preserve upstream LICENSE. Python source snapshot includes local N4 fixes. Binary transport redistribution needs separate review. |
| Micro protocol reference / default keymap | conol-ai/openmicrokbd, 0f4de97f18c303a90822848a89e8d0a8a6c80aae | MIT; preserve upstream/openmicrokbd/LICENSE for adapted material. |
| Virtual HID driver | Microsoft vhidmini2-derived sources | MS-PL; preserve driver/codexmicro-umdf/MS-PL.txt and source copyright notices. |
| Phosphor icons | core 2.1.1 | MIT; assets/themes/icons/LICENSE. |
| Noto Sans SC (optional) | Noto CJK | SIL OFL; assets/NotoSans-LICENSE.txt. Source export omits the font binary; Windows font fallback is available. |
| Node.js runtime | supplied by packager | Node.js and bundled third-party terms. Version-matching complete license must accompany binaries. |
| CPython runtime | supplied by packager | Preserve LICENSE.txt and bundled library notices. |
| hidapi, Pillow, WMI, pywin32 | requirements.txt | Preserve installed distribution licenses/metadata when bundling. |

Current robot states reuse the original image-generated pixel portraits, mapped by assets/themes/status-art.json. Compiled 96px portraits are included in source exports; large generation masters are omitted. Alternate code-native robot artwork remains available from scripts/build-status-robots.cjs. Original project artwork is covered by the root license to the extent applicable rights are held; third-party icon licenses remain unchanged.

This project is not an official OpenAI, Work Louder or Mirabox release. Compatibility names and USB identifiers do not imply endorsement. Do not include the locally trusted certificate, private keys or machine-signed driver package in a public release.

Before public binary release, a maintainer must review all bundled transport/runtime redistributables and select a production driver distribution/signing approach. The source license alone does not complete that review.
