#!/usr/bin/env python3
"""Emit the MMC5 ExRAM level blob for keen4.

ExRAM model:

  * EVERY distinct 8x8 tile the level needs gets its own CHR (NO merging).
    Tiles are pooled globally (dedup on the 16-byte 2bpp pattern) and laid
    into 4KB banks of 256 tiles.  A cell's full tile index = bank*256 + id.
  * The metatile map is u16 (per 16x16 cell -> metatile 0..N-1), removing
    the 256-metatile cap that crushed Border Village (766 metatiles -> 256).
  * Each 8x8 cell carries its OWN palette (2 ExRAM bits) and CHR bank (6
    ExRAM bits): ExRAM byte = pal<<6 | (bank & 0x3F).

The byte layout is documented in src/level_fmt.h (the engine's contract).
Only the item table's empty-metatile field widens u8->u16 (forced by the u16
metatile space).

Outputs, per level, into assets/converted/ck4/levelNN/:
  mmc5.bin      -- the level blob (header + rows + u16 map + metatile SoA +
                   palette sets + span bounds + entity tables)
  mmc5_chr.bin  -- the unique-tile CHR-ROM (4KB banks, 256 tiles each)

usage: KEEN_EP=4 python tools/gen_mmc5_level.py [level numbers...]  (default 1 2 3 4)
"""
import os
import struct
import sys
from collections import Counter
from pathlib import Path

os.environ.setdefault("KEEN_EP", "4")
sys.path.insert(0, str(Path(__file__).parent))
import convert_nes as C  # noqa: E402
import keenlib as KL  # noqa: E402

EP = int(os.environ["KEEN_EP"])
ROOT = Path(__file__).resolve().parent.parent
assert EP in (4, 5, 6), "MMC5 emit supports keen4/5/6 (Galaxy engine)"

# --- blob header (see src/level_fmt.h "MMC5 EXRAM LEVEL FORMAT") ---
MAGIC = 0x4D           # 'M'
VERSION = 2
HDR = 88               # fixed header size; section offsets are u32 from blob
                       # start (data spans multiple 8KB PRG banks).

# metatile table is STRUCT-OF-ARRAYS: 10 parallel u8 arrays of N entries in
# this order.  Indexing a field = field_base + mt_index (16-bit add, no
# runtime multiply): the engine precomputes the 10 field bases once at load
# (mt_base + k*N), one base pointer per field.
MT_FIELDS = ["tl", "tr", "bl", "br",           # nametable tile-id (u8)
             "tl_ex", "tr_ex", "bl_ex", "br_ex",  # ExRAM byte pal<<6|bank
             "top", "flags"]                    # collision (unchanged)


