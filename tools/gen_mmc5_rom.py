#!/usr/bin/env python3
"""Pack the emitted keen4 MMC5 ExRAM level blobs + CHR into the engine ROM
image. Reads assets/converted/ck4/levelNN/{mmc5.bin,mmc5_chr.bin} (format in
src/level_fmt.h) and emits:

  src/gen/leveldata_mmc5.c  -- CHR-ROM (.chr_rom = bg ExRAM banks + 4KB
                               sprites) + each level blob split into 8KB
                               .prg_rom_<bank> chunks (avoiding the reserved
                               code banks 6-12) + lvl_blob_refs[]/lvl_bank[]/
                               lvl_game_no[] tables the engine reads.
  src/gen/levels.h          -- EPISODE, NUM_LEVELS, PRG_ROM_KB, SPR_CHR_BANK,
                               CHR_ROM_KB, TOTAL_CHR_KB, CHR_UPPER. Consumed
                               by main.c (MAPPER size macros), level.c, and
                               gen_title.py/gen_status.py.

The ExRAM CHR bank is an ABSOLUTE 4KB index into CHR-ROM, but the converter
emits per-level RELATIVE banks (0..k). Here we concatenate all levels' bg CHR
(keen4: 10+7+8+5 = 30 banks < 64, so chr_upper=0) and REWRITE each level's MT
ExRAM bank fields += that level's CHR bank offset so they address the absolute
banks. The 4KB player sprite CHR (player_chr.bin) follows the bg banks (4KB-bank
30 = 1KB-bank 120 = SPR_CHR_BANK). Title CHR (titledata.c, .chr_rom.title) is
appended after that by the linker.
"""
import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("KEEN_EP", "4")
EP = int(os.environ["KEEN_EP"])
ROOT = Path(__file__).resolve().parent.parent
# Level set: CLI args (must match gen_mmc5_level.py), else the keen4 demo set.
LEVELS = [int(a) for a in sys.argv[1:]] or [1, 2, 3, 4]
BANK = 8192            # 8KB PRG bank
CHRBANK = 4096         # 4KB CHR bank
FIXED_BANKS = 3        # last 24KB = fixed engine region (banked-8 layout)
# 8KB PRG banks owned by other generators (shared bank map): HUD(6),
# music(7,9,10,11), sfx(8), title(12). Level blobs must skip them and each
# level's banks must be CONTIGUOUS (the reader computes spill banks as
# base_bank + (off>>13)).
RESERVED_BANKS = frozenset((6, 7, 8, 9, 10, 11, 12, 26))

# MT SoA field order (level_fmt.h): tl tr bl br  tl_ex tr_ex bl_ex br_ex  top flags
EX_FIELDS = (4, 5, 6, 7)   # the four ExRAM-byte fields to rewrite
MMC5_MT_FIELDS = 10        # SoA arrays per metatile (level_fmt.h)


def load(n):
    d = ROOT / f"assets/converted/ck{EP}/level{n:02d}"
    return bytearray((d / "mmc5.bin").read_bytes()), \
        bytearray((d / "mmc5_chr.bin").read_bytes())


# ---- per-level sprite CHR packing (see gen_player_data.py spr_manifest) ----
# Keen's full frame set + a dense level's whole bestiary exceed the 256-tile
# sprite pattern table (a hard NES limit). So the sprite bank is packed PER
# LEVEL: SHARED tiles (Keen locomotion + stunner + platform + font) stay at
# their fixed global ids on every level; the FLEX slots hold only THAT level's
# enemies, and Keen's LOOK/DEATH pose tiles fill whatever headroom the enemy
# set leaves (priority DEATH > LOOK-up > LOOK-down). The per-level ms_load
# image is rewritten with the remapped tile ids; poses that don't fit keep the
# compiled STAND/JUMP fallback already in the shared image.
ENT_REC = (5, 3, 4, 3, 3, 2, 2, 4)     # level_fmt.h entity-slot record sizes


def level_enemy_tags(blob):
    """Which episode-specific enemy groups spawn at any difficulty."""
    tags = set()

    def slot(i):
        off = struct.unpack_from("<I", blob, 40 + i * 6)[0]
        cnt = struct.unpack_from("<H", blob, 40 + i * 6 + 4)[0]
        return off, cnt
    if EP == 4:
        if slot(1)[1]: tags.add("slug")
        if slot(3)[1]: tags.add("lick")
        off2, c2 = slot(2)
        for j in range(c2):
            t = blob[off2 + j * ENT_REC[2] + 2]
            tags.add({0: "mush", 1: "skypest", 2: "bounder",
                      3: "worm", 4: "mim"}.get(t, "mush"))
    elif EP == 5:
        if slot(1)[1]: tags.add("sparky")
        if slot(2)[1]: tags.add("ampton")
        if slot(3)[1]: tags.add("slice")
    else:
        if slot(1)[1]: tags.add("bloog")
        if slot(2)[1]: tags.add("blet")
        if slot(3)[1]: tags.add("bab")
    return tags


