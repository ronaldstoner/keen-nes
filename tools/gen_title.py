#!/usr/bin/env python3
"""Generate NES title screen data from the original Keen title bitmap.

Input: the extracted EGAGRAPH title pic (Keen 4: chunk 109 PIC_TITLESCREEN,
pic index = chunk - OFF_BITMAPS = 103), a 320x200 EGA image. It is scaled
horizontally 320->256 (4/5 nearest) and cropped 200->192 vertically, then
letterboxed onto the 256x240 NES screen at a 16px-aligned row so the
16x16 palette-attribute grid lines up.

The 30 nametable rows are partitioned top-to-bottom into <= 4 horizontal
BANDS (boundaries on EVEN tile rows so no 16x16 attribute cell straddles
two bands). Each band gets:
  - its own 4KB CHR set (<= 256 unique tiles, NO lossy reduction), and
  - its own 4x3-color BG PALETTE SET, clustered per band and chained
    (shared palettes stay byte-identical and keep their slot; changes
    are priced against the boundary's blank-seam damage).
The runtime (src/title.c) swaps CHR R0/R1 between bands with scanline
IRQs. A mid-frame CHR swap is NOT atomic with respect to the PPU: the
handler's four mapper writes land partway across the boundary scanline Y,
so the left part of that line fetches from the old CHR set and the right
from the new one (and line Y's first two tiles are prefetched at the END
of line Y-1, always with the old set). To make that tear PHYSICALLY
IMPOSSIBLE, every pure-CHR boundary is emitted tear-proof: all tile
indices referenced by the two nametable rows around the boundary (rows
R-1 and R, R = Y/8) hold byte-identical pattern data in BOTH adjacent
banks at the SAME index, so wherever within lines Y-8..Y+7 the swap
lands — including one full line early/late on emulators whose mapper/PPU
alignment differs from hardware, and including the window between the R0
and R1 writes — the rendered pixels are identical. (Seamless mode uses
one global palette set, so a cell quantizes identically in every band and
sharing costs zero visual change; it only duplicates boundary-row tiles
into the neighbor bank, which the partition search budgets for.)
At boundaries whose palette delta is nonzero the IRQ handler instead
force-blanks exactly TWO scanlines (Y and Y+1: rendering off in hblank of
Y-1, on again in hblank of Y+1 — hblank-aligned so nothing tears and OAM
is never touched mid-evaluation) and rewrites the full 16-byte bg palette
during the blank; the budget is cycle-audited in src/title.c. Pure-hblank
palette writes without blanking do NOT fit (hblank is ~28 CPU cycles;
$2006 setup alone eats half), and $2007 writes while rendering are not
possible at all, so every nonzero delta pays the 2-line seam. Boundaries
with a zero delta stay pure CHR switches (no blank). The partition search
prices each candidate seam by the art it blacks out, so seams migrate to
dark rows / strong horizontal edges.

Quality passes (each measured against the original DOS pic):
  - per-band palettes (above);
  - ordered 4x4-Bayer dithering between the two nearest palette colors,
    gated per CELL: only where the box-filtered error improves, never in
    the DITHER_FIX green region (already a synthesized dither), never on
    cells whose solid error is already tiny (text/edges stay clean);
  - sprite OVERLAYS: the worst-error 8x8 cells (after the passes above)
    get static 8x8 sprites with 4 dedicated sprite palettes (<= 64
    sprites, <= 8 per tile row, front priority).

Pixel edits: Keen 4's green backdrop dot dither is re-synthesized on a
clean period-2 grid (the 4/5 scale destroys its phase; DITHER_FIX).

Outputs:
  src/gen/titledata.c
    - title_chr[nbands*4096 + 1024]  section ".chr_rom.title": lands after
      leveldata.c's all_chr (linker collects .chr_rom then .chr_rom.*), at
      1KB CHR bank CHR_ROM_KB (src/gen/levels.h). Last 1KB = overlay
      sprite tiles. Link order rule: leveldata.c precedes titledata.c.
    - title_blob section ".prg_rom_12" (VA $8000):
        [0..959]     nametable (per-band local tile indices)
        [960..1023]  attribute table
        [1024..1039] band 0 bg palette (16B)
        [1040..1055] sprite palette (16B)
        [1056]       nswitch (nbands-1; 0 = single band, no IRQ)
        [1057+21i]   switch i (21B): latch, chr_off (1KB units rel
                     CHR_ROM_KB; R0=base R1=base+2), has_pal (0/1),
                     v_hi, v_lo ($2006 restore = VRAM address of the
                     first line after the blank, Y+2), pal[16]
        [1120]       sprite CHR offset (1KB units rel CHR_ROM_KB)
        [1121]       nsprites
        [1122+4s]    OAM entries: y, tile, attr, x
      Latch semantics (armed in vblank, scanline-counter "new" reload behavior):
      switch k dispatches at line disp_k = Y_k - 1 (payload) or Y_k
      (pure CHR); its handler-strobed reload lands at the end of line
      Y_k + 2 (payload; lines Y/Y+1 have no A12 rise during the blank)
      or Y_k + 1 (pure). First latch = disp_1 (pre-render reload);
      chained latch = disp_{k+1} - reload_row_k - 1. Cycle audit in
      src/title.c.
  build/title_preview.png  reconstruction from the EMITTED data (band
      CHR + palettes, blanked boundary lines, overlay sprites), exactly
      what the PPU renders, pixel-for-pixel.

usage: KEEN_EP=4 python tools/gen_title.py
"""
import re
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import keenlib as K
import convert_nes as C
from PIL import Image

ROOT = K.ROOT
GEN = ROOT / "src/gen"
GEN.mkdir(parents=True, exist_ok=True)
BUILD = ROOT / "build"
BUILD.mkdir(exist_ok=True)

# PIC_TITLESCREEN chunk (id EGAGRAPH) - OFF_BITMAPS: ep4 109-6, ep6 34-6
TITLE_PIC = {4: 103, 5: 82, 6: 28}[K.EP]
TOP_ROW = 2         # first nametable tile row of the image (16px, aligned)
IMG_W, IMG_H = 256, 192
MAX_TILES = 256     # hard CHR limit for one pattern table (= one band)
MAX_BANDS = 4       # runtime supports up to 3 mid-frame CHR switches
MAX_SPRITES = 64
MAX_SPR_PER_ROW = 8  # hardware: 8 sprites per scanline

# Title-only NES color overrides (EGA index -> NES color), applied when
# emitting the hardware palette. The global EGA_TO_NES maps EGA green to
# the NES's dark $1A, which reads muddy across the title's big green
# backdrop; the DOS look (bright green field, light dots) matches $2A with
# $3A dots much better.
TITLE_NES = {
    4: {2: 0x2A, 10: 0x3A},
}

# Dither re-synthesis (screen coords, y0..y1 exclusive): the Keen 4 green
# backdrop is EGA green (2) with light-green (10) dots at even source x on
# odd source rows; the 320->256 nearest scale breaks the period and bands.
# Any pixel of the listed colors inside the region is re-laid on a clean
# period-2 grid: dot color at (y odd, x even), base color elsewhere.
# Green appears nowhere else in the lower half of the pic, so keying on
# color alone is safe there (the top-band vines stay untouched).
DITHER_FIX = {
    4: [(124, 208, 0, 256, 2, 10)],   # (y0, y1, x0, x1, base, dot)
}

# Ordered-dithering tuning. A cell only dithers if its 2x2-box-filtered
# error improves by at least DITHER_MIN_IMP per pixel; a pixel only
# dithers if its solid displayed error exceeds DITHER_MIN_ERR (in-palette
# pixels — text, outlines — never move).
DITHER_MIN_ERR = 24.0
DITHER_MIN_IMP = 1.5
BAYER4 = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]

