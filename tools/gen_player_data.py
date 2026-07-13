#!/usr/bin/env python3
"""Convert Keen player sprite frames to NES sprite CHR + metasprite tables.

Only right-facing frames are converted; left-facing uses NES hardware X-flip.
Outputs assets/converted/player_chr.bin and src/gen/player.c/.h.
Frame chunk IDs are the game's GFX chunk numbers (chunk - 46 = our index).
"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import keenlib as K
from convert_nes import EGA_TO_NES, NES_PALETTE
from PIL import Image

# Displayed (on-NES) RGB per EGA index, and the "neutral" EGA colors that render
# acceptably under ANY sprite palette (black is in every palette; white/greys
# fall back cleanly). Used by the identity-aware per-part palette selector below.
DRGB = [NES_PALETTE[EGA_TO_NES[c]] for c in range(16)]
NEUTRAL = frozenset((0, 7, 8, 15))   # black, light grey, dark grey, white


def _d2(a, b):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


def _disp_cost(c, p):
    """Displayed-color error of rendering EGA color c under 3-color palette p."""
    return min(_d2(DRGB[c], DRGB[q]) for q in p)

ROOT = K.ROOT
GEN = ROOT / "src/gen"
GEN.mkdir(parents=True, exist_ok=True)
OUT = ROOT / f"assets/converted/ck{K.EP}"
OUT.mkdir(parents=True, exist_ok=True)

# right-facing frames per episode (EGAGRAPH chunk numbers)
if K.EP == 6:
    FRAMES = {
        "STAND": 52,
        "RUN1": 53, "RUN2": 54, "RUN3": 55, "RUN4": 56,
        "JUMP1": 57, "JUMP2": 58, "JUMP3": 59,
        "POGO1": 133, "POGO2": 134,
    }
    LOOK_FRAMES = {"LOOKU": 68, "LOOKD1": 81, "LOOKD2": 82}
    DEATH_FRAMES = {"DEATH": 83}
    # pole-climb, ledge-hang, and ledge-pull frames; left variants use hardware X-flip.
    POLE_FRAMES = {"POLE1": 102, "POLE2": 103, "POLE3": 104}
    LEDGE_FRAMES = {"HANG": 124, "PULL1": 129, "PULL2": 130,
                    "PULL3": 131, "PULL4": 132}
    SHOT_FRAMES = {"STUN1": 96, "STUN2": 97, "STUNHIT1": 100}
    MISC_FRAMES = {"PLATFORM": 424}
    BLOOG_FRAMES = {"BLOOGW1": 342, "BLOOGW2": 343,
                    "BLOOGSTUN": 350}  # interim 2-frame cycle (CHR budget)
    BLOOGLET_FRAMES = {"BLET1": 351, "BLET2": 352}
    BABOBBA_FRAMES = {"BAB1": 288, "BAB2": 289, "BAB3": 290}
    SLUG_FRAMES = {}
    MUSH_FRAMES = {}
    LICK_FRAMES = {}
    SPARKY_FRAMES = {}
    AMPTON_FRAMES = {}
    SLICE_FRAMES = {}
    SKYPEST_FRAMES = {}
    BOUNDER_FRAMES = {}
    WORM_FRAMES = {}
    MIM_FRAMES = {}
elif K.EP == 5:  # Keen 5 (retail v1.4)
    FRAMES = {
        "STAND": 108,
        "RUN1": 109, "RUN2": 110, "RUN3": 111, "RUN4": 112,
        "JUMP1": 113, "JUMP2": 114, "JUMP3": 115,
        "POGO1": 190, "POGO2": 191,
    }
    LOOK_FRAMES = {"LOOKU": 124, "LOOKD1": 137, "LOOKD2": 138}
    DEATH_FRAMES = {"DEATH": 140}  # boink launch pose
    # pole-climb, ledge-hang, and ledge-pull frames; left variants use hardware X-flip.
    POLE_FRAMES = {"POLE1": 159, "POLE2": 160, "POLE3": 161}
    LEDGE_FRAMES = {"HANG": 181, "PULL1": 186, "PULL2": 187,
                    "PULL3": 188, "PULL4": 189}
    SHOT_FRAMES = {"STUN1": 153, "STUN2": 154, "STUNHIT1": 157}
    MISC_FRAMES = {"PLATFORM": 446}  # red axis platform sprite
    BLOOG_FRAMES = {}
    BLOOGLET_FRAMES = {}
    BABOBBA_FRAMES = {}
    SLUG_FRAMES = {}
    MUSH_FRAMES = {}
    LICK_FRAMES = {}
    # Keen 5 bestiary: 2-frame walk cycles (CHR budget) + stun.
    # Slicestar is a single spinning frame (invincible).
    SPARKY_FRAMES = {"SPARKY1": 373, "SPARKY2": 374, "SPARKYSTUN": 377}
    AMPTON_FRAMES = {"AMPTON1": 405, "AMPTON2": 406, "AMPTONSTUN": 416}
    SLICE_FRAMES = {"SLICE": 385}
    SKYPEST_FRAMES = {}
    BOUNDER_FRAMES = {}
    WORM_FRAMES = {}
    MIM_FRAMES = {}
else:  # Keen 4 (shareware v1.4)
    FRAMES = {
        "STAND": 130,
        "RUN1": 131, "RUN2": 132, "RUN3": 133, "RUN4": 134,
        "JUMP1": 135, "JUMP2": 136, "JUMP3": 137,
        "POGO1": 211, "POGO2": 212,
    }
    LOOK_FRAMES = {"LOOKU": 146, "LOOKD1": 160, "LOOKD2": 161}
    DEATH_FRAMES = {"DEATH": 162}  # boink launch pose
    # pole-climb frames; left variants use hardware X-flip.
    POLE_FRAMES = {"POLE1": 180, "POLE2": 181, "POLE3": 182}
    LEDGE_FRAMES = {"HANG": 202, "PULL1": 207, "PULL2": 208,
                    "PULL3": 209, "PULL4": 210}
    SHOT_FRAMES = {"STUN1": 174, "STUN2": 175, "STUNHIT1": 178}
    MISC_FRAMES = {"PLATFORM": 484}
    BLOOG_FRAMES = {}
    BLOOGLET_FRAMES = {}
    BABOBBA_FRAMES = {}
    # Keen 4 bestiary (game GFX chunk numbers).
    # Right-facing frames only; left uses hardware X-flip.
    SLUG_FRAMES = {"SLUG1": 315, "SLUG2": 316, "SLUGSTUN": 318}
    MUSH_FRAMES = {"MUSH1": 327, "MUSH2": 328}
    # Lick: leap frames 1 (grounded/windup) + 3 (airborne) of 4, flame
    # frames 1 + 3 of 3, stun (CHR budget: skip the in-between frames)
    LICK_FRAMES = {"LICKG": 469, "LICKA": 471,
                   "LICKF1": 477, "LICKF2": 479, "LICKSTUN": 483}
    SPARKY_FRAMES = {}
    AMPTON_FRAMES = {}
    SLICE_FRAMES = {}
    # Skypest (445/446 fly cycle, 456 for the pogo-squished harmless state)
    # and Bounder (495/496 in-place bounce, 493/494 horizontal, 497 stun).
    # Right-facing only; left uses hardware X-flip.
    SKYPEST_FRAMES = {"SKY1": 445, "SKY2": 446, "SKYSQUASH": 456}
    # 2-frame bounce cycle + stun only (CHR budget: the separate R/L
    # horizontal frames reuse the front-facing bounce frames)
    BOUNDER_FRAMES = {"BNDF1": 495, "BNDF2": 496, "BNDSTUN": 497}
    # Wormmouth: ground worm that pops up and bites. Minimal
    # frame set: HINT (peeking/moving), BITE (mouth open = the lethal frame),
    # STUN. Right-facing only; left uses hardware X-flip.
    WORM_FRAMES = {"WORMHINT": 457, "WORMBITE": 463, "WORMSTUN": 468}
    # Mimrock: inert-looking rock that walks then bonks/jumps
    # at Keen. SIT (inert), 2-frame SNEAK walk, BONK (airborne = lethal), STUN.
    MIM_FRAMES = {"MIMSIT": 388, "MIMWALK1": 389, "MIMWALK2": 390,
                  "MIMBONK": 397, "MIMSTUN": 403}

def derive_palettes(frames, npals=2):
    """Cluster the frames' 8x8-tile EGA colors into npals 3-color sprite palettes."""
    tile_sets = Counter()
    color_use = Counter()
    for img in frames:
        w, h = img.size
        for ty in range(0, h, 8):
            for tx in range(0, w, 8):
                s = Counter()
                for y in range(ty, min(ty + 8, h)):
                    for x in range(tx, min(tx + 8, w)):
                        c = nearest_ega(img.getpixel((x, y)))
                        if c is not None:
                            s[c] += 1
                            color_use[c] += 1
                if s:
                    top3 = frozenset(sorted(s, key=lambda c: -s[c])[:3])
                    tile_sets[top3] += 1
    pals = []
    for s, _ in tile_sets.most_common():
        if all(len(s & frozenset(p)) < len(s) for p in pals):
            pals.append(set(s))
        if len(pals) == npals:
            break
    for _ in range(6):  # refine
        assigned = [Counter() for _ in range(npals)]
        for s, wgt in tile_sets.items():
            best = min(range(npals),
                       key=lambda i: sum(color_use[c] for c in s
                                         if c not in pals[i]))
            for c in s:
                assigned[best][c] += wgt
        new = [set(c for c, _ in a.most_common(3)) for a in assigned]
        if new == pals:
            break
        pals = new
    return [sorted(p, key=lambda c: -color_use[c]) for p in pals]


