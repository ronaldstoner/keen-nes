#!/usr/bin/env python3
"""Decoders for the id Galaxy-engine (Keen 4-6) data formats: Carmack, RLEW,
Huffman decompression and the EGAGraph/GameMaps/AudioT table layouts.
MIT License; see LICENSE.
"""
import os
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- episode selection (KEEN_EP env var: 4 or 6, default 6) ---
EP = int(os.environ.get("KEEN_EP", "6"))

EPISODES = {
    6: dict(
        orig="original/Commander_Keen_6",
        # chunk layout (== retail v1.4; validated by decompression)
        NUM_BITMAPS=37, NUM_MASKED=3, NUM_SPRITES=390,
        OFF_BITMAPS=6, OFF_MASKED=43, OFF_SPRITES=46,
        OFF_TILE8=436, NUM_TILE8=104, OFF_TILE8M=437, NUM_TILE8M=12,
        OFF_TILE16=438, NUM_TILE16=2376, OFF_TILE16M=2814, NUM_TILE16M=2736,
        OFF_BINARIES=5550, NUM_BINARIES=5, OFF_DEMOS=5555, NUM_DEMOS=5,
        NUM_CHUNKS=5560,
        NUM_SOUNDS=60, START_ADLIB=60, START_DIGI=120, START_MUSIC=180,
        NUM_SONGS=9, NUM_SND_CHUNKS=189,
    ),
    5: dict(
        orig="original/Commander_Keen_5",
        # Keen 5 v1.4 EGA graphics-header layout (chunk counts/offsets)
        NUM_BITMAPS=93, NUM_MASKED=3, NUM_SPRITES=346,
        OFF_BITMAPS=6, OFF_MASKED=99, OFF_SPRITES=102,
        OFF_TILE8=448, NUM_TILE8=104, OFF_TILE8M=449, NUM_TILE8M=20,
        OFF_TILE16=450, NUM_TILE16=1512, OFF_TILE16M=1962, NUM_TILE16M=2952,
        OFF_BINARIES=4914, NUM_BINARIES=17, OFF_DEMOS=4926, NUM_DEMOS=5,
        NUM_CHUNKS=4931,
        NUM_SOUNDS=64, START_ADLIB=64, START_DIGI=128, START_MUSIC=192,
        NUM_SONGS=14, NUM_SND_CHUNKS=206,
    ),
    4: dict(
        orig="original/Commander_Keen_4",
        # Keen 4 v1.4 EGA (shareware/Apogee) graphics-header layout
        NUM_BITMAPS=115, NUM_MASKED=3, NUM_SPRITES=397,
        OFF_BITMAPS=6, OFF_MASKED=121, OFF_SPRITES=124,
        OFF_TILE8=521, NUM_TILE8=104, OFF_TILE8M=522, NUM_TILE8M=20,
        OFF_TILE16=523, NUM_TILE16=1296, OFF_TILE16M=1819, NUM_TILE16M=2916,
        OFF_BINARIES=4735, NUM_BINARIES=16, OFF_DEMOS=4747, NUM_DEMOS=4,
        NUM_CHUNKS=4751,
        NUM_SOUNDS=52, START_ADLIB=52, START_DIGI=104, START_MUSIC=156,
        NUM_SONGS=6, NUM_SND_CHUNKS=162,
    ),
}
CFG = EPISODES[EP]
ORIG = ROOT / CFG["orig"]
EXT = ROOT / f"assets/extracted/ck{EP}"
EXT.mkdir(parents=True, exist_ok=True)


def orig_file(stem):
    """case-insensitive lookup of e.g. 'egagraph.ck4' in the originals dir"""
    want = stem.lower()
    for f in ORIG.iterdir():
        if f.name.lower() == want:
            return f
    raise FileNotFoundError(f"{want} in {ORIG}")

