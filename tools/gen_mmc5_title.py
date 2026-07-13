#!/usr/bin/env python3
"""Emit the Keen title as an MMC5 ExRAM per-8x8 screen (near-lossless).

Replaces the raster-band title (tools/gen_title.py), which swapped CHR
between horizontal bands via a mid-frame scanline IRQ.

The ExRAM extended-attribute path gives every 8x8 cell its own 8x8 CHR
tile (unique, no 256-tile reduction) + its own CHR 4KB bank (ExRAM low 6
bits) + its own palette (ExRAM high 2 bits), so the title is a single
static ExRAM screen with no raster IRQ, bands, or tear-proofing.

CHR BANK COORDINATION: the ExRAM CHR bank is an ABSOLUTE 4KB index into
CHR-ROM.  gen_mmc5_rom.py concatenates the bg banks + the 4KB sprite bank,
sizing CHR_ROM_KB (src/gen/levels.h); the title CHR links into
.chr_rom.title right after those (the linker collects .chr_rom then
.chr_rom.*, and leveldata_mmc5.c precedes titledata.c in EX_SRCS).  So the
title's first 4KB bank is CHR_ROM_KB/4, baked into every ExRAM byte here
so the runtime (src/title.c, MMC5_EX path) blits the bytes verbatim.

Outputs src/gen/titledata.c:
  title_chr[]  .chr_rom.title : unique 8x8 tiles, 4KB banks of 256 (16B ea)
  title_blob[] .prg_rom_12    : [0..959]     nametable tile-ids (u8)
                                [960..1919]  ExRAM bytes (pal<<6 | bank)
                                [1920..1935] bg palette (16 NES color ids)
build/title_preview.png : reconstruction from the emitted data (the exact
  pixels the ExRAM PPU path renders).

usage: KEEN_EP=4 python tools/gen_mmc5_title.py
"""
import os
import re
import struct
import sys
from collections import Counter
from pathlib import Path

os.environ.setdefault("KEEN_EP", "4")
sys.path.insert(0, str(Path(__file__).parent))
import keenlib as K            # noqa: E402
import convert_nes as C        # noqa: E402
# Reuse gen_title.py's image + palette primitives (composition, DOS
# reference image, palette clustering); only the CHR/nametable back-end
# differs (ExRAM instead of IRQ).
import gen_title as GT         # noqa: E402
from PIL import Image          # noqa: E402

ROOT = K.ROOT
GEN = ROOT / "src/gen"
GEN.mkdir(parents=True, exist_ok=True)
BUILD = ROOT / "build"
BUILD.mkdir(exist_ok=True)

EGA_RGB = K.EGA_PALETTE
NES_RGB = C.NES_PALETTE

# blob layout (keep in sync with src/title.c MMC5_EX path)
TB_NT = 0        # 960 nametable tile-ids
TB_EX = 960      # 960 ExRAM bytes
TB_PAL = 1920    # 16 bg palette bytes
BLOB_LEN = 1936

CREDIT_LINES = ("NES Port By: Ron Stoner", "stoner.com - DEMO ONLY")


def read_chr_layout():
    """Absolute 4KB base bank for the title CHR + the iNES CHR-ROM size, both
    from the freshly-generated src/gen/levels.h (gen_mmc5_rom.py runs first in
    the keen4 recipe).  The title CHR links right after CHR_ROM_KB."""
    txt = (GEN / "levels.h").read_text()
    chr_kb = int(re.search(r"CHR_ROM_KB (\d+)", txt).group(1))
    total_kb = int(re.search(r"TOTAL_CHR_KB (\d+)", txt).group(1))
    assert chr_kb % 4 == 0, f"CHR_ROM_KB {chr_kb} not a 4KB multiple"
    return chr_kb // 4, chr_kb, total_kb


def draw_credits(scr):
    """Composite the two port-credit lines into the bottom letterbox as white
    (EGA 15), so they pass through the SAME per-8x8 quantize/CHR/palette
    pipeline as the art.  The game font (EGAGRAPH chunk 3) is fh px tall (10
    in Galaxy), so rows 8..fh-1 hold the descenders of y/p/g/j — each line
    spans two 8px tile rows: line 0 -> nametable rows 26-27 (y 208), line 1
    -> rows 28-29 (y 224), centered horizontally."""
    font = K.EgaGraph().chunk(3)
    fh = struct.unpack_from("<H", font)[0]
    offs = struct.unpack_from("<256H", font, 2)
    widths = font[514:514 + 256]
    WHITE = 15
    for li, text in enumerate(CREDIT_LINES):
        w = sum(widths[ord(ch)] for ch in text)
        strip = [[0] * w for _ in range(16)]
        x = 0
        for ch in text:
            c = ord(ch)
            cw = widths[c]
            if cw and offs[c]:
                wb = (cw + 7) // 8
                for y in range(min(fh, 16)):
                    base = offs[c] + y * wb
                    for pxi in range(cw):
                        if font[base + (pxi >> 3)] >> (7 - (pxi & 7)) & 1:
                            strip[y][x + pxi] = WHITE
            x += cw
        y0 = 208 + li * 16
        x0 = (256 - w) // 2
        for y in range(16):
            for xx in range(w):
                if strip[y][xx]:
                    scr[y0 + y][x0 + xx] = WHITE


