# Theme assets

Current selection: restored original pixel portrait style, still following Micro
status rather than slot. `status-art.json` maps idle → purple/resting, working →
cyan, complete → green/celebrating, attention → amber, error → coral, off/unknown → gray.
The raw Micro status colour remains on the separate status band. Compiled portraits
are included in source releases. Notes below about excluding compiled portraits or
using a single code-native mascot describe earlier design iterations only.

Current pixel theme uses the same code-native robot in seven colour/state variants:
`compiled/robot-*.png`, built with `scripts/build-status-robots.cjs`. Each is 96×96 and
is displayed without runtime resizing. Status colours come from native Micro messages;
unknown colours use a neutral face. Slot numbers, not different mascots, identify slots.
`*-native.png` action icons are rendered directly from vectors at 64×64.

The older portrait files described below are development history only, are not used by
the current robot renderer and are omitted from curated source exports. Their absence
does not prevent rebuilding current action icons and robots.

- `icons/`: Phosphor Icons core 2.1.1 SVGs, obtained from the official npm package via unpkg.
  License: MIT, included at `icons/LICENSE`. Only library artwork is used for action symbols.
- `pixel-01.png` … `pixel-06.png`: six independently generated robot portraits, using built-in ImageGen
  and the approved Pixel Crew reference. Prompts requested a chunky pixel robot bust, no text,
  no device chassis, opaque near-`#111a27` background; cyan, amber, mint, lavender, coral, gray-blue.
  Originals are retained. Generated canvases are 1254×1254; they are normalized for the device at build time.
- `compiled/`: local PNG payload assets shared by the SVG and Pillow renderers. No runtime CDN access.
  Icons are 128px (24px for pixel-style sampling); portraits are cropped to their artwork and fitted at 96px.
- `catalog.json`: allowlisted theme IDs, default key labels and asset mapping; no user-supplied file paths.

Regenerate payloads with `node scripts/build-theme-assets.cjs <path-to-sharp-module>`.
Production uses only the prepared PNGs and does not require Sharp.

The six portraits distinguish slots. Their expressions are decorative identities, not inferred task state.
Codex's actual light colour/brightness is shown separately; the unassigned key remains disabled.
