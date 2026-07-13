#!/usr/bin/env python3
"""Extract all Keen 4/5/6 (EGA) assets for the current episode (KEEN_EP)
to viewable/convertible forms.

Outputs under assets/extracted/:
  gfx/pics/*.png, gfx/masked/*.png, gfx/sprites/*.png, gfx/fonts/*.png
  gfx/tile8.png, gfx/tile8m.png, gfx/tile16.png, gfx/tile16m.png (sheets)
  gfx/sprites.json (origins/clip metadata from spritetable)
  maps/NN_name.png (rendered), maps/NN_name.json + .bin (planes)
  audio/sfx_pc_NN.bin, audio/sfx_al_NN.bin, audio/music_N.imf
"""
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import keenlib as K
from PIL import Image

OUT = K.EXT
GFX = OUT / "gfx"
MAPS = OUT / "maps"
AUD = OUT / "audio"
for d in (GFX / "pics", GFX / "masked", GFX / "sprites", GFX / "fonts", MAPS, AUD):
    d.mkdir(parents=True, exist_ok=True)


def to_image(idx, mask=None):
    h, w = len(idx), len(idx[0])
    img = Image.new("RGBA", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            if mask is not None and mask[y][x]:
                px[x, y] = (0, 0, 0, 0)
            else:
                r, g, b = K.EGA_PALETTE[idx[y][x]]
                px[x, y] = (r, g, b, 255)
    return img


def main():
    ega = K.EgaGraph()

    # --- dimension tables ---
    pictable = struct.unpack(f"<{K.NUM_BITMAPS * 2}H", ega.chunk(0))
    picmtable = struct.unpack(f"<{K.NUM_MASKED * 2}H", ega.chunk(1))
    sprtable_raw = ega.chunk(2)
    sprites_meta = []
    for i in range(K.NUM_SPRITES):
        w, h, ox, oy, xl, yl, xh, yh, sh = struct.unpack_from("<2H6hH", sprtable_raw, i * 18)
        sprites_meta.append(dict(width_bytes=w, height=h, origin=[ox, oy],
                                 clip=[xl, yl, xh, yh], shifts=sh))
    (GFX / "sprites.json").write_text(json.dumps(sprites_meta, indent=0))

    # --- fonts (id font format: uint16 height, 256 uint16 offsets, 256 widths) ---
    for i in range(3):
        data = ega.chunk(3 + i)
        if not data:
            continue
        height = struct.unpack_from("<H", data)[0]
        offs = struct.unpack_from("<256H", data, 2)
        widths = data[514:514 + 256]
        sheet = Image.new("RGBA", (16 * 16, 16 * height))
        for c in range(256):
            if widths[c] == 0 or offs[c] == 0:
                continue
            wb = (widths[c] + 7) // 8
            glyph = data[offs[c]:offs[c] + wb * height]
            if len(glyph) < wb * height:
                continue
            for y in range(height):
                for x in range(widths[c]):
                    if glyph[y * wb + x // 8] >> (7 - x % 8) & 1:
                        sheet.putpixel(((c % 16) * 16 + x, (c // 16) * height + y),
                                       (255, 255, 255, 255))
        sheet.save(GFX / "fonts" / f"font{i}.png")

    # --- pics ---
    for i in range(K.NUM_BITMAPS):
        data = ega.chunk(K.OFF_BITMAPS + i)
        if not data:
            continue
        wb, h = pictable[i * 2], pictable[i * 2 + 1]
        idx, _ = K.planar_to_indices(data, wb, h)
        to_image(idx).save(GFX / "pics" / f"pic{i:02d}.png")

    # --- masked pics ---
    for i in range(K.NUM_MASKED):
        data = ega.chunk(K.OFF_MASKED + i)
        if not data:
            continue
        wb, h = picmtable[i * 2], picmtable[i * 2 + 1]
        idx, mask = K.planar_to_indices(data, wb, h, nplanes=5, mask_first=True)
        to_image(idx, mask).save(GFX / "masked" / f"maskpic{i}.png")

    # --- sprites ---
    n_sprites = 0
    for i in range(K.NUM_SPRITES):
        data = ega.chunk(K.OFF_SPRITES + i)
        if not data:
            continue
        m = sprites_meta[i]
        wb, h = m["width_bytes"], m["height"]
        if wb * h * 5 > len(data):
            continue
        idx, mask = K.planar_to_indices(data, wb, h, nplanes=5, mask_first=True)
        to_image(idx, mask).save(GFX / "sprites" / f"sprite{i:03d}.png")
        n_sprites += 1

    # --- tile sheets ---
    def tile_sheet(first, count, size, masked, out_name, per_chunk=True):
        cols = 32
        rows = (count + cols - 1) // cols
        sheet = Image.new("RGBA", (cols * size, rows * size))
        present = 0
        if per_chunk:
            for t in range(count):
                data = ega.chunk(first + t)
                if data is None:
                    continue
                idx, mask = K.planar_to_indices(data, size // 8, size,
                                                nplanes=5 if masked else 4,
                                                mask_first=masked)
                sheet.paste(to_image(idx, mask if masked else None),
                            ((t % cols) * size, (t // cols) * size))
                present += 1
        else:  # all tiles in one chunk (tile8)
            data = ega.chunk(first)
            tsz = (size // 8) * size * (5 if masked else 4)
            for t in range(count):
                sub = data[t * tsz:(t + 1) * tsz]
                idx, mask = K.planar_to_indices(sub, size // 8, size,
                                                nplanes=5 if masked else 4,
                                                mask_first=masked)
                sheet.paste(to_image(idx, mask if masked else None),
                            ((t % cols) * size, (t // cols) * size))
                present += 1
        sheet.save(GFX / out_name)
        return present

    n8 = tile_sheet(K.OFF_TILE8, K.NUM_TILE8, 8, False, "tile8.png", per_chunk=False)
    n8m = tile_sheet(K.OFF_TILE8M, K.NUM_TILE8M, 8, True, "tile8m.png", per_chunk=False)
    n16 = tile_sheet(K.OFF_TILE16, K.NUM_TILE16, 16, False, "tile16.png")
    n16m = tile_sheet(K.OFF_TILE16M, K.NUM_TILE16M, 16, True, "tile16m.png")

    # --- maps ---
    gm = K.GameMaps()
    tile16_cache = {}
    tile16m_cache = {}

    def get_tile(cache, base, t, masked):
        if t not in cache:
            data = ega.chunk(base + t)
            if data is None:
                cache[t] = None
            else:
                idx, mask = K.planar_to_indices(data, 2, 16,
                                                nplanes=5 if masked else 4,
                                                mask_first=masked)
                cache[t] = to_image(idx, mask if masked else None)
        return cache[t]

    level_stats = []
    for n in range(100):
        lv = gm.level(n)
        if lv is None:
            continue
        w, h = lv["width"], lv["height"]
        bg, fg, info = lv["planes"]
        img = Image.new("RGBA", (w * 16, h * 16))
        for y in range(h):
            for x in range(w):
                t = bg[y * w + x]
                ti = get_tile(tile16_cache, K.OFF_TILE16, t, False)
                if ti:
                    img.paste(ti, (x * 16, y * 16))
                f = fg[y * w + x]
                if f:
                    tm = get_tile(tile16m_cache, K.OFF_TILE16M, f, True)
                    if tm:
                        img.paste(tm, (x * 16, y * 16), tm)
        safe = "".join(c if c.isalnum() else "_" for c in lv["name"]).strip("_") or f"level{n}"
        img.save(MAPS / f"{n:02d}_{safe}.png")
        (MAPS / f"{n:02d}_{safe}.bin").write_bytes(
            struct.pack(f"<{w*h}H", *bg) + struct.pack(f"<{w*h}H", *fg)
            + struct.pack(f"<{w*h}H", *info))
        meta = dict(num=n, name=lv["name"], width=w, height=h,
                    bg_unique=len(set(bg)), fg_unique=len(set(fg)),
                    info_unique=len(set(info)))
        (MAPS / f"{n:02d}_{safe}.json").write_text(json.dumps(meta, indent=1))
        level_stats.append(meta)
        print(f"map {n:2d} '{lv['name']}' {w}x{h}  bg_unique={meta['bg_unique']} fg_unique={meta['fg_unique']}")

    # --- audio ---
    au = K.AudioFile()
    n_pc = n_al = n_mus = 0
    for i in range(K.NUM_SOUNDS):
        c = au.chunk(K.START_PC + i)
        if c:
            (AUD / f"sfx_pc_{i:02d}.bin").write_bytes(c)
            n_pc += 1
        c = au.chunk(K.START_ADLIB + i)
        if c:
            (AUD / f"sfx_al_{i:02d}.bin").write_bytes(c)
            n_al += 1
    for i in range(K.NUM_SONGS):
        c = au.chunk(K.START_MUSIC + i)
        if c:
            # AudioT music chunk: uint16 IMF data length, then IMF type-0 stream
            (AUD / f"music_{i}.imf").write_bytes(c)
            n_mus += 1

    print(f"\npics={K.NUM_BITMAPS} sprites={n_sprites} tile8={n8}/{K.NUM_TILE8} "
          f"tile8m={n8m} tile16={n16}/{K.NUM_TILE16} tile16m={n16m}/{K.NUM_TILE16M}")
    print(f"levels={len(level_stats)} sfx_pc={n_pc} sfx_adlib={n_al} music={n_mus}")


if __name__ == "__main__":
    main()
