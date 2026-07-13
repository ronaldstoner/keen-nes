#!/usr/bin/env python3
"""Locate and extract the asset support tables embedded in an unpacked
Galaxy-engine executable (episode chosen by KEEN_EP env, default 6).

All tables are located heuristically and cross-validated against the
data files, so no per-version offsets are needed:
  EGADICT/AUDIODICT: 255-node Huffman trees validated structurally, then
    assigned by test-decompressing EGAGRAPH chunk 0 (pictable, whose
    expanded size must equal numBitmaps*4).
  MAPHEAD: 0xABCD RLEW tag + 100 plausible level offsets; TILEINFO follows.
  EGAHEAD: anchored at the entry equal to the EGAGRAPH file size, exactly
    NUM_CHUNKS entries back.
  AUDIOHED: reconstructed chunk offsets (greedy Huffman walk) searched as
    a u32 pattern in the EXE.
Outputs episode-neutral names under assets/extracted/ck<EP>/.
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import keenlib as K

EXE = K.EXT / f"keen{K.EP}_unpacked.exe"


def find_huffman_dicts(data):
    hits = []
    for base in range(0, len(data) - 1020, 2):
        words = struct.unpack_from("<510H", data, base)
        if not all(w < 511 for w in words):
            continue
        seen, leaves, stack, ok = set(), set(), [254], True
        while stack and ok:
            n = stack.pop()
            if n in seen:
                ok = False
                break
            seen.add(n)
            for v in (words[n * 2], words[n * 2 + 1]):
                if v < 256:
                    leaves.add(v)
                elif v - 256 < 255:
                    stack.append(v - 256)
                else:
                    ok = False
        if ok and len(leaves) == 256:
            hits.append(base)
    return hits


def huff_expand_count(comp, words, exp_len):
    out = bytearray()
    node = 254
    for i, byte in enumerate(comp):
        for bit in range(8):
            v = words[node * 2 + ((byte >> bit) & 1)]
            if v < 256:
                out.append(v)
                if len(out) == exp_len:
                    return bytes(out), i + 1
                node = 254
            else:
                node = v - 256
    return None


def main():
    exe = EXE.read_bytes()
    ega = K.orig_file(f"egagraph.ck{K.EP}").read_bytes()
    aud = K.orig_file(f"audio.ck{K.EP}").read_bytes()
    gm_size = K.orig_file(f"gamemaps.ck{K.EP}").stat().st_size

    # --- Huffman dictionaries ---
    dicts = find_huffman_dicts(exe)
    print("dict candidates:", [hex(d) for d in dicts])

    # --- EGAHEAD: anchor at file-size entry, walk back NUM_CHUNKS ---
    target = struct.pack("<I", len(ega))[:3]
    egahead = None
    idx = exe.find(target)
    while idx != -1 and egahead is None:
        start = idx - 3 * K.NUM_CHUNKS
        if start >= 0:
            offs = [int.from_bytes(exe[start + i * 3:start + i * 3 + 3], "little")
                    for i in range(K.NUM_CHUNKS + 1)]
            good = all(o == 0xFFFFFF or o <= len(ega) for o in offs)
            mono = [o for o in offs if o != 0xFFFFFF]
            if good and all(a <= b for a, b in zip(mono, mono[1:])) and offs[0] == 0:
                egahead = (start, offs)
        idx = exe.find(target, idx + 1)
    assert egahead, "EGAHEAD not found"
    eh_start, offs = egahead
    print(f"EGAHEAD at {hex(eh_start)} ({K.NUM_CHUNKS} chunks)")

    # --- assign EGADICT: must expand the LARGE spritetable (chunk 2)
    # exactly; small chunks can decode by luck under the wrong dict ---
    explen0 = struct.unpack_from("<i", ega, offs[0])[0]
    assert explen0 == K.NUM_BITMAPS * 4, f"pictable explen {explen0}"
    o2 = offs[2]
    o3 = next(o for o in offs[3:] if o != 0xFFFFFF)
    exp2 = struct.unpack_from("<i", ega, o2)[0]
    assert exp2 == K.NUM_SPRITES * 18, f"spritetable explen {exp2}"
    egadict = None
    for d in dicts:
        words = struct.unpack_from("<510H", exe, d)
        r = huff_expand_count(ega[o2 + 4:o3], words, exp2)
        if r is not None:
            egadict = d
            break
    assert egadict is not None, "EGADICT not identified"
    audiodicts = [d for d in dicts if d != egadict]
    print(f"EGADICT at {hex(egadict)}")

    # --- AUDIODICT + AUDIOHED: greedy-walk audio, search offset pattern ---
    audiohed = None
    audiodict = None
    for d in audiodicts:
        words = struct.unpack_from("<510H", exe, d)
        pos, chunks = 0, [0]
        while pos + 4 <= len(aud) and len(chunks) < 5:
            exp = struct.unpack_from("<i", aud, pos)[0]
            if not (0 < exp < 0x18000):
                break
            r = huff_expand_count(aud[pos + 4:], words, exp)
            if r is None:
                break
            pos += 4 + r[1]
            chunks.append(pos)
        if len(chunks) >= 4:
            pat = struct.pack("<4I", *chunks[:4])
            hit = exe.find(pat)
            if hit != -1:
                audiohed = hit
                audiodict = d
                break
    assert audiohed is not None, (
        "AUDIOHED/AUDIODICT could not be located in the game EXE. Make sure "
        "original/ holds a supported Keen EXE for this episode.")
    print(f"AUDIODICT at {hex(audiodict)}, AUDIOHED at {hex(audiohed)}")

    # --- MAPHEAD (+ TILEINFO appended) ---
    maphead = None
    pos = exe.find(b"\xcd\xab")
    while pos != -1 and maphead is None:
        if pos + 402 <= len(exe):
            lv = struct.unpack_from("<100i", exe, pos + 2)
            real = [o for o in lv if o > 0]
            if real and all(o < gm_size for o in real) and \
               all(b >= a for a, b in zip(real, real[1:])):
                maphead = pos
        pos = exe.find(b"\xcd\xab", pos + 2)
    assert maphead is not None, "MAPHEAD not found"
    ti_len = 2 * K.NUM_TILE16 + 7 * K.NUM_TILE16M
    print(f"MAPHEAD at {hex(maphead)}, TILEINFO {ti_len} bytes")

    out = {
        "EGAHEAD.BIN": exe[eh_start:eh_start + 3 * (K.NUM_CHUNKS + 1)],
        "EGADICT.BIN": exe[egadict:egadict + 1024],
        "MAPHEAD.BIN": exe[maphead:maphead + 402],
        "TILEINFO.BIN": exe[maphead + 402:maphead + 402 + ti_len],
        "AUDIOHED.BIN": exe[audiohed:audiohed + 4 * (K.NUM_SND_CHUNKS + 1)],
        "AUDIODICT.BIN": exe[audiodict:audiodict + 1024],
    }
    for name, data in out.items():
        (K.EXT / name).write_bytes(data)
        print(f"  {name}: {len(data)} bytes")


if __name__ == "__main__":
    main()