def cell_hist(grid8):
    return Counter(c for row in grid8 for c in row if c != 0)


def quantize_8(grid8, pal_slots, ctx):
    """Pick the best of the 4 palettes for one 8x8 EGA cell (min displayed
    RGB error, exactly GT.PalCtx.cell_err) and pack the 16-byte 2bpp NES
    pattern by mapping each pixel to the nearest displayed color of that
    palette + the black backdrop.  Returns (slot 0..3, 16-byte pattern)."""
    hist = cell_hist(grid8)
    slot = min(range(4), key=lambda i: ctx.cell_err(hist, pal_slots[i]))
    pal = pal_slots[slot]
    disp = [NES_RGB[0x0F]] + [NES_RGB[ctx.e2n[c]] for c in pal]
    lut = {0: 0}

    def nearest(c):
        if c not in lut:
            src = EGA_RGB[c]
            lut[c] = min(range(len(disp)),
                         key=lambda i: GT._d(src, disp[i]))
        return lut[c]

    lo = bytearray(8)
    hi = bytearray(8)
    for y in range(8):
        for x in range(8):
            v = nearest(grid8[y][x])
            lo[y] |= (v & 1) << (7 - x)
            hi[y] |= ((v >> 1) & 1) << (7 - x)
    return slot, bytes(lo) + bytes(hi)