SPR_PALS = None  # derived in main() from the real frame pixels


def load_frame(chunk):
    idx = chunk - K.OFF_SPRITES
    img = Image.open(K.EXT / "gfx" / "sprites" / f"sprite{idx:03d}.png").convert("RGBA")
    meta = json.loads((K.EXT / "gfx" / "sprites.json").read_text())[idx]
    return img, meta


def nearest_ega(rgba):
    r, g, b, a = rgba
    if a == 0:
        return None
    best, bd = 0, 1 << 30
    for i, (er, eg, eb) in enumerate(K.EGA_PALETTE):
        d = (r - er) ** 2 + (g - eg) ** 2 + (b - eb) ** 2
        if d < bd:
            best, bd = i, d
    return best


def main():
    global SPR_PALS
    tiles = []          # list of 16-byte CHR patterns
    tile_lut = {}
    frames_meta = {}

    # derive_palettes() picks by frequency, which drops low-count but
    # identity-critical colors (helmet green, skin). Hand-tuned instead:
    # black outline must occupy a slot in both (sprite color 0=transparent).
    # measured from the art: outline 0, white 15, grey 7 (Keen body),
    # yellow 14 (helmet), light magenta 13 (face); greens for Bloogs
    if K.EP == 6:
        SPR_PALS = [[0, 15, 7], [0, 14, 13], [0, 2, 10], [0, 4, 12]]
    elif K.EP == 5:  # Sparky is light-cyan/magenta, Ampton magenta/blue,
        # Slicestar white+grey (pal 0), platform red (pal 3)
        SPR_PALS = [[0, 15, 7], [0, 14, 13], [0, 11, 13], [0, 4, 12]]
    else:  # Keen 4: per-part 8x8 palette roles, composed as paired hardware
        # sprites by the 8x16 packer below.
        # Jeans are predominantly EGA 11 (light cyan), not EGA 9 (light blue):
        # STAND alone has 22 cyan pixels versus only 5 light-blue. Give the
        # dominant cyan its exact NES color and keep dark blue for the shadow;
        # the build-time layer splitter preserves the whole cyan/blue pants
        # region without any runtime palette computation.
        SPR_PALS = [[0, 15, 7], [0, 14, 2], [0, 11, 1], [0, 4, 13]]
    print("sprite palettes (EGA):", SPR_PALS,
          "(auto-derive suggested:", derive_palettes(
              [load_frame(c)[0] for c in FRAMES.values()]), ")")

    # Per-part selector: protects low-count identity hues without letting
    # mostly-neutral face/pogo cells become green or blue; deterministic in
    # content (no animation palette strobe).
    PAL_MARGIN = 8
    # POSE groups (look/death) are processed LAST so their unique tiles get
    # the highest tile ids: the resident sprite bank (player_chr.bin, <=256) keeps
    # only the RESIDENT tiles; the pose tiles live past that cutoff and are
    # packed per-level by gen_mmc5_rom (MMC5_EX) into whatever bank headroom a
    # level's enemy set leaves. (Keen's full frame set won't co-reside with a
    # dense level's whole bestiary in one 256-tile table — a hard NES limit.)
    POSE_GROUPS = {"look", "death", "pole", "ledge"}
    frame_groups = (("keen", FRAMES), ("bloog", BLOOG_FRAMES),
                    ("shot", SHOT_FRAMES), ("misc", MISC_FRAMES),
                    ("blet", BLOOGLET_FRAMES), ("bab", BABOBBA_FRAMES),
                    ("slug", SLUG_FRAMES), ("mush", MUSH_FRAMES),
                    ("lick", LICK_FRAMES), ("sparky", SPARKY_FRAMES),
                    ("ampton", AMPTON_FRAMES), ("slice", SLICE_FRAMES),
                    ("skypest", SKYPEST_FRAMES), ("bounder", BOUNDER_FRAMES),
                    ("worm", WORM_FRAMES), ("mim", MIM_FRAMES),
                    ("look", LOOK_FRAMES), ("death", DEATH_FRAMES),
                    ("pole", POLE_FRAMES), ("ledge", LEDGE_FRAMES))
    npals = len(SPR_PALS)

    def pal_err(hist, p):
        return sum(n for c, n in hist.items() if c not in p)

    def choose_pal(hist):
        fe = [pal_err(hist, p) for p in SPR_PALS]
        de = [sum(n * _disp_cost(c, p) for c, n in hist.items())
              for p in SPR_PALS]
        bf = min(fe)
        cand = [i for i in range(npals) if fe[i] <= bf + PAL_MARGIN]
        opaque = sum(hist.values())
        chroma = sum(n for c, n in hist.items() if c not in NEUTRAL)
        cover = [sum(n for c, n in hist.items()
                     if c not in NEUTRAL and c in SPR_PALS[i])
                 for i in range(npals)]
        bestcov = max(cover[i] for i in cand)
        if chroma and bestcov and bestcov >= 0.5 * chroma \
                and chroma >= 0.35 * opaque:
            winners = [i for i in cand if cover[i] == bestcov]
            return min(winners, key=lambda i: de[i])
        return min(cand, key=lambda i: de[i])

    # --- 8x16 sprite mode: every sprite part is an 8-wide x 16-tall PAIR of
    # vertically-adjacent 8x8 tiles (top even + bottom odd in the $1000 pattern
    # table; PPUCTRL bit 5). A pair shares ONE OAM attribute, so its top and
    # bottom 8x8 halves share one palette (chosen over the combined 16-row
    # histogram). Halves the OAM entries + metasprite parts vs 8x8 -> eases the
    # 8-sprites-per-scanline dropout and cuts sprite-draw CPU. Cells step 16px
    # in y; a pair is kept if EITHER half is opaque (transparent halves become a
    # blank tile in the pair). See build_frame + the pack in gen_mmc5_rom.
    prep = []          # (group, name, meta fields..., pair cells)
    for gname, fdict in frame_groups:
        for name, chunk in fdict.items():
            img, meta = load_frame(chunk)
            # crop to opaque bounding box (big frames like Bloog have wide
            # transparent margins that would waste sprites)
            bbox = img.getbbox()
            crop_x, crop_y = (bbox[0], bbox[1]) if bbox else (0, 0)
            img = img.crop(bbox) if bbox else img
            w, h = img.size
            # pad to 8px width grid + 16px height grid (8x16 pair rows)
            gw, gh = (w + 7) // 8 * 8, (h + 15) // 16 * 16
            ega = [[None] * gw for _ in range(gh)]
            for y in range(h):
                for x in range(w):
                    ega[y][x] = nearest_ega(img.getpixel((x, y)))
            cells = []
            for ty in range(0, gh, 16):
                for tx in range(0, gw, 8):
                    hist = Counter()           # combined 16-row histogram
                    for y in range(16):
                        for x in range(8):
                            c = ega[ty + y][tx + x]
                            if c is not None:
                                hist[c] += 1
                    if not hist:               # both 8x8 halves transparent
                        continue
                    top = [ega[ty + y][tx:tx + 8] for y in range(8)]
                    bot = [ega[ty + 8 + y][tx:tx + 8] for y in range(8)]
                    cells.append((tx, ty, hist, top, bot))
            prep.append((gname, name, w, h, crop_x, crop_y, meta, gh, cells))

    tile_group = []  # group name per PAIR id (parallel to `tiles`; 32B pairs)

    def render_tile(blk, pal):
        """Render an 8x8 block under 3-color palette `pal` -> 16-byte tile."""
        lo = bytearray(8)
        hi = bytearray(8)
        for y in range(8):
            for x in range(8):
                c = blk[y][x]
                if c is None:
                    continue
                if c in pal:
                    v = pal.index(c) + 1
                else:  # nearest within palette
                    rgb = K.EGA_PALETTE[c]
                    v = 1 + min(range(3), key=lambda i: sum(
                        (a - b) ** 2 for a, b in
                        zip(K.EGA_PALETTE[pal[i]], rgb)))
                lo[y] |= (v & 1) << (7 - x)
                hi[y] |= ((v >> 1) & 1) << (7 - x)
        return bytes(lo) + bytes(hi)

    def split_layers(blk, allow_split):
        """Return [(palette_index, masked_8x8), ...], at most two layers.
        choose_pal supplies the identity-safe primary palette; a second
        transparent layer is admitted only when it reduces displayed error and
        owns real pixels. Separates face/shirt, shirt/pants and pants/shoes
        colors that share one 8x8 grid cell."""
        hist = Counter(c for row in blk for c in row if c is not None)
        if not hist:
            return []
        primary = choose_pal(hist)
        one = sum(n * _disp_cost(c, SPR_PALS[primary])
                  for c, n in hist.items())
        # Prefer the complement that exactly owns the most source-color
        # pixels (blue pants beat the slightly smaller red-shoe patch), then
        # break ties by measured display error.
        second = min(
            (i for i in range(npals) if i != primary),
            key=lambda i: (-sum(n for c, n in hist.items()
                                if c in SPR_PALS[i] and c not in SPR_PALS[primary]),
                           sum(n * min(_disp_cost(c, SPR_PALS[primary]),
                                       _disp_cost(c, SPR_PALS[i]))
                               for c, n in hist.items()), i))
        two = sum(n * min(_disp_cost(c, SPR_PALS[primary]),
                          _disp_cost(c, SPR_PALS[second]))
                  for c, n in hist.items())
        second_owned = sum(n for c, n in hist.items()
                           if _disp_cost(c, SPR_PALS[second]) <
                              _disp_cost(c, SPR_PALS[primary]))
        selected = ([primary, second]
                    if allow_split and two < one and second_owned >= 9
                    else [primary])
        masks = [[[None] * 8 for _ in range(8)] for _ in selected]
        owned = [0] * len(selected)
        for y in range(8):
            for x in range(8):
                c = blk[y][x]
                if c is None:
                    continue
                k = min(range(len(selected)),
                        key=lambda j: (_disp_cost(c, SPR_PALS[selected[j]]), j))
                masks[k][y][x] = c
                owned[k] += 1
        return [(selected[i], masks[i]) for i in range(len(selected)) if owned[i]]

    def build_frame(entry):
        gname, name, w, h, crop_x, crop_y, meta, gh, cells = entry
        sprites = []

        def add_pair(tx, ty, pat, pal_i):
            if pat not in tile_lut:
                tile_lut[pat] = len(tiles)
                tiles.append(pat)
                tile_group.append(gname)
            sprites.append((tx, ty, tile_lut[pat], pal_i))

        for tx, ty, hist, top, bot in cells:
            # Keen gets a second intra-tile color layer where a substantial
            # region (>=8 pixels) would otherwise be recolored. Enemies retain
            # the old per-8x8 selection; per-level pair capacity is finite.
            tl = split_layers(top, gname == "keen")
            bl = split_layers(bot, gname == "keen")
            by_pal = {}
            for pi, mask in tl:
                by_pal.setdefault(pi, [None, None])[0] = mask
            for pi, mask in bl:
                by_pal.setdefault(pi, [None, None])[1] = mask
            blank_grid = [[None] * 8 for _ in range(8)]
            for pi, halves in by_pal.items():
                tmask = halves[0] if halves[0] is not None else blank_grid
                bmask = halves[1] if halves[1] is not None else blank_grid
                pal = SPR_PALS[pi]
                add_pair(tx, ty, render_tile(tmask, pal) + render_tile(bmask, pal), pi)
        frames_meta[name] = dict(width=w, height=h, crop=[crop_x, crop_y],
                                 origin=meta["origin"], clip=meta["clip"],
                                 sprites=sprites)

    # SHARED groups FIRST (Keen locomotion + stunner + platform), then the
    # font glyphs below, so the always-resident set (keen/shot/misc/font) takes
    # the LOWEST tile ids (< 135). This matters for the MMC5 per-level packer:
    # it treats every tile id whose group is shared as a fixed bank slot, and
    # classifies shared vs flex over the 0..255 id range — the font MUST keep
    # ids < 256 even though the full bestiary (Wormmouth/Mimrock) now pushes the
    # non-shared enemy + pose tiles past 255. Enemies come after (ids 135+),
    # poses last (past resident_count).
    SHARED_FG = {"keen", "shot", "misc"}
    for entry in prep:
        if entry[0] in SHARED_FG:
            build_frame(entry)

    # --- font glyphs for the status overlay: digits + letters, white ---
    import keenlib as _K
    ega = _K.EgaGraph()
    font = ega.chunk(3)
    import struct as _s
    fheight = _s.unpack_from("<H", font)[0]
    foffs = _s.unpack_from("<256H", font, 2)
    fwidths = font[514:514 + 256]
    glyph_chars = "0123456789FPS:E"
    font_tiles = {}
    BLANK16 = bytes(16)   # transparent bottom half of each font pair (8x16)
    for ch in glyph_chars:
        c = ord(ch)
        wb = (fwidths[c] + 7) // 8
        lo = bytearray(8)
        for y in range(min(8, fheight)):
            if wb:
                lo[y] = font[foffs[c] + y * wb]
        # 8x16 pair: glyph 8x8 (color 3 = white in pal 0) on top, blank bottom
        pat = bytes(lo) + bytes(lo) + BLANK16
        if pat not in tile_lut:
            tile_lut[pat] = len(tiles)
            tiles.append(pat)
            tile_group.append("font")
        font_tiles[ch] = tile_lut[pat]

    # --- HUD stat icons: tiny WHITE glyphs (palette 0, same 8x16 font-pair
    # path as the digits) so they cost NO new sprite palette and pack into the
    # resident font block. Hand-authored 8x8 bitmaps (bit7 = leftmost pixel);
    # both bitplanes = the shape -> color 3 = white in the HUD palette. These
    # are the "icon" half of the icon+number HUD (hud.c): a keygem diamond, a
    # Keen-head-ish life mark, and a blaster shot. ~3 extra resident pairs
    # (font ids stay < 128, so the per-level packer's bank-A encoding holds).
    hud_icons = {
        "life": [0x3C, 0x7E, 0xDB, 0xFF, 0xFF, 0xBD, 0x7E, 0x3C],  # round face
        "ammo": [0x18, 0x3C, 0x3C, 0x3C, 0x3C, 0x7E, 0x7E, 0x00],  # shot/bullet
        "gem":  [0x18, 0x3C, 0x7E, 0xFF, 0x7E, 0x3C, 0x18, 0x00],  # keygem
    }
    icon_tiles = {}
    for _nm, _rows in hud_icons.items():
        _lo = bytes(_rows)
        _pat = _lo + _lo + BLANK16
        if _pat not in tile_lut:
            tile_lut[_pat] = len(tiles)
            tiles.append(_pat)
            tile_group.append("font")
        icon_tiles[_nm] = tile_lut[_pat]

    # ENEMY frames next (after the shared set + font), so shared ids stay < 135.
    for entry in prep:
        if entry[0] not in POSE_GROUPS and entry[0] not in SHARED_FG:
            build_frame(entry)

    # tiles[0:resident_count] = the always-loaded set (shared Keen locomotion,
    # shot, platform, font, all enemies). The resident bank (player_chr.bin) holds
    # exactly these. POSE groups are packed AFTER this cutoff and never enter
    # player_chr.bin (they overflow 256; MMC5_EX packs them per-level).
    resident_count = len(tiles)
    for entry in prep:
        if entry[0] in POSE_GROUPS:
            build_frame(entry)

    # Per-level packing may remap the now->128 shared set. Reserve the font at
    # fixed low pair slots so HUD OAM constants remain valid on every level.
    font_ids = [i for i, g in enumerate(tile_group) if g == "font"]
    fixed_pair_slots = {tid: i for i, tid in enumerate(font_ids)}

    def packed_oam(tid):
        i = fixed_pair_slots[tid]
        return 2 * i + 1 if i < 128 else 2 * (i - 128)

    # Resident sprite bank (player_chr.bin): resident tiles only; pose tiles
    # excluded. With the full keen4 bestiary (incl. Wormmouth + Mimrock) the
    # resident set exceeds one 256-tile table, which is expected on MMC5 (each
    # level loads only its own enemies via gen_mmc5_rom's per-level pack).
    # player_chr.bin is the legacy 8x8 image only; cap it so the file stays
    # well-formed and warn.
    chr_data = b"".join(tiles[:min(resident_count, 256)])
    if resident_count > 256:
        print(f"[spr] NOTE resident={resident_count} > 256: MMC5 per-level "
              f"packing required (player_chr.bin capped)")
    (OUT / "player_chr.bin").write_bytes(chr_data)

    # emit C metasprite tables in neslib oam_meta_spr format:
    # {dx, dy, tile, attr} * n, terminator 128. Also flipped variants.
    lines = ["// generated by gen_player_data.py\n#include \"player.h\"\n"]
    names = []

    # REAL metasprite ints for every slot (global tile ids as Python ints;
    # pose slots carry ids >= resident_count = >255 which don't fit a u8). The
    # manifest hands these to gen_mmc5_rom, which repacks the flex tiles into
    # per-level ids <= 255. The COMPILED player.c can't hold >255 tile bytes,
    # so pose slots are emitted as their FALLBACK pose (STAND / JUMP1, resident
    # ids), padded to reserve room for the real (per-level) pose: this is what
    # legacy path draws (LOOK/DEATH -> STAND/JUMP, unchanged) and the MMC5 boot
    # default before level_load overwrites ms_wram with the per-level image.
    POSE_FALLBACK = {"LOOKU": "STAND", "LOOKD1": "STAND", "LOOKD2": "STAND",
                     "DEATH": "JUMP1", "POLE1": "STAND", "POLE2": "STAND",
                     "POLE3": "STAND", "HANG": "STAND", "PULL1": "STAND",
                     "PULL2": "STAND", "PULL3": "STAND", "PULL4": "STAND"}

    def slot_ints(name, flip):
        fm = frames_meta[name]
        w = fm["width"]
        cx, cy = fm["crop"]
        out = []
        for tx, ty, tile, pal in fm["sprites"]:
            dx = cx + (w - 8 - tx) if flip else cx + tx
            attr = (0x40 | pal) if flip else pal
            out += [dx, cy + ty, tile, attr]
        out.append(128)
        return out

    ms_real = {}       # slot name -> real ints (global tile ids)
    for name, fm in frames_meta.items():
        for flip in (0, 1):
            nm = f"ms_{name}_{'L' if flip else 'R'}"
            real = slot_ints(name, flip)
            ms_real[nm] = real
            names.append(nm)
            if name in POSE_FALLBACK:
                # compile the fallback pose, padded so the per-level real pose
                # (possibly longer) fits the same fixed ms_wram slot.
                fb = slot_ints(POSE_FALLBACK[name], flip)
                emit = fb + [128] * max(0, len(real) - len(fb))
            else:
                emit = real
            lines.append(f"const unsigned char {nm}[] = {{ "
                         + ", ".join(str(v) for v in emit) + " };\n")
    # frame lookup tables [anim][dir]
    lines.append("const unsigned char *const ms_frames[][2] = {\n")
    order = ["STAND", "RUN1", "RUN2", "RUN3", "RUN4", "JUMP1", "JUMP2",
             "JUMP3", "POGO1", "POGO2",
             "LOOKU", "LOOKD1", "LOOKD2", "DEATH",
             "POLE1", "POLE2", "POLE3"]
    for o in order:
        lines.append(f"  {{ ms_{o}_R, ms_{o}_L }},\n")
    lines.append("};\n")
    # dummy stub so the OTHER episode's tables always link
    lines.append("static const unsigned char ms_none[] = { 128 };\n")
    if BLOOG_FRAMES:
        lines.append("const unsigned char *const ms_bloog[][2] = {\n")
        for o in ["BLOOGW1", "BLOOGW2", "BLOOGW1", "BLOOGW2"]:
            lines.append(f"  {{ ms_{o}_R, ms_{o}_L }},\n")
        lines.append("};\n")
    else:
        lines.append("const unsigned char *const ms_bloog[4][2] = "
                     "{ {ms_none, ms_none}, {ms_none, ms_none}, "
                     "{ms_none, ms_none}, {ms_none, ms_none} };\n")
    lines.append("const unsigned char *const ms_bloogstun = "
                 + ("ms_BLOOGSTUN_R" if BLOOG_FRAMES else "ms_none") + ";\n")
    # font_tile[] is used DIRECTLY as an OAM tile byte by hud.c (8x16 mode):
    # a font glyph is shared pair id i -> bank tiles (2i,2i+1), so the OAM byte
    # = 2*i+1 (bit0=1 selects the $1000 sprite table, top tile = 2i, blank
    # bottom = 2i+1). Shared pairs keep position==id on every level, so this is
    # a link-time constant. (gen_mmc5_rom asserts font ids < 128.)
    lines.append("const unsigned char font_tile[] = { "
                 + ", ".join(str(packed_oam(font_tiles[c])) for c in glyph_chars)
                 + " };\n")
    lines.append('const char font_chars[] = "' + glyph_chars + '";\n')
    lines.append("const unsigned char *const ms_platform = ms_PLATFORM_R;\n")
    if BABOBBA_FRAMES:
        lines.append("const unsigned char *const ms_bab[][2] = { {ms_BAB1_R, ms_BAB1_L}, {ms_BAB2_R, ms_BAB2_L}, {ms_BAB3_R, ms_BAB3_L} };\n")
    else:
        lines.append("const unsigned char *const ms_bab[3][2] = "
                     "{ {ms_none, ms_none}, {ms_none, ms_none}, {ms_none, ms_none} };\n")
    # blooglets: same CHR, forced whole-frame palette per color variant
    for pal, tag in (((3, "red"), (2, "grn")) if BLOOGLET_FRAMES else ()):
        for name in ["BLET1", "BLET2"]:
            fm = frames_meta[name]
            wpx = fm["width"]
            cx, cy = fm["crop"]
            for flip in (0, 1):
                entries = []
                for tx, ty, tile, _p in fm["sprites"]:
                    dx = cx + (wpx - 8 - tx) if flip else cx + tx
                    attr = (0x40 | pal) if flip else pal
                    entries += [str(dx), str(cy + ty), str(tile), str(attr)]
                entries.append("128")
                lines.append(f"const unsigned char ms_blet_{tag}_{name}_{'L' if flip else 'R'}[] = {{ {', '.join(entries)} }};\n")
        lines.append(f"const unsigned char *const ms_blet_{tag}[][2] = {{\n")
        for name in ["BLET1", "BLET2", "BLET1", "BLET2", "BLET1"]:
            lines.append(f"  {{ ms_blet_{tag}_{name}_R, ms_blet_{tag}_{name}_L }},\n")
        lines.append("};\n")
    if not BLOOGLET_FRAMES:
        for tag in ("red", "grn"):
            lines.append(f"const unsigned char *const ms_blet_{tag}[5][2] = "
                         "{ {ms_none, ms_none}, {ms_none, ms_none}, {ms_none, ms_none}, "
                         "{ms_none, ms_none}, {ms_none, ms_none} };\n")

    # --- Keen 4 enemies: frames of one enemy are aligned in a shared
    # bounding box (honoring each frame's sprite origin, unlike the Keen 6
    # tables whose frames all share origin 0,0); L variants mirror about the
    # box so the body stays put when flipping ---
    def emit_enemy(tag, rows, box_names=None):
        box = box_names or rows
        fnames = list(dict.fromkeys(rows + box))

        def fpos(n):
            fm = frames_meta[n]
            return (fm["origin"][0] // 16 + fm["crop"][0],
                    fm["origin"][1] // 16 + fm["crop"][1],
                    fm["width"], fm["height"])
        x0 = min(fpos(n)[0] for n in box)
        y0 = min(fpos(n)[1] for n in box)
        bw = max(fpos(n)[0] + fpos(n)[2] for n in box) - x0
        bh = max(fpos(n)[1] + fpos(n)[3] for n in box) - y0
        for n in fnames:
            ox, oy, _w, _h = fpos(n)
            for flip in (0, 1):
                entries = []
                for tx, ty, tile, pal in frames_meta[n]["sprites"]:
                    dx = ox - x0 + tx
                    if flip:
                        dx = bw - 8 - dx  # may go negative (e.g. Lick tongue)
                    attr = (0x40 | pal) if flip else pal
                    entries += [str(dx), str(oy - y0 + ty), str(tile), str(attr)]
                entries.append("128")
                lines.append(f"const unsigned char ms_{tag}_{n}_{'L' if flip else 'R'}[] "
                             f"= {{ {', '.join(entries)} }};\n")
        lines.append(f"const unsigned char *const ms_{tag}[][2] = {{\n")
        for n in rows:
            lines.append(f"  {{ ms_{tag}_{n}_R, ms_{tag}_{n}_L }},\n")
        lines.append("};\n")
        return bw, bh

    def stub_table(tag, nrows):
        lines.append(f"const unsigned char *const ms_{tag}[{nrows}][2] = {{ "
                     + ", ".join("{ms_none, ms_none}" for _ in range(nrows))
                     + " };\n")

    if SLUG_FRAMES:  # rows 0-1 crawl cycle (x2 to match ms_bloog indexing)
        slug_wh = emit_enemy("slug", ["SLUG1", "SLUG2", "SLUG1", "SLUG2"],
                             box_names=["SLUG1", "SLUG2", "SLUGSTUN"])
        lines.append("const unsigned char *const ms_slugstun = ms_slug_SLUGSTUN_R;\n")
    else:
        slug_wh = (1, 1)
        stub_table("slug", 4)
        lines.append("const unsigned char *const ms_slugstun = ms_none;\n")
    if MUSH_FRAMES:
        mush_wh = emit_enemy("mush", ["MUSH1", "MUSH2"])
    else:
        mush_wh = (1, 1)
        stub_table("mush", 2)
    if LICK_FRAMES:  # rows: 0 ground, 1 air, 2-3 flame, 4 stun; body box
        # excludes the flame tongue (it pokes out ahead of the box)
        lick_wh = emit_enemy("lick", ["LICKG", "LICKA", "LICKF1", "LICKF2", "LICKSTUN"],
                             box_names=["LICKG", "LICKA", "LICKSTUN"])
    else:
        lick_wh = (1, 1)
        stub_table("lick", 5)
    if SKYPEST_FRAMES:  # rows 0-1 fly cycle; SKYSQUASH = squished harmless
        skypest_wh = emit_enemy("skypest", ["SKY1", "SKY2"],
                                box_names=["SKY1", "SKY2", "SKYSQUASH"])
        lines.append("const unsigned char *const ms_skypestsquash = "
                     "ms_skypest_SKYSQUASH_R;\n")
    else:
        skypest_wh = (1, 1)
        stub_table("skypest", 2)
        lines.append("const unsigned char *const ms_skypestsquash = ms_none;\n")
    if BOUNDER_FRAMES:  # rows 0-1 bounce cycle (reused for horizontal); stun
        bounder_wh = emit_enemy("bounder", ["BNDF1", "BNDF2"],
                                box_names=["BNDF1", "BNDF2", "BNDSTUN"])
        lines.append("const unsigned char *const ms_bounderstun = "
                     "ms_bounder_BNDSTUN_R;\n")
    else:
        bounder_wh = (1, 1)
        stub_table("bounder", 2)
        lines.append("const unsigned char *const ms_bounderstun = ms_none;\n")

    # Wormmouth: row 0 = hint/walk, row 1 = bite (lethal); body box spans all
    # incl. stun. Draw picks row by state (walk vs bite).
    if WORM_FRAMES:
        worm_wh = emit_enemy("worm", ["WORMHINT", "WORMBITE"],
                             box_names=["WORMHINT", "WORMBITE", "WORMSTUN"])
        lines.append("const unsigned char *const ms_wormstun = "
                     "ms_worm_WORMSTUN_R;\n")
    else:
        worm_wh = (1, 1)
        stub_table("worm", 2)
        lines.append("const unsigned char *const ms_wormstun = ms_none;\n")
    # Mimrock: rows 0 sit, 1-2 walk (sneak), 3 bonk (airborne/lethal); box
    # spans sit/walk/bonk/stun.
    if MIM_FRAMES:
        mim_wh = emit_enemy("mim", ["MIMSIT", "MIMWALK1", "MIMWALK2", "MIMBONK"],
                            box_names=["MIMSIT", "MIMWALK1", "MIMBONK", "MIMSTUN"])
        lines.append("const unsigned char *const ms_mimstun = "
                     "ms_mim_MIMSTUN_R;\n")
    else:
        mim_wh = (1, 1)
        stub_table("mim", 4)
        lines.append("const unsigned char *const ms_mimstun = ms_none;\n")

    # --- Keen 5 enemies (aligned shared-box emission like Keen 4) ---
    if SPARKY_FRAMES:  # 2-frame walk ping-pong in a 4-row walker table
        sparky_wh = emit_enemy("sparky",
                               ["SPARKY1", "SPARKY2", "SPARKY1", "SPARKY2"],
                               box_names=["SPARKY1", "SPARKY2", "SPARKYSTUN"])
        lines.append("const unsigned char *const ms_sparkystun = "
                     "ms_sparky_SPARKYSTUN_R;\n")
    else:
        sparky_wh = (1, 1)
        stub_table("sparky", 4)
        lines.append("const unsigned char *const ms_sparkystun = ms_none;\n")
    if AMPTON_FRAMES:
        ampton_wh = emit_enemy("ampton",
                               ["AMPTON1", "AMPTON2", "AMPTON1", "AMPTON2"],
                               box_names=["AMPTON1", "AMPTON2", "AMPTONSTUN"])
        lines.append("const unsigned char *const ms_amptonstun = "
                     "ms_ampton_AMPTONSTUN_R;\n")
    else:
        ampton_wh = (1, 1)
        stub_table("ampton", 4)
        lines.append("const unsigned char *const ms_amptonstun = ms_none;\n")
    if SLICE_FRAMES:
        slice_wh = emit_enemy("slice", ["SLICE"])
    else:
        slice_wh = (1, 1)
        stub_table("slice", 1)
    lines.append("const unsigned char *const ms_shot[4] = "
                 "{ ms_STUN1_R, ms_STUN2_R, ms_STUNHIT1_R, ms_STUNHIT1_R };\n")
    # NES sprite palettes (16 bytes; backdrop + 3 colors x 4)
    palbytes = []
    for p in (SPR_PALS + [[0, 0, 0]] * 4)[:4]:
        palbytes.append(0x0F)
        palbytes += [EGA_TO_NES[c] for c in p]
    lines.append(f"const unsigned char spr_pal[16] = {{ "
                 + ", ".join(str(b) for b in palbytes) + " };\n")

    # --- metasprite byte arrays -> WRAM ---------------------------------
    # The ~2KB of ms_* byte tables were the single biggest tenant of the
    # contested 24KB fixed PRG region. They are read every frame with the
    # LEVEL bank mapped at $8000, so a switched bank can't hold them
    # directly — but WRAM ($6000) is ALWAYS mapped: the load image goes
    # in the hud bank (.prg_rom_6, plenty free) and main() copies it into
    # ms_wram[] at boot. Each array becomes "#define name (ms_wram+off)",
    # so the pointer DIRECTORIES keep compiling to link-time constants
    # and the draw path is completely unchanged.
    import re as _re
    _pat = _re.compile(
        r"^(?:static )?const unsigned char (ms_\w+)\[\] = \{ (.*) \};\n$")
    blob, off, out = [], 0, []
    slot_layout = {}   # ms slot name -> (offset, length) in ms_wram
    for ln in lines:
        m = _pat.match(ln)
        if not m:
            out.append(ln)
            continue
        nm, body = m.groups()
        vals = [int(v) for v in body.split(",")]
        if nm.startswith(("ms_HANG_", "ms_PULL")):
            # Ledge metasprites are remapped by gen_mmc5_rom and emitted into
            # draw bank 26 beside player_draw. Keeping five two-direction
            # frames in saturated WRAM would cost ~340 bytes unnecessarily.
            continue
        out.append(f"#define {nm} (ms_wram + {off})\n")
        slot_layout[nm] = (off, len(vals))
        blob += vals
        off += len(vals)
    out.append(f"unsigned char ms_wram[{off}]; /* .bss -> PRG-RAM */\n")
    # ms_load is a BUILD-TIME image consumed from spr_manifest.json by the
    # per-level packer. Do not also link its ~3.6KB duplicate into bank 6.
    lines = out
    (GEN / "player.c").write_text("".join(lines))

    # --- per-level sprite-CHR manifest (consumed by gen_mmc5_rom, MMC5_EX) ---
    # gen_mmc5_rom repacks the sprite bank PER LEVEL: the SHARED tiles (Keen
    # locomotion + stunner + platform + font) stay at their fixed global ids;
    # the FLEX region (enemy tiles + Keen's LOOK/DEATH pose tiles) is packed
    # fresh per level from whatever that level's bestiary leaves free, and the
    # per-level ms_load image is rewritten with the remapped tile ids. Only
    # tiles a level actually references are loaded, so LOOK/DEATH poses fit
    # wherever a level's enemy set is small enough (a dense all-bestiary level
    # keeps the STAND/JUMP fallback -- a hard 256-tile-per-table NES limit).
    import json as _json
    SHARED_GROUPS = ["keen", "shot", "misc", "font"]  # always resident, fixed ids
    ENEMY_GROUPS = ["slug", "mush", "lick", "skypest", "bounder", "worm", "mim",
                    "bloog", "blet", "bab", "sparky", "ampton", "slice"]
    POSE_GROUP_NAMES = ["look", "death", "pole", "ledge"]
    # classify each ms slot: shared | <enemy-tag> | pose. Enemy slots are
    # named ms_<tag>_<FRAME>_<R|L>; pose slots ms_<LOOKU|LOOKD1|LOOKD2|DEATH>_.
    POSE_FRAMES = (set(LOOK_FRAMES) | set(DEATH_FRAMES) | set(POLE_FRAMES)
                   | set(LEDGE_FRAMES))
    ENEMY_FRAMESETS = {
        "slug": SLUG_FRAMES, "mush": MUSH_FRAMES, "lick": LICK_FRAMES,
        "skypest": SKYPEST_FRAMES, "bounder": BOUNDER_FRAMES,
        "worm": WORM_FRAMES, "mim": MIM_FRAMES, "bloog": BLOOG_FRAMES,
        "blet": BLOOGLET_FRAMES, "bab": BABOBBA_FRAMES,
        "sparky": SPARKY_FRAMES, "ampton": AMPTON_FRAMES,
        "slice": SLICE_FRAMES,
    }

    def slot_kind(nm):
        for tag in ENEMY_GROUPS:
            if nm.startswith("ms_" + tag + "_"):
                return tag
            # build_frame also emits the raw ms_<FRAME> slots which the
            # actor-specific ms_<tag>_<FRAME> aliases copy. They are not
            # shared data: classify them with their actor so per-level packing
            # does not accidentally pull the entire bestiary into every ROM.
            for fn in ENEMY_FRAMESETS.get(tag, {}):
                if nm == f"ms_{fn}_R" or nm == f"ms_{fn}_L":
                    return tag
        for fn in POSE_FRAMES:
            if nm == f"ms_{fn}_R" or nm == f"ms_{fn}_L":
                return "pose"
        return "shared"

    manifest = {
        "resident_count": resident_count,
        "n_tiles": len(tiles),
        "tiles_hex": [t.hex() for t in tiles],
        "tile_group": tile_group,
        "shared_groups": SHARED_GROUPS,
        "enemy_groups": [g for g in ENEMY_GROUPS if g in set(tile_group)],
        "pose_groups": POSE_GROUP_NAMES,
        "ms_wram_len": off,
        "slots": {nm: list(slot_layout[nm]) for nm in slot_layout},
        "slot_kind": {nm: slot_kind(nm) for nm in slot_layout},
        # REAL pose slots only (their global tile ids exceed 255, so the
        # compiled ms_load carries the STAND/JUMP fallback instead). Every
        # other slot is already real in ms_load; gen_mmc5_rom reads it there.
        "ms_real": {nm: ms_real[nm] for fn in POSE_FRAMES
                    for nm in (f"ms_{fn}_R", f"ms_{fn}_L") if nm in ms_real},
        "ms_load": blob,           # compiled default (poses = fallback)
        "fixed_pair_slots": {str(k): v for k, v in fixed_pair_slots.items()},
    }
    (OUT / "spr_manifest.json").write_text(_json.dumps(manifest))
    _pg = {}
    for i, g in enumerate(tile_group):
        _pg.setdefault(g, [i, i])
        _pg[g][1] = i
    print("[spr] resident=%d flex(pose)=%d groups=%s"
          % (resident_count, len(tiles) - resident_count,
             {g: (_pg[g][1] - _pg[g][0] + 1) for g in _pg}))

    hdr = ["#ifndef PLAYER_H\n#define PLAYER_H\n"]
    hdr.append(f"#define MS_WRAM_LEN {off}\n")
    hdr.append("extern unsigned char ms_wram[];\n")
    for i, o in enumerate(order):
        hdr.append(f"#define FRAME_{o} {i}\n")
    for i, o in enumerate(("HANG", "PULL1", "PULL2", "PULL3", "PULL4"),
                          start=len(order)):
        hdr.append(f"#define FRAME_{o} {i}\n")
    hdr.append("extern const unsigned char *const ms_frames[][2];\n")
    hdr.append("extern const unsigned char *const ms_ledge[][2];\n")
    hdr.append("extern const unsigned char *const ms_bloog[][2];\n")
    hdr.append("extern const unsigned char *const ms_bloogstun;\n")
    hdr.append("extern const unsigned char *const ms_shot[4];\n")
    hdr.append("extern const unsigned char *const ms_platform;\n")
    hdr.append("extern const unsigned char *const ms_blet_red[][2];\n")
    hdr.append("extern const unsigned char *const ms_blet_grn[][2];\n")
    hdr.append("extern const unsigned char *const ms_bab[][2];\n")
    hdr.append("extern const unsigned char *const ms_slug[][2];\n")
    hdr.append("extern const unsigned char *const ms_slugstun;\n")
    hdr.append("extern const unsigned char *const ms_mush[][2];\n")
    hdr.append("extern const unsigned char *const ms_lick[][2];\n")
    hdr.append("extern const unsigned char *const ms_sparky[][2];\n")
    hdr.append("extern const unsigned char *const ms_sparkystun;\n")
    hdr.append("extern const unsigned char *const ms_ampton[][2];\n")
    hdr.append("extern const unsigned char *const ms_amptonstun;\n")
    hdr.append("extern const unsigned char *const ms_slice[][2];\n")
    hdr.append("extern const unsigned char *const ms_skypest[][2];\n")
    hdr.append("extern const unsigned char *const ms_skypestsquash;\n")
    hdr.append("extern const unsigned char *const ms_bounder[][2];\n")
    hdr.append("extern const unsigned char *const ms_bounderstun;\n")
    hdr.append("extern const unsigned char *const ms_worm[][2];\n")
    hdr.append("extern const unsigned char *const ms_wormstun;\n")
    hdr.append("extern const unsigned char *const ms_mim[][2];\n")
    hdr.append("extern const unsigned char *const ms_mimstun;\n")
    hdr.append(f"#define WORM_W {worm_wh[0]}\n#define WORM_H {worm_wh[1]}\n")
    hdr.append(f"#define MIM_W {mim_wh[0]}\n#define MIM_H {mim_wh[1]}\n")
    hdr.append(f"#define SKYPEST_W {skypest_wh[0]}\n"
               f"#define SKYPEST_H {skypest_wh[1]}\n")
    hdr.append(f"#define BOUNDER_W {bounder_wh[0]}\n"
               f"#define BOUNDER_H {bounder_wh[1]}\n")
    hdr.append(f"#define SLUG_W {slug_wh[0]}\n#define SLUG_H {slug_wh[1]}\n")
    hdr.append(f"#define MUSH_W {mush_wh[0]}\n#define MUSH_H {mush_wh[1]}\n")
    hdr.append(f"#define LICK_W {lick_wh[0]}\n#define LICK_H {lick_wh[1]}\n")
    hdr.append(f"#define SPARKY_W {sparky_wh[0]}\n"
               f"#define SPARKY_H {sparky_wh[1]}\n")
    hdr.append(f"#define AMPTON_W {ampton_wh[0]}\n"
               f"#define AMPTON_H {ampton_wh[1]}\n")
    hdr.append(f"#define SLICE_W {slice_wh[0]}\n"
               f"#define SLICE_H {slice_wh[1]}\n")
    if "BAB1" in frames_meta:
        bb = frames_meta["BAB1"]
        hdr.append(f"#define BAB_W {bb['width']}\n#define BAB_H {bb['height']}\n")
    else:
        hdr.append("#define BAB_W 1\n#define BAB_H 1\n")
    if "BLET1" in frames_meta:
        bl = frames_meta["BLET1"]
        hdr.append(f"#define BLET_W {bl['width']}\n#define BLET_H {bl['height']}\n")
    else:
        hdr.append("#define BLET_W 1\n#define BLET_H 1\n")
    order2 = [font_tiles[c] for c in "0123456789"]
    hdr.append("#define FONT_DIGIT0 " + str(packed_oam(font_tiles["0"])) + "\n")
    # HUD stat-icon OAM tile bytes (bank-A odd encoding, same as font_tile[]).
    hdr.append("#define HUD_ICON_LIFE " + str(packed_oam(icon_tiles["life"])) + "\n")
    hdr.append("#define HUD_ICON_AMMO " + str(packed_oam(icon_tiles["ammo"])) + "\n")
    hdr.append("#define HUD_ICON_GEM " + str(packed_oam(icon_tiles["gem"])) + "\n")
    pw, ph = frames_meta["PLATFORM"]["width"], frames_meta["PLATFORM"]["height"]
    hdr.append(f"#define PLAT_W {pw}\n#define PLAT_H {ph}\n")
    hdr.append("extern const unsigned char spr_pal[16];\n")
    hdr.append("extern const unsigned char font_tile[];\n")
    hdr.append("extern const char font_chars[];\n")
    if "BLOOGW1" in frames_meta:
        bw, bh = frames_meta["BLOOGW1"]["width"], frames_meta["BLOOGW1"]["height"]
    else:
        bw, bh = 1, 1
    hdr.append(f"#define BLOOG_W {bw}\n#define BLOOG_H {bh}\n")
    # clip box of standing frame, in Keen global units (16/px)
    clip = frames_meta["STAND"]["clip"]
    org = frames_meta["STAND"]["origin"]
    hdr.append(f"#define KEEN_CLIP_XL {clip[0]}\n#define KEEN_CLIP_YL {clip[1]}\n")
    hdr.append(f"#define KEEN_CLIP_XH {clip[2]}\n#define KEEN_CLIP_YH {clip[3]}\n")
    hdr.append(f"#define KEEN_ORG_X {org[0]}\n#define KEEN_ORG_Y {org[1]}\n")
    hdr.append("#endif\n")
    (GEN / "player.h").write_text("".join(hdr))

    print(f"player: {len(tiles)} sprite CHR tiles, {len(frames_meta)} frames")
    for name, fm in frames_meta.items():
        print(f"  {name}: {fm['width']}x{fm['height']} sprites={len(fm['sprites'])} clip={fm['clip']} org={fm['origin']}")


if __name__ == "__main__":
    main()