def pack_level_sprite_pairs(mani, tags):
    """Pack one level's independently-colored 8x8 halves as 8x16 pairs.
    Shared Keen/font pairs retain ids 0..119; only this level's enemies and
    real LOOK/DEATH poses consume flex ids. Runtime pays only CHR bank swaps."""
    tiles = [bytes.fromhex(h) for h in mani["tiles_hex"]]
    tg, slots = mani["tile_group"], mani["slots"]
    kinds, real, ms0 = mani["slot_kind"], mani["ms_real"], mani["ms_load"]
    shared = set(mani["shared_groups"])

    active_real_pose = set()

    def content(nm):
        if nm in active_real_pose:
            return real[nm]
        off, ln = slots[nm]
        if off < 0:  # banked-only (map Keen / flag): real global-id template
            return real.get(nm, [128])
        return ms0[off:off + ln]

    active_core = [nm for nm in slots
                   if kinds[nm] == "shared" or kinds[nm] in tags]

    def collect_required():
        out = set()
        for nm in active_core:
            src, i = content(nm), 0
            while i < len(src) and src[i] != 128:
                out.add(src[i + 2])
                i += 4
        return out

    required = collect_required()

    # Keep every shared pair, including HUD font pairs referenced directly by
    # OAM constants rather than an ms slot.
    shared_ids = [i for i, group in enumerate(tg) if group in shared]
    assert shared_ids
    required.update(shared_ids)

    def real_tids(frames):
        out = set()
        for frame in frames:
            for side in ("R", "L"):
                src, i = real.get(f"ms_{frame}_{side}", []), 0
                while i < len(src) and src[i] != 128:
                    out.add(src[i + 2])
                    i += 4
        return out

    # Normal bank: death first, then cosmetic look poses while they fit. Pole
    # art lives in a transition-switched overlay bank built below, so it costs
    # no enemy frames and no normal-bank capacity.
    for frames in (("DEATH",), ("LOOKU",), ("LOOKD1",), ("LOOKD2",)):
        names = [f"ms_{frame}_{side}" for frame in frames for side in ("R", "L")]
        tids = set()
        for nm in names:
            src, i = real.get(nm, []), 0
            while i < len(src) and src[i] != 128:
                tids.add(src[i + 2])
                i += 4
        if len(required | tids) <= 256:
            required.update(tids)
            active_real_pose.update(n for n in names if n in real)

    active = set(active_core) | {nm for nm in slots if kinds[nm] == "pose"}
    mapping = {int(old): new for old, new in mani["fixed_pair_slots"].items()}
    free = iter([i for i in range(256) if i not in set(mapping.values())])
    for tid in shared_ids:
        if tid not in mapping:
            mapping[tid] = next(free)
    for tid in sorted(required):
        if tid not in mapping:
            mapping[tid] = next(free)
    assert len(mapping) <= 256, (tags, len(mapping))

    bank_a = [bytes(16)] * 256
    bank_b = [bytes(16)] * 256
    for old, new in mapping.items():
        bank, slot = (bank_a, new) if new < 128 else (bank_b, new - 128)
        pair = tiles[old]
        bank[slot * 2] = pair[:16]
        bank[slot * 2 + 1] = pair[16:]

    # Pole overlay: preserve every concurrent enemy/HUD/projectile slot, and
    # replace 17 Keen-locomotion-only positions with the DOS SHINNY patterns.
    # The bank is selected only while pl_pole is true, so those displaced run/
    # jump patterns are never requested. This buys exact pole art without
    # trimming a single enemy animation or doing per-frame tile uploads.
    protected = {i for i, group in enumerate(tg) if group != "keen"}
    for nm in active_core:
        if kinds[nm] in tags:
            src, i = content(nm), 0
            while i < len(src) and src[i] != 128:
                protected.add(src[i + 2])
                i += 4
    by_page = {}
    for old in sorted(required):
        if old in mapping and tg[old] == "keen" and old not in protected \
                and mapping[old] < 128:
            by_page.setdefault(mapping[old] // 32, []).append(old)
    page, replaceable = max(by_page.items(), key=lambda kv: len(kv[1]))

    def build_overlay(frames):
        tids = sorted(real_tids(frames))
        assert len(replaceable) >= len(tids), \
            f"not enough Keen-only overlay slots in one 1KB page: {len(replaceable)} < {len(tids)}"
        omap = {old: mapping[replaceable[i]] for i, old in enumerate(tids)}
        obank = list(bank_a)
        for old, new in omap.items():
            pair = tiles[old]
            obank[new * 2] = pair[:16]
            obank[new * 2 + 1] = pair[16:]
        return b"".join(obank[page * 64:(page + 1) * 64]), omap

    pole_page, pole_mapping = build_overlay(("POLE1", "POLE2", "POLE3"))
    ledge_page, ledge_mapping = build_overlay(
        ("HANG", "PULL1", "PULL2", "PULL3", "PULL4"))

    def oam(i):
        return 2 * i + 1 if i < 128 else 2 * (i - 128)

    ms = list(ms0)
    mapkeen_ms = {}
    for nm, (off, ln) in slots.items():
        if off < 0:
            # Map Keen banked-only: remap into bank-26 tables
            if kinds.get(nm) not in tags and "mapkeen" not in tags:
                continue
            src = real.get(nm) or content(nm)
            if not src:
                continue
            out, i = [], 0
            while i < len(src) and src[i] != 128:
                dx, dy, tid, at = src[i:i + 4]
                out += [dx, dy, oam(mapping[tid]), at]
                i += 4
            out.append(128)
            mapkeen_ms[nm] = bytes(v & 0xFF for v in out)
            continue
        if nm not in active:
            ms[off:off + ln] = [128] * ln
            continue
        is_pole = nm.startswith("ms_POLE") and nm in real
        is_ledge = (nm.startswith("ms_HANG") or nm.startswith("ms_PULL")) and nm in real
        src = real[nm] if (is_pole or is_ledge) else content(nm)
        tile_map = pole_mapping if is_pole else ledge_mapping if is_ledge else mapping
        out, i = [], 0
        while i < len(src) and src[i] != 128:
            dx, dy, tid, at = src[i:i + 4]
            out += [dx, dy, oam(tile_map[tid]), at]
            i += 4
        out.append(128)
        out += [128] * (ln - len(out))
        assert len(out) == ln, (nm, len(out), ln)
        ms[off:off + ln] = out
    ledge_ms = {}
    for frame in ("HANG", "PULL1", "PULL2", "PULL3", "PULL4"):
        for side in ("R", "L"):
            nm = f"ms_{frame}_{side}"
            src, out, i = real[nm], [], 0
            while i < len(src) and src[i] != 128:
                dx, dy, tid, at = src[i:i + 4]
                out += [dx, dy, oam(ledge_mapping[tid]), at]
                i += 4
            out.append(128)
            ledge_ms[nm] = bytes(v & 0xFF for v in out)
    return (b"".join(bank_a) + b"".join(bank_b),
            pole_page, ledge_page, 4 + page, ms, ledge_ms, mapkeen_ms, len(mapping),
            sorted(nm for nm in active_real_pose if nm.endswith("_R")))



def realign_mt(blob):
    """Insert padding so the MT hot-AoS/collision-SoA begins at an 8KB PRG-bank
    boundary. MT is <=10*N bytes (keen4 max N=782 -> 7820 < 8192), so once
    bank-aligned it occupies exactly ONE bank: the engine's seam renderer maps
    that bank ONCE per pass and decodes every cell with plain indexed reads
    (no per-cell $5114 switch). Shifts every section offset at/after off_mt
    (off_mt, off_palsets, off_spans, reserved36, the 8 entity-dir u32 offs);
    ROWS/MAP precede off_mt and are untouched. Returns the padded blob."""
    off_mt = struct.unpack_from("<I", blob, 24)[0]
    pad = (-off_mt) % BANK
    if pad == 0:
        return blob
    N = struct.unpack_from("<H", blob, 8)[0]
    assert MMC5_MT_FIELDS * N <= BANK, \
        f"MT {MMC5_MT_FIELDS*N}B > 8KB bank (N={N}); single-bank layout needs it to fit"
    blob[off_mt:off_mt] = b"\0" * pad
    for o in (24, 28, 32):                            # section u32 offsets (mt,
        v = struct.unpack_from("<I", blob, o)[0]      # palsets, spans). NOT 36:
        if v >= off_mt:                               # bytes 36/37 = anim bank/
            struct.pack_into("<I", blob, o, v + pad)  # count (not an offset).
    ext = struct.unpack_from("<H", blob, 38)[0]      # gem-door/switch ext (u16)
    if ext and ext >= off_mt:
        struct.pack_into("<H", blob, 38, ext + pad)
    for i in range(8):                               # entity-dir u32 offsets
        o = 40 + i * 6
        v = struct.unpack_from("<I", blob, o)[0]
        if v >= off_mt:
            struct.pack_into("<I", blob, o, v + pad)
    return blob


def rewrite_exram_banks(blob, chr_off_banks):
    """Add the level's CHR bank offset to every MT ExRAM byte's bank field
    (low 6 bits) AND to the header's animation base bank (byte 36), so per-cell
    banks and the anim region address the absolute concatenated CHR."""
    N = struct.unpack_from("<H", blob, 8)[0]
    off_mt = struct.unpack_from("<I", blob, 24)[0]
    for k in EX_FIELDS:
        for i in range(N):
            p = off_mt + i * 8 + k
            ex = blob[p]
            bank = (ex & 0x3F) + chr_off_banks
            assert bank < 64, f"absolute CHR bank {bank} >= 64 (needs $5130)"
            blob[p] = (ex & 0xC0) | bank
    # animation region: byte 14 = F, byte 36 = LOCAL base bank, byte 37 =
    # region size in banks. Offset the base; assert the whole region fits.
    F = blob[14]
    n_anim_banks = blob[37]
    if F > 1 and n_anim_banks:
        base = blob[36] + chr_off_banks
        assert base + n_anim_banks - 1 < 64, \
            f"anim region top bank {base + n_anim_banks - 1} >= 64 (needs $5130)"
        blob[36] = base


def alloc_banks(part_counts):
    """Assign each level a contiguous run of free banks, skipping
    RESERVED_BANKS. Returns (first bank per level, PRG KB needed)."""
    firsts = []
    nxt = 0
    for n in part_counts:
        while any((nxt + i) in RESERVED_BANKS for i in range(n)):
            nxt += 1
        firsts.append(nxt)
        nxt += n
    prg_kb = 128
    while nxt > prg_kb // 8 - FIXED_BANKS:
        prg_kb *= 2
    assert prg_kb <= 1024, "MMC5 PRG limit"
    return firsts, prg_kb


def main():
    # ---- gather, assign CHR offsets, rewrite ExRAM banks ----
    chr_all = bytearray()
    per_level = []
    chr_off = 0                                  # 4KB banks so far
    for n in LEVELS:
        blob, chrom = load(n)
        assert len(chrom) % CHRBANK == 0
        nbanks = len(chrom) // CHRBANK
        realign_mt(blob)                 # MT -> its own 8KB bank (seam fast path)
        rewrite_exram_banks(blob, chr_off)
        per_level.append(dict(n=n, blob=blob, chr_off=chr_off, nbanks=nbanks))
        chr_all += chrom
        chr_off += nbanks
    bg_chr_banks = chr_off
    assert bg_chr_banks <= 64, f"{bg_chr_banks} CHR banks exceed 64"

    # Keep the ExRAM-addressed all-zero seam bank below 64 alongside gameplay
    # backgrounds. Sprite overlay banks may safely live above it because the
    # sprite CHR registers have their own wider bank addressing.
    seam_blank_bank = len(chr_all) // CHRBANK
    assert seam_blank_bank < 64, "blank CHR bank >= 64 (needs $5130 upper bits)"
    chr_all += bytearray(CHRBANK)

    # ---- per-level sprite CHR: two 4KB banks per level. This keeps gameplay
    # in fast 8x16 mode while allowing EVERY actor's top/bottom halves to use
    # the independent palette choices of the known-good 8x8 build. ----
    import json as _json
    mani = _json.loads(
        (ROOT / f"assets/converted/ck{EP}/spr_manifest.json").read_text())
    ms_len = mani["ms_wram_len"]
    page_lut = {}
    def add_page(p):
        p = bytes(p)
        if p not in page_lut:
            page_lut[p] = len(chr_all) // 1024
            chr_all.extend(p)
        return page_lut[p]
    mapkeen_ms_best = {}
    for lv in per_level:
        tags = level_enemy_tags(lv["blob"])
        # World map (GAMEMAPS 0): pack map Keen (+ flag), not combat bestiary.
        if lv["n"] == 0:
            tags.add("mapkeen")
        (spr_chr, pole_chr, ledge_chr, overlay_slot, ms, ledge_ms,
         mapkeen_ms, used_pairs, poses) = pack_level_sprite_pairs(mani, tags)
        assert (len(spr_chr) == 2 * CHRBANK and len(pole_chr) == 1024
                and len(ledge_chr) == 1024) \
            and len(ms) == ms_len
        # Pool identical 1KB pages globally. Shared Keen/HUD pages deduplicate
        # across levels, leaving the pole overlay below MMC5's 8-bit CHR-bank
        # ceiling without changing any per-level tile ids.
        a = [spr_chr[i:i + 1024] for i in range(0, 4096, 1024)]
        b = [spr_chr[i:i + 1024] for i in range(4096, 8192, 1024)]
        lv["spr_pages"] = [add_page(p) for p in b + a]  # $5120..$5127 order
        lv["pole_page"] = add_page(pole_chr)
        lv["ledge_page"] = add_page(ledge_chr)
        lv["overlay_slot"] = overlay_slot
        lv["spr_tags"] = sorted(tags)
        lv["spr_pairs"] = used_pairs
        lv["spr_poses"] = poses
        lv["ms"] = ms
        lv["ledge_ms"] = ledge_ms
        if mapkeen_ms:
            mapkeen_ms_best = mapkeen_ms
    # 1KB page indices may exceed 255 once bg (4KB banks) + sprites grow past
    # 256KB; the engine writes $5130 upper bits + $5120 low byte (10-bit bank).
    assert len(chr_all) // 1024 <= 1024, \
        f"gameplay CHR pages exceed MMC5 1MB: {len(chr_all)//1024}"
    while len(chr_all) % CHRBANK:
        chr_all.extend(bytes(1024))
    ledge_ms = per_level[0]["ledge_ms"]
    assert all(lv["ledge_ms"] == ledge_ms for lv in per_level), \
        "pooled ledge metasprite ids must be identical across levels"

    # ---- reserved all-zero "blank" CHR bank (vertical-scroll overscan seam) --
    # The 30-row nametable + vertical mirroring alias the 31st (partial) row: the
    # seam renderer (main.c) keeps that row -- which is always entirely in the
    # top/bottom 8px overscan -- black by pointing its cells at an all-zero CHR
    # bank (every pixel = color 0 = backdrop = $0F black). We reserve an explicit
    # zero bank here (instead of relying on incidental CHR padding, which shifts
    # as sprite/title CHR grows) and export SEAM_BLANK_EX. It sits after the bg +
    # sprite banks, before the title CHR (which links via CHR_ROM_KB, so it just
    # moves up one bank). CHR_UPPER = 0, so the ExRAM byte is the bank index.
    chr_rom_kb = len(chr_all) // 1024                # bg + sprites + blank (title after)

    # total CHR-ROM includes the appended title (currently up to 12KB) and
    # the final status-font section.  Reserve 16KB before power-of-two
    # rounding (8KB left too little headroom for K6).
    total_kb = 8
    while total_kb < chr_rom_kb + 16:
        total_kb <<= 1
    assert total_kb & (total_kb - 1) == 0

    # ---- assign PRG banks: level blobs skipping reserved code banks ----
    for lv in per_level:
        lv["nprg"] = (len(lv["blob"]) + BANK - 1) // BANK
    firsts, prg_kb = alloc_banks([lv["nprg"] for lv in per_level])
    for lv, f in zip(per_level, firsts):
        lv["base_bank"] = f

    # One remapped metasprite image per level -- a cheap ROM trade for exact
    # 8x8 palette freedom with no runtime calculation.
    used = set(RESERVED_BANKS)
    for lv in per_level:
        used.update(range(lv["base_bank"], lv["base_bank"] + lv["nprg"]))
    b = 0
    for lv in per_level:
        while b in used:
            b += 1
        lv["ms_bank"] = b
        used.add(b)
        b += 1
    while max(used) + 1 > prg_kb // 8 - FIXED_BANKS:
        prg_kb *= 2
    assert prg_kb <= 1024, "MMC5 PRG limit"

    # ---- emit leveldata_mmc5.c ----
    outc = ROOT / "src/gen/leveldata_mmc5.c"
    outc.parent.mkdir(parents=True, exist_ok=True)
    L = ["// GENERATED by tools/gen_mmc5_rom.py -- keen4 MMC5 ExRAM ROM image.",
         ""]

    def carr(name, data, sect):
        s = [f'__attribute__((used, section("{sect}"))) '
             f"const unsigned char {name}[{len(data)}] = {{"]
        for i in range(0, len(data), 24):
            s.append("  " + ",".join(str(b) for b in data[i:i + 24]) + ",")
        s.append("};")
        return "\n".join(s)

    # CHR-ROM (bg banks + sprites) split into <=32KB C arrays; the compiler
    # caps a single array below 64KB, and the pieces link contiguously into
    # .chr_rom in definition order. Title CHR (.chr_rom.title) lands after.
    CHUNK = 32768
    for ci in range(0, len(chr_all), CHUNK):
        L.append(carr(f"all_chr_{ci // CHUNK}", chr_all[ci:ci + CHUNK],
                      ".chr_rom"))
        L.append("")

    # Ledge frames are only dereferenced by player_draw while bank 26 is
    # mapped, so keep their ~340B metasprite commands beside that routine
    # instead of consuming saturated gameplay WRAM.
    for nm, data in ledge_ms.items():
        L.append(carr(nm, data, ".prg_rom_26"))
    L.append('__attribute__((used, section(".prg_rom_26"))) '
             'const unsigned char *const ms_ledge[][2] = {')
    for frame in ("HANG", "PULL1", "PULL2", "PULL3", "PULL4"):
        L.append(f"  {{ ms_{frame}_R, ms_{frame}_L }},")
    L.append("};")
    L.append("")

    # Map Keen frames (bank 26): remapped tile ids for world-map sprite CHR.
    # Flag metasprite is bank 6 (map_flags_draw). Not in ms_wram.
    if mapkeen_ms_best:
        for nm, data in sorted(mapkeen_ms_best.items()):
            if nm.startswith("ms_FLAG"):
                continue
            L.append(carr(nm, data, ".prg_rom_26"))
        # dir order 0N 1NE 2E 3SE 4S 5SW 6W 7NW; diagonals alias cardinals
        mk_alias = ("N", "E", "E", "S", "S", "W", "W", "N")
        L.append('__attribute__((used, section(".prg_rom_26"))) '
                 'const unsigned char *const ms_mapkeen[8][3] = {')
        for d in mk_alias:
            # walk frame twice (no separate walk2 in the lean set), then stand
            w1 = f"ms_MK_{d}_W1_R"
            st = f"ms_MK_{d}_ST_R"
            if w1 not in mapkeen_ms_best:
                w1 = st = "ms_MK_E_ST_R"  # fallback
            L.append(f"  {{ {w1}, {w1}, {st} }},")
        L.append("};")
    else:
        L.append('__attribute__((used, section(".prg_rom_26"))) '
                 'static const unsigned char ms_mapkeen_none[] = {128};')
        L.append('__attribute__((used, section(".prg_rom_26"))) '
                 'const unsigned char *const ms_mapkeen[8][3] = {')
        for _ in range(8):
            L.append("  { ms_mapkeen_none, ms_mapkeen_none, ms_mapkeen_none },")
        L.append("};")
    # Planted flag metasprite → bank 6 (map_flags_draw).
    if mapkeen_ms_best and "ms_FLAG_R" in mapkeen_ms_best:
        L.append(carr("ms_FLAG_R_b6", mapkeen_ms_best["ms_FLAG_R"], ".prg_rom_6"))
        L.append('__attribute__((used, section(".prg_rom_6"))) '
                 'const unsigned char *const ms_mapflag = ms_FLAG_R_b6;')
    else:
        L.append('__attribute__((used, section(".prg_rom_6"))) '
                 'static const unsigned char ms_mapflag_none[] = {128};')
        L.append('__attribute__((used, section(".prg_rom_6"))) '
                 'const unsigned char *const ms_mapflag = ms_mapflag_none;')
    L.append("")

    # each level blob -> 8KB chunks in .prg_rom_<bank>
    refs = {}
    for lv in per_level:
        blob = lv["blob"]
        for ci in range((len(blob) + BANK - 1) // BANK):
            bank = lv["base_bank"] + ci
            chunk = blob[ci * BANK: (ci + 1) * BANK]
            nm = f"lvl{lv['n']}_bank{ci}"
            L.append(carr(nm, chunk, f".prg_rom_{bank}"))
            L.append("")
            refs[bank] = nm

    for lv in per_level:
        L.append(carr(f"lvl{lv['n']}_ms", bytes(v & 0xFF for v in lv["ms"]),
                      f".prg_rom_{lv['ms_bank']}"))
        L.append("")

    # Level index tables: keep lvl_bank + lvl_game_no in the fixed region
    # (read from hot paths). Bulk tables live in bank 6 (copied in level_load).
    maxbank = max(refs) if refs else 0
    L.append("__attribute__((used)) const unsigned char lvl_bank[] = { "
             + ", ".join(str(lv["base_bank"]) for lv in per_level) + " };")
    L.append("__attribute__((used)) const unsigned char lvl_game_no[] = { "
             + ", ".join(str(lv["n"]) for lv in per_level) + " };")
    # Relative 1KB page indices from SPR_PAGE_BASE (keeps tables u8-sized).
    all_pages = []
    for lv in per_level:
        all_pages.extend(lv["spr_pages"])
        all_pages.append(lv["pole_page"])
        all_pages.append(lv["ledge_page"])
    spr_page_base = min(all_pages) if all_pages else 0
    assert max(all_pages) - spr_page_base < 256, \
        f"sprite page span {max(all_pages)-spr_page_base} exceeds u8 relative"
    T6 = '.prg_rom_6'  # cold level tables (level_load maps bank 6 to read)
    L.append(f'__attribute__((used, section("{T6}"))) const unsigned char '
             f'*const lvl_blob_refs[] = {{')
    for b in range(maxbank + 1):
        L.append(f"  {refs.get(b, '0')},")
    L.append("};")
    L.append(f'__attribute__((used, section("{T6}"))) '
             f'const unsigned char lvl_spr_pages[][8] = {{')
    for lv in per_level:
        rel = [p - spr_page_base for p in lv["spr_pages"]]
        L.append("  { " + ", ".join(str(v) for v in rel) + " },")
    L.append("};")
    L.append(f'__attribute__((used, section("{T6}"))) '
             f'const unsigned char lvl_pole_page[] = {{ '
             + ", ".join(str(lv["pole_page"] - spr_page_base)
                         for lv in per_level) + " };")
    L.append(f'__attribute__((used, section("{T6}"))) '
             f'const unsigned char lvl_ledge_page[] = {{ '
             + ", ".join(str(lv["ledge_page"] - spr_page_base)
                         for lv in per_level) + " };")
    L.append(f'__attribute__((used, section("{T6}"))) '
             f'const unsigned char lvl_overlay_slot[] = {{ '
             + ", ".join(str(lv["overlay_slot"]) for lv in per_level) + " };")
    L.append(f'__attribute__((used, section("{T6}"))) '
             f'const unsigned char lvl_ms_bank[] = {{ '
             + ", ".join(str(lv["ms_bank"]) for lv in per_level) + " };")
    L.append(f'__attribute__((used, section("{T6}"))) '
             f'const unsigned char *const lvl_ms_ref[] = {{ '
             + ", ".join(f"lvl{lv['n']}_ms" for lv in per_level) + " };")
    # world-map tables are appended to L below, then written once

    # ---- world-map tables (GAMEMAPS level 0) when included in this ROM ----
    map_json = ROOT / f"assets/converted/ck{EP}/map_nodes.json"
    has_map = 0 in LEVELS and map_json.is_file()
    map_rom_slot = LEVELS.index(0) if 0 in LEVELS else 0xFF
    n_enter = n_fence = n_flag = 0
    if has_map:
        import json
        meta = json.loads(map_json.read_text())
        enters = meta.get("enters", [])
        fences = meta.get("fences", [])
        flags = meta.get("flags", [])
        n_enter, n_fence, n_flag = len(enters), len(fences), len(flags)
        playable = {n for n in LEVELS if n != 0}

        def carr_u8(name, vals, sec=".prg_rom_26"):
            body = ", ".join(str(v & 0xFF) for v in vals) if vals else "0"
            return (f'__attribute__((used, section("{sec}"))) '
                    f'const unsigned char {name}[] = {{ {body} }};')

        L.append("/* world map nodes (from map_nodes.json) */")
        L.append(carr_u8("map_enter_x", [e[0] for e in enters]))
        L.append(carr_u8("map_enter_y", [e[1] for e in enters]))
        L.append(carr_u8("map_enter_lv", [e[2] for e in enters]))
        L.append(carr_u8("map_fence_x", [f[0] for f in fences]))
        L.append(carr_u8("map_fence_y", [f[1] for f in fences]))
        L.append(carr_u8("map_fence_lv", [f[2] for f in fences]))
        L.append(carr_u8("map_fence_mt_lo", [f[3] & 0xFF for f in fences]))
        L.append(carr_u8("map_fence_mt_hi", [(f[3] >> 8) & 0xFF for f in fences]))
        # Flag holders: bank 6 (with map_flags_draw).
        L.append(carr_u8("map_flag_x", [f[0] for f in flags], ".prg_rom_6"))
        L.append(carr_u8("map_flag_y", [f[1] for f in flags], ".prg_rom_6"))
        L.append(carr_u8("map_flag_lv", [f[2] for f in flags], ".prg_rom_6"))
        # Enter tiles are game-levels 1..18 (18 = BWB ship) — need that many slots.
        max_glv = max(max(LEVELS), 18)
        rom_of = [0xFF] * (max_glv + 1)
        for slot, gn in enumerate(LEVELS):
            if 0 <= gn <= max_glv:
                rom_of[gn] = slot
        L.append(carr_u8("map_rom_of_lv", rom_of))
        L.append("")
        print(f"  world map: slot={map_rom_slot} enter={n_enter} "
              f"fence={n_fence} flag={n_flag} "
              f"playable_in_rom={sorted(playable)}")
    else:
        def stub(name):
            return (f'__attribute__((used, section(".prg_rom_26"))) '
                    f'const unsigned char {name}[] = {{0}};')
        for nm in ("map_enter_x", "map_enter_y", "map_enter_lv",
                   "map_fence_x", "map_fence_y", "map_fence_lv",
                   "map_fence_mt_lo", "map_fence_mt_hi",
                   "map_flag_x", "map_flag_y", "map_flag_lv"):
            L.append(stub(nm))
        L.append('__attribute__((used, section(".prg_rom_26"))) '
                 'const unsigned char map_rom_of_lv[19] = {'
                 + ", ".join(["255"] * 19) + "};")
        L.append("")

    outc.write_text("\n".join(L) + "\n")

    # ---- emit levels.h (engine-facing) ----
    outh = ROOT / "src/gen/levels.h"
    H = ["#ifndef GEN_LEVELS_H", "#define GEN_LEVELS_H",
         f"#define EPISODE {EP}",
         f"#define NUM_LEVELS {len(LEVELS)}",
         f"#define PRG_ROM_KB {prg_kb} /* level banks skip reserved code banks */",
         f"#define SPR_PAGE_BASE {spr_page_base} /* abs 1KB page = BASE + rel */",
         f"#define SPR_CHR_BANK {per_level[0]['spr_pages'][4]} /* L0 $1000 sprite page (abs) */",
         f"#define SPR_MS_LEN {ms_len} /* per-level ms_wram image size */",
         f"#define CHR_ROM_KB {chr_rom_kb} /* bg ExRAM + normal/pole sprite pairs + blank */",
         f"#define TOTAL_CHR_KB {total_kb} /* power-of-two iNES CHR size */",
         f"#define CHR_UPPER 0 /* $5130: keen4 = {bg_chr_banks} bg banks < 64 */",
         f"#define SEAM_BLANK_EX {seam_blank_bank} /* ExRAM byte: all-zero CHR bank (overscan seam -> black) */",
         f"#define HAS_WORLD_MAP {1 if has_map else 0}",
         f"#define MAP_ROM_SLOT {map_rom_slot if has_map else 0xFF}",
         # info 0xC001..0xC012 => game levels 1..18 (18 = BWB ship).
         f"#define MAP_MAX_GAME_LV {max(max(LEVELS), 18) if has_map else 17}",
         f"#define MAP_SHIP_LV 18 /* BWB ship: always re-enterable */",
         f"#define MAP_N_ENTER {n_enter}",
         f"#define MAP_N_FENCE {n_fence}",
         f"#define MAP_N_FLAG {n_flag}",
         "extern const unsigned char map_enter_x[], map_enter_y[], map_enter_lv[];",
         "extern const unsigned char map_fence_x[], map_fence_y[], map_fence_lv[];",
         "extern const unsigned char map_fence_mt_lo[], map_fence_mt_hi[];",
         "extern const unsigned char map_flag_x[], map_flag_y[], map_flag_lv[];",
         "extern const unsigned char map_rom_of_lv[]; /* game-level -> ROM slot or 0xFF */",
         "#endif", ""]
    outh.write_text("\n".join(H))

    print(f"keen{EP} MMC5 ROM image: PRG {prg_kb}KB, CHR {total_kb}KB "
          f"(bg {bg_chr_banks} 4KB-banks + pooled sprite/pole pages + blank "
          f"= {chr_rom_kb}KB); title CHR base {chr_rom_kb}KB")
    print(f"  8x16 source: {mani['n_tiles']} independently-colored pairs; "
          f"per-level packed into <=256 with real LOOK/DEATH poses")
    for lv in per_level:
        print(f"  L{lv['n']}: blob {len(lv['blob'])}B banks "
              f"{lv['base_bank']}..{lv['base_bank']+lv['nprg']-1}; "
              f"sprites={lv['spr_pairs']}/256 tags={lv['spr_tags']} "
              f"poses={lv['spr_poses']} pages={lv['spr_pages']} "
              f"overlay=slot{lv['overlay_slot']} pole@{lv['pole_page']} "
              f"ledge@{lv['ledge_page']} msbank={lv['ms_bank']}")


if __name__ == "__main__":
    main()