def main():
    title_base_4k, chr_rom_kb, total_chr_kb = read_chr_layout()

    # 1. DOS title image -> 256x240 EGA screen (letterbox + hscale/vcrop,
    #    green-dither re-synthesis) + ground truth, reusing the gen_title path.
    src, sw, sh = GT.load_ega_image()
    scr = GT.compose_screen(src, sw, sh)
    gt = GT.ground_truth_rgb(src, sw, sh)
    for (y0, y1, x0, x1, base, dot) in GT.DITHER_FIX.get(K.EP, []):
        for y in range(y0, y1):
            row = scr[y]
            for x in range(x0, x1):
                if row[x] == base or row[x] == dot:
                    row[x] = dot if (y & 1) and not (x & 1) else base
    draw_credits(scr)
    # credits are white text: make them part of the ground truth so the
    # error metric rewards rendering them (letterbox is black otherwise).
    for y in range(208, 240):
        for x in range(256):
            if scr[y][x] == 15:
                gt[y][x] = EGA_RGB[15]

    # 2. cluster 4 bg palettes over EVERY 8x8 cell (displayed-error k-medoids
    #    from gen_title), then assign each cell its best palette.  This is the
    #    ExRAM per-8x8 palette a 16x16 attribute grid could not give.
    ctx = GT.PalCtx()
    cells8 = []                 # (cx, cy) -> 8x8 EGA grid
    hists = []
    for cy in range(30):
        for cx in range(32):
            g = [scr[cy * 8 + y][cx * 8:cx * 8 + 8] for y in range(8)]
            cells8.append(g)
            h = cell_hist(g)
            if h:               # blank cells don't steer the clustering
                hists.append((h, 1))
    _, pals = GT.cluster_band(hists, ctx)
    pals = [GT.order_palette(p, hists, ctx) for p in pals]

    # 3. per-8x8: best palette + unique CHR tile (dedup on the 16B pattern) ->
    #    nametable tile-id + ExRAM byte (pal<<6 | ABSOLUTE 4KB bank).
    chr_pool = {}
    chr_order = []

    def tile_index(pat):
        gi = chr_pool.get(pat)
        if gi is None:
            gi = chr_pool[pat] = len(chr_order)
            chr_order.append(pat)
        return gi

    nt = bytearray(960)
    ex = bytearray(960)
    max_bank = 0
    title_chr_upper = title_base_4k >> 6
    assert title_chr_upper < 4
    for k, g in enumerate(cells8):
        slot, pat = quantize_8(g, pals, ctx)
        gi = tile_index(pat)
        bank = title_base_4k + (gi >> 8)
        within = gi & 0xFF
        assert (bank >> 6) == title_chr_upper, (
            f"title CHR banks cross a $5130 64-bank window: "
            f"base={title_base_4k} bank={bank}")
        max_bank = max(max_bank, bank)
        nt[k] = within
        ex[k] = (slot << 6) | (bank & 0x3F)

    n_tiles = len(chr_order)
    n_banks = (n_tiles + 255) // 256
    assert chr_rom_kb + n_banks * 4 <= total_chr_kb, (
        f"title CHR {n_banks*4}KB at base {chr_rom_kb}KB overflows the "
        f"{total_chr_kb}KB iNES CHR-ROM")

    # 4. CHR-ROM image: 4KB banks of 256 tiles (16B each).
    chr_bin = bytearray(n_banks * 4096)
    for gi, pat in enumerate(chr_order):
        off = (gi >> 8) * 4096 + (gi & 0xFF) * 16
        chr_bin[off:off + 16] = pat

    # 5. bg palette bytes: 4 palettes x [backdrop, c1, c2, c3] -> NES ids.
    palbytes = bytearray()
    for p in pals:
        row = [0x0F] + [ctx.e2n[c] for c in list(p)[:3]]
        row += [0x0F] * (4 - len(row))
        palbytes += bytes(row)

    blob = bytes(nt) + bytes(ex) + bytes(palbytes)
    assert len(blob) == BLOB_LEN, len(blob)

    def carr(name, section, data):
        return (f'__attribute__((used, section("{section}"))) '
                f"const unsigned char {name}[{len(data)}] = {{ "
                + ", ".join(str(b) for b in data) + " };\n")

    (GEN / "titledata.c").write_text("".join([
        "// GENERATED by tools/gen_mmc5_title.py -- Keen title as an MMC5\n",
        "// ExRAM per-8x8 screen (near-lossless, no raster IRQ).  Blob:\n",
        f"//   [0..959]     nametable tile-ids\n",
        f"//   [960..1919]  ExRAM bytes (pal<<6 | abs 4KB bank)\n",
        f"//   [1920..1935] bg palette (16 NES color ids)\n",
        f"// title CHR abs 4KB banks {title_base_4k}..{max_bank}.\n",
        "extern const unsigned char title_chr[];\n",
        "extern const unsigned char title_blob[];\n",
        f"const unsigned char title_chr_upper = {title_chr_upper};\n",
        carr("title_chr", ".chr_rom.title", chr_bin),
        carr("title_blob", ".prg_rom_12", blob),
    ]))

    # 6. preview from the EMITTED data (== the ExRAM PPU render).
    img = [[NES_RGB[0x0F]] * 256 for _ in range(240)]
    for cy in range(30):
        for cx in range(32):
            k = cy * 32 + cx
            exb = ex[k]
            bank = (title_chr_upper << 6) | (exb & 0x3F)
            palsel = exb >> 6
            tof = (bank - title_base_4k) * 4096 + nt[k] * 16
            disp = [NES_RGB[0x0F]] + [
                NES_RGB[palbytes[palsel * 4 + j]] for j in (1, 2, 3)]
            for y in range(8):
                lo, hi = chr_bin[tof + y], chr_bin[tof + 8 + y]
                for x in range(8):
                    v = ((lo >> (7 - x)) & 1) | (((hi >> (7 - x)) & 1) << 1)
                    img[cy * 8 + y][cx * 8 + x] = disp[v]
    out = Image.new("RGB", (256, 240))
    pxo = out.load()
    for y in range(240):
        for x in range(256):
            pxo[x, y] = img[y][x]
    out.save(BUILD / "title_preview.png")

    err, per_band, _ = GT.measure(img, gt, (0,))

    print(f"MMC5 ExRAM title: pic {GT.TITLE_PIC} ({sw}x{sh}) -> "
          f"{GT.IMG_W}x{GT.IMG_H} @ row {GT.TOP_ROW}")
    print(f"  unique 8x8 tiles: {n_tiles} -> {n_banks} CHR 4KB bank(s) "
          f"({n_banks*4}KB), abs banks {title_base_4k}..{max_bank}")
    print(f"  palettes(NES): " + "  ".join(
        "/".join("%02X" % palbytes[s * 4 + j] for j in range(4))
        for s in range(4)))
    print(f"  blob {len(blob)}B in .prg_rom_12; CHR base {chr_rom_kb}KB "
          f"of {total_chr_kb}KB iNES")
    print(f"  ERR/px vs DOS ground truth (mean per-16px-cell RGB dist): "
          f"{err:.2f}")
    print(f"wrote {GEN/'titledata.c'} and {BUILD/'title_preview.png'}")


if __name__ == "__main__":
    main()