# 16-color EGA palette (RGB)
EGA_PALETTE = [
    (0x00, 0x00, 0x00), (0x00, 0x00, 0xAA), (0x00, 0xAA, 0x00), (0x00, 0xAA, 0xAA),
    (0xAA, 0x00, 0x00), (0xAA, 0x00, 0xAA), (0xAA, 0x55, 0x00), (0xAA, 0xAA, 0xAA),
    (0x55, 0x55, 0x55), (0x55, 0x55, 0xFF), (0x55, 0xFF, 0x55), (0x55, 0xFF, 0xFF),
    (0xFF, 0x55, 0x55), (0xFF, 0x55, 0xFF), (0xFF, 0xFF, 0x55), (0xFF, 0xFF, 0xFF),
]

# episode chunk/audio layout
NUM_BITMAPS = CFG["NUM_BITMAPS"]; NUM_MASKED = CFG["NUM_MASKED"]
NUM_SPRITES = CFG["NUM_SPRITES"]
OFF_BITMAPS = CFG["OFF_BITMAPS"]; OFF_MASKED = CFG["OFF_MASKED"]
OFF_SPRITES = CFG["OFF_SPRITES"]
OFF_TILE8 = CFG["OFF_TILE8"]; NUM_TILE8 = CFG["NUM_TILE8"]
OFF_TILE8M = CFG["OFF_TILE8M"]; NUM_TILE8M = CFG["NUM_TILE8M"]
OFF_TILE16 = CFG["OFF_TILE16"]; NUM_TILE16 = CFG["NUM_TILE16"]
OFF_TILE16M = CFG["OFF_TILE16M"]; NUM_TILE16M = CFG["NUM_TILE16M"]
OFF_BINARIES = CFG["OFF_BINARIES"]; NUM_BINARIES = CFG["NUM_BINARIES"]
OFF_DEMOS = CFG["OFF_DEMOS"]; NUM_DEMOS = CFG["NUM_DEMOS"]
NUM_CHUNKS = CFG["NUM_CHUNKS"]

RLE_TAG = 0xABCD

NUM_SOUNDS = CFG["NUM_SOUNDS"]
START_PC = 0
START_ADLIB = CFG["START_ADLIB"]; START_DIGI = CFG["START_DIGI"]
START_MUSIC = CFG["START_MUSIC"]
NUM_SONGS = CFG["NUM_SONGS"]; NUM_SND_CHUNKS = CFG["NUM_SND_CHUNKS"]


def huff_expand(comp: bytes, dictionary: bytes, exp_len: int) -> bytes:
    """id Huffman expansion: LSB-first bit walk of a 255-node tree, root 254.
    Node values < 256 emit a byte; >= 256 jump to node (v - 256)."""
    words = struct.unpack("<510H", dictionary[:1020])
    out = bytearray()
    node = 254
    for byte in comp:
        for _ in range(8):
            v = words[node * 2 + (byte & 1)]
            byte >>= 1
            if v < 256:
                out.append(v)
                if len(out) == exp_len:
                    return bytes(out)
                node = 254
            else:
                node = v - 256
    if len(out) != exp_len:
        raise ValueError(f"huffman underrun: {len(out)}/{exp_len}")
    return bytes(out)


def carmack_expand(src: bytes, exp_len: int) -> bytes:
    """id Carmack expansion: word stream with 0xA7 (near) / 0xA8 (far) tags."""
    out = []
    pos = 0
    words_left = exp_len // 2
    while words_left > 0:
        ch = struct.unpack_from("<H", src, pos)[0]
        pos += 2
        high, count = ch >> 8, ch & 0xFF
        if high == 0xA7:
            if count == 0:  # escaped literal: A7xx stored as tag + low byte
                out.append(0xA700 | src[pos])
                pos += 1
                words_left -= 1
            else:  # near: copy count words from (here - offset)
                offset = src[pos]
                pos += 1
                base = len(out) - offset
                for i in range(count):
                    out.append(out[base + i])
                words_left -= count
        elif high == 0xA8:
            if count == 0:
                out.append(0xA800 | src[pos])
                pos += 1
                words_left -= 1
            else:  # far: copy count words from absolute word index
                offset = struct.unpack_from("<H", src, pos)[0]
                pos += 2
                for i in range(count):
                    out.append(out[offset + i])
                words_left -= count
        else:
            out.append(ch)
            words_left -= 1
    return struct.pack(f"<{len(out)}H", *out)