SPRITE_MIN_GAIN = 10.0   # min mean per-pixel raw improvement for an overlay

# Per-band palette REWRITES at band boundaries cost a 2-scanline forced-
# blank seam (black lines; see src/title.c). The title must show no
# scanline breaks, so boundaries are constrained to a zero palette delta —
# one shared palette set, clustered over the whole screen — and every band
# switch stays a seamless pure-CHR swap. Set True to re-enable measured
# per-band palette rewrites (the optimizer prices each seam by the art it
# hides).
ALLOW_BLANK_SEAMS = False

EGA_RGB = K.EGA_PALETTE
NES_RGB = C.NES_PALETTE


def _d(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2
            + (a[2] - b[2]) ** 2) ** 0.5


def ega_nes_map():
    m = dict(C.EGA_TO_NES)
    m.update(TITLE_NES.get(K.EP, {}))
    return m


def load_ega_image():
    """Title pic as a 2D list of EGA color indices (0..15)."""
    img = Image.open(
        K.EXT / "gfx" / "pics" / f"pic{TITLE_PIC:02d}.png").convert("RGB")
    w, h = img.size
    px = img.load()
    cache = {}

    def ega_of(rgb):
        if rgb not in cache:
            cache[rgb] = min(range(16), key=lambda i: sum(
                (a - b) ** 2 for a, b in zip(EGA_RGB[i], rgb)))
        return cache[rgb]

    return [[ega_of(px[x, y]) for x in range(w)] for y in range(h)], w, h


