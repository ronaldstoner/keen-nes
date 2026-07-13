#!/usr/bin/env python3
"""Generate the "hello MMC5 ExRAM" proof-ROM data (tools -> src/mmc5/gen_mmc5_data.c).

Proves the two things ExRAM extended-attribute mode (mode 1) buys us:

  1. >256 distinct 8x8 background tiles on ONE screen.  A plain nametable
     tile id is a u8, so a screen can name at most 256 distinct patterns.
     With ExRAM each 8x8 cell ALSO carries a 4KB CHR-bank number (ExRAM
     low 6 bits), so cell C shows pattern (bank<<12 | tile<<4 | fineY)
     even though the nametable byte only spans 0..255.  We render a smooth
     2D gradient sampled per-cell, which produces hundreds of unique 8x8
     bitmaps -- far past the 256 wall.

  2. Per-8x8 palette.  A plain attribute grid is one palette per 16x16
     pixels (2x2 tiles).  ExRAM high 2 bits give every 8x8 cell its own
     palette; we lay the 4 palettes in a diagonal (cx+cy)&3 stripe so the
     palette changes every 8px -- impossible on the 16x16 grid.

Output C file provides, all consumed by src/mmc5/hello_mmc5.c:
  chr_data[]      -- CHR-ROM (.chr_rom section), N 4KB banks of 256 tiles
  nt_tiles[960]   -- nametable byte (within-bank tile id, 0..255) per cell
  exram_attr[960] -- ExRAM byte (pal<<6 | bank) per cell
  bg_palettes[16] -- 4 BG palettes x 4 entries (entry0 = shared backdrop)
  plus MAPPER_PRG_ROM_KB / MAPPER_CHR_ROM_KB and count #defines.

ExRAM extended-attribute fetch semantics: index = vram_addr & 0x3FF
(coarse y*32+x), bank = exram&0x3F | chr_upper<<6, palette = exram>>6,
pattern = (bank<<12)|(tile<<4)|fineY read straight from CHR-ROM.
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from convert_nes import NES_PALETTE  # noqa: E402

W, H = 256, 240          # screen pixels
CW, CH = 32, 30          # screen in 8x8 cells
NCELL = CW * CH          # 960

# 4 background palettes: entry 0 is the shared backdrop ($3F00), entries
# 1..3 are a dark/mid/bright ramp of one hue.  Values are NES color ids.
BACKDROP = 0x0F
BG_PALETTES = [
    [BACKDROP, 0x06, 0x16, 0x26],   # pal0 red ramp
    [BACKDROP, 0x0A, 0x1A, 0x2A],   # pal1 green ramp
    [BACKDROP, 0x02, 0x12, 0x22],   # pal2 blue ramp
    [BACKDROP, 0x08, 0x18, 0x38],   # pal3 olive/yellow ramp
]

# 4x4 Bayer ordered-dither matrix (normalized 0..1), so a smooth level maps
# to a stable per-pixel 2-bit pattern -> neighbouring cells differ slightly.
BAYER = [
    [0, 8, 2, 10],
    [12, 4, 14, 6],
    [3, 11, 1, 9],
    [15, 7, 13, 5],
]


def level(x, y):
    """Smooth 2D plasma field in [0,1].  Independent horizontal and vertical
    structure (several sinusoids at different scales/phases) so that almost
    every 8x8 window is a distinct bitmap -- this is what pushes the unique
    tile count well past 256."""
    v = (math.sin(x / 16.0)
         + math.sin(y / 13.0)
         + math.sin((x + y) / 19.0)
         + math.sin(math.hypot(x - 128, y - 120) / 17.0))
    return (v + 4.0) / 8.0            # normalize -4..4 -> 0..1


def pixel_value(x, y):
    """2-bit pattern index 0..3 via ordered dithering of the 0..3 ramp."""
    lv = level(x, y) * 3.0            # 0..3
    base = int(lv)
    frac = lv - base
    thr = (BAYER[y & 3][x & 3] + 0.5) / 16.0
    v = base + (1 if frac > thr else 0)
    return 3 if v > 3 else v


def build():
    # 1. Per-cell 8x8 bitmap (16 bytes: 8 lo-plane, 8 hi-plane) + palette id.
    cell_tile = []          # 16-byte CHR tiles, one per cell (may duplicate)
    cell_pal = []           # palette 0..3 per cell
    for cy in range(CH):
        for cx in range(CW):
            lo = bytearray(8)
            hi = bytearray(8)
            for ry in range(8):
                lob = hib = 0
                for rx in range(8):
                    v = pixel_value(cx * 8 + rx, cy * 8 + ry)
                    lob = (lob << 1) | (v & 1)
                    hib = (hib << 1) | ((v >> 1) & 1)
                lo[ry] = lob
                hi[ry] = hib
            cell_tile.append(bytes(lo) + bytes(hi))
            cell_pal.append((cx + cy) & 3)   # diagonal 8px palette stripes

    # 2. Dedup identical bitmaps -> CHR tile pool (proves the >256 count).
    pool = {}               # bitmap -> global tile index
    order = []
    for t in cell_tile:
        if t not in pool:
            pool[t] = len(order)
            order.append(t)
    n_unique = len(order)

    # 3. Lay tiles into 4KB banks (256 tiles each), pad bank count to a
    #    power of two (llvm-mos requires power-of-2 CHR-ROM size).
    def next_pow2(n):
        p = 1
        while p < n:
            p <<= 1
        return p
    n_banks = next_pow2(max(1, (n_unique + 255) // 256))
    chr_size = bytearray(n_banks * 4096)
    for gi, t in enumerate(order):
        bank, within = gi // 256, gi % 256
        off = bank * 4096 + within * 16
        chr_size[off:off + 16] = t

    # 4. Per-cell nametable byte (within-bank tile id) + ExRAM byte.
    nt = bytearray(NCELL)
    ex = bytearray(NCELL)
    for c in range(NCELL):
        gi = pool[cell_tile[c]]
        bank, within = gi // 256, gi % 256
        assert bank < 64, "bank exceeds ExRAM 6-bit field"
        nt[c] = within
        ex[c] = (cell_pal[c] << 6) | bank

    return chr_size, nt, ex, n_unique, n_banks


def carr(name, data, per=16, sect=None):
    # `used` is required: chr_data is not referenced by name (the C code
    # reaches it only through the PPU/ExRAM banking), so without it LLD
    # garbage-collects the section and the CHR-ROM comes out all zeros.
    attr = f' __attribute__((used, section("{sect}")))' if sect else ''
    out = [f"const unsigned char {name}[{len(data)}]{attr} = {{"]
    for i in range(0, len(data), per):
        out.append("  " + ",".join(str(b) for b in data[i:i + per]) + ",")
    out.append("};")
    return "\n".join(out)


def render_reference(chr_data, nt, ex, pal, path):
    """Render exactly what the MMC5 ExRAM extended-attribute PPU path must
    produce, so the emitted data can be diffed against it pixel-for-pixel.
    Per cell: bank=exram&0x3F, palette=exram>>6,
    pattern=(bank<<12)|(tile<<4)|fineY straight out of CHR-ROM."""
    from PIL import Image
    img = Image.new("RGB", (W, H))
    px = img.load()
    for cy in range(CH):
        for cx in range(CW):
            c = cy * CW + cx
            tile = nt[c]
            bank = ex[c] & 0x3F
            palsel = (ex[c] >> 6) & 3
            for ry in range(8):
                addr = (bank << 12) | (tile << 4) | ry
                lo = chr_data[addr]
                hi = chr_data[addr + 8]
                for rx in range(8):
                    bit = 7 - rx
                    v = ((lo >> bit) & 1) | (((hi >> bit) & 1) << 1)
                    cidx = pal[0] if v == 0 else pal[palsel * 4 + v]
                    px[cx * 8 + rx, cy * 8 + ry] = NES_PALETTE.get(
                        cidx & 0x3F, (255, 0, 255))
    img.save(path)
    return path


def main():
    chr_data, nt, ex, n_unique, n_banks = build()
    chr_kb = len(chr_data) // 1024
    out = Path("src/mmc5/gen_mmc5_data.c")
    pal = []
    for p in BG_PALETTES:
        pal.extend(p)
    lines = [
        "// GENERATED by tools/gen_mmc5_proof.py -- do not edit.",
        "// Proof data for the hello-MMC5-ExRAM ROM.",
        "#include <ines.h>",
        "",
        f"// {n_unique} distinct 8x8 tiles on one 32x30 screen "
        f"(> 256 = impossible under a plain u8 nametable).",
        f"MAPPER_PRG_ROM_KB(32);",
        f"MAPPER_CHR_ROM_KB({chr_kb});",
        "",
        f"#define MMC5_PROOF_UNIQUE_TILES {n_unique}",
        f"#define MMC5_PROOF_CHR_BANKS {n_banks}",
        "",
        carr("chr_data", chr_data, 16, ".chr_rom"),
        "",
        carr("nt_tiles", nt, 32),
        "",
        carr("exram_attr", ex, 32),
        "",
        carr("bg_palettes", pal, 4),
        "",
    ]
    out.write_text("\n".join(lines))
    print(f"wrote {out}: unique_tiles={n_unique} banks={n_banks} "
          f"chr={chr_kb}KB nt={len(nt)} exram={len(ex)}")
    Path("build").mkdir(exist_ok=True)
    ref = render_reference(chr_data, nt, ex, pal, "build/mmc5_proof_ref.png")
    print(f"wrote reference render {ref}")
    assert n_unique > 256, f"only {n_unique} unique tiles; proof needs >256"
    print("PROOF OK: >256 distinct 8x8 tiles on one screen")


if __name__ == "__main__":
    main()
