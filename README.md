# car-unpacker-py

Pure Python tool to list and extract assets from Apple `.car` (Asset Catalog) files. No Apple dependencies — works on Windows, macOS, and Linux.

## Features

- Parses BOMStore container format (block table, named variables, B+ trees)
- Extracts CoreUI rendition metadata (CSI headers, TLV entries)
- Decodes deepmap2 (dmp2) image payloads with all 4 decode types
- Handles banded LZFSE streams with automatic band height detection
- Extracts palette-img (compression type 8) with LZFSE decompression
- Extracts raw data renditions (JPEG, HEIF, PDF, text) via RAWD tag
- Extracts named colors (COLR/RLOC) as JSON with RGBA float64 components
- Outputs PNG files (RGB, RGBA, or grayscale) using stdlib only

## Supported Rendition Types

| Type | Layout ID | Status | Output |
|------|-----------|--------|--------|
| deepmap2 (None/Default/Lossless/Palette) | 10-12 | ✅ | PNG |
| palette-img (compression 8) | 10-12 | ✅ | PNG |
| Raw data (JPEG/HEIF/PDF/text) | 1000 | ✅ | Original format |
| Color | 1009 | ✅ | JSON |

## Prerequisites

- Python 3.8+
- `lzfse` binary in PATH or next to the script

Build lzfse from source:

```bash
cd _tools/lzfse
gcc -O2 -o lzfse src/lzfse_*.c src/lzvn_*.c -Isrc
```

## Usage

```bash
python car_unpacker.py <Assets.car> [output_dir]
```

Lists all named blocks, trees, and renditions, then extracts assets to `output_dir/`.

## Architecture

Single-file implementation (`car_unpacker.py`) with these sections:

| Section | Purpose |
|---------|---------|
| BOMStore Parser | Header, block table, variables, B+ tree traversal |
| CSI Header Parser | 184-byte CSI header, TLV entries, layout/pixel format names |
| Deepmap2 Decoder | All 4 decode types: None, Default (zigzag+predictor+YCoCg), Lossless, Palette |
| Palette-img | MLEC → LZFSE → 0xCAFEF00D header + BGRA palette + index plane |
| RAWD Extractor | Raw data (JPEG/HEIF/PDF/text) with format detection |
| Color Extractor | COLR/RLOC named colors with float64 RGBA components |
| LZFSE Decompression | Calls external `lzfse` binary via subprocess |
| PNG Writer | Stdlib-only PNG output using `zlib`, supports RGB/RGBA/grayscale |
| Main Extraction | CLI entry point, bitmap tag dispatch, band stitching |

### Key Gotchas

- **`trunc_div2` must truncate toward zero**, not floor toward -∞. Use `int(v / 2)` not `v // 2`. This caused pixel-level mismatches with Go.
- **Pixel byte order in RGBA buffer is BGR**, not RGB. The `row_to_rgba` function writes `[b, g, r, a]`.
- **Banded streams** are size-prefixed: `[u32_le size][data]` repeated. Each band is decompressed independently.
- **Band height** is calculated from decompressed size: `height = len(decompressed) / (width * (alpha_size + components * 2) + 1)`.
- **Paeth predictor** is Apple's variant — only compares `left` vs `up`, not `up_left`.
- **LZFSE streams** may be concatenated with `bvx$` end-of-stream markers, or may be size-prefixed without markers.
- **Palette-img format**: `0xCAFEF00D(u32) + version(u32) + palette_count(u16) + BGRA[count] + u8[w*h]`.
- **Color (RLOC)**: `RLOC(4) + flags(4) + color_type(4) + num_components(4) + f64[num_components]`.
- **PNG write_png**: Outputs RGBA (color_type=6) when any pixel has alpha < 0xFF, otherwise RGB (color_type=2).