def rlew_expand(src: bytes, exp_len: int, tag: int = RLE_TAG) -> bytes:
    out = bytearray()
    pos = 0
    while len(out) < exp_len:
        w = struct.unpack_from("<H", src, pos)[0]
        pos += 2
        if w == tag:
            count, value = struct.unpack_from("<HH", src, pos)
            pos += 4
            out += struct.pack("<H", value) * count
        else:
            out += struct.pack("<H", w)
    return bytes(out)


class EgaGraph:
    """Chunk-level access to EGAGRAPH.CK6 via extracted EGAHEAD/EGADICT."""

    def __init__(self):
        self.data = orig_file(f"egagraph.ck{EP}").read_bytes()
        self.dict = (EXT / "EGADICT.BIN").read_bytes()
        head = (EXT / "EGAHEAD.BIN").read_bytes()
        self.offsets = [int.from_bytes(head[i:i + 3], "little")
                        for i in range(0, len(head), 3)]

    def chunk_compressed(self, i: int):
        o = self.offsets[i]
        if o == 0xFFFFFF:
            return None
        j = i + 1
        while self.offsets[j] == 0xFFFFFF:
            j += 1
        return self.data[o:self.offsets[j]]

    def chunk(self, i: int) -> bytes | None:
        """Expanded chunk. Tile chunks have implicit sizes; others carry a
        uint32 expanded-length prefix."""
        comp = self.chunk_compressed(i)
        if comp is None or len(comp) == 0:
            return None
        if OFF_TILE8 <= i < OFF_BINARIES:
            if i == OFF_TILE8:
                exp = NUM_TILE8 * 32
            elif i == OFF_TILE8M:
                exp = NUM_TILE8M * 40
            elif OFF_TILE16 <= i < OFF_TILE16 + NUM_TILE16:
                exp = 128
            else:
                exp = 160
            return huff_expand(comp, self.dict, exp)
        exp = struct.unpack_from("<i", comp)[0]
        return huff_expand(comp[4:], self.dict, exp)


def planar_to_indices(planes: bytes, width_bytes: int, height: int,
                      nplanes: int = 4, mask_first: bool = False):
    """Convert EGA planar data to (indices, mask) 2D lists.
    Plane order: [mask,] blue, green, red, intensity; plane-major storage."""
    plane_size = width_bytes * height
    idx = [[0] * (width_bytes * 8) for _ in range(height)]
    mask = [[1] * (width_bytes * 8) for _ in range(height)]
    order = list(range(nplanes))
    for p in order:
        plane = planes[p * plane_size:(p + 1) * plane_size]
        is_mask = mask_first and p == 0
        color_bit = p - 1 if mask_first else p
        for y in range(height):
            for xb in range(width_bytes):
                b = plane[y * width_bytes + xb]
                for bit in range(8):
                    px = (b >> (7 - bit)) & 1
                    x = xb * 8 + bit
                    if is_mask:
                        mask[y][x] = px  # 1 = transparent
                    elif px:
                        idx[y][x] |= 1 << color_bit
    return idx, mask


