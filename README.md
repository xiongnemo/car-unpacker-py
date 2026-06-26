# car-unpacker-py

Pure Python tool to list and extract images from Apple `.car` (Asset Catalog) files. No Apple dependencies — works on Windows, macOS, and Linux.

## Features

- Parses BOMStore container format (block table, named variables, B+ trees)
- Extracts CoreUI rendition metadata (CSI headers, TLV entries)
- Decodes deepmap2 (dmp2) image payloads with all 4 decode types
- Handles banded LZFSE streams with automatic band height detection
- Outputs PNG files (RGB or grayscale) using stdlib only

## Prerequisites

- Python 3.8+
- `lzfse` binary in PATH or at `../lzfse/lzfse[.exe]`

Build lzfse from source:

```bash
cd _tools/lzfse
gcc -O2 -o lzfse.exe src/lzfse_*.c src/lzvn_*.c -Isrc
```

## Usage

```bash
python3 car_unpacker.py <Assets.car> [output_dir]
```

Lists all named blocks, trees, and renditions, then extracts images to `output_dir/`.

## Architecture

Single-file implementation (`car_unpacker.py`) with these sections:

| Section | Purpose |
|---------|---------|
| BOMStore Parser | Header, block table, variables, B+ tree traversal |
| CSI Header Parser | 184-byte CSI header, TLV entries, layout/pixel format names |
| Deepmap2 Decoder | All 4 decode types: None, Default (zigzag+predictor+YCoCg), Lossless, Palette |
| LZFSE Decompression | Calls external `lzfse` binary via subprocess |
| PNG Writer | Stdlib-only PNG output using `zlib` |
| Main Extraction | CLI entry point, MLEC/RAWD handling, band stitching |

### Key Gotchas

- **`trunc_div2` must truncate toward zero**, not floor toward -∞. Use `int(v / 2)` not `v // 2`. This caused pixel-level mismatches with Go.
- **Pixel byte order in RGBA buffer is BGR**, not RGB. The `row_to_rgba` function writes `[b, g, r, a]`.
- **Banded streams** are size-prefixed: `[u32_le size][data]` repeated. Each band is decompressed independently.
- **Band height** is calculated from decompressed size: `height = len(decompressed) / (width * (alpha_size + components * 2) + 1)`.
- **Paeth predictor** is Apple's variant — only compares `left` vs `up`, not `up_left`.
- **LZFSE streams** may be concatenated with `bvx$` end-of-stream markers, or may be size-prefixed without markers.
