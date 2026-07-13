#!/usr/bin/env python3
"""Convert extracted Keen 4/5/6 level data into NES-native formats.

Key idea: Keen's foreground (masked) tiles are static overlays on background
tiles, so we composite fg-over-bg per map cell at BUILD time. Each unique
composited 16x16 cell becomes one NES metatile (2x2 CHR tiles + 1 palette).
No runtime masking is needed on the NES.

Levels are split into K (1-4) camera-X REGIONS, each with its own 256-tile
CHR set (one 4KB bank pair) AND its own 256-entry metatile table; the
engine switches both from cam_x. Metatile IDs are region-local slots, so
both budgets scale with K where the level's art is regional. Cells within
ZONE_L/ZONE_R metatile columns of a region boundary can be on screen while
either adjacent region is active, so they may only use shared slots whose
CHR sits at identical indices (with identical data) in both regions.
K is chosen per level by measuring the preview error of each candidate.

Tile ANIMATION (authentic, from TILEINFO): cells whose bg/fg tile chains
animate get their full composite cycle baked as CHR variants. All
phase-varying CHR tiles live in the region's UPPER 2KB (indices 128-255,
the runtime-swapped CHR window); that 2KB is emitted once per phase (F variants), so
the runtime animates everything on screen by cycling one CHR bank
register — zero VRAM writes. F = lcm of the cycle lengths (capped at 8,
falling back to the best-coverage F <= 8); the step rate is the level's
dominant TILEINFO speed (DOS runs per-tile speeds — our simplification).

Per level outputs under assets/converted/ck<ep>/levelNN/:
  chr.bin        - per region: 2KB static lower half + F_r x 2KB upper
                   (R1) animation variants (F_r = 1 if no animated tiles)
  metatiles.bin  - 256 palette bytes, then per region 4x256 planes
                   (tl, tr, bl, br CHR indices)
  map.bin        - width, height (u16 LE) then w*h metatile slots (u8)
  collision.bin  - 256 top codes + 256 flag bytes (global across regions)
  palettes.bin   - 16 bytes: 4 NES bg palettes
  preview.png    - reconstruction rendered THROUGH the NES constraints
  preview_phaseN.png - per animation phase (animated cells redrawn)
  info.json      - stats incl. region start columns + preview error

Also emits assets/converted/ck<ep>/stats.md with the feasibility table.
"""
import json
import struct
import sys
from collections import Counter
from math import gcd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import keenlib as K
from PIL import Image

OUT = K.ROOT / f"assets/converted/ck{K.EP}"
OUT.mkdir(exist_ok=True)

# --- NES master palette (NTSC-ish), index -> RGB ---
NES_PALETTE = {
    0x0F: (0, 0, 0), 0x00: (102, 102, 102), 0x10: (173, 173, 173), 0x20: (255, 254, 255),
    0x01: (0, 42, 136), 0x11: (21, 95, 217), 0x21: (100, 176, 255), 0x31: (192, 223, 255),
    0x02: (20, 18, 167), 0x12: (66, 64, 255), 0x22: (146, 144, 255), 0x32: (211, 210, 255),
    0x03: (59, 0, 164), 0x13: (117, 39, 254), 0x23: (198, 118, 255), 0x33: (232, 200, 255),
    0x04: (92, 0, 126), 0x14: (160, 26, 204), 0x24: (243, 106, 255), 0x34: (251, 194, 255),
    0x05: (110, 0, 64), 0x15: (183, 30, 123), 0x25: (254, 110, 204), 0x35: (254, 196, 234),
    0x06: (108, 6, 0), 0x16: (181, 49, 32), 0x26: (254, 129, 112), 0x36: (254, 204, 197),
    0x07: (86, 29, 0), 0x17: (153, 78, 0), 0x27: (234, 158, 34), 0x37: (247, 216, 165),
    0x08: (51, 53, 0), 0x18: (107, 109, 0), 0x28: (188, 190, 0), 0x38: (228, 229, 148),
    0x09: (11, 72, 0), 0x19: (56, 135, 0), 0x29: (136, 216, 0), 0x39: (207, 239, 150),
    0x0A: (0, 82, 0), 0x1A: (12, 147, 0), 0x2A: (92, 228, 48), 0x3A: (189, 244, 171),
    0x0B: (0, 79, 8), 0x1B: (0, 143, 50), 0x2B: (69, 224, 130), 0x3B: (179, 243, 204),
    0x0C: (0, 64, 77), 0x1C: (0, 124, 141), 0x2C: (72, 205, 222), 0x3C: (181, 235, 242),
    0x2D: (79, 79, 79), 0x1D: (0, 0, 0), 0x3D: (184, 184, 184),
    0x30: (255, 254, 255),  # true display palette
}

# EGA index -> best NES color index (hand-tuned for Keen's look)
EGA_TO_NES = {  # nearest NES color by RGB distance
    0: 0x0F,   # black
    1: 0x02,   # blue
    2: 0x1A,   # green
    3: 0x2C,   # cyan
    4: 0x16,   # red
    5: 0x14,   # magenta
    6: 0x17,   # brown
    7: 0x10,   # light grey (display-exact 173)
    8: 0x00,   # dark grey
    9: 0x12,   # light blue
    10: 0x2A,  # light green (nearer true display)
    11: 0x3C,  # light cyan (Keen sky)
    12: 0x26,  # light red
    13: 0x24,  # light magenta
    14: 0x38,  # yellow
    15: 0x20,  # white
}


def _cdist(a, b):
    return sum((p - q) ** 2 for p, q in zip(a, b)) ** 0.5


# EGA_SUB_COST[a][b]: displayed error when a pixel of EGA color a is shown
# with the NES color that EGA color b maps to, measured against a's own
# NES-mapped ideal (the best this port can show), so the diagonal is ZERO.
# Measuring against raw EGA RGB instead folds the irreducible EGA->NES
# mapping error into every palette decision, making covering a hue look
# cheaper than its true on-screen loss.
EGA_SUB_COST = [
    [_cdist(NES_PALETTE[EGA_TO_NES[a]], NES_PALETTE[EGA_TO_NES[b]])
     for b in range(16)]
    for a in range(16)
]


def load_level(n):
    files = sorted((K.EXT / "maps").glob(f"{n:02d}_*.bin"))
    metas = sorted((K.EXT / "maps").glob(f"{n:02d}_*.json"))
    if not files:
        return None
    meta = json.loads(metas[0].read_text())
    raw = files[0].read_bytes()
    w, h = meta["width"], meta["height"]
    cnt = w * h
    bg = struct.unpack_from(f"<{cnt}H", raw, 0)
    fg = struct.unpack_from(f"<{cnt}H", raw, cnt * 2)
    info = struct.unpack_from(f"<{cnt}H", raw, cnt * 4)
    return meta, bg, fg, info


# item type (info value - 57) -> first sprite chunk of its idle animation;
# type 12 = the episode's quest object (sandwich / council member / keycard)
ITEM_CHUNKS_BY_EP = {
    6: {0: 164, 1: 166, 2: 168, 3: 170,
        4: 150, 5: 152, 6: 154, 7: 156, 8: 158, 9: 160,
        10: 162, 11: 173, 12: 182},
    4: {0: 242, 1: 244, 2: 246, 3: 248,
        4: 227, 5: 229, 6: 231, 7: 233, 8: 235, 9: 237,
        10: 239, 11: 251, 12: 356},  # 12 = council member
    # Keen 5 item sprite chunk numbers: gems A-D 224/226/228/230,
    # 100..5000 pts 210-220, 1UP 222, stunner 233; 12 = the Omegamatic
    # security keycard sprite 207 (info 70)
    5: {0: 224, 1: 226, 2: 228, 3: 230,
        4: 210, 5: 212, 6: 214, 7: 216, 8: 218, 9: 220,
        10: 222, 11: 233, 12: 207},
}
ITEM_CHUNKS = ITEM_CHUNKS_BY_EP[K.EP]
# info-plane value whose cell is the touch-to-complete quest goal; in ck5
# the keycard (70) is not a level-exit but opens the security exit door
# (main.c gives item type 12 per-episode semantics)
QUEST_INFO = {6: 69, 4: 4, 5: 70}[K.EP]