def compose_screen(src, sw, sh):
    """256x240 EGA grid: black letterbox + hscaled/vcropped title image."""
    scr = [[0] * 256 for _ in range(240)]
    y0 = (sh - IMG_H) // 2                      # vertical center crop
    top = TOP_ROW * 8
    for y in range(IMG_H):
        row = src[y0 + y]
        out = scr[top + y]
        for x in range(IMG_W):
            out[x] = row[x * sw // IMG_W]       # 320->256 nearest
    return scr


def screen_cells(scr):
    """Unique 16x16 cells over the screen (16 cols x 15 rows)."""
    cells = {}
    usage = Counter()
    cellmap = []
    for cy in range(15):
        rowk = []
        for cx in range(16):
            cell = tuple(tuple(scr[cy * 16 + y][cx * 16 + x]
                               for x in range(16)) for y in range(16))
            if cell not in cells:
                cells[cell] = [list(r) for r in cell]
            usage[cell] += 1
            rowk.append(cell)
        cellmap.append(rowk)
    return cells, usage, cellmap


# ---------------------------------------------------------------------------
# per-band palette clustering (displayed-RGB error, exact triple rebuild)
# ---------------------------------------------------------------------------

class PalCtx:
    """Precomputed displayed-color cost tables for this episode."""

    def __init__(self):
        self.e2n = ega_nes_map()
        # cost[src][pal_color]: displayed error of showing EGA src with the
        # NES color EGA pal_color maps to. cost[src][None] = backdrop black.
        self.cost = [[_d(EGA_RGB[s], NES_RGB[self.e2n[p]])
                      for p in range(16)] for s in range(16)]
        self.cost0 = [_d(EGA_RGB[s], NES_RGB[0x0F]) for s in range(16)]

    def cell_err(self, hist, pal):
        """Total displayed error of a cell histogram under palette `pal`
        (tuple of EGA colors) + black backdrop."""
        t = 0.0
        for c, n in hist.items():
            best = self.cost0[c]
            for p in pal:
                v = self.cost[c][p]
                if v < best:
                    best = v
            t += n * best
        return t


def _best_triple(px_hist, ctx, maxn=10):
    """Exact best <=3-color palette for a weighted color histogram."""
    cols = [c for c, _ in px_hist.most_common(maxn) if c != 0]
    best = (ctx.cell_err(px_hist, ()), ())
    pool = []
    for r in (1, 2, 3):
        pool += list(combinations(cols, r))
    for cand in pool:
        e = ctx.cell_err(px_hist, cand)
        if e < best[0]:
            best = (e, cand)
    return best[1]


def cluster_band(hists, ctx, frozen=(), nfree=None, iters=10):
    """K-medoids-style: pick 4 palettes (frozen ones fixed) minimizing the
    band's total displayed error. hists = [(hist, weight)]. Returns
    (err, [palette tuples])."""
    if nfree is None:
        nfree = 4 - len(frozen)
    frozen = [tuple(p) for p in frozen]
    if nfree == 0:
        pals = list(frozen)
        e = sum(w * min(ctx.cell_err(h, p) for p in pals)
                for h, w in hists)
        return e, pals

    # seed free palettes with the most-wanted distinct top-3 triples
    trip_w = Counter()
    for h, w in hists:
        t = tuple(c for c, _ in h.most_common(3) if c != 0)
        if t:
            trip_w[t] += w * sum(h.values())
    seeds = []
    for t, _ in trip_w.most_common():
        if all(len(set(t) & set(s)) < max(1, len(t) - 1)
               for s in seeds + frozen):
            seeds.append(t)
        if len(seeds) == nfree:
            break
    while len(seeds) < nfree:
        seeds.append(())
    pals = frozen + seeds

    best_e = None
    for _ in range(iters):
        groups = [Counter() for _ in pals]
        tot = 0.0
        for h, w in hists:
            errs = [ctx.cell_err(h, p) for p in pals]
            bi = min(range(len(pals)), key=lambda i: errs[i])
            tot += w * errs[bi]
            for c, n in h.items():
                if c != 0:
                    groups[bi][c] += n * w
        if best_e is not None and tot >= best_e - 1e-6:
            break
        best_e = tot
        new = list(frozen)
        for g in groups[len(frozen):]:
            new.append(_best_triple(g, ctx) if g else ())
        pals = new
    return best_e, pals


def order_palette(pal, hists, ctx):
    """Fix intra-palette color order by weighted usage (stable emission)."""
    use = Counter()
    for h, w in hists:
        for c, n in h.items():
            if c in pal:
                use[c] += n * w
    return tuple(sorted(pal, key=lambda c: -use[c]))


def chain_palettes(band_hists, ctx, blank_pens, max_changes=2):
    """Palette sets per band, minimizing displayed error with <=2 palettes
    changed per boundary. A nonzero change at boundary k costs a forced-
    blank scanline there, priced at blank_pens[k-1] (same units as the
    cluster error), so palette rewrites migrate to boundaries where a
    black line hides well (dark rows / strong art edges). Returns
    (sets, kept): kept[k] = palettes of band k byte-shared with k-1."""
    if max_changes == 0:
        # seamless mode: one set for the whole screen (cells appearing in
        # several bands weigh once per band — matching their screen area)
        allh = [h for hs in band_hists for h in hs]
        _, pals = cluster_band(allh, ctx)
        pals = [order_palette(p, allh, ctx) for p in pals]
        sets = [list(pals) for _ in band_hists]
        kept = [[]] + [list(pals) for _ in band_hists[1:]]
        return sets, kept
    sets, kept = [], []
    prev = None
    for bi, hists in enumerate(band_hists):
        if prev is None:
            e, pals = cluster_band(hists, ctx)
            pals = [order_palette(p, hists, ctx) for p in pals]
            sets.append(pals)
            kept.append([])
            prev = pals
            continue
        best = None
        for keepn in range(4, 3 - max_changes, -1):
            for Kset in combinations(range(4), keepn):
                fr = [prev[i] for i in Kset]
                e, pals = cluster_band(hists, ctx, frozen=fr,
                                       nfree=4 - keepn)
                e += 500.0 * (4 - keepn)
                if keepn < 4:
                    e += blank_pens[bi - 1]
                if best is None or e < best[0]:
                    best = (e, fr, pals)
        _, fr, pals = best
        out = list(fr) + [order_palette(p, hists, ctx)
                          for p in pals[len(fr):]]
        sets.append(out)
        kept.append(list(fr))
        prev = out
    return sets, kept


def assign_slots(sets, kept):
    """Slot (attribute-index) assignment per band: shared palettes keep
    their slot (so a boundary whose sets match byte-for-byte needs no
    palette payload at all), changed palettes fill the vacated slots.
    Returns slots[k] = list of 4 palettes in slot order, or None."""
    from itertools import permutations

    def dfs(k, prev_slots, acc):
        if k == len(sets):
            return acc
        keep = kept[k]
        new = [p for p in sets[k] if p not in keep]
        vac = [i for i in range(4) if prev_slots[i] not in keep]
        # sanity: kept palettes exist in prev_slots
        if len(vac) != len(new):
            return None
        for perm in permutations(new):
            slots = list(prev_slots)
            for i, p in zip(vac, perm):
                slots[i] = p
            r = dfs(k + 1, slots, acc + [slots])
            if r:
                return r
        # len(new) == 0 path
        if not new:
            return dfs(k + 1, list(prev_slots), acc + [list(prev_slots)])
        return None

    for perm0 in permutations(sets[0]):
        r = dfs(1, list(perm0), [list(perm0)])
        if r:
            return r
    return None


# ---------------------------------------------------------------------------
# quantization (solid + gated ordered dithering)
# ---------------------------------------------------------------------------

def quantize_cell_band(cell, pal_slots, ctx, dither_ok):
    """Quantize a 16x16 EGA cell against 4 band palettes. Returns
    (slot, solid_grid, dith_grid_or_None, imp) where imp is the
    box-filtered error improvement of dithering (per cell, absolute)."""
    hist = Counter(c for row in cell for c in row if c != 0)
    errs = [ctx.cell_err(hist, p) for p in pal_slots]
    slot = min(range(4), key=lambda i: errs[i])
    pal = pal_slots[slot]
    disp = [NES_RGB[0x0F]] + [NES_RGB[ctx.e2n[c]] for c in pal]

    def nearest(c):
        src = EGA_RGB[c]
        return min(range(len(disp)), key=lambda i: _d(src, disp[i]))

    lut = {0: 0}
    solid = [[lut.setdefault(c, nearest(c)) for c in row] for row in cell]
    if not dither_ok:
        return slot, solid, None, 0.0

    dith = [row[:] for row in solid]
    any_d = False
    for y in range(16):
        for x in range(16):
            c = cell[y][x]
            src = EGA_RGB[c]
            i1 = solid[y][x]
            e1 = _d(src, disp[i1])
            if e1 <= DITHER_MIN_ERR:
                continue
            # best partner: minimize distance from src to the mix line
            best = None
            for i2 in range(len(disp)):
                if i2 == i1:
                    continue
                c1, c2 = disp[i1], disp[i2]
                dv = tuple(b - a for a, b in zip(c1, c2))
                dd = sum(v * v for v in dv)
                if dd == 0:
                    continue
                f = sum((s - a) * v for s, a, v in
                        zip(src, c1, dv)) / dd
                f = max(0.0, min(1.0, f))
                mix = tuple(a + f * v for a, v in zip(c1, dv))
                em = _d(src, mix) + 0.18 * f * (1 - f) * dd ** 0.5
                if best is None or em < best[0]:
                    best = (em, i2, f)
            if best and best[0] < e1 * 0.8:
                _, i2, f = best
                if f > (BAYER4[y & 3][x & 3] + 0.5) / 16.0:
                    dith[y][x] = i2
                any_d = True
    if not any_d:
        return slot, solid, None, 0.0

    # gate on 2x2-box-filtered error (perceived local average)
    def filt_err(grid):
        t = 0.0
        for by in range(8):
            for bx in range(8):
                sr = [0.0, 0.0, 0.0]
                dr = [0.0, 0.0, 0.0]
                for dy in range(2):
                    for dx in range(2):
                        s = EGA_RGB[cell[by * 2 + dy][bx * 2 + dx]]
                        p = disp[grid[by * 2 + dy][bx * 2 + dx]]
                        for i in range(3):
                            sr[i] += s[i]
                            dr[i] += p[i]
                t += _d(sr, dr)  # uniform x4 scale (comparison only)
        return t
    imp = filt_err(solid) - filt_err(dith)
    if imp < DITHER_MIN_IMP * 64:      # 64 blocks ~ per-pixel threshold
        return slot, solid, None, 0.0
    return slot, solid, dith, imp


def cell_to_chr(grid):
    return C.cell_to_chr(grid)


CREDIT_LINES = ("NES Port By: Ron Stoner", "stoner.com - DEMO ONLY")


def build_credit_tiles(last_pals, ctx):
    """Render the port credit lines with the game's proportional font into
    dedicated 8x8 CHR tiles: returns (tiles, per-line tile-index rows,
    palette slot used). They live in the bottom letterbox rows = last
    band, so they use the last band's whitest palette slot."""
    import struct
    pi = si = 0
    best = -1
    for p in range(4):
        for s, c in enumerate(last_pals[p][:3]):
            lum = sum(EGA_RGB[c]) + (10 ** 6 if c == 15 else 0)
            if lum > best:
                pi, si, best = p, s, lum
    color = si + 1
    font = K.EgaGraph().chunk(3)
    fh = struct.unpack_from("<H", font)[0]
    offs = struct.unpack_from("<256H", font, 2)
    widths = font[514:514 + 256]
    # The font is fh px tall (10 in the Galaxy games): rows 8..fh-1 hold
    # the descenders of y/p/g/q/j, so each text line spans TWO tile rows.
    # The lower row is None when no glyph descends (dedup makes the check
    # cheap: an all-blank lower strip emits no tiles at all).
    tiles, lut, rows = [], {}, []
    for text in CREDIT_LINES:
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
                    for px in range(cw):
                        if font[base + (px >> 3)] >> (7 - (px & 7)) & 1:
                            strip[y][x + px] = color
            x += cw
        line_rows = []
        for y0 in (0, 8):
            row = []
            blank = all(v == 0 for y in range(y0, y0 + 8) for v in strip[y])
            if y0 == 8 and blank:
                line_rows.append(None)
                continue
            for tx in range(0, w, 8):
                lo, hi = bytearray(8), bytearray(8)
                for y in range(8):
                    for b in range(8):
                        v = strip[y0 + y][tx + b] if tx + b < w else 0
                        lo[y] |= (v & 1) << (7 - b)
                        hi[y] |= ((v >> 1) & 1) << (7 - b)
                pat = bytes(lo) + bytes(hi)
                if pat not in lut:
                    lut[pat] = len(tiles)
                    tiles.append(pat)
                row.append(lut[pat])
            line_rows.append(row)
        rows.append(line_rows)
    return tiles, rows, pi


def palbytes(slots_b, e2n):
    """Emitted 16-byte bg palette of one band's slot list."""
    out = bytearray()
    for s in range(4):
        row = [0x0F] + [e2n[c] for c in slots_b[s][:3]]
        row += [0x0F] * (4 - len(row))
        out += bytes(row)
    return bytes(out)


def share_flags(bb, e2n):
    """Per interior boundary: tear-proof tile sharing applies iff the two
    bands' emitted palette sets are byte-identical (always true in
    ALLOW_BLANK_SEAMS=False mode — one global set). Identical palettes =>
    identical quantization of any cell in both bands, so shared patterns
    are well-defined. A differing boundary carries a palette payload and
    hides its swap inside the 2-line forced blank instead."""
    return [palbytes(bb["slots"][j], e2n) == palbytes(bb["slots"][j + 1],
                                                      e2n)
            for j in range(bb["nb"] - 1)]


# ---------------------------------------------------------------------------
# band partition search
# ---------------------------------------------------------------------------

def chr_headroom_kb():
    txt = (GEN / "levels.h").read_text()
    chrkb = int(re.search(r"CHR_ROM_KB (\d+)", txt).group(1))
    total = int(re.search(r"TOTAL_CHR_KB (\d+)", txt).group(1))
    return total - chrkb


def probe_row_tiles(cells, cellmap, ctx):
    """Approximate unique-tile sets per nametable row using one global
    4-palette quantization (partition feasibility probe only)."""
    hists = [(Counter(c for row in cells[k] for c in row if c != 0), n)
             for k, n in Counter(
                 cellmap[cy][cx] for cy in range(15)
                 for cx in range(16)).items()]
    _, pals = cluster_band(hists, ctx)
    lut, tiles = {}, []

    def idx(t):
        if t not in lut:
            lut[t] = len(tiles)
            tiles.append(t)
        return lut[t]

    idx(cell_to_chr([[0] * 16 for _ in range(16)])[0])
    row_tiles = [set() for _ in range(30)]
    for cy in range(15):
        for cx in range(16):
            key = cellmap[cy][cx]
            _, grid, _, _ = quantize_cell_band(
                cells[key], pals, ctx, dither_ok=False)
            tl, tr, bl, br = (idx(t) for t in cell_to_chr(grid))
            row_tiles[cy * 2].update((tl, tr, 0))
            row_tiles[cy * 2 + 1].update((bl, br, 0))
    return row_tiles


def edge_strength(gt, y):
    """Mean RGB difference between screen rows y-1 and y (art edge)."""
    if y <= 0 or y >= 240:
        return 0.0
    return sum(_d(gt[y - 1][x], gt[y][x]) for x in range(256)) / 256


def enumerate_partitions(row_tiles, max_bands, cap, last_reserve=0):
    """All feasible band partitions: boundaries on even rows strictly
    inside the image (clear of the letterbox/credit rows), per-band probe
    tile count <= cap INCLUDING the shared boundary rows: a band also
    carries the nametable row just past each of its interior edges (rows
    R-1 and R of a boundary at row R live at identical indices in both
    adjacent banks so the mid-scanline CHR swap cannot tear — see
    emit_nt_chr). The LAST band additionally reserves last_reserve slots
    (the credit-line tiles appended to it). Returns boundary-row tuples."""
    even = [r for r in range(4, 26, 2)]
    outs = []
    for nb in range(0, max_bands):
        for bs in combinations(even, nb):
            ok = True
            edges = (0,) + bs + (30,)
            for i in range(len(edges) - 1):
                u = set()
                for r in range(edges[i], edges[i + 1]):
                    u |= row_tiles[r]
                if edges[i] != 0:        # shared row above the top edge
                    u |= row_tiles[edges[i] - 1]
                if edges[i + 1] != 30:   # shared first row of next band
                    u |= row_tiles[edges[i + 1]]
                lim = cap - (last_reserve if edges[i + 1] == 30 else 0)
                if len(u) > lim:
                    ok = False
                    break
            if ok:
                outs.append(bs)
    return outs


# ---------------------------------------------------------------------------
# preview + error measurement
# ---------------------------------------------------------------------------

def ground_truth_rgb(src, sw, sh):
    gt = [[(0, 0, 0)] * 256 for _ in range(240)]
    y0 = (sh - IMG_H) // 2
    top = TOP_ROW * 8
    for y in range(IMG_H):
        for x in range(IMG_W):
            gt[top + y][x] = EGA_RGB[src[y0 + y][x * sw // IMG_W]]
    return gt


def measure(img, gt, bands):
    """(overall mean cell err, per-band means, per-8x8-cell errors)."""
    cell_err = {}
    for cy in range(1, 13):
        for cx in range(16):
            t = 0.0
            for y in range(cy * 16, cy * 16 + 16):
                for x in range(cx * 16, cx * 16 + 16):
                    t += _d(gt[y][x], img[y][x])
            cell_err[(cy, cx)] = t / 256
    errs = list(cell_err.values())
    bl = sorted(y * 8 for y in bands) + [240]
    per_band = []
    for i in range(len(bl) - 1):
        cs = [e for (cy, cx), e in cell_err.items()
              if bl[i] <= cy * 16 < bl[i + 1]]
        per_band.append(sum(cs) / len(cs) if cs else 0.0)
    return sum(errs) / len(errs), per_band, cell_err


def measure_filt(img, gt):
    """Overall mean cell error after a 2x2 box filter on both images —
    the perceptual metric ordered dithering optimizes (local average)."""
    tot = 0.0
    for cy in range(1, 13):
        for cx in range(16):
            t = 0.0
            for by in range(8):
                for bx in range(8):
                    sa = [0, 0, 0]
                    sb = [0, 0, 0]
                    for dy in range(2):
                        for dx in range(2):
                            y = cy * 16 + by * 2 + dy
                            x = cx * 16 + bx * 2 + dx
                            g = gt[y][x]
                            p = img[y][x]
                            for i in range(3):
                                sa[i] += g[i]
                                sb[i] += p[i]
                    t += _d(sa, sb) / 4.0
            tot += t / 64
    return tot / 192


# ---------------------------------------------------------------------------
# main build
# ---------------------------------------------------------------------------

def blank_penalty(gt, y):
    """Cost (cluster-error units) of the forced-blank seam of a palette
    rewrite at boundary y: TWO black scanlines (y and y+1 — the audited
    IRQ schedule disables rendering in hblank of y-1 and re-enables in
    hblank of y+1), with a mild salience surcharge (a black seam inside
    bright art is more visible than its raw error, but a wrong-hued
    region is worse still)."""
    return 0.35 * sum(_d(gt[yy][x], (0, 0, 0))
                     for yy in (y, y + 1) for x in range(256))


def build_bands(partition, cells, cellmap, ctx, dfix_keys, gt):
    """Full pipeline for one candidate partition. Returns dict or None if
    a band overflows 256 tiles even undithered."""
    edges = (0,) + tuple(partition) + (30,)
    nb = len(edges) - 1
    band_cells = []     # per band: ordered unique cell keys
    band_hists = []
    for b in range(nb):
        keys, seen = [], set()
        h = []
        cnt = Counter()
        for cy in range(edges[b] // 2, edges[b + 1] // 2):
            for cx in range(16):
                key = cellmap[cy][cx]
                cnt[key] += 1
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        for key in keys:
            h.append((Counter(c for row in cells[key]
                              for c in row if c != 0), cnt[key]))
        band_cells.append(keys)
        band_hists.append(h)

    blank_pens = [blank_penalty(gt, edges[k] * 8) for k in range(1, nb)]
    sets, kept = chain_palettes(band_hists, ctx, blank_pens,
                                max_changes=2 if ALLOW_BLANK_SEAMS else 0)
    slots = assign_slots(sets, kept)
    if slots is None:
        # multi-change chains can be slot-infeasible; a 1-change-per-
        # boundary chain always assigns
        sets, kept = chain_palettes(band_hists, ctx, blank_pens,
                                    max_changes=1)
        slots = assign_slots(sets, kept)
    if slots is None:
        return None

    # quantize each band (solid + dithered variants)
    band_q = []
    for b in range(nb):
        q = {}
        for key in band_cells[b]:
            dither_ok = key not in dfix_keys
            q[key] = quantize_cell_band(cells[key], slots[b], ctx,
                                        dither_ok)
        band_q.append(q)
    return {
        "edges": edges, "nb": nb, "slots": slots, "kept": kept,
        "band_cells": band_cells, "band_q": band_q,
    }


def emit_nt_chr(bb, cells, cellmap, ctx, use_dither, share_bounds):
    """Assemble per-band CHR + nametable + attributes from quantized
    cells. use_dither: set of cell keys rendered dithered.

    share_bounds[j] (interior boundary j between bands j and j+1, at tile
    row R = edges[j+1]) marks a pure-CHR boundary that must be TEAR-PROOF:
    every tile index referenced by nametable rows R-1 and R gets byte-
    identical pattern data in BOTH banks at the SAME index. Band j
    allocates rows R-1 and R at its lowest free slots (row R uses band
    j+1's quantization — identical to band j's, since shared boundaries
    only exist where the two bands' palette sets are byte-equal) and band
    j+1 pre-seeds its LUT with those patterns pinned at band j's indices,
    so the IRQ's mid-scanline CHR swap renders the same pixels no matter
    where within (or a full line around) the boundary scanline it lands.
    Slots are allocated lowest-free, so pinned indices never collide,
    stay low for the next band, and waste no capacity.

    Returns (nt8, pal_grid, chr_banks, band_info, over_cap:list);
    band_info carries each band's slot high-water mark."""
    edges, nb = bb["edges"], bb["nb"]
    nt8 = bytearray(960)
    pal_grid = [[0] * 16 for _ in range(15)]
    chr_banks = []
    band_info = []
    over = []
    blank16 = [[0] * 16 for _ in range(16)]
    blank_pat = cell_to_chr(blank16)[0]

    def row_pats(b, r):
        """Left-to-right (tile-pair) patterns of nametable row r under
        band b's quantization, plus the row's cell palette slots."""
        cy, half = r // 2, r & 1
        pairs, slots_ = [], []
        for cx in range(16):
            key = cellmap[cy][cx]
            slot, solid, dith, _ = bb["band_q"][b][key]
            grid = dith if (dith and key in use_dither) else solid
            t4 = cell_to_chr(grid)
            pairs.append(t4[2:] if half else t4[:2])
            slots_.append(slot)
        return pairs, slots_

    pending = {}  # pattern -> index pinned by the boundary above
    for b in range(nb):
        lut = dict(pending)                  # pattern -> index
        used = {i: p for p, i in pending.items()}  # index -> pattern
        free = [0]

        def idx(t, lut=lut, used=used, free=free):
            i = lut.get(t)
            if i is None:
                while free[0] in used:
                    free[0] += 1
                i = free[0]
                lut[t] = i
                used[i] = t
                free[0] += 1
            return i

        idx(blank_pat)   # blank letterbox tile (index 0 unless pinned)
        # tear-proof boundary below: allocate the shared rows R-1..R
        # FIRST so their slots (pinned into the next band too) stay at
        # low indices — appending them last would pin the next band's
        # high-water mark near this band's tile count and waste slots
        share_below = b + 1 < nb and share_bounds[b]
        if share_below:
            for pair in row_pats(b, edges[b + 1] - 1)[0]:  # row R-1
                for t in pair:
                    idx(t)
            for pair in row_pats(b + 1, edges[b + 1])[0]:  # row R
                for t in pair:
                    idx(t)
        for r in range(edges[b], edges[b + 1]):
            pairs, slots_ = row_pats(b, r)
            cy = r // 2
            for cx in range(16):
                nt8[r * 32 + cx * 2] = idx(pairs[cx][0]) & 0xFF
                nt8[r * 32 + cx * 2 + 1] = idx(pairs[cx][1]) & 0xFF
                pal_grid[cy][cx] = slots_[cx]
        # pin rows R-1..R at their (now low) indices for the next band
        pending = {}
        if share_below:
            for r in (edges[b + 1] - 1, edges[b + 1]):
                qb = b if r < edges[b + 1] else b + 1
                for pair in row_pats(qb, r)[0]:
                    for t in pair:
                        pending[t] = lut[t]
        hi = max(used) + 1
        chr_banks.append([used.get(i, blank_pat) for i in range(hi)])
        band_info.append((edges[b], edges[b + 1], hi))
        if hi > MAX_TILES:
            over.append(b)
    return nt8, pal_grid, chr_banks, band_info, over


def main():
    src, sw, sh = load_ega_image()
    scr = compose_screen(src, sw, sh)
    gt = ground_truth_rgb(src, sw, sh)
    for (y0, y1, x0, x1, base, dot) in DITHER_FIX.get(K.EP, []):
        for y in range(y0, y1):
            row = scr[y]
            for x in range(x0, x1):
                if row[x] == base or row[x] == dot:
                    row[x] = dot if (y & 1) and not (x & 1) else base

    ctx = PalCtx()
    cells, usage, cellmap = screen_cells(scr)
    dfix_keys = set()
    for (y0, y1, x0, x1, _b, _d2) in DITHER_FIX.get(K.EP, []):
        for cy in range(y0 // 16, (y1 + 15) // 16):
            for cx in range(x0 // 16, (x1 + 15) // 16):
                if cy < 15 and cx < 16:
                    dfix_keys.add(cellmap[cy][cx])

    avail = chr_headroom_kb()
    max_bands = min(MAX_BANDS, (avail - 1) // 4)
    assert max_bands >= 1, (
        f"only {avail}KB of CHR headroom after CHR_ROM_KB — the lossless "
        f"banded title needs up to {MAX_BANDS * 4 + 1}KB (nbands*4KB + 1KB "
        f"overlay sprites). gen_mmc5_rom.py sizes TOTAL_CHR_KB with a "
        f"stale '+4KB title' reserve; bump that reserve (levels.h "
        f"TOTAL_CHR_KB rounds to the next power of two)")

    # --- candidate partitions, scored by independent-band cluster error
    # plus a boundary penalty preferring strong horizontal art edges ---
    row_tiles = probe_row_tiles(cells, cellmap, ctx)
    # credit-line tile count: appended to the LAST band, so its capacity
    # is reserved throughout the search (the count is independent of the
    # palette the credits end up drawn in — dedup only compares glyph
    # shapes, and the ink color is constant across the strip)
    ncred = len(build_credit_tiles(((15,),) * 4, ctx)[0])
    # The probe is approximate in both directions (its global palette
    # differs slightly from the final one and dithering/backoff move the
    # count), so enumeration is tolerant: the full quantization below is
    # the real feasibility arbiter (shared rows + credit reserve included).
    parts = enumerate_partitions(row_tiles, max_bands, MAX_TILES + 8,
                                 last_reserve=ncred)
    assert parts, "no feasible band partition (tile overflow)"
    hist_cache = {}
    err_cache = {}

    def band_err(a, b):
        if (a, b) not in err_cache:
            cnt = Counter()
            for cy in range(a // 2, b // 2):
                for cx in range(16):
                    cnt[cellmap[cy][cx]] += 1
            h = [(hist_cache.setdefault(k, Counter(
                c for row in cells[k] for c in row if c != 0)), n)
                for k, n in cnt.items()]
            e, _ = cluster_band(h, ctx, iters=6)
            err_cache[(a, b)] = e
        return err_cache[(a, b)]

    def dup_cost(bs):
        """Tiles duplicated across banks by tear-proof boundary sharing
        (probe estimate): row R's tiles the band above doesn't already
        own + row R-1's tiles the band below doesn't. Zero visual cost,
        pure CHR-budget pressure — so boundaries prefer rows whose tiles
        the neighbor band already has (typically quiet/dark rows)."""
        edges = (0,) + bs + (30,)
        d = 0
        for i in range(1, len(edges) - 1):
            r = edges[i]
            above = set().union(*row_tiles[edges[i - 1]:r])
            below = set().union(*row_tiles[r:edges[i + 1]])
            d += len(row_tiles[r] - above) + len(row_tiles[r - 1] - below)
        return d

    DUP_TILE_W = 40.0   # error-units per duplicated tile: light enough
                        # that measured art error still dominates the
                        # boundary choice

    def score(bs):
        """Pruning pre-score: independent-band cluster error + half the
        blank-line damage per boundary (boundaries that end up with a
        zero palette delta won't blank at all — the full evaluation below
        decides that) + the shared-tile duplication pressure."""
        edges = (0,) + bs + (30,)
        e = sum(band_err(edges[i], edges[i + 1])
                for i in range(len(edges) - 1))
        return (e + sum(0.5 * blank_penalty(gt, r * 8) for r in bs)
                + DUP_TILE_W * dup_cost(bs))

    e2n = ctx.e2n

    def blanks_of(bb):
        """Boundary scanlines forced blank by a palette payload (TWO black
        lines Y, Y+1): any boundary where fewer than 4 palettes are kept."""
        out = []
        for k in range(1, bb["nb"]):
            if len(bb["kept"][k]) < 4:
                y = bb["edges"][k] * 8
                out += [y, y + 1]
        return out

    def render_stage(bb, use_dither, blanks=(), sprites=()):
        edges, nb, slots = bb["edges"], bb["nb"], bb["slots"]
        img = [[NES_RGB[0x0F]] * 256 for _ in range(240)]
        for b in range(nb):
            disp = [[NES_RGB[0x0F]]
                    + [NES_RGB[e2n[c]] for c in slots[b][s]]
                    for s in range(4)]
            for cy in range(edges[b] // 2, edges[b + 1] // 2):
                for cx in range(16):
                    key = cellmap[cy][cx]
                    slot, solid, dith, _ = bb["band_q"][b][key]
                    grid = dith if (dith and key in use_dither) else solid
                    for y in range(16):
                        for x in range(16):
                            img[cy * 16 + y][cx * 16 + x] = \
                                disp[slot][grid[y][x]]
        for y in blanks:
            img[y] = [(0, 0, 0)] * 256
        for (ty, tx, grid8, pal_rgb) in sprites:
            for y in range(8):
                for x in range(8):
                    v = grid8[y][x]
                    if v:
                        img[ty * 8 + y][tx * 8 + x] = pal_rgb[v]
        return img

    # --- pick the partition by MEASURED stage-1 error (incl. its blank
    # lines) + a salience surcharge for each blank line ---
    def probe_excess(bs):
        """Probe-estimated worst-band overflow past MAX_TILES, shares and
        credit reserve included (0 = probe-feasible). Orders the full
        evaluation: probe-feasible candidates first (the probe is a good
        but imperfect predictor of the real per-band counts)."""
        edges = (0,) + bs + (30,)
        worst = 0
        for i in range(len(edges) - 1):
            u = set()
            for r in range(edges[i], edges[i + 1]):
                u |= row_tiles[r]
            if edges[i] != 0:
                u |= row_tiles[edges[i] - 1]
            if edges[i + 1] != 30:
                u |= row_tiles[edges[i + 1]]
            worst = max(worst, len(u)
                        + (ncred if edges[i + 1] == 30 else 0))
        return max(0, worst - MAX_TILES)

    parts.sort(key=lambda bs: (probe_excess(bs) > 0, score(bs)))
    scored = []
    tried = 0
    for cand in parts:
        tried += 1
        if tried > 60 and scored:
            break       # keep runtime bounded
        bb = build_bands(cand, cells, cellmap, ctx, dfix_keys, gt)
        if bb is None:
            continue
        nt_s = emit_nt_chr(bb, cells, cellmap, ctx, use_dither=set(),
                           share_bounds=share_flags(bb, e2n))
        if nt_s[4] or nt_s[3][-1][2] + ncred > MAX_TILES:
            continue    # a band overflows 256 tiles (incl. shared rows
                        # and the last band's credit-tile reserve)
        bl = blanks_of(bb)
        img = render_stage(bb, set(), blanks=bl)
        m = measure(img, gt, bb["edges"][:-1])
        sal = sum(blank_penalty(gt, y) for y in bl) / (192 * 256)
        scored.append((m[0] + sal, cand, bb, m))
        if len(scored) >= 6:
            break       # enough feasible candidates measured
    assert scored, "no candidate partition survived full quantization"
    scored.sort(key=lambda t: t[0])
    _, cand, bb, m1 = scored[0]
    edges, nb, slots = bb["edges"], bb["nb"], bb["slots"]
    bands_rows = edges[:-1]
    stage_blanks = blanks_of(bb)

    # --- boundary palette deltas -> payloads + blank lines -------------
    pal_sets = [palbytes(slots[b], e2n) for b in range(nb)]
    boundaries = []       # (Y, chr_off, payload:bytes|None)
    blank_lines = []
    for k in range(1, nb):
        y = edges[k] * 8
        diff = any(pal_sets[k][i] != pal_sets[k - 1][i]
                   for i in range(16))
        payload = None
        if diff:
            # full 16-byte rewrite inside the 2-line forced blank
            payload = bytes(pal_sets[k])
            blank_lines += [y, y + 1]
        boundaries.append((y, 4 * k, payload))
    img1f = render_stage(bb, set(), blanks=blank_lines)
    if blank_lines != stage_blanks:   # kept-but-identical palettes
        m1 = measure(img1f, gt, bands_rows)

    # stage 2: + ordered dithering (with CHR-capacity backoff)
    dith_keys = set()
    for b in range(nb):
        for key in bb["band_cells"][b]:
            _, _, dith, imp = bb["band_q"][b][key]
            if dith is not None and imp > 0:
                dith_keys.add(key)
    shares = share_flags(bb, e2n)
    while True:
        nt8, pal_grid, chr_banks, band_info, over = emit_nt_chr(
            bb, cells, cellmap, ctx, use_dither=dith_keys,
            share_bounds=shares)
        if band_info[-1][2] + ncred > MAX_TILES and nb - 1 not in over:
            over = list(over) + [nb - 1]   # credit reserve counts too
        if not over:
            break
        # un-dither the least-improving cells of the overflowing bands;
        # neighbor bands' cells qualify too — shared boundary rows pull
        # their patterns into the overflowing bank
        cands, seen = [], set()
        for b in over:
            for b2 in (b - 1, b, b + 1):
                if not 0 <= b2 < nb:
                    continue
                for key in bb["band_cells"][b2]:
                    if key in dith_keys and key not in seen:
                        seen.add(key)
                        cands.append((bb["band_q"][b2][key][3], key))
        cands.sort()
        assert cands, "band overflows 256 tiles even undithered"
        for _, key in cands[:4]:
            dith_keys.discard(key)
    img2 = render_stage(bb, dith_keys, blanks=blank_lines)
    m2 = measure(img2, gt, bands_rows)

    # latches (cycle-audited schedule, see src/title.c): a payload switch
    # dispatches one line EARLY (disp = Y-1) so rendering can blank in
    # hblank of Y-1; its $C001 re-strobe reloads at the end of line Y+2
    # (line Y's and Y+1's A12 rises are suppressed by the blank), a pure
    # CHR switch dispatches at Y and reloads at the end of Y+1. The
    # vblank-armed first latch (pre-render reload) is simply disp_1;
    # chained latches are disp_{k+1} - reload_row_k - 1.
    sw_entries = []
    prev_reload = None
    for (y, chroff, payload) in boundaries:
        disp = y - 1 if payload else y
        latch = disp if prev_reload is None else disp - prev_reload - 1
        assert 0 < latch < 240, latch
        # v restore: address of the first rendered line after the blank
        # (Y+2; its fine-y is 2 since band boundaries are tile-aligned)
        coarse = (y + 2) >> 3
        vhi = 0x20 | (coarse >> 3)
        vlo = (coarse << 5) & 0xFF
        if payload:
            entry = bytes([latch, chroff, 1, vhi, vlo]) + payload
        else:
            entry = bytes([latch, chroff, 0, vhi, vlo]) + bytes(16)
        assert len(entry) == 21
        sw_entries.append(entry)
        prev_reload = y + (2 if payload else 1)

    # --- credits into the last band ------------------------------------
    credit_tiles, credit_rows, credit_pal = build_credit_tiles(
        slots[-1], ctx)
    last = chr_banks[-1]
    credit_base = len(last)
    assert credit_base + len(credit_tiles) <= MAX_TILES, "credits overflow"
    clut = {}
    for i, t in enumerate(credit_tiles):
        if t not in clut:
            clut[t] = credit_base + i
        last.append(t)
    for li, (top, bot) in enumerate(credit_rows):
        ntrow = 26 if li == 0 else 28
        x0 = (32 - len(top)) // 2
        for i, tidx in enumerate(top):
            nt8[ntrow * 32 + x0 + i] = credit_base + tidx
        if bot is not None:  # descender row (rows 27/29 are letterbox)
            for i, tidx in enumerate(bot):
                nt8[(ntrow + 1) * 32 + x0 + i] = credit_base + tidx
    for cy in (13, 14):
        for cx in range(16):
            pal_grid[cy][cx] = credit_pal
    band_info[-1] = (band_info[-1][0], band_info[-1][1], len(last))

    # --- tear-proof boundary proof (HARD guarantee, do not weaken) -----
    # For every pure-CHR boundary, every tile index referenced by the
    # nametable rows around the boundary scanline must hold byte-identical
    # pattern data in both adjacent banks: then the IRQ's CHR swap renders
    # identical pixels wherever within lines Y-8..Y+7 it lands (nominal is
    # early in line Y; the prefetch of line Y's first two tiles and a
    # one-line-early/late IRQ on off-nominal emulators are all covered),
    # and the non-atomic R0-then-R1 write pair is covered at any split.
    # Runs on the FINAL banks (after credit tiles) so regressions can't
    # ship. Checked cross-bank at the indices the nametable actually uses.
    shared_stats = []
    for j in range(nb - 1):
        R = edges[j + 1]
        assert 4 <= R <= 24, (
            f"boundary {j} at row {R}: outside the image rows — the "
            f"shared-row scheme doesn't cover letterbox/credit rows")
        if boundaries[j][2] is not None:
            shared_stats.append(None)  # payload boundary: blank hides it
            continue
        idxs = set()
        for r in (R - 1, R):
            for tx in range(32):
                i = nt8[r * 32 + tx]
                a, c2 = chr_banks[j], chr_banks[j + 1]
                assert i < len(a) and i < len(c2) and a[i] == c2[i], (
                    f"boundary {j} (row {R}, scanline {R * 8}): NT row "
                    f"{r} col {tx} tile index {i} is not byte-identical "
                    f"in bands {j} and {j + 1} — CHR-swap tear possible")
                idxs.add(i)
        shared_stats.append(len(idxs))

    # --- attribute table ------------------------------------------------
    attr = bytearray(64)
    for ay in range(8):
        for ax in range(8):
            def pg(cy, cx):
                return pal_grid[cy][cx] if cy < 15 else 0
            attr[ay * 8 + ax] = (pg(ay * 2, ax * 2)
                                 | (pg(ay * 2, ax * 2 + 1) << 2)
                                 | (pg(ay * 2 + 1, ax * 2) << 4)
                                 | (pg(ay * 2 + 1, ax * 2 + 1) << 6))

    # --- sprite overlays -------------------------------------------------
    img2b = img2
    boundary_rows = {y // 8 for y in blank_lines}
    tile_err = {}
    for ty in range(TOP_ROW, TOP_ROW + IMG_H // 8):
        if ty in boundary_rows:
            continue   # blanked/OAM-quirk row: no overlays here
        for tx in range(32):
            t = 0.0
            for y in range(ty * 8, ty * 8 + 8):
                for x in range(tx * 8, tx * 8 + 8):
                    t += _d(gt[y][x], img2b[y][x])
            tile_err[(ty, tx)] = t / 64
    cand_cells = sorted(tile_err.items(), key=lambda kv: -kv[1])[:120]

    # desired NES color per pixel of candidate cells
    nes_keys = list(NES_RGB.keys())

    def nearest_nes(rgb, _c={}):
        if rgb not in _c:
            _c[rgb] = min(nes_keys, key=lambda k: _d(NES_RGB[k], rgb))
        return _c[rgb]

    # cluster 4 sprite palettes over the candidates (maximize gain)
    def cell_gain(ty, tx, pal):
        disp = [NES_RGB[c] for c in pal]
        g = 0.0
        for y in range(ty * 8, ty * 8 + 8):
            for x in range(tx * 8, tx * 8 + 8):
                eb = _d(gt[y][x], img2b[y][x])
                es = min((_d(gt[y][x], d) for d in disp), default=1e9)
                if es < eb:
                    g += eb - es
        return g / 64

    wish = []
    for (ty, tx), e in cand_cells:
        h = Counter()
        for y in range(ty * 8, ty * 8 + 8):
            for x in range(tx * 8, tx * 8 + 8):
                eb = _d(gt[y][x], img2b[y][x])
                if eb > 16:
                    h[nearest_nes(gt[y][x])] += eb
        if h:
            wish.append(((ty, tx), e, h))
    spals = []
    for (_, _, h) in wish:
        t = tuple(c for c, _ in h.most_common(3))
        if t and all(len(set(t) & set(s)) < len(t) for s in spals):
            spals.append(t)
        if len(spals) == 4:
            break
    while len(spals) < 4:
        spals.append((0x0F,))
    for _ in range(6):
        groups = [Counter() for _ in range(4)]
        for (cell, e, h) in wish:
            gains = [cell_gain(cell[0], cell[1], p) for p in spals]
            bi = max(range(4), key=lambda i: gains[i])
            if gains[bi] > 0:
                groups[bi].update(h)
        new = []
        for g in groups:
            if not g:
                new.append((0x0F,))
                continue
            cols = [c for c, _ in g.most_common(8)]
            best = None
            for cmb in combinations(cols, min(3, len(cols))):
                tot = sum(min(_d(NES_RGB[a], NES_RGB[b])
                              for b in cmb) * w for a, w in g.items())
                if best is None or tot < best[0]:
                    best = (tot, cmb)
            new.append(best[1])
        if new == spals:
            break
        spals = new

    # greedy sprite selection under hardware limits, best-gain first
    ranked = []
    for (cell, e, h) in wish:
        gains = [cell_gain(cell[0], cell[1], p) for p in spals]
        pi = max(range(4), key=lambda i: gains[i])
        ranked.append((gains[pi], cell, pi))
    ranked.sort(key=lambda t: -t[0])
    row_count = Counter()
    sprites = []      # (ty, tx, palidx, grid8)
    spr_gain = []
    for (gain, cell, pi) in ranked:
        if len(sprites) >= MAX_SPRITES:
            break
        ty, tx = cell
        if row_count[ty] >= MAX_SPR_PER_ROW:
            continue
        if gain < SPRITE_MIN_GAIN:
            continue
        disp = [NES_RGB[c] for c in spals[pi]]
        grid8 = [[0] * 8 for _ in range(8)]
        used = False
        for y in range(8):
            for x in range(8):
                gy, gx = ty * 8 + y, tx * 8 + x
                eb = _d(gt[gy][gx], img2b[gy][gx])
                bi, bd = 0, eb - 2.0
                for j, d in enumerate(disp):
                    dd = _d(gt[gy][gx], d)
                    if dd < bd:
                        bi, bd = j + 1, dd
                grid8[y][x] = bi
                used = used or bi
        if not used:
            continue
        row_count[ty] += 1
        sprites.append((ty, tx, pi, grid8))
        spr_gain.append((cell, round(gain, 1), pi))

    # sprite CHR (dedupe patterns)
    spr_lut, spr_tiles, oam = {}, [], []
    spal_rgb = [[NES_RGB[0x0F]] + [NES_RGB[c] for c in (list(p) + [0x0F] * 3)[:3]]
                for p in spals]
    for (ty, tx, pi, grid8) in sprites:
        lo, hi = bytearray(8), bytearray(8)
        for y in range(8):
            for x in range(8):
                v = grid8[y][x]
                lo[y] |= (v & 1) << (7 - x)
                hi[y] |= ((v >> 1) & 1) << (7 - x)
        pat = bytes(lo) + bytes(hi)
        if pat not in spr_lut:
            spr_lut[pat] = len(spr_tiles)
            spr_tiles.append(pat)
        oam.append((ty * 8 - 1, spr_lut[pat], pi, tx * 8))
    assert len(spr_tiles) <= 64, len(spr_tiles)

    img3 = render_stage(
        bb, dith_keys, blanks=blank_lines,
        sprites=[(ty, tx, g, spal_rgb[pi]) for (ty, tx, pi, g) in sprites])
    m3 = measure(img3, gt, bands_rows)

    # --- blob + CHR emission ---------------------------------------------
    chr_bin = b""
    for tiles in chr_banks:
        bank = b"".join(tiles)
        chr_bin += bank + b"\0" * (4096 - len(bank))
    spr_off = len(chr_bin) // 1024
    sprbank = b"".join(spr_tiles)
    chr_bin += sprbank + b"\0" * (1024 - len(sprbank))
    assert len(chr_bin) // 1024 <= avail, \
        f"title CHR {len(chr_bin)//1024}KB > headroom {avail}KB"

    spr_palbytes = bytearray()
    for p in spals:
        row = [0x0F] + [c for c in p[:3]]
        row += [0x0F] * (4 - len(row))
        spr_palbytes += bytes(row)

    blob = bytes(nt8) + bytes(attr) + bytes(pal_sets[0]) \
        + bytes(spr_palbytes)
    assert len(blob) == 1056
    blob += bytes([nb - 1])
    for e in sw_entries:
        blob += e
    blob += bytes(21) * (3 - len(sw_entries))
    blob += bytes([spr_off, len(oam)])
    for (sy, t, a, sx) in oam:
        blob += bytes([sy, t, a, sx])

    def carr(name, section, data):
        return (f'__attribute__((used, section("{section}"))) '
                f"const unsigned char {name}[{len(data)}] = {{ "
                + ", ".join(str(b) for b in data) + " };\n")

    srcout = [
        "// generated by gen_title.py — Keen title screen data\n",
        f"// {nb} CHR band(s) of 4KB + 1KB overlay sprites; per-band bg\n",
        "// palettes rewritten at band boundaries inside one forced-blank\n",
        "// scanline (see src/title.c). Blob layout in gen_title.py.\n",
        "extern const unsigned char title_chr[];\n",
        "extern const unsigned char title_blob[];\n",
        carr("title_chr", ".chr_rom.title", chr_bin),
        carr("title_blob", ".prg_rom_12", blob),
    ]
    (GEN / "titledata.c").write_text("".join(srcout))

    # --- final preview from the EMITTED data (nt8/chr/attr/palettes) ----
    img = Image.new("RGB", (256, 240))
    px = img.load()
    for ty in range(30):
        bi = max(i for i, (s, _e, _n) in enumerate(band_info) if s <= ty)
        disp = []
        for s in range(4):
            row = [NES_RGB[pal_sets[bi][s * 4]]]
            row += [NES_RGB[pal_sets[bi][s * 4 + j]] for j in (1, 2, 3)]
            disp.append(row)
        cbase = bi * 4096
        for tx in range(32):
            t = nt8[ty * 32 + tx]
            a = attr[(ty >> 2) * 8 + (tx >> 2)]
            pal = (a >> (((ty >> 1) & 1) * 4 + ((tx >> 1) & 1) * 2)) & 3
            tof = cbase + t * 16
            for y in range(8):
                lo, hi = chr_bin[tof + y], chr_bin[tof + 8 + y]
                for x in range(8):
                    v = ((lo >> (7 - x)) & 1) | (((hi >> (7 - x)) & 1) << 1)
                    px[tx * 8 + x, ty * 8 + y] = disp[pal][v]
    for y in blank_lines:
        for x in range(256):
            px[x, y] = (0, 0, 0)
    for (sy, t, a, sx) in oam:
        tof = spr_off * 1024 + t * 16
        for y in range(8):
            lo, hi = chr_bin[tof + y], chr_bin[tof + 8 + y]
            for x in range(8):
                v = ((lo >> (7 - x)) & 1) | (((hi >> (7 - x)) & 1) << 1)
                if v:
                    px[sx + x, sy + 1 + y] = NES_RGB[
                        spr_palbytes[(a & 3) * 4 + v]]
    img.save(BUILD / "title_preview.png")

    # --- report -----------------------------------------------------------
    print(f"title pic {TITLE_PIC} ({sw}x{sh}) -> {IMG_W}x{IMG_H} @ row "
          f"{TOP_ROW}; CHR headroom {avail}KB")
    print(f"partition rows {list(edges)} "
          f"({len(parts)} feasible candidates scored)")
    for bi2, (s, e, n) in enumerate(band_info):
        pp = ["/".join(str(c) for c in p) or "-" for p in slots[bi2]]
        print(f"  band {bi2}: rows {s:2d}-{e - 1:2d} scanlines {s * 8:3d}-"
              f"{e * 8 - 1:3d} tiles {n:3d} pals(EGA) {pp}")
    for i, (y, chroff, payload) in enumerate(boundaries):
        if payload:
            d = sum(1 for j in range(16)
                    if pal_sets[i + 1][j] != pal_sets[i][j])
            print(f"  boundary {i} @y={y}: {d} palette bytes differ -> "
                  f"2-line forced blank + full 16B rewrite "
                  f"(latch {sw_entries[i][0]})")
        else:
            print(f"  boundary {i} @y={y}: palettes identical -> pure CHR "
                  f"switch, no blank (latch {sw_entries[i][0]}); "
                  f"tear-proof: rows {y // 8 - 1}-{y // 8} share "
                  f"{shared_stats[i]} tile slots byte-identical in both "
                  f"banks (verified)")
    print(f"dithered cells: {len(dith_keys)}")
    print(f"overlay sprites: {len(oam)} using {len(spr_tiles)} tiles, "
          f"pals(NES) {[['%02X' % c for c in p] for p in spals]}")
    if spr_gain:
        print("  " + ", ".join(f"({ty},{tx})+{g}" for (ty, tx), g, _p
                               in spr_gain))
    print("error vs DOS ground truth (mean per-16px-cell RGB distance;"
          " filt = 2x2-box perceptual):")
    for name, m, im in (("band-pals", m1, img1f), ("+dither", m2, img2),
                        ("+sprites", m3, img3)):
        bstr = " ".join(f"b{i}={v:6.2f}" for i, v in enumerate(m[1]))
        print(f"  {name:9s} overall {m[0]:6.2f} filt {measure_filt(im, gt):6.2f}  {bstr}")
    print(f"wrote {GEN/'titledata.c'} and {BUILD/'title_preview.png'}")


if __name__ == "__main__":
    main()