def quantize_8(grid8, backdrop, pals, force=None):
    """Pick the best of the 4 palettes for one 8x8 EGA grid and return
    (pal_idx 0..3, 16-byte 2bpp NES pattern): displayed-error palette choice
    plus nearest-slot mapping, at 8x8 granularity.
    `force` pins the palette index (all animation phases of one cell share a
    palette so only the CHR bank changes across the cycle)."""
    hist = Counter(c for row in grid8 for c in row if c != backdrop)
    if force is not None:
        best = force
    else:
        best, best_err = 0, None
        for pi, p in enumerate(pals):
            avail = [backdrop] + list(p)
            err = sum(n * min(C.EGA_SUB_COST[c][d] for d in avail)
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
            lut[c] = min(cands, key=lambda t: C.EGA_SUB_COST[c][t[0]])[1]
    lo = bytearray(8)
    hi = bytearray(8)
    for y in range(8):
        for x in range(8):
            v = lut[grid8[y][x]]
            lo[y] |= (v & 1) << (7 - x)
            hi[y] |= ((v >> 1) & 1) << (7 - x)
    return best, bytes(lo) + bytes(hi)


def build_cells(n, ts):
    """Build the level's cells and item records from the EGA source.  Returns
    everything the MMC5 emit needs: composited cells, the per-map-cell key
    list, item records (with their empty-cell key), and the collision
    (top,flags) per key."""
    meta, bg, fg, info = C.load_level(n)
    w, h = meta["width"], meta["height"]
    ti0 = KL.TileInfo()

    cells = {}
    usage = Counter()
    cellmap = []
    items = []            # (mx, my, type, empty_key)
    for i in range(w * h):
        item_chunk = 0
        iv = info[i]
        if 57 <= iv <= 68 or iv == C.QUEST_INFO:
            t = 12 if iv == C.QUEST_INFO else iv - 57
            item_chunk = C.ITEM_CHUNKS[t]
            items.append((i % w, i // w, t, (bg[i], fg[i], 0)))
        # Keen 4 lifewater drops are TILEINFO misc=4 foreground tiles, not
        # INFO-plane sprite items.  They use the same touch-and-clear
        # mechanism as candy, but feed their own persistent 0..99 counter
        # (100 drops = one extra Keen).  Type 15 is NES-private; 13/14 are
        # already reserved for CK5 fuse pseudo-items.
        elif fg[i] and (ti0.misc(fg[i]) & 0x7F) == 4:
            items.append((i % w, i // w, 15, (bg[i], 0, 0)))
        elif fg[i] and 21 <= (ti0.misc(fg[i]) & 0x7F) <= 28:
            items.append((i % w, i // w, (ti0.misc(fg[i]) & 0x7F) - 17,
                          (bg[i], 0, 0)))
        key = (bg[i], fg[i], item_chunk)
        if key not in cells:
            cells[key] = ts.composite(*key)
        # camera clamps 2 tiles inside the border ring -> ring cells weightless
        x, y = i % w, i // w
        usage[key] += 1 if 2 <= x <= w - 3 and 2 <= y <= h - 3 else 0
        cellmap.append(key)
    for (_, _, _, ekey) in items:
        if ekey not in cells:
            cells[ekey] = ts.composite(*ekey)
            usage[ekey] += 1

    # --- gem holders + gem doors (placing a gem in its holder opens the
    # matching door) and plat/bridge switches (up-press toggles platform or
    # bridge). The FG misc flag identifies the tile; the INFO plane at that
    # cell carries the target (destX<<8 | destY). Swapping T -> T +
    # TI_ForeAnimTile(T) toggles to the next tile in the chain (holder +18,
    # door +1, switch +1). The swapped variants are composited here so
    # emit_level can bake them into the metatile table. ---
    def addkey(k):
        if k not in cells:
            cells[k] = ts.composite(*k)
            usage[k] += 1
        return k

    gemholders = []
    switches = []
    for i in range(w * h):
        f = fg[i]
        if not f:
            continue
        m = ti0.misc(f) & 0x7F
        x, y = i % w, i // w
        if 7 <= m <= 10:                     # gem-holder misc codes 7..10
            tv = info[i]
            dx, dy = tv >> 8, tv & 0xFF
            if not (2 <= dx < w and 2 <= dy < h):
                continue                     # bad holder target -> skip
            dfg = fg[dy * w + dx]
            dh = 0
            while dy + dh < h and fg[(dy + dh) * w + dx] == dfg:
                dh += 1
            placed_key = addkey((bg[i], f + ti0.fg_anim_offset(f), 0))  # +18
            open_key = addkey((bg[dy * w + dx],
                               dfg + ti0.fg_anim_offset(dfg), 0))       # +1
            gemholders.append((x, y, m - 7, placed_key, dx, dy, dh, open_key))
        elif m in (5, 6, 15):                # SWITCHPLATON/OFF/BRIDGE
            tv = info[i]
            tx, ty = tv >> 8, tv & 0xFF
            off_key = (bg[i], f, 0)          # current switch art (already baked)
            on_key = addkey((bg[i], f + ti0.fg_anim_offset(f), 0))      # +1
            switches.append((x, y, off_key, on_key, tx, ty))

    def key_coll(key):
        f = key[1]
        if not f:
            return (0, 0)
        # flags: bit0 right-solid, bit1 bottom-solid, bit2 left-solid,
        # bit3 deadly (misc==3), bit4 POLE/climbable (misc==1), bit5 GEMHOLDER
        # (misc 7..10), bit6 SWITCH (misc 5/6/15). The holder/switch bits gate
        # player.c's per-tic gem-place / Up-press-switch scans.
        m = ti0.misc(f) & 0x7F
        return (ti0.top(f),
                (1 if ti0.right(f) else 0) | (2 if ti0.bottom(f) else 0)
                | (4 if ti0.left(f) else 0) | (8 if ti0.misc(f) == 3 else 0)
                | (0x10 if m == 1 else 0)
                | (0x20 if 7 <= m <= 10 else 0)
                | (0x40 if m in (5, 6, 15) else 0))

    return dict(meta=meta, w=w, h=h, bg=bg, fg=fg, info=info,
                cells=cells, usage=usage, cellmap=cellmap, items=items,
                key_coll=key_coll, gemholders=gemholders, switches=switches)


def _lcm(a, b):
    from math import gcd
    return a * b // gcd(a, b)


def compute_anim(cells, usage, ts):
    """Authentic background-tile animation from TILEINFO (id Galaxy chains),
    for the MMC5 per-8x8 model.

    Returns (anim_steps, F, speed_frames):
      anim_steps  {cell key -> [16x16 EGA composite grids], one per phase},
                  cycle length = lcm(bg cycle, fg cycle) capped at 8; phase 0
                  == the authored map cell.
      F           global phase-bank count for the level: lcm of all cycle
                  lengths, or (if that exceeds 8) the F in 2..8 keeping the
                  most animated cells glitch-free.  Each animated cell's CHR
                  occupies F consecutive 4KB banks (phase k of a length-L cell
                  in bank base + (k mod L)); the engine cycles one global phase
                  counter and adds it to the cell's ExRAM bank byte, so ALL
                  on-screen animated cells step together for a handful of ExRAM
                  writes/tick.
      speed_frames dominant TILEINFO speed (tics) -> NES 60Hz frames/step.
    """
    ti0 = KL.TileInfo()
    anim_steps = {}
    speed_votes = Counter()
    len_votes = Counter()
    for key in list(cells):
        bg_t, fg_t, item_chunk = key
        bcyc = ti0.bg_anim_cycle(bg_t) or [bg_t]
        fcyc = (ti0.fg_anim_cycle(fg_t) or [fg_t]) if fg_t else [0]
        clen = _lcm(len(bcyc), len(fcyc))
        if clen == 1:
            continue
        if clen > 8:
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
    F = 1
    for L in len_votes:
        F = _lcm(F, L)
    if F > 8:
        F = max(range(2, 9),
                key=lambda f: (sum(v for L, v in len_votes.items()
                                   if f % L == 0), -f))
    dom_speed = speed_votes.most_common(1)[0][0] if speed_votes else 0
    speed_frames = max(3, round(dom_speed * 60 / 70)) if dom_speed else 0
    return anim_steps, F, speed_frames


def collect_map_nodes(w, bg, info):
    """World-map (GAMEMAPS level 0) info-plane nodes.

    enter  : info in [0xC001, 0xC012]  -> A/B enters that game level
    fence  : high byte 0xD0, low = level  -> FG removed when level done
    flag   : high byte 0xF0, low = level  -> flag holder when level done
    """
    enters, fences, flags = [], [], []
    for i, v in enumerate(info):
        x, y = i % w, i // w
        lo, hi = v & 0xFF, v >> 8
        if 0xC001 <= v <= 0xC012:
            enters.append((x, y, v - 0xC000))
        elif hi == 0xD0 and 1 <= lo <= 17:
            fences.append((x, y, lo, bg[i]))  # bg tile for empty-mt bake
        elif hi == 0xF0 and 1 <= lo <= 17:
            flags.append((x, y, lo))
    enters.sort(key=lambda e: (e[2], e[0], e[1]))
    fences.sort(key=lambda e: (e[2], e[0], e[1]))
    flags.sort(key=lambda e: (e[2], e[0], e[1]))
    return enters, fences, flags


def build_entities(n, w, h, info):
    """Episode-aware enemy/platform tables from the Galaxy info plane.

    The three runtime enemy slots are deliberately generic: walker, secondary
    ground/flying actor, and tertiary actor.  Their concrete meaning is chosen
    by EPISODE in actors.c and must use that episode's ScanInfoLayer values.

    Level 0 is the world map: no combat entities. High info values there are
    map markers (0xC0xx enter / 0xD0xx fence / 0xF0xx flag), not door teleports.
    """
    if n == 0:
        return [], [], [], [], [], [], []

    def cells_of(pred):
        return [(i % w, i // w, v) for i, v in enumerate(info) if pred(v)]

    if EP == 4:
        wmap = {22: 0, 43: 1, 44: 2}                # Poison Slug
        bamap = {14: 0, 47: 1, 48: 2}               # Lick
        # K4 slot 2 carries a critter TYPE in byte 2.
        slot2 = {21: (0, 0),                         # Mad Mushroom
                 8: (1, 0), 45: (1, 1), 46: (1, 2), # Skypest
                 12: (2, 0),                         # Bounder
                 7: (3, 0), 51: (3, 1), 52: (3, 2), # Wormmouth
                 19: (4, 0)}                         # Mimrock
        bloogs = [(x, y, wmap[v]) for x, y, v in cells_of(lambda v: v in wmap)]
        blets = [(x, y, slot2[v][0], slot2[v][1])
                 for x, y, v in cells_of(lambda v: v in slot2)]
        babs = [(x, y, bamap[v]) for x, y, v in cells_of(lambda v: v in bamap)]
    elif EP == 5:
        wmap = {4: 0, 5: 1, 6: 2}                   # Sparky
        blmap = {42: 0, 43: 1, 44: 2}               # Ampton
        bamap = {10: 0, 11: 1, 12: 2}               # Slicestar North
        bloogs = [(x, y, wmap[v]) for x, y, v in cells_of(lambda v: v in wmap)]
        blets = [(x, y, 0, blmap[v]) for x, y, v in cells_of(lambda v: v in blmap)]
        babs = [(x, y, bamap[v]) for x, y, v in cells_of(lambda v: v in bamap)]
    else:
        wmap = {4: 0, 5: 1, 6: 2}                   # Bloog
        blmap = {v: 0 for v in range(7, 15)}         # Blooglet colors
        bamap = {102: 0, 103: 1, 104: 2}             # Babobba
        bloogs = [(x, y, wmap[v]) for x, y, v in cells_of(lambda v: v in wmap)]
        blets = [(x, y, (v - 7) & 3, blmap[v])
                 for x, y, v in cells_of(lambda v: v in blmap)]
        babs = [(x, y, bamap[v]) for x, y, v in cells_of(lambda v: v in bamap)]
    plats = [(x, y, v - 27) for x, y, v in cells_of(lambda v: 27 <= v <= 30)]
    if EP == 5:
        plats += [(x, y, v - 84) for x, y, v in cells_of(lambda v: 84 <= v <= 87)]
    fplats = [(x, y) for x, y, v in cells_of(lambda v: v == 32)]
    blocks = [(x, y) for x, y, v in cells_of(lambda v: v == 31)]
    doors = [(x, y, v >> 8, v & 0xFF) for x, y, v in cells_of(lambda v: v > 0x100)]
    for tbl in (bloogs, blets, babs, plats, fplats, blocks, doors):
        tbl.sort(key=lambda e: e[0])
    return bloogs, blets, babs, plats, fplats, blocks, doors


# ---------------------------------------------------------------------------
# Per-span palette sets (camera-X spans, each loading its own best 4-palette
# set).  PALETTE-ONLY: this changes the ExRAM palette bits + emits PALSETS[K] /
# SPANBOUNDS; CHR/collision/tile assignment are untouched.  keen4 measures out
# to n_palsets=1 for every demo level, but the machinery is real and exercised
# (KEEN_FORCE_SPANS) for future multi-biome levels.
# ---------------------------------------------------------------------------
SPAN_THRESHOLD = 0.05     # min net displayed-err reduction to ship >1 set (5%)
SPAN_OZ = 16              # boundary tear-zone half-width (metatile columns)
SPAN_KMAX = 4             # max palette sets considered


def span_of_col(x, cuts):
    """Span index of world metatile column x. cuts = [0, b1, .., b_{K-1}, w]."""
    s = 0
    while s < len(cuts) - 2 and x >= cuts[s + 1]:
        s += 1
    return s


def _balanced_bounds(col_ink, w, K):
    """K-1 world-column cuts splitting interior "ink" (weighted shown pixels)
    evenly across K spans -- deterministic candidate boundaries."""
    if K <= 1:
        return []
    tot = sum(col_ink)
    if tot == 0:
        return []
    bounds, acc = [], 0
    for x in range(w):
        acc += col_ink[x]
        if acc >= (len(bounds) + 1) * tot / K and len(bounds) < K - 1:
            bounds.append(x)
    return bounds


def _ega_cols(bd, pals):
    out = []
    for p in pals:
        cols = [bd] + list(p)[:3]
        cols += [bd] * (4 - len(cols))
        out.append(cols)
    return out


def _cell8_err(sub, ega):
    """min displayed err over the 4 palettes for one 8x8 grid + pixel count."""
    hist = Counter(c for row in sub for c in row)
    best = min(sum(n * min(C.EGA_SUB_COST[c][d] for d in cols)
                   for c, n in hist.items()) for cols in ega)
    return best, sum(hist.values())


# Cool-tone (water / ice / gem) EGA colors: blue, cyan, light blue, light cyan.
# build_palettes clusters by frequency and drops these low-count-but-identity
# colors (e.g. Border Village's light-cyan water drops -- "tears" that rendered
# white or dark because no palette held a cool tone). recover_cool_tones()
# below re-introduces a dropped cool tone iff doing so strictly REDUCES measured
# displayed error over the level's interior cells (never regresses a level).
# PALETTE-ONLY: it changes the 4-palette set the ExRAM per-cell chooser + CHR
# quantizer see; collision / anim / entity data are untouched.
COOL_TONES = (11, 9, 3, 1)          # ltcyan, ltblue, cyan, blue (pref order)


def recover_cool_tones(d, palsets):
    """Refine each palette set: greedily swap the least-useful palette slot for
    a level-present cool tone that no palette holds, keeping only strictly
    error-reducing swaps. Returns the (possibly) refined palsets."""
    w, h, cells, cellmap = d["w"], d["h"], d["cells"], d["cellmap"]
    subs = {k: [[v[hy + yy][hx:hx + 8] for yy in range(8)]
                for hy in (0, 8) for hx in (0, 8)] for k, v in cells.items()}

    def interior(i):
        x, y = i % w, i // w
        return 2 <= x <= w - 3 and 2 <= y <= h - 3

    kw = Counter()                                   # interior usage per cell key
    for i, key in enumerate(cellmap):
        if interior(i):
            kw[key] += 1

    def set_err(bd, pals):
        ega = _ega_cols(bd, pals)
        return sum(cnt * _cell8_err(g, ega)[0]
                   for key, cnt in kw.items() for g in subs[key])

    out = []
    for bd, pals in palsets:
        pals = [list(p) for p in pals]
        present = set()
        for key, cnt in kw.items():
            if cnt:
                present.update(c for row in cells[key] for c in row)
        want = [c for c in COOL_TONES if c in present and c != bd]
        improved = True
        while improved:
            improved = False
            cur = set_err(bd, pals)
            best = None                              # (err, pi, si, color, old)
            for c in want:
                if any(c in p for p in pals):
                    continue                         # already covered
                for pi in range(len(pals)):
                    for si in range(len(pals[pi])):
                        old = pals[pi][si]
                        pals[pi][si] = c
                        e = set_err(bd, pals)
                        pals[pi][si] = old
                        if e < cur - 1e-9 and (best is None or e < best[0]):
                            best = (e, pi, si, c, old)
            if best:
                _, pi, si, c, _ = best
                pals[pi][si] = c
                improved = True
        out.append((bd, [tuple(p) for p in pals]))
    return out


def choose_palette_spans(d):
    """Decide per-level palette-set spans by MEASURED displayed err (canonical
    per-cell units == convert_nes eval_error).  Returns (palsets, span_cols):
      palsets    list of (backdrop, pals), 1..K sets of 4 palettes each.
      span_cols  K-1 ascending world-column cuts (empty for the 1-set case).

    For K in 1..KMAX with ink-balanced cuts, each span uses the BETTER of the
    {level-global set, span-tuned set} on ITS OWN cells (guard -> no-tear
    per-cell err monotonically <= 1-set).  A tear penalty (boundary-zone cells
    shown through the neighbour set for ~half the on-screen transition) is
    added; pick argmin(no-tear + tear) whose net gain over K=1 clears
    SPAN_THRESHOLD, else 1 set.  ALL sets must share the $3F00 backdrop
    (contract) or the level is forced to a single set.  KEEN_FORCE_SPANS=
    "b1,b2,.." forces explicit world-col cuts (verification path)."""
    w, h, cells, cellmap = d["w"], d["h"], d["cells"], d["cellmap"]
    gbd, gpals = C.build_palettes(cells, d["usage"])
    gega, global_set = _ega_cols(gbd, gpals), (gbd, gpals)
    subs = {k: [[v[hy + yy][hx:hx + 8] for yy in range(8)]
                for hy in (0, 8) for hx in (0, 8)] for k, v in cells.items()}

    def interior(i):
        x, y = i % w, i // w
        return 2 <= x <= w - 3 and 2 <= y <= h - 3

    col_ink = [0] * w
    for i in range(len(cellmap)):
        if interior(i):
            col_ink[i % w] += 1

    def span_sets(cuts):
        K = len(cuts) - 1
        su = [Counter() for _ in range(K)]
        for i, key in enumerate(cellmap):
            if interior(i):
                su[span_of_col(i % w, cuts)][key] += 1
        sets, egas, shared = [], [], True
        for s in range(K):
            if K == 1:
                sets.append(global_set); egas.append(gega); continue
            sbd, spals = C.build_palettes(cells, su[s])
            if sbd != gbd:                          # shared-backdrop contract
                sets.append(global_set); egas.append(gega); shared = False
                continue
            sega = _ega_cols(sbd, spals)

            def inspan(ega):
                t = 0.0
                for key, cnt in su[s].items():
                    if cnt:
                        for g in subs[key]:
                            t += cnt * _cell8_err(g, ega)[0]
                return t
            if inspan(sega) < inspan(gega):
                sets.append((sbd, spals)); egas.append(sega)
            else:
                sets.append(global_set); egas.append(gega)
        return sets, egas, shared

    def evaluate(cuts):
        sets, egas, shared = span_sets(cuts)
        K = len(cuts) - 1
        tot_e = tot_px = 0.0
        for i, key in enumerate(cellmap):
            if not interior(i):
                continue
            ega = egas[span_of_col(i % w, cuts)]
            for g in subs[key]:
                e, npx = _cell8_err(g, ega)
                tot_e += e
                tot_px += npx
        tear = 0.0
        for bi in range(K - 1):
            b = cuts[bi + 1]
            for i, key in enumerate(cellmap):
                if not interior(i) or abs(i % w - b) >= SPAN_OZ:
                    continue
                home = span_of_col(i % w, cuts)
                nb = min(K - 1, max(0, home + (1 if i % w < b else -1)))
                for g in subs[key]:
                    tear += 0.5 * max(0.0, _cell8_err(g, egas[nb])[0]
                                      - _cell8_err(g, egas[home])[0])
        px = max(tot_px, 1)
        return sets, shared, tot_e / px, (tot_e + tear) / px

    forced = os.environ.get("KEEN_FORCE_SPANS")
    if forced is not None:
        cols = [int(x) for x in forced.split(",") if x != ""]
        sets, shared, _, _ = evaluate([0] + cols + [w])
        assert shared, "KEEN_FORCE_SPANS: spans disagree on backdrop"
        return sets, cols

    base_sets, _, _, base_net = evaluate([0, w])
    best = (base_sets, [], base_net)
    for K in range(2, SPAN_KMAX + 1):
        cuts = [0] + _balanced_bounds(col_ink, w, K) + [w]
        if len(cuts) != K + 1:
            continue
        sets, shared, _, net = evaluate(cuts)
        if shared and net < best[2] * (1 - SPAN_THRESHOLD):
            best = (sets, cuts[1:-1], net)
    return best[0], best[1]


def emit_level(n, ts):
    d = build_cells(n, ts)
    w, h, cells, usage = d["w"], d["h"], d["cells"], d["usage"]
    cellmap, items, key_coll = d["cellmap"], d["items"], d["key_coll"]

    # --- palette set(s): choose_palette_spans MEASURES whether per-camera-X
    # spans (each loading its own best 4-palette set) reduce displayed err
    # enough to ship >1 set; else one global set covers the level.  Each 8x8
    # cell then picks the best of its span's 4 palettes (the ExRAM 2 palette
    # bits), recovering the fidelity a coarse 16x16-attribute grid loses.  keen4
    # measures out to 1 set for every demo level. ---
    palsets, span_cols = choose_palette_spans(d)
    # Recover cool tones (water/ice/gem) that build_palettes dropped by
    # frequency, only when it strictly lowers measured displayed error
    # (never regresses a level). Palette-only.
    palsets = recover_cool_tones(d, palsets)
    backdrop = palsets[0][0]            # shared $3F00 backdrop (all sets agree)
    cuts = [0] + list(span_cols) + [w]
    # SPANBOUNDS are camera-X PIXELS: the engine loads set s while cam_x is in
    # span s.  A cell is baked in its HOME world-column span; to keep at most
    # half a screen ever showing the "wrong" set, the set swaps when the
    # boundary COLUMN reaches screen centre -> cam_x bound = col*16 - 128
    # (clamped, ascending).  RUNTIME DEPENDENCY: the engine must load palsets[s]
    # per this rule; it currently always loads set 0.
    span_bounds = [max(0, b * 16 - 128) for b in span_cols]

    # --- authentic TILEINFO background-tile animation (waterfalls, flames,
    # torches, water drops, shimmer). Each animated 8x8 sub-cell's CHR is a
    # SEQUENCE of F per-phase patterns packed into F consecutive 4KB banks
    # (the "anim region", appended after the static banks): phase k of a
    # length-L cell lives in bank anim_base_local + (k mod L). The runtime
    # keeps ONE global phase counter and adds it to each on-screen animated
    # cell's ExRAM bank byte, so the whole screen's animation steps together
    # for a small set of ExRAM writes/tick. ---
    anim_steps, F, speed_frames = compute_anim(cells, usage, ts)

    # --- global 8x8 CHR pool (dedup on 16-byte 2bpp pattern) + per-key
    # metatile (4 tile-ids + 4 ExRAM bytes + top + flags), u16-deduped. ---
    chr_pool = {}       # 16B pattern -> global tile index (STATIC banks)
    chr_order = []
    anim_pool = {}      # F-pattern sequence tuple -> anim slot (ANIM banks)
    anim_order = []     # anim slot -> tuple of F 16B patterns

    def tile_index(pat):
        gi = chr_pool.get(pat)
        if gi is None:
            gi = chr_pool[pat] = len(chr_order)
            chr_order.append(pat)
        return gi

    def anim_index(seq):
        s = anim_pool.get(seq)
        if s is None:
            s = anim_pool[seq] = len(anim_order)
            anim_order.append(seq)
        return s

    def subtile(grid8, ps):
        bd, pl = ps
        pal_i, pat = quantize_8(grid8, bd, pl)
        gi = tile_index(pat)
        bank, within = gi >> 8, gi & 0xFF
        exram = (pal_i << 6) | (bank & 0x3F)
        return within, exram, bank, False

    def subtile_anim(grids, ps):
        """grids = F per-phase 8x8 EGA grids for one sub-cell. All phases
        share ONE palette (attributes don't animate). If every phase yields
        the same pattern the cell is really static -> normal pool; else it
        becomes an anim slot. The anim region is blocks of 256 slots x F banks;
        a slot's within-region bank offset = (slot>>8)*F (block base) and its
        nametable tile-id = slot&0xFF. The ExRAM bank stored here is that LOCAL
        block-base offset; it is patched += anim_base_local (the static bank
        count) once known, giving the absolute region-relative base."""
        bd, pl = ps
        union = [row for g in grids for row in g]
        pal_i, _ = quantize_8(union, bd, pl)
        seq = tuple(quantize_8(g, bd, pl, force=pal_i)[1] for g in grids)
        if len(set(seq)) == 1:
            gi = tile_index(seq[0])
            bank, within = gi >> 8, gi & 0xFF
            return within, (pal_i << 6) | (bank & 0x3F), bank, False
        slot = anim_index(seq)
        block_base = (slot >> 8) * F         # local bank offset within region
        return slot & 0xFF, (pal_i << 6) | (block_base & 0x3F), 0, True

    mt_key_lut = {}     # (cell key, span) -> u16 metatile index
    mt_row_lut = {}     # metatile row tuple -> u16 index (dedup)
    mt_rows = []        # list of (tl,tr,bl,br, tl_ex,..br_ex, top, flags)
    mt_anim = []        # per metatile: 4-bit mask of which ExRAM fields are anim
    max_bank = 0

    def metatile_for(key, span):
        # A cell is baked in its home span's palette set: same EGA cell in a
        # different span -> different ExRAM palette bits / 2bpp pattern -> a
        # distinct metatile (natural via the row-tuple dedup below).  With
        # n_palsets==1 span is always 0, so this is byte-identical to the
        # single-set path.
        mi = mt_key_lut.get((key, span))
        if mi is not None:
            return mi
        comp = cells[key]
        steps = anim_steps.get(key)     # None -> static; else F-phase animation
        ps = palsets[span]
        ids = []
        exs = []
        amask = 0
        nonlocal max_bank
        q = 0
        for hy in (0, 8):
            for hx in (0, 8):
                if steps is not None:
                    clen = len(steps)
                    grids = [[steps[k % clen][hy + yy][hx:hx + 8]
                              for yy in range(8)] for k in range(F)]
                    within, exram, bank, is_anim = subtile_anim(grids, ps)
                else:
                    g = [comp[hy + yy][hx:hx + 8] for yy in range(8)]
                    within, exram, bank, is_anim = subtile(g, ps)
                ids.append(within)
                exs.append(exram)
                if is_anim:
                    amask |= (1 << q)
                elif bank > max_bank:
                    max_bank = bank
                q += 1
        top, flags = key_coll(key)
        row = tuple(ids) + tuple(exs) + (top, flags)
        mi = mt_row_lut.get(row)        # dedup identical metatiles
        if mi is None:
            mi = len(mt_rows)
            mt_rows.append(row)
            mt_anim.append(amask)
            mt_row_lut[row] = mi
        mt_key_lut[(key, span)] = mi
        return mi

    # metatile 0 = the blank backdrop cell (ExRAM pal 0, tile 0); backdrop-only
    # so it is identical under every set -> one metatile across all spans.
    if (0, 0, 0) not in cells:
        cells[(0, 0, 0)] = ts.composite(0, 0, 0)
    metatile_for((0, 0, 0), 0)
    mmap = [metatile_for(k, span_of_col(i % w, cuts))
            for i, k in enumerate(cellmap)]
    for (ix, _, _, ekey) in items:
        metatile_for(ekey, span_of_col(ix, cuts))  # empty variants must exist
    # gem-door + switch metatiles (baked so they exist in the MT table; the
    # runtime swaps map cells to these on gem-place / switch-press)
    gd_recs = []
    for (hx, hy, color, pk, dx, dy, dh, ok) in d["gemholders"]:
        gd_recs.append((hx, hy, color,
                        metatile_for(pk, span_of_col(hx, cuts)),
                        dx, dy, dh, metatile_for(ok, span_of_col(dx, cuts))))
    sw_recs = []
    for (sx, sy, ofk, onk, tx, ty) in d["switches"]:
        sw_recs.append((sx, sy, metatile_for(ofk, span_of_col(sx, cuts)),
                        metatile_for(onk, span_of_col(sx, cuts)), tx, ty))
    assert len(gd_recs) <= 6 and len(sw_recs) <= 4, \
        f"L{n}: gemdoors {len(gd_recs)}/6 switches {len(sw_recs)}/4 exceed cap"

    # World map (level 0): bake fence empty-MTs and write map_nodes.json for
    # gen_mmc5_rom (enter tiles, path fences, flag holders).
    map_meta = None
    if n == 0:
        ens, fns, fls = collect_map_nodes(w, d["bg"], d["info"])
        fence_recs = []
        for (fx, fy, glv, bgt) in fns:
            ekey = (bgt, 0, 0)
            if ekey not in cells:
                cells[ekey] = ts.composite(*ekey)
            empty = metatile_for(ekey, span_of_col(fx, cuts))
            fence_recs.append((fx, fy, glv, empty))
        map_meta = dict(enters=ens, fences=fence_recs, flags=fls,
                        w=w, h=h, spawn=None)
        print(f"  map nodes: enter={len(ens)} fence={len(fence_recs)} "
              f"flag={len(fls)}")

    N = len(mt_rows)
    assert N <= 0x10000, f"L{n}: {N} metatiles exceed u16"

    n_static_banks = (len(chr_order) + 255) // 256
    # anim region: ceil(#anim tiles / 256) blocks, each F consecutive banks
    # (block b at anim_base_local + b*F). A block's bank k holds every slot's
    # phase (k mod L) pattern at its 8x8 tile-id.
    if not anim_order:
        F = 1                         # no animated cells -> no anim banks
    n_anim_blocks = (len(anim_order) + 255) // 256
    anim_base_local = n_static_banks
    n_anim_banks = n_anim_blocks * F
    n_banks = n_static_banks + n_anim_banks
    # patch anim ExRAM bytes: local block-base offset -> += anim region base
    for mi, amask in enumerate(mt_anim):
        if not amask:
            continue
        row = list(mt_rows[mi])
        for q in range(4):
            if amask & (1 << q):
                row[4 + q] = ((row[4 + q] & 0xC0)
                              | ((row[4 + q] & 0x3F) + anim_base_local))
        mt_rows[mi] = tuple(row)
    max_bank = max(max_bank, anim_base_local + n_anim_banks - 1)
    assert max_bank < 64, (f"L{n}: CHR bank {max_bank} >= 64 -- needs $5130 "
                           "upper-bit spans (see level_fmt.h >64 plan)")
    chr_upper = 0                      # keen4: all levels < 64 banks

    # --- CHR-ROM: static 4KB banks of 256 tiles, then the anim blocks
    # (block b, bank k holds every slot's phase (k mod L) pattern). ---
    chr_rom = bytearray(n_banks * 4096)
    for gi, pat in enumerate(chr_order):
        off = (gi >> 8) * 4096 + (gi & 0xFF) * 16
        chr_rom[off:off + 16] = pat
    for s, seq in enumerate(anim_order):
        block, within = s >> 8, s & 0xFF
        for k in range(F):
            off = (anim_base_local + block * F + k) * 4096 + within * 16
            chr_rom[off:off + 16] = seq[k]   # seq already length F (k mod L)

    # --- entity tables ---
    bloogs, blets, babs, plats, fplats, blocks, doors = build_entities(
        n, w, h, d["info"])
    # items: x,y,type,empty_mt(u16) -- empty_mt widened to u16 (u16 mt space);
    # the reverted-to empty metatile is the one baked for the item's own span.
    item_recs = sorted(((x, y, t, mt_key_lut[(ekey, span_of_col(x, cuts))])
                        for (x, y, t, ekey) in items), key=lambda e: e[0])

    meta = d["meta"]
    # Spawn from the info plane:
    #   combat: 1 = face right, 2 = face left
    #   world map (L0): 3 = map Keen (not 1/2)
    # Fall back to bottom-left if no marker.
    spawn = None
    if n == 0:
        for i, v in enumerate(d["info"]):
            if v == 3:
                spawn = (i % w, i // w, 1)  # face E on map by default
                break
    if spawn is None:
        for i, v in enumerate(d["info"]):
            if v in (1, 2):
                spawn = (i % w, i // w, 1 if v == 1 else -1)
                break
    if spawn is None:
        spawn = (2, h - 4, 1)
    spawn_dir = spawn[2] & 0xFF
    if map_meta is not None:
        map_meta["spawn"] = [spawn[0], spawn[1], spawn[2]]
    print(f"  spawn ({spawn[0]},{spawn[1]}) dir={spawn[2]}"
          f"{' [MapKeen info=3]' if n == 0 else ''}")

    # ======================= assemble the blob =======================
    body = bytearray()
    off_tab = {}

    def section(name, data):
        off_tab[name] = HDR + len(body)
        body.extend(data)

    # rows[my] = u16 byte-offset within the map section of row my's first cell
    section("rows", struct.pack(f"<{h}H", *[my * w * 2 for my in range(h)]))
    # u16 metatile map (row-major)
    section("map", struct.pack(f"<{w*h}H", *mmap))
    # Hot render AoS + cold collision SoA, still exactly 10*N bytes:
    #   N x {tl,tr,bl,br, tl_ex,tr_ex,bl_ex,br_ex}, top[N], flags[N]
    # Scrolling computes mi*8 once and uses fixed offsets; collision keeps its
    # old one-byte indexed lookup without a runtime mi*10 multiply.
    mt_hot = bytearray()
    for r in mt_rows:
        mt_hot.extend(bytes(v & 0xFF for v in r[:8]))
    mt_hot.extend(bytes(r[8] & 0xFF for r in mt_rows))
    mt_hot.extend(bytes(r[9] & 0xFF for r in mt_rows))
    section("mt", mt_hot)
    # palette sets: n × 16 bytes (4 palettes × [backdrop, c1, c2, c3] NES ids)
    psb = bytearray()
    for (bd, pl) in palsets:
        for p in pl:
            egacols = [bd] + list(p)[:3]
            egacols += [bd] * (4 - len(egacols))
            psb.extend(C.EGA_TO_NES[c] for c in egacols)
    section("palsets", psb)
    # span bounds: (n_palsets-1) u16 camera-X px boundaries
    section("spanbounds", struct.pack(f"<{len(span_bounds)}H", *span_bounds))
    # entity tables, fixed order: items,bloogs,blets,babs,plats,fplats,blocks,doors
    ent = [("items", item_recs, "<BBBH"),
           ("bloogs", bloogs, "<BBB"),
           ("blets", blets, "<BBBB"),
           ("babs", babs, "<BBB"),
           ("plats", plats, "<BBb"),
           ("fplats", fplats, "<BB"),
           ("blocks", blocks, "<BB"),
           ("doors", doors, "<BBBB")]
    ent_dir = []
    for name, recs, fmt in ent:
        off_tab[name] = HDR + len(body)
        ent_dir.append((HDR + len(body), len(recs)))
        for rec in recs:
            body.extend(struct.pack(fmt, *[v & 0xFF if c != "H" else v
                                           for v, c in zip(rec, fmt[1:])]))

    # gem-door + switch extension section (see level_fmt.h MMC5_OFF_EXT). Its
    # blob offset goes in header byte 38 (0 = none). Placed after the entity
    # tables (offset >= off_mt), so gen_mmc5_rom.realign_mt shifts it too.
    ext_off = 0
    if gd_recs or sw_recs:
        ext_off = HDR + len(body)
        body.append(len(gd_recs))
        body.append(len(sw_recs))
        for (hx, hy, color, placed, dx, dy, dh, openm) in gd_recs:
            body.extend(struct.pack("<BBBHBBBH",
                                    hx, hy, color, placed, dx, dy, dh, openm))
        for (sx, sy, offm, onm, tx, ty) in sw_recs:
            body.extend(struct.pack("<BBHHBB", sx, sy, offm, onm, tx, ty))

    # --- header ---
    hdr = bytearray(HDR)
    struct.pack_into("<BBBBBBBB", hdr, 0,
                     MAGIC, VERSION, w & 0xFF, h & 0xFF,
                     spawn[0] & 0xFF, spawn[1] & 0xFF, spawn_dir,
                     C.EGA_TO_NES[backdrop])
    # bytes 14/15 = anim frame count F / phase-step speed (60Hz frames).
    struct.pack_into("<HHBBBB", hdr, 8,
                     N & 0xFFFF, n_banks & 0xFFFF, len(palsets), chr_upper,
                     F & 0xFF, speed_frames & 0xFF)
    struct.pack_into("<6I", hdr, 16,
                     off_tab["rows"], off_tab["map"], off_tab["mt"],
                     off_tab["palsets"], off_tab["spanbounds"], 0)
    # byte 36 (low byte of the old reserved u32) = LOCAL anim region base bank;
    # gen_mmc5_rom rewrites it += the level's CHR bank offset (like the ExRAM
    # bank fields) so the runtime reads an ABSOLUTE 4KB bank. byte 37 = the
    # anim region size in 4KB banks (the runtime's animated-cell range test).
    hdr[36] = anim_base_local & 0xFF
    hdr[37] = n_anim_banks & 0xFF
    struct.pack_into("<H", hdr, 38, ext_off & 0xFFFF)  # gem-door/switch ext
    for i, (o, c) in enumerate(ent_dir):
        struct.pack_into("<IH", hdr, 40 + i * 6, o, c & 0xFFFF)

    blob = bytes(hdr) + bytes(body)
    outd = ROOT / f"assets/converted/ck{EP}/level{n:02d}"
    outd.mkdir(parents=True, exist_ok=True)
    (outd / "mmc5.bin").write_bytes(blob)
    (outd / "mmc5_chr.bin").write_bytes(bytes(chr_rom))
    if map_meta is not None:
        import json
        # Episode-level sidecar (gen_mmc5_rom packs it into mapdata).
        map_path = ROOT / f"assets/converted/ck{EP}/map_nodes.json"
        map_path.write_text(json.dumps(map_meta, indent=1))

    ecnt = [sum(1 for t in (bloogs, blets, babs) for e in t if e[-1] <= dd)
            for dd in (0, 1, 2)]
    anim_cells = sum(1 for k in cellmap if k in anim_steps)
    print(f"L{n:02d} '{meta['name']}': {w}x{h}  "
          f"metatiles={N}  chr_tiles={len(chr_order)}(static)+"
          f"{len(anim_order)}(anim x{F}ph) "
          f"banks={n_static_banks}+{n_anim_banks}={n_banks} ({n_banks*4}KB)  "
          f"palsets={len(palsets)}  blob={len(blob)}B  "
          f"anim_cells={anim_cells} F={F} spd={speed_frames}fr "
          f"items={len(item_recs)} enemies e/n/h {ecnt[0]}/{ecnt[1]}/{ecnt[2]} "
          f"gemdoors={len(gd_recs)} switches={len(sw_recs)}")
    return dict(level=n, w=w, h=h, N=N, tiles=len(chr_order), banks=n_banks,
                static_banks=n_static_banks, anim_banks=n_anim_banks,
                anim_tiles=len(anim_order), anim_cells=anim_cells, F=F,
                blob=len(blob), chr_kb=n_banks * 4, palsets=len(palsets))


def main(levels):
    ts = C.TileSource()
    rows = []
    for n in levels:
        rows.append(emit_level(n, ts))
    print("\n=== keen4 MMC5 emit summary ===")
    tot_chr = sum(r["chr_kb"] for r in rows)
    for r in rows:
        print(f"  L{r['level']:02d}: {r['N']} metatiles, "
              f"{r['tiles']} tiles -> {r['banks']} banks ({r['chr_kb']}KB), "
              f"blob {r['blob']}B, {r['palsets']} palset(s)")
    print(f"  total CHR across 4 levels: {tot_chr}KB "
          f"(MMC5 1MB budget: {'OK' if tot_chr <= 1024 else 'OVER'})")


if __name__ == "__main__":
    ns = [int(a) for a in sys.argv[1:]] or [1, 2, 3, 4]
    main(ns)