class TileSource:
    """EGA-indexed pixels for composited cells."""

    def __init__(self):
        self.ega = K.EgaGraph()
        self.t16 = {}
        self.t16m = {}
        self.spr = {}

    def sprite_ega(self, chunk):
        """Sprite chunk -> (16x16 EGA grid, mask grid), centered/cropped,
        for compositing items into background metatiles."""
        if chunk in self.spr:
            return self.spr[chunk]
        from PIL import Image as _Im
        img = _Im.open(K.EXT / "gfx" / "sprites" / f"sprite{chunk-K.OFF_SPRITES:03d}.png").convert("RGBA")
        bbox = img.getbbox()
        img = img.crop(bbox) if bbox else img
        w, h = img.size
        grid = [[0] * 16 for _ in range(16)]
        mask = [[1] * 16 for _ in range(16)]
        ox, oy = max(0, (16 - w) // 2), max(0, 16 - h)  # bottom-center
        for y in range(min(h, 16)):
            for x in range(min(w, 16)):
                r, g, b, a = img.getpixel((x, y))
                if a:
                    c = min(range(16), key=lambda i: sum(
                        (p - q) ** 2 for p, q in zip(K.EGA_PALETTE[i], (r, g, b))))
                    grid[oy + y][ox + x] = c
                    mask[oy + y][ox + x] = 0
        self.spr[chunk] = (grid, mask)
        return self.spr[chunk]

    def bg_tile(self, t):
        if t not in self.t16:
            data = self.ega.chunk(K.OFF_TILE16 + t)
            self.t16[t] = None if data is None else K.planar_to_indices(data, 2, 16)[0]
        return self.t16[t]

    def fg_tile(self, t):
        if t not in self.t16m:
            data = self.ega.chunk(K.OFF_TILE16M + t)
            self.t16m[t] = None if data is None else K.planar_to_indices(
                data, 2, 16, nplanes=5, mask_first=True)
        return self.t16m[t]

    def composite(self, bg_t, fg_t, item_chunk=0):
        """16x16 EGA-index grid of fg (and optional item) over bg."""
        base = self.bg_tile(bg_t)
        cell = [row[:] for row in base] if base else [[0] * 16 for _ in range(16)]
        if fg_t:
            f = self.fg_tile(fg_t)
            if f:
                idx, mask = f
                for y in range(16):
                    for x in range(16):
                        if not mask[y][x]:
                            cell[y][x] = idx[y][x]
        if item_chunk:
            grid, mask = self.sprite_ega(item_chunk)
            for y in range(16):
                for x in range(16):
                    if not mask[y][x]:
                        cell[y][x] = grid[y][x]
        return cell


def build_palettes(cells, cell_usage):
    """Choose backdrop + 4x3 NES bg palettes for a level.

    Displayed-RGB-error k-medoids: per-cell color histograms are assigned
    to the palette with the least DISPLAYED error (EGA source vs the NES
    color actually shown, incl. out-of-palette fallbacks), then each palette
    is rebuilt as the exact best <=3-color triple for its assigned pixels.
    This keeps hues alive (e.g. tree orange) that pure lost-pixel counting
    starves."""
    from itertools import combinations
    color_use = Counter()
    hists = {}
    for key, cell in cells.items():
        n = cell_usage[key]
        h = Counter()
        for row in cell:
            for c in row:
                h[c] += 1
        hists[key] = h
        for c, cnt in h.items():
            color_use[c] += cnt * n
    backdrop = color_use.most_common(1)[0][0]

    def cell_err(h, pal):
        t = 0.0
        for c, n in h.items():
            row = EGA_SUB_COST[c]
            best = row[backdrop]
            for p in pal:
                v = row[p]
                if v < best:
                    best = v
            t += n * best
        return t

    weighted = [(h, cell_usage[k]) for k, h in hists.items()]
    # seeds: most-wanted distinct top-3 triples (weighted by shown pixels)
    trip_w = Counter()
    for h, wgt in weighted:
        t = tuple(c for c, _ in h.most_common(4) if c != backdrop)[:3]
        if t:
            trip_w[t] += wgt * sum(h.values())
    pals = []
    for t, _ in trip_w.most_common():
        if all(len(set(t) & set(s)) < max(1, len(t) - 1) for s in pals):
            pals.append(t)
        if len(pals) == 4:
            break
    while len(pals) < 4:
        pals.append(())

    def best_triple(g):
        cols = [c for c, _ in g.most_common(10) if c != backdrop]
        best = (cell_err(g, ()), ())
        for r in (1, 2, 3):
            for cand in combinations(cols, r):
                e = cell_err(g, cand)
                if e < best[0]:
                    best = (e, cand)
        return best[1]

    best_e = None
    for _ in range(12):
        groups = [Counter() for _ in range(4)]
        tot = 0.0
        for h, wgt in weighted:
            errs = [cell_err(h, p) for p in pals]
            bi = min(range(4), key=lambda i: errs[i])
            tot += wgt * errs[bi]
            for c, n in h.items():
                if c != backdrop:
                    groups[bi][c] += n * wgt
        if best_e is not None and tot >= best_e - 1e-6:
            break
        best_e = tot
        pals = [best_triple(g) if g else () for g in groups]

    # under-full palettes get free extra colors: the group's next-best
    # error reducers (a 2-color optimum still has a spare hardware slot)
    groups = [Counter() for _ in range(4)]
    for h, wgt in weighted:
        bi = min(range(4), key=lambda i: cell_err(h, pals[i]))
        for c, n in h.items():
            if c != backdrop:
                groups[bi][c] += n * wgt
    full = []
    for pal, g in zip(pals, groups):
        pal = list(pal)
        while len(pal) < 3:
            cands = [c for c in g if c not in pal and c != backdrop]
            if not cands:
                break
            pal.append(min(cands, key=lambda c: cell_err(g, pal + [c])))
        full.append(pal)
    pals = full

    return backdrop, [sorted(p, key=lambda c: -color_use[c]) for p in pals]


HDR_V3 = 134          # blob header bytes (contract v3); mt region starts here
CHR_BUDGET = 256      # per-REGION background tiles (HUD uses sprite CHR)
REGION_TILE_TARGET = 220  # raw tiles one 256-tile set absorbs comfortably
MAX_REGIONS = 4       # per level (4KB CHR each)
MIN_STRIP_W = 30      # min region width (metatile cols) — must exceed zone
# Boundary zone in metatile columns: cells in [b-ZONE_L, b+ZONE_R] around a
# boundary col b can be on screen (incl. the seam column) while EITHER
# adjacent region is active, given the engine's +-8px switch hysteresis:
# left region draws up to col b+16, right region from col b-1. Such cells
# may only use CHR tiles present in BOTH regions' sets at the SAME index,
# so the mid-scroll bank swap is invisible.
ZONE_L, ZONE_R = 2, 17


M64 = (1 << 64) - 1


def pat_masks(pat):
    """16B 2bpp pattern -> per-index-value 64-bit pixel masks (v = 0..3)."""
    lo = int.from_bytes(pat[:8], "big")
    hi = int.from_bytes(pat[8:], "big")
    return (~(lo | hi) & M64, lo & ~hi, hi & ~lo, lo & hi)


def disp_dist(ma, mb, cd):
    """Displayed-RGB error of showing pattern b in place of pattern a,
    given cd[va][vb] = RGB distance between the palette colors index va
    and vb render as. Hue-aware: an orange-bark pattern replaced by a
    white-speckle pattern costs its full color distance per pixel, while
    bark<->bark merges stay cheap."""
    d = 0.0
    for va in range(4):
        av = ma[va]
        if not av:
            continue
        row = cd[va]
        for vb in range(4):
            if not row[vb]:  # 0.0 on the diagonal for same-palette cd
                continue
            bv = mb[vb]
            if bv:
                c = (av & bv).bit_count()
                if c:
                    d += c * row[vb]
    return d


def merge_pool(pool, budget, dist, use, protect=(), frozen=()):
    """Greedy-merge least-valuable tiles of `pool` into their nearest alive
    neighbor until len(pool | frozen | {0}) <= budget. `frozen` tiles may be
    merge targets but never victims. Returns remap: pool member -> survivor.

    Victim order = usage x merge-cost (displayed distance to the nearest
    ALIVE neighbor) via a lazy-repriced heap: a cost estimated against the
    initial pool goes stale once a texture family collapses, and a static
    ordering then over-merges the dominant texture. `protect` tiles (item
    cells) merge only as a last resort: merging them into common terrain
    garbles pickups."""
    import heapq
    alive = set(pool) | set(frozen) | {0}
    prot = set(protect)

    # nearest-alive estimates run against a deterministic sample of the
    # CURRENT alive set (rebuilt as it shrinks); final targets are exact
    ref = []
    ref_stamp = [0]

    def rebuild_ref():
        r = sorted(alive - {0})
        if len(r) > 384:
            r = r[::len(r) // 320]
        ref[:] = r
        ref_stamp[0] = len(alive)

    rebuild_ref()

    def cost(t):
        best = min((dist(t, o) for o in ref if o != t), default=6400.0)
        return use[t] * (best + 32.0)

    remap = {t: t for t in pool}
    heap = [(1 if t in prot else 0, cost(t), t)
            for t in set(pool) - set(frozen) - {0}]
    heapq.heapify(heap)
    while heap and len(alive) > budget:
        if ref_stamp[0] - len(alive) > 64:
            rebuild_ref()
        tier, c, t = heapq.heappop(heap)
        c2 = cost(t)  # reprice against current alive
        if heap and c2 > c and (tier, c2) > heap[0][:2]:
            heapq.heappush(heap, (tier, c2, t))
            continue
        alive.discard(t)
        # scenery must not adopt item/text art: protected tiles are merge
        # targets only for other protected tiles (or as a last resort)
        cands = [o for o in alive if o != 0
                 and (t in prot or o not in prot)]
        if not cands:
            cands = [o for o in alive if o != 0]
        remap[t] = min(cands, key=lambda o: dist(t, o))

    def final(t):  # resolve chains (a target may be victimized later)
        while remap.get(t, t) != t:
            t = remap[t]
        return t
    fin = {t: final(t) for t in pool}

    # re-elect each group's representative as its usage-weighted medoid:
    # victim ordering favors DISTINCT survivors (high nearest-neighbor
    # distance), which can leave a rare tile representing a whole group.
    # The rep that minimizes the group's displayed error represents its
    # texture class instead.
    frozen = set(frozen)
    groups = {}
    for t, ft in fin.items():
        groups.setdefault(ft, []).append(t)
    for ft, members in groups.items():
        if ft == 0 or ft in frozen or ft not in fin or len(members) < 2:
            continue  # target lives outside the pool: its slot is fixed
        cands = sorted(members, key=lambda t: -use[t])[:8]
        if ft not in prot:  # never elect item art over scenery
            cands = [c for c in cands if c not in prot] or cands
        rep = min(cands,
                  key=lambda c: sum(use[m] * dist(m, c) for m in members))
        if rep != ft:
            for m in members:
                fin[m] = rep
    return fin


def quantize_cell(cell, backdrop, pals, force=None):
    """Pick best palette for a 16x16 cell; return (pal_idx, 2bpp 16x16 grid).

    Palette choice minimizes DISPLAYED error (EGA source color vs the NES
    color actually shown, incl. out-of-palette fallbacks), so a cell's
    dominant hue is never abandoned when a covering palette exists.
    `force` pins the palette index (animation steps of one cell must all
    use the SAME palette: the attribute table never changes with the
    CHR-bank animation phase)."""
    hist = Counter(c for row in cell for c in row if c != backdrop)
    if force is not None:
        best = force
    else:
        best, best_err = 0, None
        for pi, p in enumerate(pals):
            avail = [backdrop] + list(p)
            err = sum(n * min(EGA_SUB_COST[c][d] for d in avail)
                      for c, n in hist.items())
            if best_err is None or err < best_err:
                best, best_err = pi, err
    p = pals[best]
    lut = {backdrop: 0}
    for i, c in enumerate(p):
        lut[c] = i + 1
    cands = [(backdrop, 0)] + [(cc, i + 1) for i, cc in enumerate(p)]
    for c in hist:
        if c not in lut:
            # nearest displayed NES color among {backdrop}+palette
            lut[c] = min(cands, key=lambda t: EGA_SUB_COST[c][t[0]])[1]
    grid = [[lut[c] for c in row] for row in cell]
    return best, grid


def cell_to_chr(grid):
    """Split a quantized 16x16 grid into 4 8x8 CHR tiles (2bpp, NES format)."""
    tiles = []
    for qy in (0, 8):
        for qx in (0, 8):
            lo = bytearray(8)
            hi = bytearray(8)
            for y in range(8):
                for x in range(8):
                    v = grid[qy + y][qx + x]
                    lo[y] |= (v & 1) << (7 - x)
                    hi[y] |= ((v >> 1) & 1) << (7 - x)
            tiles.append(bytes(lo) + bytes(hi))
    return tiles  # TL, TR, BL, BR


def convert_level(n, ts, write=True):
    lv = load_level(n)
    if lv is None:
        return None
    meta, bg, fg, info = lv
    w, h = meta["width"], meta["height"]

    # unique composited cells; items (info 57-68) composite into the bg
    # metatile so they cost zero sprites — pickup rewrites the nametable
    cells = {}
    usage = Counter()
    cellmap = []
    items = []  # (mx, my, type, empty_key)
    titem_keys = set()  # occupied cells of TILE items (CHR protection)
    ti0 = K.TileInfo()

    # cells the camera can never show: the runtime clamps the camera 2
    # tiles inside the map's border ring on all four sides (main.c
    # cam_bounds), so the ring gets ZERO usage weight. Otherwise the
    # border ring's many cells become a favorite merge target and stamp
    # border-art fragments over real visible textures.
    def vis_cell(i):
        x, y = i % w, i // w
        return 1 if 2 <= x <= w - 3 and 2 <= y <= h - 3 else 0

    for i in range(w * h):
        item_chunk = 0
        iv = info[i]
        if 57 <= iv <= 68 or iv == QUEST_INFO:
            t = 12 if iv == QUEST_INFO else iv - 57
            item_chunk = ITEM_CHUNKS[t]
            items.append((i % w, i // w, t, (bg[i], fg[i], 0)))
        elif K.EP == 5 and fg[i] and ti0.top(fg[i]) == 0x39:
            # Keen 5 QED fuse: FG top code 0x39, two cells tall; pogo-
            # breaking every fuse completes the level. Broken art is stashed
            # in the map's corner FG tiles (0,0)/(0,1). Emitted as pseudo-
            # items: type 13 = fuse top (interactive), 14 = fuse bottom
            # (carries the second cell's broken metatile).
            items.append((i % w, i // w, 13, (bg[i], fg[0], 0)))
            items.append((i % w, i // w + 1, 14, (bg[i + w], fg[w], 0)))
        elif fg[i] and 21 <= (ti0.misc(fg[i]) & 0x7F) <= 28:
            # DOS TILE items: FG tiles with misc 21-28 are touch-pickups
            # (the special-tile check maps such a tile to item type
            # misc - 17) — the SAME item-type space 4..11 as the info-plane
            # items (100..5000 points, 1UP, ammo). Most items in every level
            # are this kind. Pickup clears the FG tile (replaced with its
            # empty tile), so the empty variant is the bare background.
            # misc 29 (item 12) never occurs and is excluded: the port gives
            # type 12 quest semantics.
            items.append((i % w, i // w, (ti0.misc(fg[i]) & 0x7F) - 17,
                          (bg[i], 0, 0)))
            titem_keys.add((bg[i], fg[i], 0))
        key = (bg[i], fg[i], item_chunk)
        if key not in cells:
            cells[key] = ts.composite(*key)
        usage[key] += vis_cell(i)
        cellmap.append(key)
    # ensure the "empty" variant of every item cell exists too
    for (_, _, _, ekey) in items:
        if ekey not in cells:
            cells[ekey] = ts.composite(*ekey)
            usage[ekey] += 1

    backdrop, pals = build_palettes(cells, usage)

    # ================= authentic tile animation (TILEINFO) =================
    # Per unique cell key, follow the bg/fg animation chains (tile ->
    # tile+offset -> ... until the chain loops back). The composite cycle is
    # lcm(bg cycle, fg cycle), capped at 8 steps. Phase 0 == the authored
    # map tile, matching the static pipeline.
    def _lcm(a, b):
        return a * b // gcd(a, b)

    anim_steps = {}    # key -> [16x16 EGA grids], len = cycle length (2..8)
    anim_capped = []   # (key, true lcm) where the composite cycle was capped
    speed_votes = Counter()   # anim speed (tics) weighted by cell usage
    len_votes = Counter()     # cycle length weighted by cell usage
    for key in list(cells):
        bg_t, fg_t, item_chunk = key
        bcyc = ti0.bg_anim_cycle(bg_t) or [bg_t]
        fcyc = (ti0.fg_anim_cycle(fg_t) or [fg_t]) if fg_t else [0]
        clen = _lcm(len(bcyc), len(fcyc))
        if clen == 1:
            continue
        if clen > 8:
            anim_capped.append((key, clen))
            clen = 8
        anim_steps[key] = [ts.composite(bcyc[k % len(bcyc)],
                                        fcyc[k % len(fcyc)], item_chunk)
                           for k in range(clen)]
        n_use = usage[key]
        len_votes[clen] += n_use
        for t in bcyc:
            s = ti0.bg_anim_speed(t)
            if s:
                speed_votes[s] += n_use
        if fg_t:
            for t in fcyc:
                s = ti0.fg_anim_speed(t)
                if s:
                    speed_votes[s] += n_use

    # global frame count F = lcm of the cycle lengths; if that exceeds 8,
    # pick the F <= 8 that keeps the most animated cells glitch-free
    # (cells whose cycle length divides F loop perfectly; others hiccup
    # once per F steps when the global phase wraps).
    F = 1
    for L in len_votes:
        F = _lcm(F, L)
    anim_uncovered = []
    if F > 8:
        F = max(range(2, 9),
                key=lambda f: (sum(v for L, v in len_votes.items()
                                   if f % L == 0), -f))
        anim_uncovered = sorted(L for L in len_votes if F % L != 0)
    # DOS animates each tile at its own speed; we run ONE global rate =
    # the dominant (most visible) speed. tics are 70Hz -> NES frames 60Hz.
    dom_speed = speed_votes.most_common(1)[0][0] if speed_votes else 0
    speed_frames = max(3, round(dom_speed * 60 / 70)) if dom_speed else 0

    def key_coll(key):
        f = key[1]
        if not f:
            return (0, 0)
        return (ti0.top(f),
                (1 if ti0.right(f) else 0) | (2 if ti0.bottom(f) else 0)
                | (4 if ti0.left(f) else 0) | (8 if ti0.misc(f) == 3 else 0))

    # quantize each unique cell -> metatile (4 chr tiles + palette)
    chr_lut = {}
    chr_tiles = []

    def chr_index(t):
        if t not in chr_lut:
            chr_lut[t] = len(chr_tiles)
            chr_tiles.append(t)
        return chr_lut[t]

    # animated CHR tiles: one index per distinct per-phase data SEQUENCE
    # (kept apart from static tiles with identical phase-0 data, or a
    # static cell elsewhere would start animating). chr_tiles[i] holds the
    # phase-0 data (used for distances/preview); anim_seq the full cycle.
    anim_lut = {}
    anim_seq = {}   # chr index -> tuple of per-step 16B patterns

    def chr_index_seq(seq):
        if len(set(seq)) == 1:      # static across every phase
            return chr_index(seq[0])
        if seq not in anim_lut:
            anim_lut[seq] = len(chr_tiles)
            anim_seq[len(chr_tiles)] = seq
            chr_tiles.append(seq[0])
        return anim_lut[seq]

    # metatile 0 = blank backdrop
    blank = cell_to_chr([[0] * 16 for _ in range(16)])
    meta_lut = {}
    metatiles = []  # (4 chr idx, pal)

    def metatile_for(key):
        if key in meta_lut:
            return meta_lut[key]
        steps = anim_steps.get(key)
        if steps:
            # one palette for ALL steps (attributes don't animate); chosen
            # on the combined histogram of the whole cycle
            pal_i, _ = quantize_cell([row for g in steps for row in g],
                                     backdrop, pals)
            quads = [cell_to_chr(quantize_cell(g, backdrop, pals,
                                               force=pal_i)[1])
                     for g in steps]
            entry = tuple(chr_index_seq(tuple(q[i] for q in quads))
                          for i in range(4)) + (pal_i,)
        else:
            pal_i, grid = quantize_cell(cells[key], backdrop, pals)
            entry = tuple(chr_index(t) for t in cell_to_chr(grid)) + (pal_i,)
        ekey = entry + key_coll(key)  # same look, different collision: keep apart
        if ekey in metatiles_index:
            meta_lut[key] = metatiles_index[ekey]
        else:
            metatiles_index[ekey] = len(metatiles)
            meta_lut[key] = len(metatiles)
            metatiles.append(entry)
            meta_coll.append(key_coll(key))
        return meta_lut[key]

    metatiles_index = {}
    meta_coll = []
    chr_index(blank[0])  # tile 0 = blank
    mmap = [metatile_for(k) for k in cellmap]
    for (_, _, _, ekey) in items:  # empty variants of item cells
        metatile_for(ekey)
    chr_raw = len(chr_tiles)

    # ================= camera-X regions =================
    # Split the level into K vertical strips; each strip gets its own
    # 256-tile CHR set (4KB) AND its own 256-entry metatile table, both
    # switched at runtime from cam_x. Metatile IDs are region-local slots:
    # cells map to a slot valid in their home region, so both the CHR and
    # the metatile budget scale with K. Cells within a boundary zone use
    # SHARED slots whose tiles sit at identical CHR indices in the two
    # adjacent regions, making the mid-scroll bank swap invisible.
    mmap_raw = mmap
    nraw = len(metatiles)
    rm_use = Counter(mmap_raw)
    protected_rm = set()   # soft: empty item variants
    item_hard = set()      # hard-ish: with-item cells (garbled pickups)
    for (_, _, _, ekey) in items:
        rm_use[meta_lut[ekey]] += 4  # keep empty variants alive
        protected_rm.add(meta_lut[ekey])
    for k in cellmap:
        if len(k) == 3 and k[2]:
            item_hard.add(meta_lut[k])
    for k in titem_keys:  # tile-item art: protect like composited items
        item_hard.add(meta_lut[k])

    import os as _os
    K_max = min(MAX_REGIONS,
                max(1, -(-chr_raw // REGION_TILE_TARGET)),
                max(1, w // MIN_STRIP_W),
                int(_os.environ.get("CONV_KMAX", "9")))  # debug clamp

    def regs_of_key(key):
        s = set()
        for b in key:
            s.update((b, b + 1))
        return s

    use = Counter()
    pal_votes = {}
    for i, m in enumerate(mmap_raw):
        v = vis_cell(i)
        for t in metatiles[m][:4]:
            use[t] += v
            pal_votes.setdefault(t, Counter())[metatiles[m][4]] += v
    for (_, _, _, ekey) in items:
        m = meta_lut[ekey]
        for t in metatiles[m][:4]:
            use[t] += 4
            pal_votes.setdefault(t, Counter())[metatiles[m][4]] += 4
    dom_pal = {t: v.most_common(1)[0][0] for t, v in pal_votes.items()}
    item_chr = set()
    for m in item_hard:
        item_chr.update(metatiles[m][:4])

    # displayed-color machinery for all merge decisions: CD[p][va][vb] =
    # RGB distance between what index values va and vb render as under
    # bg palette p; masks[t] = per-index-value pixel masks of tile t.
    pal_rgb = []
    for p in pals:
        row = [NES_PALETTE[EGA_TO_NES[backdrop]]]
        row += [NES_PALETTE[EGA_TO_NES[c]] for c in p][:3]
        row += [NES_PALETTE[0x0F]] * (4 - len(row))
        pal_rgb.append(row)
    CD = [[[_cdist(pr[va], pr[vb]) for vb in range(4)] for va in range(4)]
          for pr in pal_rgb]
    # cross-palette variant for METATILE merges (the victim's cells adopt
    # the target's palette as well as its patterns): CD2[pa][pb][va][vb]
    CD2 = [[[[_cdist(pra[va], prb[vb]) for vb in range(4)]
             for va in range(4)] for prb in pal_rgb] for pra in pal_rgb]
    masks = [pat_masks(t) for t in chr_tiles]

    def base_dist(a, b):
        if a == b:
            return 0.0
        return disp_dist(masks[a], masks[b], CD[dom_pal.get(a, 0)])

    # apen[t]: displayed value of tile t's ANIMATION = mean per-phase
    # deviation from its phase-0 pattern. Pricing demotion by visual value
    # (not a flat penalty) keeps a subtle shimmer cheap to lose while
    # waterfalls stay expensive to demote.
    apen = {}
    _mask_cache = {}

    def _pm(pat):
        m = _mask_cache.get(pat)
        if m is None:
            m = _mask_cache[pat] = pat_masks(pat)
        return m

    phase_masks = {}   # anim tile -> per-phase masks, aligned to global F
    for t, seq in anim_seq.items():
        cd = CD[dom_pal.get(t, 0)]
        m0 = _pm(seq[0])
        apen[t] = sum(disp_dist(_pm(s), m0, cd) for s in seq[1:]) / len(seq)
        phase_masks[t] = tuple(_pm(seq[p % len(seq)]) for p in range(F))

    rm_cols = [set() for _ in range(nraw)]
    for i, m in enumerate(mmap_raw):
        rm_cols[m].add(i % w)
    for (ix, _, _, ekey) in items:
        rm_cols[meta_lut[ekey]].add(ix)

    def build(K_regions):
        # per-build animation view: a tile can be DEMOTED to static (its
        # phase-0 pattern in every variant) when the R1 window can't hold
        # it or its animation isn't worth a slot. anim_seq itself is
        # shared across the K candidates.
        anim_of = dict(anim_seq)

        def dist(a, b):
            """Directional: cost of showing tile b where a was = mean
            per-phase displayed distance between the two SEQUENCES (a
            static tile is a constant sequence; a demoted anim tile shows
            its phase 0). Exact sequence comparison keeps same-family
            anim merges cheap (drip variant -> drip variant) while
            cross-family ones pay their real per-phase difference. A
            static cell adopting animation pays the target's phase
            deviation on top: spurious motion reads worse than its
            numeric error."""
            sa, sb = anim_of.get(a), anim_of.get(b)
            cd = CD[dom_pal.get(a, 0)]
            if sa is None and sb is None:
                return disp_dist(masks[a], masks[b], cd)
            pa = phase_masks[a] if sa is not None else None
            pb = phase_masks[b] if sb is not None else None
            d = 0.0
            for p in range(F):
                d += disp_dist(pa[p] if pa else masks[a],
                               pb[p] if pb else masks[b], cd)
            d /= F
            if sa is None and sb is not None:
                d += apen[b]  # motion added to a still cell
            return d

        # --- boundary columns: near-even spacing, adjusted to balance
        # per-region raw metatile demand (zone-blocked + region-own)
        def demand_score(bs):
            blocked = [0] * K_regions
            own = [0] * K_regions
            for cols in rm_cols:
                if not cols:
                    continue
                zoned = set()
                regs = set()
                for x in cols:
                    hr = 0
                    inz = False
                    for bi, b in enumerate(bs):
                        if x >= b:
                            hr += 1
                        if b - ZONE_L <= x <= b + ZONE_R:
                            zoned.update((bi, bi + 1))
                            inz = True
                    if not inz:
                        regs.add(hr)
                for r in zoned:
                    blocked[r] += 1
                for r in regs - zoned:
                    own[r] += 1
            tot = [blocked[r] + own[r] for r in range(K_regions)]
            return (max(tot), sum(tot))

        if K_regions > 1:
            import itertools
            spans = []
            for i in range(1, K_regions):
                c0 = w * i // K_regions
                span = max(4, w // (K_regions * 4))
                spans.append(list(range(max(1, c0 - span),
                                        min(w - 1, c0 + span) + 1, 2)))
            bounds = list(min(itertools.product(*spans), key=demand_score))
        else:
            bounds = []
        starts = [0] + bounds

        def home_region(x):
            r = 0
            for b in bounds:
                if x >= b:
                    r += 1
            return r

        col_regs, col_bnds = [], []
        for x in range(w):
            regs = {home_region(x)}
            bs = set()
            for bi, b in enumerate(bounds):
                if b - ZONE_L <= x <= b + ZONE_R:
                    regs.update((bi, bi + 1))
                    bs.add(bi)
            col_regs.append(regs)
            col_bnds.append(bs)

        # every draw site: map cells + item empty variants
        sites = [(i % w, mmap_raw[i]) for i in range(w * h)]
        sites += [(ix, meta_lut[ekey]) for (ix, _, _, ekey) in items]

        # ---- CHR first: per-region 256-tile sets with per-boundary
        # shared groups. Tiles referenced by zone cells must sit at
        # identical indices (with identical data) in both regions adjacent
        # to that boundary, so the mid-scroll bank swap is invisible;
        # interior cells keep pristine tiles where they earn a slot.
        tile_regs = {}
        tile_bnds = {}
        for x, m in sites:
            for t in metatiles[m][:4]:
                if t:
                    tile_regs.setdefault(t, set()).update(col_regs[x])
                    if col_bnds[x]:
                        tile_bnds.setdefault(t, set()).update(col_bnds[x])
        chr_groups = {}
        for t, bs in tile_bnds.items():
            chr_groups.setdefault(tuple(sorted(bs)), []).append(t)
        cgkeys = sorted(chr_groups, key=lambda k: (-len(k), k))
        shared_raw_n = len(tile_bnds)
        cf_r = []
        for r in range(K_regions):
            blocked_raw = sum(len(g) for k, g in chr_groups.items()
                              if r in regs_of_key(k))
            own0 = sum(1 for t, rs in tile_regs.items()
                       if r in rs and t not in tile_bnds)
            cf_r.append(min(1.0, 255.0 / max(1, blocked_raw + own0)))
        cg_budget = {k: max(min(len(chr_groups[k]), 4),
                            int(len(chr_groups[k])
                                * min(cf_r[r] for r in regs_of_key(k))))
                     for k in cgkeys}
        total_b = sum(cg_budget.values())
        if total_b > 224:  # leave room for per-region own tiles
            for k in cgkeys:
                cg_budget[k] = max(min(len(chr_groups[k]), 4),
                                   cg_budget[k] * 224 // total_b)
        remap1 = {}
        cg_alive = {}
        for k in cgkeys:
            g = sorted(chr_groups[k])
            budget = cg_budget[k]
            if len(g) > budget:
                rem = merge_pool(g, budget + 1, dist, use,
                                 protect=item_chr)
            else:
                rem = {t: t for t in g}
            remap1.update(rem)
            cg_alive[k] = sorted(t for t in g if rem[t] == t)
        sh_demoted = sum(1 for t, tt in remap1.items()
                         if t != tt and t in anim_seq and tt not in anim_seq)

        # slot split: indices 128-255 are the R1 ANIMATION window (that
        # 2KB is emitted once per phase). Phase-varying tiles MUST sit
        # there; static tiles prefer the lower half (freeing anim
        # capacity) and spill upward only when the lower half is full
        # (upper statics are simply identical across phases).
        sh_tiles = [t for k in cgkeys for t in cg_alive[k]]

        def sh_upper_need():
            a = sum(1 for t in sh_tiles if t in anim_of)
            return a + max(0, (len(sh_tiles) - a) - 127)

        demote_order = sorted((t for t in sh_tiles if t in anim_seq),
                              key=lambda t: use[t] * (apen[t] + 1.0))
        while sh_upper_need() > 128 and demote_order:
            # lowest animation VALUE (usage x phase deviation) goes static
            del anim_of[demote_order.pop(0)]
            sh_demoted += 1

        sh_lo, sh_hi = [], []
        for k in cgkeys:
            for t in cg_alive[k]:
                (sh_hi if t in anim_of else sh_lo).append(t)
        sh_hi += sh_lo[127:]  # static spill into the upper window
        sh_lo = sh_lo[:127]
        cslot_of = {}
        for i, t in enumerate(sh_lo):
            cslot_of[t] = 1 + i
        for i, t in enumerate(sh_hi):
            cslot_of[t] = 128 + i
        cnext = 1 + len(cslot_of)
        assert len(sh_hi) <= 128, f"level {n}: {len(sh_hi)} shared upper CHR"
        blocked_idx = [set() for _ in range(K_regions)]
        for k in cgkeys:
            for r in regs_of_key(k):
                if r < K_regions:
                    blocked_idx[r].update(cslot_of[t] for t in cg_alive[k])

        region_sets = []
        region_raw = []   # per region: chr index -> raw tile (None = blank)
        region_look = []  # per region: raw tile -> chr index
        region_stats = []
        anim_demoted = [0] * K_regions
        rmasks = []
        for r in range(K_regions):
            demand = {t for t, rs in tile_regs.items() if r in rs}
            placed = {t for k in cgkeys if r in regs_of_key(k)
                      for t in cg_alive[k]}
            own = sorted(demand - placed)
            free = set(range(1, 256)) - blocked_idx[r]
            free_hi = sorted(s for s in free if s >= 128)
            free_lo = sorted(s for s in free if s < 128)
            remap_own = {t: t for t in own}
            # ONE pool for animated + static tiles: an animated victim's
            # best target is often a static (freeze), and restricting anim
            # overflow to anim-only targets forces whole anim families onto
            # unrelated anim art. dist() prices anim freeze/adoption, so
            # mixed merges pick the cheapest outcome.
            navail = len(free_lo) + len(free_hi)
            if len(own) > navail:
                remap_own.update(merge_pool(
                    own, 1 + len(placed) + navail, dist, use,
                    protect=item_chr, frozen=placed))
            own_ok = [t for t in own if remap_own[t] == t]
            # the R1 window holds at most len(free_hi) phase-varying
            # tiles: demote the least-valuable surviving animations to
            # their phase-0 static (no merge, art unchanged at phase 0)
            anim_ok = [t for t in own_ok if t in anim_of]
            if len(anim_ok) > len(free_hi):
                for t in sorted(anim_ok, key=lambda t: use[t]
                                * (apen[t] + 1.0))[:len(anim_ok)
                                                   - len(free_hi)]:
                    del anim_of[t]
                    anim_demoted[r] += 1
                anim_ok = [t for t in own_ok if t in anim_of]
            stat_ok = [t for t in own_ok if t not in anim_of]
            own_ok = anim_ok + stat_ok
            oslot = {t: free_hi[i] for i, t in enumerate(anim_ok)}
            stat_slots = free_lo + free_hi[len(anim_ok):]
            oslot.update({t: stat_slots[i] for i, t in enumerate(stat_ok)})
            anim_demoted[r] += sum(1 for t in own
                                   if t in anim_seq and remap_own[t] != t
                                   and remap_own[t] not in anim_of)
            rmap = {0: 0}
            for t in placed:
                rmap[t] = cslot_of[t]
            for t in own:
                ft = remap_own[t]
                rmap[t] = oslot[ft] if ft in oslot else cslot_of[ft]
            chrset = [chr_tiles[0]] * 256
            slot_raw = [None] * 256
            for t in placed:
                chrset[cslot_of[t]] = chr_tiles[t]
                slot_raw[cslot_of[t]] = t
            for t in own_ok:
                chrset[oslot[t]] = chr_tiles[t]
                slot_raw[oslot[t]] = t
            # invariant: phase-varying tiles only in the R1 window
            assert all(s >= 128 for s in range(256)
                       if slot_raw[s] in anim_of), (n, r)
            region_sets.append(chrset)
            region_raw.append(slot_raw)
            rmasks.append([pat_masks(d) for d in chrset])
            cands = [(o, i) for o, i in rmap.items() if o]
            # fallback lookups must not land scenery on item art either
            cands_np = [c for c in cands if c[0] not in item_chr] or cands
            near_cache = {}

            def look(t, rmap=rmap, cands=cands, cands_np=cands_np,
                     near_cache=near_cache):
                s = rmap.get(t)
                if s is not None:
                    return s
                s = near_cache.get(t)
                if s is None:
                    cs = cands if t in item_chr else cands_np
                    s = min(cs, key=lambda c: dist(t, c[0]))[1]
                    near_cache[t] = s
                return s

            region_look.append(look)
            region_stats.append(dict(
                shared=len(placed), own=len(own_ok), raw=len(demand), mt=0,
                upper=sum(1 for s in range(128, 256)
                          if slot_raw[s] is not None),
                anim_chr=sum(1 for s in range(128, 256)
                             if slot_raw[s] in anim_of)))

        # ---- metatile identities: cells rendered through their region's
        # CHR remap collapse together (the crush consolidates near-twins).
        # Zone cells render through shared CHR, so their identity is the
        # same in both adjacent regions.
        def zone_quad(m):
            return tuple(cslot_of[remap1[t]] if t else 0
                         for t in metatiles[m][:4])

        def own_quad(r, m):
            return tuple(region_look[r](t) if t else 0
                         for t in metatiles[m][:4])

        idents = {}       # (quad, pal, coll) -> dict(use, bounds, regs)
        prot_hard = set()
        prot_soft = set()

        def touch(x, m, weight, hard=False, soft=False):
            pal = metatiles[m][4]
            coll = meta_coll[m]
            q = zone_quad(m) if col_bnds[x] else own_quad(home_region(x), m)
            key = (q, pal, coll)
            d = idents.get(key)
            if d is None:
                d = idents[key] = dict(use=0, bounds=set(), regs=set())
            d["use"] += weight
            d["bounds"] |= col_bnds[x]
            d["regs"] |= col_regs[x]
            if hard or m in item_hard:
                prot_hard.add(key)
            if soft:
                prot_soft.add(key)
            return key

        cell_ident = [touch(i % w, mmap_raw[i], vis_cell(i))
                      for i in range(w * h)]
        item_ident = {}
        for (ix, iy, _, ekey) in items:
            item_ident[(ix, iy)] = touch(ix, meta_lut[ekey], 4, soft=True)

        def valid_regs(m):
            return regs_of_key(tuple(sorted(idents[m]["bounds"]))) \
                | idents[m]["regs"]

        # per-region slot animation view: per-phase masks (None = static)
        # and the displayed value of the slot's animation
        slot_ph = [[phase_masks[rw[s]] if rw[s] in anim_of else None
                    for s in range(256)] for rw in region_raw]
        slot_pen = [[apen[rw[s]] if rw[s] in anim_of else 0.0
                     for s in range(256)] for rw in region_raw]
        _idc = {}

        def ident_dist(rr, a, b):
            """Displayed-RGB error of showing identity b where a was:
            b's patterns under b's PALETTE replace a's (CD2 cross-palette
            table); animated quads compare as full phase sequences like
            dist(), plus the spurious-motion term for still cells."""
            ck = (rr, a, b)
            d = _idc.get(ck)
            if d is not None:
                return d
            cd = CD2[a[1]][b[1]]
            mk = rmasks[rr]
            ph = slot_ph[rr]
            pn = slot_pen[rr]
            same_pal = a[1] == b[1]
            d = 0.0
            for i in range(4):
                sa, sb = a[0][i], b[0][i]
                if sa == sb and same_pal:
                    continue
                pa, pb = ph[sa], ph[sb]
                if pa is None and pb is None:
                    d += disp_dist(mk[sa], mk[sb], cd)
                else:
                    e = 0.0
                    for p in range(F):
                        e += disp_dist(pa[p] if pa else mk[sa],
                                       pb[p] if pb else mk[sb], cd)
                    d += e / F
                    if pa is None and pb is not None:
                        d += pn[sb]  # motion added to a still cell
            _idc[ck] = d
            return d

        def ident_merge(pool, budget, rr, extra=()):
            import heapq
            alive = set(pool)
            extra_set = set(extra)
            by_coll = {}
            for m in set(pool) | extra_set:
                by_coll.setdefault(m[2], []).append(m)
            remap = {m: m for m in pool}

            def mcost(m):  # vs the CURRENT alive set (lazy repricing)
                best = min((ident_dist(rr, m, o) for o in by_coll[m[2]]
                            if o != m and (o in alive or o in extra_set)),
                           default=64000.0)
                return idents[m]["use"] * (best + 32.0)

            def tier(m):
                return 2 if m in prot_hard else 1 if m in prot_soft else 0

            heap = [(tier(m), mcost(m), m) for m in pool]
            heapq.heapify(heap)
            while heap and len(alive) > budget:
                ti, c, victim = heapq.heappop(heap)
                group = [m for m in by_coll.get(victim[2], ())
                         if m != victim and (m in alive or m in extra_set)]
                if not group:
                    continue
                c2 = mcost(victim)
                if heap and c2 > c and (ti, c2) > heap[0][:2]:
                    heapq.heappush(heap, (ti, c2, victim))
                    continue
                if victim not in prot_hard:  # scenery never adopts item art
                    grp2 = [m for m in group if m not in prot_hard]
                    group = grp2 or group
                alive.discard(victim)
                remap[victim] = min(group,
                                    key=lambda m: ident_dist(rr, victim, m))

            def fin(m):
                while remap.get(m, m) != m:
                    m = remap[m]
                return m
            final = {m: fin(m) for m in pool}
            # re-elect group representatives (usage-weighted medoid), as
            # in merge_pool: the group's look should be its dominant
            # texture, not its most-distinct member
            groups = {}
            for m, fm in final.items():
                groups.setdefault(fm, []).append(m)
            for fm, members in groups.items():
                if fm in extra_set or fm not in final or len(members) < 2:
                    continue
                cands = sorted(members, key=lambda m: -idents[m]["use"])[:8]
                if fm not in prot_hard:
                    cands = [c for c in cands if c not in prot_hard] or cands
                rep = min(cands, key=lambda c: sum(
                    idents[m]["use"] * ident_dist(rr, m, c) for m in members))
                if rep != fm:
                    for m in members:
                        final[m] = rep
            for v, tgt in final.items():  # survivors inherit victims' uses
                if tgt != v:
                    idents[tgt]["regs"] |= idents[v]["regs"]
                    idents[tgt]["bounds"] |= idents[v]["bounds"]
            return final, sorted(m for m in pool if final[m] == m)

        mt_groups = {}
        for m, d in idents.items():
            if d["bounds"]:
                mt_groups.setdefault(tuple(sorted(d["bounds"])), []).append(m)
        gkeys = sorted(mt_groups, key=lambda k: (-len(k), k))
        own_pool = [sorted(m for m, d in idents.items()
                           if not d["bounds"] and r in d["regs"])
                    for r in range(K_regions)]

        f_r = []
        for r in range(K_regions):
            blocked_raw = sum(1 for m, d in idents.items()
                              if d["bounds"] and r in valid_regs(m))
            f_r.append(min(1.0, 256.0 /
                           max(1, blocked_raw + len(own_pool[r]))))
        g_budget = {k: max(min(len(mt_groups[k]), 4),
                           int(len(mt_groups[k])
                               * min(f_r[r] for r in regs_of_key(k))))
                    for k in gkeys}
        total_g = sum(g_budget.values())
        if total_g > 224:  # leave slots for per-region own metatiles
            for k in gkeys:
                g_budget[k] = max(min(len(mt_groups[k]), 4),
                                  g_budget[k] * 224 // total_g)
        sh_remap = {}
        g_alive = {}
        for k in gkeys:
            g = mt_groups[k]
            budget = g_budget[k]
            extra = [m for k2 in gkeys if set(k2) > set(k)
                     for m in g_alive[k2]]
            rem, alv = ident_merge(g, budget, rr=k[0], extra=extra)
            sh_remap.update(rem)
            g_alive[k] = alv
        shared_total = sum(len(a) for a in g_alive.values())
        sh_all = [m for k in gkeys for m in g_alive[k]]

        own_remap = []
        own_alive = []
        for r in range(K_regions):
            valid_sh = [m for m in sh_all if r in valid_regs(m)]
            cap = 256 - len(valid_sh)
            rem, alv = ident_merge(own_pool[r], cap, rr=r, extra=valid_sh)
            own_remap.append(rem)
            own_alive.append(alv)

        # feasibility: own identities pack into slots per SIGNATURE
        # (collision + palette are global per slot)
        def sig_of(m):
            return m[2] + (m[1],)

        virgin = 256 - shared_total
        while True:
            need = Counter()
            for r in range(K_regions):
                cnt = Counter(sig_of(m) for m in own_alive[r])
                for sg, c in cnt.items():
                    need[sg] = max(need[sg], c)
            shrinkable = {sg for sg, c in need.items() if c > 1}
            if sum(need.values()) <= virgin or not shrinkable:
                break
            sg = max(shrinkable, key=lambda x: need[x])
            r = max(range(K_regions),
                    key=lambda q: sum(1 for m in own_alive[q]
                                      if sig_of(m) == sg))
            members = [m for m in own_alive[r] if sig_of(m) == sg]
            victim = min(members, key=lambda m: (2 if m in prot_hard else
                                                 1 if m in prot_soft else 0,
                                                 idents[m]["use"]))
            cands = [m for m in members if m != victim]
            cands += [m for m in sh_all
                      if r in valid_regs(m) and sig_of(m) == sg]
            tgt = min(cands, key=lambda o: ident_dist(r, victim, o))
            idents[tgt]["regs"] |= idents[victim]["regs"]
            own_alive[r] = [m for m in own_alive[r] if m != victim]
            own_remap[r] = {kk: (tgt if v == victim else v)
                            for kk, v in own_remap[r].items()}
            own_remap[r][victim] = tgt

        # ---- slot allocation: shared identities first (occupying every
        # region that uses them), then own identities packed by signature
        slot_sig = [None] * 256
        slot_occ = [[None] * 256 for _ in range(K_regions)]
        shared_slot = {}
        next_slot = 0
        for k in gkeys:
            for m in g_alive[k]:
                shared_slot[m] = next_slot
                slot_sig[next_slot] = sig_of(m)
                for r in valid_regs(m):
                    if r < K_regions:
                        slot_occ[r][next_slot] = m
                next_slot += 1
        assert next_slot <= 256, f"level {n}: {next_slot} shared mt slots"

        own_slot = [dict() for _ in range(K_regions)]
        order_all = sorted(((r, m) for r in range(K_regions)
                            for m in own_alive[r]),
                           key=lambda rm_: -idents[rm_[1]]["use"])
        for r, m in order_all:
            sg = sig_of(m)
            placed = None
            for s in range(256):
                if slot_occ[r][s] is None and slot_sig[s] in (None, sg):
                    placed = s
                    break
            if placed is None:
                # merge into the nearest same-collision identity already
                # available in this region
                cands = [o for o in own_slot[r] if o[2] == m[2]]
                cands += [o for o in sh_all
                          if r in valid_regs(o) and o[2] == m[2]]
                assert cands, f"level {n}: metatile slot packing failed"
                tgt = min(cands, key=lambda o: ident_dist(r, m, o))
                idents[tgt]["regs"] |= idents[m]["regs"]
                own_remap[r] = {kk: (tgt if v == m else v)
                                for kk, v in own_remap[r].items()}
                own_remap[r][m] = tgt
                continue
            slot_occ[r][placed] = m
            slot_sig[placed] = sg
            own_slot[r][m] = placed

        import os
        if os.environ.get("CONV_DEBUG"):
            print("  mt groups: " + " ".join(
                f"{k}:{len(mt_groups[k])}->{len(g_alive[k])}" for k in gkeys))
            for r in range(K_regions):
                print(f"  r{r}: own_mt raw={len(own_pool[r])} "
                      f"alive={len(own_alive[r])} placed={len(own_slot[r])} "
                      f"chr[{region_stats[r]['shared']}sh"
                      f"+{region_stats[r]['own']}own"
                      f"/{region_stats[r]['raw']}raw]")

        slot_pal = [0] * 256
        coll_top = [0] * 256
        coll_flags = [0] * 256  # b0 right, b1 bottom, b2 left, b3 deadly
        for s in range(256):
            if slot_sig[s] is not None:
                coll_top[s], coll_flags[s], slot_pal[s] = slot_sig[s]

        def sh_final(m):
            while sh_remap.get(m, m) != m:
                m = sh_remap[m]
            return m

        def final_slot(idt, x):
            if col_bnds[x]:  # zone cell -> shared slot (region-agnostic)
                return shared_slot[sh_final(idt)]
            r = home_region(x)
            m = own_remap[r].get(idt)
            if m is None:  # zone identity reused at an interior cell
                return shared_slot[sh_final(idt)]
            return shared_slot[m] if m in shared_slot else own_slot[r][m]

        mmap = [final_slot(cell_ident[i], i % w) for i in range(w * h)]

        if os.environ.get("CONV_TRACE_CELL"):
            tx, ty = map(int, os.environ["CONV_TRACE_CELL"].split(","))
            i = ty * w + tx
            idt = cell_ident[i]
            r = home_region(tx)
            mraw = mmap_raw[i]
            print(f"TRACE cell({tx},{ty}) K={K_regions} region={r} "
                  f"raw_mt={mraw} quads={metatiles[mraw][:4]} "
                  f"pal={metatiles[mraw][4]} coll={meta_coll[mraw]}")
            print(f"  ident={idt}")
            print(f"  bounds={idents[idt]['bounds']} use={idents[idt]['use']}")
            om = own_remap[r].get(idt)
            print(f"  own_remap -> {om}")
            if om is not None and om != idt:
                print(f"  target use={idents[om]['use']} "
                      f"dist={ident_dist(r, idt, om):.0f}")
                same = [o for o in own_alive[r] if o[2] == idt[2]]
                near = sorted(same, key=lambda o: ident_dist(r, idt, o))[:4]
                print("  nearest alive same-coll:",
                      [(round(ident_dist(r, idt, o)), idents[o]['use'])
                       for o in near])
            print(f"  slot={mmap[i]}")
        item_slot = {}
        for (ix, iy, _, ekey) in items:
            item_slot[(ix, iy)] = final_slot(item_ident[(ix, iy)], ix)

        # per-region tables straight from the occupants
        mt_tabs = []
        for r in range(K_regions):
            mt_tabs.append([slot_occ[r][s][0] if slot_occ[r][s] is not None
                            else (0, 0, 0, 0) for s in range(256)])
            region_stats[r]["mt"] = len(own_slot[r])

        # zone slots must render identically in both adjacent regions
        slot_bounds = [set() for _ in range(256)]
        for i, s in enumerate(mmap):
            slot_bounds[s] |= col_bnds[i % w]
        for (ix, iy, _, _t) in items:
            slot_bounds[item_slot[(ix, iy)]] |= col_bnds[ix]
        for s in range(256):
            for b in slot_bounds[s]:
                assert mt_tabs[b][s] == mt_tabs[b + 1][s], (n, s, b)
                for i in range(4):
                    assert (region_sets[b][mt_tabs[b][s][i]]
                            == region_sets[b + 1][mt_tabs[b][s][i]]), (n, s)
                    # animation variants must match too: same raw tile ->
                    # identical per-phase data at the same index, so the
                    # mid-scroll bank swap stays invisible in every phase
                    assert (region_raw[b][mt_tabs[b][s][i]]
                            == region_raw[b + 1][mt_tabs[b][s][i]]), (n, s)

        return dict(K_regions=K_regions, starts=starts, bounds=bounds,
                    home_region=home_region, col_bnds=col_bnds,
                    col_regs=col_regs, mmap=mmap,
                    item_slot=item_slot, slot_pal=slot_pal,
                    coll_top=coll_top, coll_flags=coll_flags,
                    mt_tabs=mt_tabs, region_sets=region_sets,
                    region_raw=region_raw, anim_demoted=anim_demoted,
                    sh_demoted=sh_demoted, anim_of=anim_of,
                    region_stats=region_stats, shared_total=shared_total,
                    shared_raw_n=shared_raw_n, chr_shared=cnext - 1,
                    slot_sig=slot_sig, own_slot=own_slot, banded=False,
                    B=1, split_rows=[])

    # ===================== vertical CHR banding (v2) =====================
    # Split world ROWS into B (<=4) bands at quiet rows. Contract v2 gives
    # each band its OWN metatile table (per-region ART: tl/tr/bl/br CHR
    # indices, region-specific; per-band DECODE: pal-index/top/flags,
    # region-consistent), plus a per-(region,band) CHR bank swapped by a
    # scanline IRQ. So each band reduces its OWN cells to <=256 metatiles
    # and <=256 CHR tiles INDEPENDENTLY — band 0 spends its budget on
    # sky/upper art, band 3 on ground art, unrelated slot spaces. This gives
    # near-lossless map fidelity where the whole level would overflow 256.
    # B=1 never uses this path (build() reproduces today's bytes exactly);
    # B>1 ships only when it MEASURES better.
    #
    # Tear-proofing: every tile used in a boundary ROW (R-1,R around a split
    # R) or a region-boundary COLUMN is pinned to a GLOBAL CHR index holding
    # byte-identical data in every grid cell that can show it, so the mid-
    # scanline swap is invisible wherever it lands. Hard-asserted at
    # generation.
    BLANK4 = (0, 0, 0, 0)

    def build_grid(base_art, split_rows):
        K = base_art["K_regions"]
        bounds = base_art["bounds"]
        starts = base_art["starts"]
        home_region = base_art["home_region"]
        col_regs = base_art["col_regs"]
        col_bnds = base_art["col_bnds"]
        B = len(split_rows) + 1

        def band_of(y):
            return sum(1 for R in split_rows if y >= R)

        band_bnd = {}                      # row -> set of boundary idx k
        for k, R in enumerate(split_rows):
            band_bnd.setdefault(R - 1, set()).add(k)
            band_bnd.setdefault(R, set()).add(k)

        def row_bands(y):
            bs = {band_of(y)}
            for k in band_bnd.get(y, ()):
                bs.update((k, k + 1))
            return bs

        grid_cells = [(r, j) for r in range(K) for j in range(B)]

        # sites: (col, row, metatile, weight, tag) — tag routes the result
        # back to the map cell / item coordinate.
        sites = [(i % w, i // w, mmap_raw[i], vis_cell(i), ("c", i))
                 for i in range(w * h)]
        for (ix, iy, _t, ekey) in items:
            sites.append((ix, iy, meta_lut[ekey], 4, ("i", ix, iy)))

        def reach(col, row):
            return {(r, j) for r in col_regs[col] for j in row_bands(row)}

        # per-build animation view (demotable), same pricing as build()
        anim_of = dict(anim_seq)

        def dist(a, b):
            sa, sb = anim_of.get(a), anim_of.get(b)
            cd = CD[dom_pal.get(a, 0)]
            if sa is None and sb is None:
                return disp_dist(masks[a], masks[b], cd)
            pa = phase_masks[a] if sa is not None else None
            pb = phase_masks[b] if sb is not None else None
            d = 0.0
            for p in range(F):
                d += disp_dist(pa[p] if pa else masks[a],
                               pb[p] if pb else masks[b], cd)
            d /= F
            if sa is None and sb is not None:
                d += apen[b]
            return d

        # ---- tiles used in a ZONE cell (region OR band boundary) are
        # SHARED: one global CHR index, identical data in every grid cell
        # in reach -> tear-proof. Interior tiles are per-grid-cell.
        shared_t = set()
        tile_reach = {}
        gc_own_use = {gc: Counter() for gc in grid_cells}
        for (col, row, m, wg, tag) in sites:
            rc = reach(col, row)
            zone = len(col_regs[col]) > 1 or len(row_bands(row)) > 1
            for t in metatiles[m][:4]:
                if not t:
                    continue
                tile_reach.setdefault(t, set()).update(rc)
                if zone:
                    shared_t.add(t)
        for (col, row, m, wg, tag) in sites:
            if len(col_regs[col]) > 1 or len(row_bands(row)) > 1:
                continue
            gc = (home_region(col), band_of(row))
            for t in metatiles[m][:4]:
                if t and t not in shared_t:
                    gc_own_use[gc][t] += wg

        sh_anim = sorted(t for t in shared_t if t in anim_of)
        sh_stat = sorted(t for t in shared_t if t not in anim_of)
        while len(sh_anim) > 128:
            t = min(sh_anim, key=lambda t: use[t] * (apen[t] + 1.0))
            del anim_of[t]
            sh_anim.remove(t)
            sh_stat.append(t)
        cslot_of = {}
        for i, t in enumerate(sh_stat[:127]):
            cslot_of[t] = 1 + i
        hi = 128
        for t in sh_anim:
            cslot_of[t] = hi
            hi += 1
        for t in sh_stat[127:]:
            cslot_of[t] = hi
            hi += 1
        if hi > 256:
            return None                    # boundary zones demand too much

        # ---- per grid-cell CHR set + look (index for a raw tile) ----
        grid_sets = {}
        grid_raw = {}
        grid_look = {}
        for gc in grid_cells:
            blocked = {cslot_of[t] for t in shared_t if gc in tile_reach.get(t, ())}
            own = sorted(gc_own_use[gc])
            guse = gc_own_use[gc]
            free = sorted(set(range(1, 256)) - blocked)
            free_lo = [s for s in free if s < 128]
            free_hi = [s for s in free if s >= 128]
            remap = {t: t for t in own}
            navail = len(free)
            if len(own) > navail:
                remap.update(merge_pool(own, 1 + navail, dist,
                                        guse, protect=item_chr))
            own_ok = [t for t in own if remap[t] == t]
            anim_ok = [t for t in own_ok if t in anim_of]
            if len(anim_ok) > len(free_hi):
                for t in sorted(anim_ok, key=lambda t: guse[t]
                                * (apen[t] + 1.0))[:len(anim_ok) - len(free_hi)]:
                    del anim_of[t]
                anim_ok = [t for t in own_ok if t in anim_of]
            stat_ok = [t for t in own_ok if t not in anim_of]
            oslot = {t: free_hi[i] for i, t in enumerate(anim_ok)}
            stat_slots = free_lo + free_hi[len(anim_ok):]
            oslot.update({t: stat_slots[i] for i, t in enumerate(stat_ok)})
            rmap = {0: 0}
            for t in shared_t:
                if gc in tile_reach.get(t, ()):
                    rmap[t] = cslot_of[t]
            for t in own:
                ft = remap[t]
                rmap[t] = oslot[ft] if ft in oslot else rmap.get(ft)
            chrset = [chr_tiles[0]] * 256
            slot_raw = [None] * 256
            for t, s in rmap.items():
                if s is not None and t:
                    chrset[s] = chr_tiles[t]
                    slot_raw[s] = t
            grid_sets[gc] = chrset
            grid_raw[gc] = slot_raw
            cands = [(o, i) for o, i in rmap.items() if o]
            cands_np = [c for c in cands if c[0] not in item_chr] or cands
            nc = {}

            def look(t, rmap=rmap, cands=cands, cands_np=cands_np, nc=nc):
                s = rmap.get(t)
                if s is not None:
                    return s
                s = nc.get(t)
                if s is None:
                    cs = cands if t in item_chr else cands_np
                    s = min(cs, key=lambda c: dist(t, c[0]))[1]
                    nc[t] = s
                return s
            grid_look[gc] = look

        # ---- per-band metatile tables (per-region ART, per-band DECODE) ----
        # coalesce cells into identities: a cell's ART quad is region-local
        # (region-boundary cells use shared/global indices -> region-agnostic
        # quad). Two cells share a slot when their DECODE sig matches and, in
        # every region they overlap, their quad matches. ART is per region,
        # so cells of the same sig in DIFFERENT regions share a slot number
        # (each fills its own region's ART entry).
        idents = {}
        cell_key = {}

        def quad_of(gc, m):
            lk = grid_look[gc]
            return tuple(lk(t) if t else 0 for t in metatiles[m][:4])

        for (col, row, m, wg, tag) in sites:
            j = band_of(row)
            regs = col_regs[col]
            sig = (metatiles[m][4], meta_coll[m])
            # a cell on a region OR band boundary renders through SHARED CHR
            # (global indices) so it stays tear-proof; such identities must
            # never be merged into an interior identity (local indices).
            bnd = len(regs) > 1 or len(row_bands(row)) > 1
            if len(regs) > 1:                         # region boundary: shared
                q = quad_of((min(regs), j), m)        # global indices -> same
                key = (j, frozenset(regs), q, sig)
            else:
                r = home_region(col)
                q = quad_of((r, j), m)
                key = (j, r, q, sig)
            d = idents.get(key)
            if d is None:
                d = idents[key] = dict(j=j, regs=set(regs), quad=q, sig=sig,
                                       use=0, hard=(m in item_hard), bnd=bnd)
            d["use"] += wg
            d["bnd"] = d["bnd"] or bnd
            if m in item_hard:
                d["hard"] = True
            cell_key[tag] = key

        # per-band metatile merge to <=255 identities (collision-preserving):
        # the whole level's ~800 metatiles split across bands, but a tall
        # band (e.g. a dense village) can still exceed 256 slots — merge its
        # least-used identities into the nearest SAME-COLLISION survivor (by
        # displayed ART distance) so collision is never lost and the band
        # still fits. This is the graceful path; quiet-row splits keep most
        # bands well under 256 (near-lossless).
        ident_remap = {}

        def iqdist(a, b):
            ja = idents[a]["j"]
            ra = min(idents[a]["regs"] & set(range(K)) or {0})
            cs = grid_sets[(ra, ja)]
            cd = CD[idents[a]["sig"][0]]
            qa, qb = idents[a]["quad"], idents[b]["quad"]
            return sum(disp_dist(pat_masks(cs[qa[i]]), pat_masks(cs[qb[i]]), cd)
                       for i in range(4))

        for j in range(B):
            bnd_ids = [k for k in idents if idents[k]["j"] == j
                       and idents[k]["bnd"]]
            inter = [k for k in idents if idents[k]["j"] == j
                     and not idents[k]["bnd"]]
            cap = 255 - len(bnd_ids)   # boundary identities are never merged
            if cap < 1:
                return None            # boundary rows alone overflow the band
            if len(inter) <= cap:
                continue
            by_coll = {}
            for k in inter:
                by_coll.setdefault(idents[k]["sig"][1], []).append(k)
            alive = set(inter)
            order = sorted(inter, key=lambda k: (idents[k]["hard"],
                                                 idents[k]["use"]))
            for v in order:
                if len(alive) <= cap:
                    break
                cands = [o for o in by_coll[idents[v]["sig"][1]]
                         if o in alive and o != v]
                if not cands:
                    continue
                tgt = min(cands, key=lambda o: iqdist(v, o))
                ident_remap[v] = tgt
                idents[tgt]["use"] += idents[v]["use"]
                idents[tgt]["regs"] |= idents[v]["regs"]
                alive.discard(v)

        def final_ident(k):
            while k in ident_remap:
                k = ident_remap[k]
            return k

        # None = unassigned ART slot (distinct from BLANK4 (0,0,0,0), which is
        # a VALID quad — a genuinely blank cell). Conflating them let a blank
        # cell's slot read as "empty" and get overwritten -> a boundary tear.
        art_tabs = {gc: [None] * 256 for gc in grid_cells}
        dec_pal = [[0] * 256 for _ in range(B)]
        dec_top = [[0] * 256 for _ in range(B)]
        dec_flags = [[0] * 256 for _ in range(B)]
        for gc in grid_cells:
            art_tabs[gc][0] = BLANK4        # slot 0 = blank in every band
        nslot = [1] * B
        slot_of_key = {}
        overflow = [0] * B

        def art_dist(gc, s, q):
            a = art_tabs[gc][s]
            if a is None:
                return 0.0
            return sum(dist(a[i], q[i]) for i in range(4))

        for j in range(B):
            keys = sorted((k for k in idents
                           if idents[k]["j"] == j and k not in ident_remap),
                          key=lambda k: -idents[k]["use"])
            for k in keys:
                d = idents[k]
                sig = d["sig"]
                q = d["quad"]
                dregs = [r for r in d["regs"] if r < K]
                placed = None
                for s in range(nslot[j]):
                    if dec_pal[j][s] != sig[0] or (dec_top[j][s], dec_flags[j][s]) != sig[1]:
                        continue
                    if all(art_tabs[(r, j)][s] in (None, q) for r in dregs):
                        placed = s
                        break
                if placed is None and nslot[j] < 256:
                    placed = nslot[j]
                    nslot[j] += 1
                    dec_pal[j][placed] = sig[0]
                    dec_top[j][placed], dec_flags[j][placed] = sig[1]
                if placed is None:            # 256 full: merge into nearest
                    cands = [s for s in range(1, 256)
                             if dec_top[j][s] == sig[1][0]
                             and dec_flags[j][s] == sig[1][1]]
                    if not cands:
                        return None           # a collision class has no slot
                    r0 = dregs[0] if dregs else 0
                    placed = min(cands, key=lambda s: art_dist((r0, j), s, q))
                    overflow[j] += 1
                    slot_of_key[k] = placed
                    continue
                for r in dregs:
                    if art_tabs[(r, j)][placed] is None:
                        art_tabs[(r, j)][placed] = q
                slot_of_key[k] = placed
        # unfilled ART slots emit as blank (0,0,0,0)
        for gc in grid_cells:
            art_tabs[gc] = [BLANK4 if a is None else a for a in art_tabs[gc]]

        gmap = [slot_of_key[final_ident(cell_key[("c", i)])]
                for i in range(w * h)]
        item_slot = {(ix, iy): slot_of_key[final_ident(cell_key[("i", ix, iy)])]
                     for (ix, iy, _t, _e) in items}

        # ---- tear-proof asserts ----
        for k, R in enumerate(split_rows):
            for row in (R - 1, R):
                for col in range(w):
                    r = home_region(col)
                    s = gmap[row * w + col]
                    a, b = grid_sets[(r, k)], grid_sets[(r, k + 1)]
                    for idx in art_tabs[(r, k if row == R - 1 else k + 1)][s]:
                        assert a[idx] == b[idx], (n, "band-tear", k, r, col, row)
        # region-boundary ART identical across adjacent regions (same slot)
        for col in range(w):
            if not col_bnds[col]:
                continue
            for row in range(h):
                s = gmap[row * w + col]
                j = band_of(row)
                for bnd in col_bnds[col]:
                    assert art_tabs[(bnd, j)][s] == art_tabs[(bnd + 1, j)][s], \
                        (n, "region-tear", col, row, bnd)

        split_mrow = sorted(split_rows)
        region_band_f = [[F if any(grid_raw[(r, j)][s] in anim_of
                                   for s in range(128, 256)) else 1
                          for j in range(B)] for r in range(K)]
        region_stats = []
        for r in range(K):
            for j in range(B):
                rw = grid_raw[(r, j)]
                region_stats.append(dict(
                    region=r, band=j, slots=nslot[j], overflow=overflow[j],
                    tiles=sum(1 for s in range(1, 256) if rw[s] is not None),
                    anim=sum(1 for s in range(128, 256) if rw[s] in anim_of)))
        art = dict(base_art)
        art.update(dict(banded=True, B=B, split_rows=split_mrow, K_regions=K,
                        home_region=home_region, band_of=band_of,
                        grid_sets=grid_sets, grid_raw=grid_raw,
                        art_tabs=art_tabs, dec_pal=dec_pal, dec_top=dec_top,
                        dec_flags=dec_flags, nslot=nslot, mmap=gmap,
                        item_slot=item_slot, anim_of=anim_of,
                        region_band_f=region_band_f, region_stats=region_stats,
                        mt_slots=max(nslot)))
        return art


    # NES-mapped colors for candidate scoring
    bd_rgb = NES_PALETTE[EGA_TO_NES[backdrop]]
    nes_rows = []
    for p in pals:
        row = [bd_rgb] + [NES_PALETTE[EGA_TO_NES[c]] for c in p][:3]
        row += [NES_PALETTE[0x0F]] * (4 - len(row))
        nes_rows.append(row)
    orig_nes = {}

    def orig_cell_rgb(key):
        g = orig_nes.get(key)
        if g is None:
            g = [[NES_PALETTE[EGA_TO_NES[c]] for c in row]
                 for row in cells[key]]
            orig_nes[key] = g
        return g

    def eval_error(art):
        # mean per-cell RGB error of the would-be preview vs the original.
        # Renders each visible cell through the CHR set that the runtime
        # actually shows there: home region AND (for banded art) home band.
        disp = {}
        banded = art.get("banded")
        hr = art["home_region"]
        bof = art.get("band_of", (lambda y: 0))
        if banded:
            gsets = art["grid_sets"]
            art_tabs = art["art_tabs"]
            dec_pal = art["dec_pal"]
        else:
            rsets = art["region_sets"]
            mt_tabs = art["mt_tabs"]
            slot_pal = art["slot_pal"]

        def disp_rgb(gc, s):
            g = disp.get((gc, s))
            if g is None:
                if banded:
                    quads = art_tabs[gc][s]
                    cs = gsets[gc]
                    pal = dec_pal[gc[1]][s]
                else:
                    quads = mt_tabs[gc[0]][s]
                    cs = rsets[gc[0]]
                    pal = slot_pal[s]
                g = []
                for sy in range(16):
                    row = []
                    d = cs[quads[(sy // 8) * 2]]
                    d2 = cs[quads[(sy // 8) * 2 + 1]]
                    lo, hi = d[sy % 8], d[8 + sy % 8]
                    lo2, hi2 = d2[sy % 8], d2[8 + sy % 8]
                    for sx in range(8):
                        v = ((lo >> (7 - sx)) & 1) | (((hi >> (7 - sx)) & 1) << 1)
                        row.append(nes_rows[pal][v])
                    for sx in range(8):
                        v = ((lo2 >> (7 - sx)) & 1) | (((hi2 >> (7 - sx)) & 1) << 1)
                        row.append(nes_rows[pal][v])
                    g.append(row)
                disp[(gc, s)] = g
            return g

        total = 0.0
        nvis = 0
        cerr = {}
        mm = art["mmap"]
        for i, key in enumerate(cellmap):
            if not vis_cell(i):  # camera can never show the border ring
                continue
            nvis += 1
            gc = (hr(i % w), bof(i // w))
            s = mm[i]
            ck = (key, gc, s)
            e = cerr.get(ck)
            if e is None:
                og = orig_cell_rgb(key)
                dg = disp_rgb(gc, s)
                e = 0.0
                for yy in range(16):
                    orow, drow = og[yy], dg[yy]
                    for xx in range(16):
                        a, b = orow[xx], drow[xx]
                        e += ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2
                              + (a[2] - b[2]) ** 2) ** 0.5
                cerr[ck] = e
            total += e
        return total / (max(1, nvis) * 256)

    # per-world-row unique-tile demand (visible cols): scores candidate
    # band boundaries — a "quiet" row has few unique tiles, so sharing its
    # tiles across the two adjacent bands is nearly free.
    row_tiles = [set() for _ in range(h)]
    row_meta = [set() for _ in range(h)]
    for i in range(w * h):
        if vis_cell(i):
            row_meta[i // w].add(mmap_raw[i])
            for t in metatiles[mmap_raw[i]][:4]:
                if t:
                    row_tiles[i // w].add(t)
    MIN_BAND = 3  # metatile rows (48px)

    def make_splits(B):
        """B-1 ascending split rows: even band spacing, each boundary snapped
        to the QUIETEST row in a small window. Band boundaries are CHR-shared
        across the two adjacent bands (tear-proof), so a boundary on a dense
        row would overflow the shared CHR budget — the quiet snap lands it on
        a sky/dirt gap where sharing is near-free. Even spacing spreads the
        bands so no single band swallows a whole dense stratum (a level whose
        dense region has no quiet row inside it just can't thin-band there and
        stays B=1 by measured error)."""
        counts = [len(row_tiles[y]) for y in range(h)]
        lo, hi = 2, h - 2
        if hi - lo < B * MIN_BAND:
            return None
        splits = []
        prev = lo
        for k in range(1, B):
            target = lo + (hi - lo) * k // B
            win = max(2, (hi - lo) // (B * 3))
            a = max(prev + MIN_BAND, target - win)
            b = min(hi - MIN_BAND, target + win)
            y = target if a > b else min(range(a, b + 1),
                                         key=lambda y: counts[y])
            splits.append(y)
            prev = y
        if len(splits) != B - 1 or any(splits[i] >= splits[i + 1]
                                       for i in range(len(splits) - 1)):
            return None
        return splits

    import os
    best = None
    nb_best = None
    arts = {}
    for Kr in range(1, K_max + 1):
        art = build(Kr)
        arts[Kr] = art
        err = eval_error(art)
        if os.environ.get("CONV_DEBUG"):
            print(f"  K={Kr} B=1: err={err:.2f}")
        # a bigger K must earn its extra 4KB CHR + 1KB PRG tables; all
        # candidates are evaluated so a K=2 plateau can't hide a K=3 win.
        if nb_best is None or err < nb_best[0] - 0.25:
            nb_best = (err, art)
    best = nb_best
    # vertical CHR banding candidates (post-process each base build): try
    # B=2..4 at K=1 and the best non-banded K. Banding costs sum(K*B) CHR
    # banks (ample ROM headroom) plus 1KB header/grid tables, so it must
    # MEASURE better by a margin to ship.
    if not os.environ.get("CONV_NOBAND"):
        band_ks = sorted({1, nb_best[1]["K_regions"]})
        Bmax = int(os.environ.get("CONV_BMAX", "8"))
        # banding only adds CHR banks (the metatile table is shared across
        # bands), so it is CHEAPER than another region and earns a smaller
        # margin than K's 0.25. It is tear-proof and never renders worse
        # than B=1, so any measured gain past this margin is safe to ship.
        BAND_MARGIN = float(os.environ.get("CONV_BAND_MARGIN", "0.10"))
        # the whole metatile region (ART K*B*1024 + DECODE B*768) lives in
        # PRG bank A alongside the 134B header, the h*2 row-offset table and
        # the entity tables (map rows can spill to banks B-D, the mt region
        # cannot). Reject any (K,B) whose mt region won't leave room, so a
        # level gracefully falls back to the largest B that packs.
        # entity records: enemies/plats/doors/blocks/fplats (info-plane spawn
        # values, matching gen_mmc5_level) + items (4B blob record: x,y,type,
        # slot-as-u8) + h*2 row-offset table + margin. Count ONLY the values
        # gen_mmc5_level emits (the info plane also holds non-entity markers).
        _ENEMY = {6: set(range(4, 15)) | {102, 103, 104},
                  4: {22, 43, 44, 21, 14, 47, 48},
                  5: {4, 5, 6, 42, 43, 44, 10, 11, 12}}[K.EP]
        ent_bytes = len(items) * 4 + h * 2 + 32
        for v in info:
            if v in _ENEMY:
                ent_bytes += 4                       # bloog/blet/bab <=4B
            elif 27 <= v <= 30 or (K.EP == 5 and 84 <= v <= 87):
                ent_bytes += 3                       # plats
            elif v == 31 or v == 32:
                ent_bytes += 2                       # blocks / fall-plats
            elif v > 256:
                ent_bytes += 4                       # doors

        def band_fits(Kr, B):
            return HDR_V3 + Kr * B * 1024 + B * 768 + ent_bytes <= 8192
        for Kr in band_ks:
            if Kr > K_max:
                continue
            for B in range(2, Bmax + 1):
                if not band_fits(Kr, B):
                    continue
                splits = make_splits(B)
                if not splits:
                    continue
                art = build_grid(arts[Kr], splits)
                if art is None:
                    continue
                err = eval_error(art)
                if os.environ.get("CONV_DEBUG"):
                    print(f"  K={Kr} B={B} @rows{splits}: err={err:.2f} "
                          f"slots={art['nslot']} "
                          f"worst_tiles={max(s['tiles'] for s in art['region_stats'])} "
                          f"overflow={sum(s['overflow'] for s in art['region_stats'])}")
                if err < best[0] - BAND_MARGIN:
                    best = (err, art)
    art = best[1]
    preview_err = best[0]

    banded = art.get("banded", False)
    K_regions = art["K_regions"]
    starts = art["starts"]
    home_region = art["home_region"]
    mmap = art["mmap"]
    item_slot = art["item_slot"]
    anim_of = art["anim_of"]
    B = art["B"]
    split_rows = art["split_rows"]           # world metatile rows, ascending

    # uniform GRID view: per-(region,band) CHR (grid_sets/grid_raw), per-
    # (region,band) ART table (art_tabs), per-BAND DECODE (dec_pal/top/flags),
    # so emission is identical for banded and single-band. Single-band =
    # ONE band -> art_tabs[(r,0)]=build's mt_tabs[r], decode=global.
    if banded:
        grid_sets = art["grid_sets"]
        grid_raw = art["grid_raw"]
        band_of = art["band_of"]
        region_band_f = art["region_band_f"]
        art_tabs = art["art_tabs"]
        dec_pal, dec_top, dec_flags = art["dec_pal"], art["dec_top"], art["dec_flags"]
    else:
        region_sets = art["region_sets"]
        region_raw = art["region_raw"]
        region_animated = [any(region_raw[r][s] in anim_of
                               for s in range(128, 256))
                           for r in range(K_regions)]
        region_f = [F if a else 1 for a in region_animated]
        band_of = (lambda y: 0)
        grid_sets = {(r, 0): region_sets[r] for r in range(K_regions)}
        grid_raw = {(r, 0): region_raw[r] for r in range(K_regions)}
        region_band_f = [[region_f[r]] for r in range(K_regions)]
        art_tabs = {(r, 0): art["mt_tabs"][r] for r in range(K_regions)}
        dec_pal = [art["slot_pal"]]
        dec_top = [art["coll_top"]]
        dec_flags = [art["coll_flags"]]

    # chr.bin layout: region-major, band-minor. Each grid cell = 2KB static
    # lower half (indices 0-127) + F_(r,j) x 2KB upper (R1) animation
    # variants. region_band_chr[r][0] is band 0 = the region's base, so it
    # equals the legacy region_chr[r] (contract back-compat).
    def gphase(gc, s, p):
        t = grid_raw[gc][s]
        if t is None:
            return chr_tiles[0]
        if t in anim_of:
            seq = anim_of[t]
            return seq[p % len(seq)]
        return chr_tiles[t]

    n_anim_cells = sum(1 for k in cellmap if k in anim_steps)
    # player spawn from info plane (1 = facing right, 2 = facing left)
    spawn = None
    for i in range(w * h):
        if info[i] in (1, 2):
            spawn = (i % w, i // w, 1 if info[i] == 1 else -1)
            break

    mt_slots = max(sum(1 for s in range(256)
                       if any(art_tabs[(r, j)][s] != (0, 0, 0, 0)
                              for r in range(K_regions)))
                   for j in range(B))
    region_stats = art["region_stats"]
    if banded:
        worst_cell = max(s["tiles"] for s in region_stats)
    else:
        worst_cell = max(d["own"] + d["shared"] for d in region_stats)

    stats = dict(num=n, name=meta["name"], width=w, height=h,
                 unique_cells=len(cells), metatiles_raw=nraw,
                 mt_slots=mt_slots,
                 chr_raw=chr_raw, backdrop=backdrop,
                 regions=starts,
                 preview_err=round(preview_err, 2),
                 banded=banded, band_count=B, split_rows=list(split_rows),
                 region_band_f=region_band_f, worst_cell_tiles=worst_cell,
                 region_stats=region_stats,
                 spawn=spawn, palettes=[[c for c in p] for p in pals],
                 anim=dict(
                     F=F, speed_tics=dom_speed, speed_frames=speed_frames,
                     cells=n_anim_cells, keys=len(anim_steps),
                     chr_tiles=len(anim_seq),
                     cyclens={str(k): v for k, v in sorted(len_votes.items())},
                     speeds={str(k): v for k, v
                             in sorted(speed_votes.items())},
                     capped=[[list(k), L] for k, L in anim_capped],
                     uncovered_lens=anim_uncovered,
                     region_band_f=region_band_f))
    if not banded:
        stats["mt_shared"] = art["shared_total"]
        stats["mt_eff"] = art["shared_total"] + sum(len(o) for o in
                                                    art["own_slot"])
        stats["shared_raw"] = art["shared_raw_n"]
        stats["shared_slots"] = art["chr_shared"]

    if write:
        d = OUT / f"level{n:02d}"
        d.mkdir(exist_ok=True)
        chunks = []
        for r in range(K_regions):
            for j in range(B):
                gc = (r, j)
                f = region_band_f[r][j]
                chunks.append(b"".join(grid_sets[gc][:128]))
                chunks.append(b"".join(gphase(gc, s, p)
                                       for p in range(f)
                                       for s in range(128, 256)))
        (d / "chr.bin").write_bytes(b"".join(chunks))
        # metatiles.bin = the FULL v2 blob mt region (contract v2):
        #   ART sets row-major (r*B+j), each 256 tl,tr,bl,br;
        #   then DECODE sets by band j, each 256 pal,top,flags.
        # B=1 -> K*1024 art + 768 decode == today's blob mtdata byte-for-byte.
        mt = bytearray()
        for r in range(K_regions):
            for j in range(B):
                tab = art_tabs[(r, j)]
                for plane in range(4):
                    mt.extend(tab[s][plane] for s in range(256))
        for j in range(B):
            mt.extend(dec_pal[j])
            mt.extend(dec_top[j])
            mt.extend(dec_flags[j])
        (d / "metatiles.bin").write_bytes(bytes(mt))
        (d / "map.bin").write_bytes(struct.pack("<2H", w, h) +
                                    struct.pack(f"<{w*h}B", *mmap))
        palbytes = bytearray()
        bd = EGA_TO_NES[backdrop]
        for p in pals:
            row = [bd] + [EGA_TO_NES[c] for c in p][:3]
            row += [0x0F] * (4 - len(row))
            palbytes += bytes(row)
        (d / "palettes.bin").write_bytes(bytes(palbytes))
        # per-band collision (256 top + 256 flags per band) for collcheck
        collb = bytearray()
        for j in range(B):
            collb.extend(dec_top[j])
            collb.extend(dec_flags[j])
        (d / "collision.bin").write_bytes(bytes(collb))
        itembytes = bytearray()
        for (ix, iy, ityp, ekey) in items:
            itembytes += struct.pack("<BBBH", ix, iy, ityp,
                                     item_slot[(ix, iy)])
        (d / "items.bin").write_bytes(bytes(itembytes))
        (d / "info.json").write_text(json.dumps(stats, indent=1))

        # preview rendered through NES constraints, each cell drawn with the
        # CHR set the runtime shows there (home region AND band)
        img = Image.new("RGB", (w * 16, h * 16))
        px = img.load()
        nes_rgb = []
        for p in pals:
            row = [NES_PALETTE[bd]] + [NES_PALETTE[EGA_TO_NES[c]] for c in p][:3]
            row += [NES_PALETTE[0x0F]] * (4 - len(row))
            nes_rgb.append(row)

        def chr_pix(cs, t, x, y):
            lo, hi = cs[t][y], cs[t][8 + y]
            return ((lo >> (7 - x)) & 1) | (((hi >> (7 - x)) & 1) << 1)
        for cy in range(h):
            gj = band_of(cy)
            for cx in range(w):
                r = home_region(cx)
                m = mmap[cy * w + cx]
                quads = art_tabs[(r, gj)][m]
                pal = dec_pal[gj][m]
                cs = grid_sets[(r, gj)]
                for sy in range(16):
                    for sx in range(16):
                        t = quads[(sy // 8) * 2 + (sx // 8)]
                        v = chr_pix(cs, t, sx % 8, sy % 8)
                        px[cx * 16 + sx, cy * 16 + sy] = nes_rgb[pal][v]
        img.save(d / "preview.png")

        # per-phase previews: copy of the base preview with only the
        # animation-touched cells redrawn from that phase's variant data
        for old in d.glob("preview_phase*.png"):
            old.unlink()
        if F > 1:
            aslots = {gc: {s for s in range(256) if grid_raw[gc][s] in anim_of}
                      for gc in grid_sets}
            redraw = [(cx, cy) for cy in range(h) for cx in range(w)
                      if any(t in aslots[(home_region(cx), band_of(cy))]
                             for t in art_tabs[(home_region(cx), band_of(cy))]
                             [mmap[cy * w + cx]])]
            for p in range(F):
                imgp = img.copy()
                pxp = imgp.load()
                for (cx, cy) in redraw:
                    r = home_region(cx)
                    gc = (r, band_of(cy))
                    m = mmap[cy * w + cx]
                    quads = art_tabs[gc][m]
                    pal = dec_pal[gc[1]][m]
                    for sy in range(16):
                        dat_l = gphase(gc, quads[(sy // 8) * 2], p)
                        dat_r = gphase(gc, quads[(sy // 8) * 2 + 1], p)
                        for sx in range(16):
                            dat = dat_l if sx < 8 else dat_r
                            lo, hi = dat[sy % 8], dat[8 + sy % 8]
                            b = 7 - (sx % 8)
                            v = ((lo >> b) & 1) | (((hi >> b) & 1) << 1)
                            pxp[cx * 16 + sx, cy * 16 + sy] = nes_rgb[pal][v]
                imgp.save(d / f"preview_phase{p}.png")
    return stats


def main():
    ts = TileSource()
    rows = []
    only = [int(a) for a in sys.argv[1:]] or range(19)
    for n in only:
        s = convert_level(n, ts)
        if s:
            rows.append(s)
            if s.get("banded"):
                rs = " ".join(
                    f"r{d['region']}b{d['band']}[{d['tiles']}t/{d['anim']}a]"
                    for d in s["region_stats"])
            else:
                rs = " ".join(
                    f"r{i}[{d['mt']}mt {d['shared']}sh+{d['own']}own/{d['raw']}raw]"
                    for i, d in enumerate(s["region_stats"]))
            a = s["anim"]
            anim_txt = (f" anim[F={a['F']} {a['speed_tics']}tics->"
                        f"{a['speed_frames']}fr cells={a['cells']} "
                        f"chr={a['chr_tiles']} regF={a['region_band_f']}"
                        + (f" capped={len(a['capped'])}" if a['capped'] else "")
                        + (f" uncov={a['uncovered_lens']}"
                           if a['uncovered_lens'] else "") + "]") \
                if a["F"] > 1 else ""
            band_txt = (f" BANDS={s['band_count']}@{s['split_rows']} "
                        f"worst={s['worst_cell_tiles']}t"
                        if s.get("banded") else "")
            eff = (f"{s['mt_eff']:3d}eff({s['mt_shared']}sh)"
                   if not s.get("banded") else f"{s['mt_slots']:3d}slot")
            print(f"level {n:2d} {s['name']:15s} cells={s['unique_cells']:4d} "
                  f"mt={s['metatiles_raw']:4d}->{eff} chr_raw={s['chr_raw']:4d} "
                  f"regions={len(s['regions'])} @cols{s['regions']} err="
                  f"{s['preview_err']}{band_txt} {rs}{anim_txt}")
    md = ["| # | name | size | unique cells | raw mt | mt slots | raw CHR "
          "| regions | bands | err |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for s in rows:
        md.append(f"| {s['num']} | {s['name']} | {s['width']}x{s['height']} "
                  f"| {s['unique_cells']} | {s['metatiles_raw']} "
                  f"| {s['mt_slots']} | {s['chr_raw']} "
                  f"| {len(s['regions'])} | {s['band_count']} "
                  f"| {s['preview_err']} |")
    (OUT / "stats.md").write_text("\n".join(md) + "\n")


if __name__ == "__main__":
    main()