class TileInfo:
    """MAPHEAD-appended tile properties. Layout (id Galaxy TILEINFO format):
    [numTiles16 x animspd (TI_BackAnimTime)]
    [numTiles16 x animtile offset, int8 (TI_BackAnimTile)]
    then per tile16m, arrays in order: top, right, bottom, left, animtile,
    misc, animspd (7 x numTiles16m)."""

    def __init__(self):
        self.data = (EXT / "TILEINFO.BIN").read_bytes()
        self.base = NUM_TILE16 * 2

    def fore(self, array_idx, tile):
        return self.data[self.base + NUM_TILE16M * array_idx + tile]

    def top(self, t):
        return self.fore(0, t)

    def right(self, t):
        return self.fore(1, t)

    def bottom(self, t):
        return self.fore(2, t)

    def left(self, t):
        return self.fore(3, t)

    def misc(self, t):
        return self.fore(5, t)

    # --- tile animation (id Galaxy tile-animation convention) ---
    @staticmethod
    def _s8(v):
        return v - 256 if v >= 128 else v

    def bg_anim_offset(self, t):
        """TI_BackAnimTile: signed offset to the next tile16 in the chain."""
        return self._s8(self.data[NUM_TILE16 + t])

    def bg_anim_speed(self, t):
        """TI_BackAnimTime: tics per animation step (0 = not animated)."""
        return self.data[t]

    def fg_anim_offset(self, t):
        """TI_ForeAnimTile: signed offset to the next tile16m in the chain."""
        return self._s8(self.fore(4, t))

    def fg_anim_speed(self, t):
        """TI_ForeAnimTime: tics per animation step (0 = not animated)."""
        return self.fore(6, t)

    def _cycle(self, t, off, spd, maxlen=32):
        """Animation cycle starting at tile t: [t, t+off, ...] until the
        chain loops back to t (id Galaxy tile-refresh animation semantics: each
        step advances by the CURRENT tile's offset). Returns None if the
        tile is not animated (offset or speed 0) or the chain does not
        return to t within maxlen steps (dead-end / oversized loop)."""
        if off(t) == 0 or spd(t) == 0:
            return None
        cyc = [t]
        cur = t
        for _ in range(maxlen):
            nxt = cur + off(cur)
            if nxt == t:
                return cyc
            if off(cur) == 0 or nxt in cyc:
                return None  # dead-end or inner loop not through t
            cyc.append(nxt)
            cur = nxt
        return None  # longer than maxlen

    def bg_anim_cycle(self, t, maxlen=32):
        return self._cycle(t, self.bg_anim_offset, self.bg_anim_speed, maxlen)

    def fg_anim_cycle(self, t, maxlen=32):
        return self._cycle(t, self.fg_anim_offset, self.fg_anim_speed, maxlen)


class GameMaps:
    def __init__(self):
        self.data = orig_file(f"gamemaps.ck{EP}").read_bytes()
        head = (EXT / "MAPHEAD.BIN").read_bytes()
        self.tag = struct.unpack_from("<H", head, 0)[0]
        self.offsets = struct.unpack_from("<100i", head, 2)

    def level(self, n: int):
        off = self.offsets[n]
        if off <= 0:
            return None
        (po0, po1, po2, pl0, pl1, pl2, w, h) = struct.unpack_from("<3i3H2H", self.data, off)
        name = self.data[off + 22:off + 38].split(b"\0")[0].decode("ascii", "replace")
        planes = []
        for po, pl in ((po0, pl0), (po1, pl1), (po2, pl2)):
            raw = self.data[po:po + pl]
            carmack_len = struct.unpack_from("<H", raw)[0]
            expanded = carmack_expand(raw[2:], carmack_len)
            # first word of carmack output = RLEW-expanded size; skip it
            plane = rlew_expand(expanded[2:], w * h * 2, self.tag)
            planes.append(struct.unpack(f"<{w * h}H", plane))
        return {"name": name, "width": w, "height": h, "planes": planes}


class AudioFile:
    def __init__(self):
        self.data = orig_file(f"audio.ck{EP}").read_bytes()
        self.dict = (EXT / "AUDIODICT.BIN").read_bytes()
        head = (EXT / "AUDIOHED.BIN").read_bytes()
        self.offsets = struct.unpack(f"<{len(head) // 4}i", head)

    def chunk(self, i: int) -> bytes | None:
        o0, o1 = self.offsets[i], self.offsets[i + 1]
        if o1 - o0 <= 4:
            return None
        exp = struct.unpack_from("<i", self.data, o0)[0]
        if exp <= 0:
            return None
        return huff_expand(self.data[o0 + 4:o1], self.dict, exp)
