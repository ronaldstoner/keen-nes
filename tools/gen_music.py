#!/usr/bin/env python3
"""Convert AudioT/IMF (OPL2) music to full-APU NES streams.

Input: assets/extracted/ck{EP}/audio/music_N.imf. Each file is an AudioT
music chunk: uint16 data length, then IMF type-0 records of 4 bytes:
u8 OPL register, u8 value, u16 delay in 560Hz ticks AFTER the write.

OPL2 simulation. Beyond the frequency registers
  0xA0+ch : f-number low 8 bits                       (ch = 0..8)
  0xB0+ch : bit5 key-on, bits0-1 f-number high, bits2-4 block
  freq_hz = 49716 * fnum * 2^(block - 20)
we snapshot the channel's patch at every key-on (modulator op offset per
channel = [00 01 02 08 09 0A 10 11 12], carrier = modulator + 3):
  0x40+op : KSL/total level (carrier TL -> note velocity, 0 = loudest)
  0x60+op : attack/decay rate (carrier -> envelope shape)
  0x80+op : sustain level/release (carrier SL -> organ vs decay)
  0x20+op : AM/VIB/EGT/KSR/MULT (carrier VIB -> pulse vibrato LFO,
            carrier EGT+SL -> envelope shape)
  0xC0+ch : feedback/connection   \\  patch brightness -> pulse duty
  0xE0+op : waveform select       /
Register 0xBD (OPL rhythm mode) is checked: none of the 15 Keen 4/6 songs
ever sets its key bits, but if a mod does, BD/SD/TOM/CY/HH key-on edges are
converted to noise drum hits too.

VOICE ASSIGNMENT (all derived from the IMF itself, per song):
 * Channels are split into percussive (<= 2 distinct semitones, >= 0.9
   notes/s, median length <= 0.28s -- the rhythm-section chug the old
   2-voice scorer rejected) and melodic (scored by
   note_count * key-on ticks * min(distinct_pitches, 8)).
 * Top-2 melodic by score: higher mean frequency -> PULSE 1 (melody),
   lower -> TRIANGLE (bass; NB the NES triangle has no volume control, so
   velocity/envelope bits are stripped from its notes).
 * 3rd-ranked melodic -> PULSE 2 (2A03). In the ROM this voice only sounds
   while no sound effect is playing (sfx.c owns $4004-$4007 then); the
   driver keeps advancing its stream so it resumes frame-synchronized.
 * 4th- and 5th-ranked melodic -> PULSE 3 and PULSE 4, the two MMC5
   expansion pulse channels ($5000-$5003 / $5004-$5007). These carry the
   countermelody / harmony voices the 2A03-only mix used to drop. Same
   velocity / envelope / duty / vibrato treatment as the 2A03 pulses; never
   ducked by sfx. Silent on emulators/hardware without MMC5 expansion audio.
 * All percussive channels merge into one NOISE stream. Drum sound from
   OPL pitch: mean f < 85Hz -> low thud (period $C, 5-frame decay),
   < 300Hz -> snare-ish (period $6, 4-frame decay), else hat-ish
   (period $1, 3-frame decay). Simultaneous hits: louder wins, then lower.
   Notes with carrier TL >= 56 (< -42dB) are inaudible ghosts -> dropped.

PER-NOTE ATTRIBUTES (packed into the 16-bit note-table entries):
 * velocity from carrier TL: <16 -> 3, <28 -> 2, <44 -> 1, else 0
   (driver volume table {4,6,8,10}).
 * envelope shape from carrier D (decay rate), EGT and SL:
   D==0 or (EGT && SL<=2) -> 0 organ-sustain; D>=6 -> 2 pluck-fast-decay;
   else 1 piano-decay. Driver steps $4000/$4004 constant-volume per frame.
 * duty (per song-channel, in the blob flags byte) from patch brightness
   2*feedback + (63-mod_TL)/8 + 3*(any waveform != sine) - 8*(additive):
   >= 16 -> 12.5%%, >= 9 -> 25%%, else 50%%.
 * vibrato (per song-channel flag): majority of notes have carrier VIB set
   -> driver applies +-1 period LFO at ~6Hz.

OUTPUT FORMAT v2 (src/gen/musicdata.c, one blob per song per voice;
voices: p1, p2, p3, p4, tri, noi -- p3/p4 are the MMC5 expansion pulses):
  [0]        u8  flags: bits 6-7 pulse duty ($4000 bits), bit 0 vibrato
  [1]        u8  n_notes
  [2..2n+1]  note table: n little-endian u16 entries:
               bits 0-10 NES period (noise: bits 0-3 = $400E period index)
               bits 11-12 velocity 0-3
               bits 13-14 envelope shape (noise: decay speed 0/1/2)
  [2+2n..]   event stream: (dur u8 >= 1, idx u8) pairs.
             idx 0 = rest, idx 1..n = note table entry. EVERY pair with
             idx != 0 is a fresh note-on (envelope retrigger), even if the
             previous event had the same idx. A dur byte of 0 terminates
             the stream -> driver loops to start.
All voice streams of a song are padded to the same total frame count
so they stay in sync across independent loops. A song's blobs always live
in the SAME switchable PRG bank; banks 7, 9, 10, 11 are free (bank 8 is
the SFX driver's). A directory in the fixed bank maps song -> bank +
$8000-based blob pointers, plus the episode's level -> song table (the
original id game's per-level music assignments).

Time is quantized to 1/60s frames (frame = ticks * 60/560); frequencies
become NES periods:
  pulse    period = round(1789773 / (16*f)) - 1   (11 bits, octave-folded)
  triangle period = round(1789773 / (32*f)) - 1   (11 bits, octave-folded)

Also emits src/gen/music.h and preview WAVs (44.1kHz synth of the
CONVERTED streams incl. duty, envelopes, vibrato and noise drums) to
build/music_preview_song{0,1,3,4}.wav, and self-tests the emitted C
arrays by re-parsing and decoding them back to the exact note events.
"""
import math
import os
import re
import struct
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EP = int(os.environ.get("KEEN_EP", "6"))
AUD = ROOT / f"assets/extracted/ck{EP}/audio"
GEN = ROOT / "src/gen"
BUILD = ROOT / "build"
GEN.mkdir(parents=True, exist_ok=True)
BUILD.mkdir(parents=True, exist_ok=True)

NES_CLOCK = 1789773
FREE_BANKS = [7, 9, 10, 11]  # free 8KB PRG banks (8 belongs to sfx data)
BANK_SIZE = 8192

MOD_OFF = [0x00, 0x01, 0x02, 0x08, 0x09, 0x0A, 0x10, 0x11, 0x12]
VOL_TBL = [4, 6, 8, 10]              # must match src/music.c vol_tbl[]
NOI_STEP = [2, 3, 4, 6]              # per-frame decay by noise env shape
NOI_TIMER = [4, 8, 16, 32, 64, 96, 128, 160,
             202, 254, 380, 508, 762, 1016, 2034, 4068]  # NTSC $400E LUT
# Drum kinds: (encoded noise period, env shape, max hit frames).  Bit 4 of
# the encoded period selects the NES noise channel's short/periodic LFSR mode;
# it is translated to $400E bit 7 by the runtime.  Five drum identities cost
# no extra event bytes over generic clicks.
DRUM_THUD = (0x0D, 0, 7)       # low, long normal-noise body
DRUM_TOM = (0x09, 1, 6)        # mid-low tom
DRUM_SNARE = (0x06, 1, 5)      # broad normal-noise crack
DRUM_CYMBAL = (0x13, 2, 5)     # periodic metallic ring (period 3)
DRUM_HAT = (0x11, 3, 3)        # periodic, very fast decay (period 1)

# Bit 15 is free in a pulse entry (period 0..10, velocity 11..12, envelope
# 13..14); it flags a per-note soft attack derived from the OPL carrier,
# distinguishing pads/soft instruments from instant-on square-wave notes.
NOTE_SOFT_ATTACK = 0x8000


# ---------------------------------------------------------------- IMF parse
def parse_imf(path):
    data = path.read_bytes()
    (length,) = struct.unpack_from("<H", data, 0)
    stream = data[2:2 + length]
    return [struct.unpack_from("<BBH", stream, i)
            for i in range(0, len(stream) - 3, 4)]


def extract_notes(recs):
    """-> (notes[9] as lists of (start, end, freq_hz, snap), rhythm_hits,
           total_ticks); snap = patch snapshot dict at key-on."""
    regs = [0] * 256
    fnum_lo = [0] * 9
    bval = [0] * 9
    cur = [None] * 9
    notes = [[] for _ in range(9)]
    rhythm = []  # (tick, drum) from $BD rhythm mode, if any song used it
    bd_prev = 0
    t = 0

    def freq(ch):
        fn = fnum_lo[ch] | ((bval[ch] & 3) << 8)
        block = (bval[ch] >> 2) & 7
        return 49716.0 * fn * 2.0 ** (block - 20)

    def snapshot(ch):
        m = MOD_OFF[ch]
        c = m + 3
        return {
            "tl": regs[0x40 + c] & 0x3F,
            "mod_tl": regs[0x40 + m] & 0x3F,
            "att": (regs[0x60 + c] >> 4) & 0x0F,
            "dec": regs[0x60 + c] & 0x0F,
            "egt": bool(regs[0x20 + c] & 0x20),
            "vib": bool(regs[0x20 + c] & 0x40),
            "sl": (regs[0x80 + c] >> 4) & 0x0F,
            "fb": (regs[0xC0 + ch] >> 1) & 7,
            "add": regs[0xC0 + ch] & 1,
            "ws": (regs[0xE0 + c] & 3) or (regs[0xE0 + m] & 3),
        }

    def close(ch, end):
        if cur[ch] is not None:
            s, f, snap = cur[ch]
            if end > s and f > 20.0:
                notes[ch].append((s, end, f, snap))
            cur[ch] = None

    for reg, val, delay in recs:
        regs[reg] = val
        if reg == 0xBD and (val & 0x20):  # rhythm mode key-on edges
            for bit, drum in ((0x10, DRUM_THUD), (0x08, DRUM_SNARE),
                              (0x04, DRUM_TOM), (0x02, DRUM_CYMBAL),
                              (0x01, DRUM_HAT)):
                if val & bit and not bd_prev & bit:
                    rhythm.append((t, drum))
            bd_prev = val
        elif reg == 0xBD:
            bd_prev = 0
        if 0xA0 <= reg <= 0xA8 or 0xB0 <= reg <= 0xB8:
            ch = reg & 0x0F
            was_on = bool(bval[ch] & 0x20)
            old_f = freq(ch)
            if reg < 0xB0:
                fnum_lo[ch] = val
            else:
                bval[ch] = val
            now_on = bool(bval[ch] & 0x20)
            new_f = freq(ch)
            if was_on and (not now_on or new_f != old_f):
                close(ch, t)
            if now_on and (not was_on or new_f != old_f):
                cur[ch] = (t, new_f, snapshot(ch))
        t += delay
    for ch in range(9):
        close(ch, t)
    return notes, rhythm, t


# ------------------------------------------------- per-note IMF -> NES maps
def velocity(tl):
    """carrier total level (attenuation, 0 = loudest) -> 2-bit velocity"""
    if tl < 16:
        return 3
    if tl < 28:
        return 2
    if tl < 44:
        return 1
    return 0


def env_shape(sn):
    """carrier decay/EGT/sustain -> 0 organ, 1 piano-decay, 2 pluck"""
    if sn["dec"] == 0 or (sn["egt"] and sn["sl"] <= 2):
        return 0
    return 2 if sn["dec"] >= 6 else 1


def soft_attack(sn):
    """True for OPL carriers whose attack is intentionally non-percussive.
    OPL attack rates 0..7 are visibly ramped; faster rates remain instant so
    leads, bass and percussion keep their bite."""
    return sn["att"] <= 7


def brightness(sn):
    b = 2 * sn["fb"] + (63 - sn["mod_tl"]) / 8.0
    if sn["ws"]:
        b += 3
    if sn["add"]:  # additive connection: modulator doesn't shape the tone
        b -= 8
    return b


def channel_flags(ns):
    """per song-channel duty + vibrato flags byte from its note snapshots"""
    bs = sorted(brightness(sn) for _, _, _, sn in ns)
    b = bs[len(bs) // 2]
    duty = 0x00 if b >= 16 else (0x40 if b >= 9 else 0x80)
    vib = sum(1 for _, _, _, sn in ns if sn["vib"]) * 2 >= len(ns)
    return duty | (1 if vib else 0)


# ------------------------------------------------------------ voice mapping
def audible(ns):
    return [n for n in ns if n[3]["tl"] < 56]  # drop < -42dB ghost notes


def classify(notes, total_ticks):
    """-> (melodic [(score, ch)] best first, percussive [ch])"""
    secs = total_ticks / 560.0
    melodic, perc = [], []
    for ch in range(9):
        ns = audible(notes[ch])
        on = sum(e - s for s, e, _, _ in ns)
        if not ns or not on:
            continue
        distinct = len({round(12 * math.log2(f / 440.0))
                        for _, _, f, _ in ns})
        rate = len(ns) / secs
        med_len = sorted(e - s for s, e, _, _ in ns)[len(ns) // 2] / 560.0
        if distinct <= 2 and rate >= 0.9 and med_len <= 0.28:
            perc.append(ch)
        else:
            melodic.append((len(ns) * on * min(distinct, 8), ch))
    melodic.sort(reverse=True)
    return [ch for _, ch in melodic], perc


def assign_voices(notes, total_ticks):
    """-> (ch_p1, ch_tri, ch_p2, ch_p3, ch_p4, perc_channels); any of the
    melodic channels may be None. p1 = melody, tri = bass (top-2 melodic
    split by mean frequency), then the next melodic channels by activity
    score fill pulse 2, pulse 3 (MMC5) and pulse 4 (MMC5)."""
    mel, perc = classify(notes, total_ticks)

    def mean_f(ch):
        ns = audible(notes[ch])
        on = sum(e - s for s, e, _, _ in ns)
        return sum((e - s) * f for s, e, f, _ in ns) / on

    p1 = tri = p2 = p3 = p4 = None
    if len(mel) >= 2:
        a, b = mel[0], mel[1]
        p1, tri = (a, b) if mean_f(a) >= mean_f(b) else (b, a)
        if len(mel) >= 3:
            p2 = mel[2]
        if len(mel) >= 4:
            p3 = mel[3]
        if len(mel) >= 5:
            p4 = mel[4]
    elif len(mel) == 1:
        p1 = mel[0]
    return p1, tri, p2, p3, p4, perc


def pulse_period(f):
    for _ in range(8):
        p = round(NES_CLOCK / (16.0 * f)) - 1
        if p > 0x7FF:
            f *= 2.0  # too low for pulse: fold an octave up
        elif p < 8:
            f /= 2.0  # too high (sweep mutes p<8): fold an octave down
        else:
            return p
    return 0


def tri_period(f):
    for _ in range(8):
        p = round(NES_CLOCK / (32.0 * f)) - 1
        if p > 0x7FF:
            f *= 2.0
        elif p < 2:
            f /= 2.0
        else:
            return p
    return 0


def ticks_to_frame(t):
    return int(round(t * 3 / 28))  # 560Hz -> 60Hz


# --------------------------------------------- note lists -> frame events
def melodic_events(ns, nframes, conv, keep_dyn):
    """channel notes -> sorted non-overlapping (fs, fe, entry) note events.
    entry = period | vel<<11 | env<<13 (vel/env stripped when !keep_dyn,
    i.e. for the volume-less triangle)."""
    ev = []
    for s, e, f, sn in ns:
        fs, fe = ticks_to_frame(s), ticks_to_frame(e)
        if fe <= fs:
            fe = fs + 1  # keep sub-frame staccato notes audible
        if fs >= nframes:
            continue
        fe = min(fe, nframes)
        p = conv(f)
        if not p:
            continue
        entry = p
        if keep_dyn:
            entry |= velocity(sn["tl"]) << 11 | env_shape(sn) << 13
            if soft_attack(sn):
                entry |= NOTE_SOFT_ATTACK
        ev.append((fs, fe, entry))
    ev.sort()
    out = []
    for fs, fe, entry in ev:  # later notes win on overlap
        if out and out[-1][1] > fs:
            ps, pe, pent = out[-1]
            if ps < fs:
                out[-1] = (ps, fs, pent)
            else:
                out.pop()
        out.append((fs, fe, entry))
    return out


def drum_kind(ns):
    """percussive channel -> drum tuple by its mean OPL pitch"""
    on = sum(e - s for s, e, _, _ in ns)
    f = sum((e - s) * fq for s, e, fq, _ in ns) / on
    if f < 85.0:
        return DRUM_THUD
    if f < 180.0:
        return DRUM_TOM
    if f < 420.0:
        return DRUM_SNARE
    return DRUM_HAT


def noise_events(notes, perc, rhythm, nframes):
    """percussive channels + $BD rhythm hits -> noise (fs, fe, entry)"""
    hits = []  # (frame, vel, period_idx, shape, cap)
    for ch in perc:
        ns = audible(notes[ch])
        idx, shape, cap = drum_kind(ns)
        for s, e, f, sn in ns:
            fs = ticks_to_frame(s)
            if fs < nframes:
                hits.append((fs, velocity(sn["tl"]), idx, shape, cap))
    for t, (idx, shape, cap) in rhythm:
        fs = ticks_to_frame(t)
        if fs < nframes:
            hits.append((fs, 2, idx, shape, cap))
    hits.sort()
    merged = []
    for h in hits:  # same-frame collision: louder wins, then lower drum
        if merged and merged[-1][0] == h[0]:
            prev = merged[-1]
            if (h[1], h[2]) > (prev[1], prev[2]):
                merged[-1] = h
        else:
            merged.append(h)
    out = []
    for i, (fs, vel, idx, shape, cap) in enumerate(merged):
        nxt = merged[i + 1][0] if i + 1 < len(merged) else nframes
        fe = min(fs + cap, nxt, nframes)
        out.append((fs, max(fe, fs + 1), idx | vel << 11 | shape << 13))
    return out


# ---------------------------------------------------------------- encoding
def encode_voice(events, nframes, flags):
    """events -> blob; also returns the canonical (dur<=255 split) events"""
    entries = sorted({e for _, _, e in events})
    if len(entries) > 255:
        raise SystemExit(f"note table overflow ({len(entries)})")
    idx = {v: i + 1 for i, v in enumerate(entries)}
    out = bytearray([flags, len(entries)])
    for v in entries:
        out += struct.pack("<H", v)
    canon = []
    cur = 0
    for fs, fe, entry in events:
        gap = fs - cur
        while gap:  # rests merge into runs
            d = min(gap, 255)
            out += bytes([d, 0])
            gap -= d
        run = fe - fs
        while run:  # each (dur, idx!=0) pair is a fresh note-on
            d = min(run, 255)
            out += bytes([d, idx[entry]])
            canon.append((fs, fs + d, entry))
            fs += d
            run -= d
        cur = fe
    gap = nframes - cur  # pad every voice to the song length for loop sync
    while gap:
        d = min(gap, 255)
        out += bytes([d, 0])
        gap -= d
    out.append(0)  # terminator -> loop
    return bytes(out), canon


def decode_voice(blob):
    """inverse of encode_voice -> (flags, note events, total frames)"""
    flags, n = blob[0], blob[1]
    table = [struct.unpack_from("<H", blob, 2 + 2 * i)[0] for i in range(n)]
    pos = 2 + 2 * n
    events, t = [], 0
    while blob[pos] != 0:
        d, ix = blob[pos], blob[pos + 1]
        if ix:
            events.append((t, t + d, table[ix - 1]))
        t += d
        pos += 2
    return flags, events, t


# ------------------------------------------------ level -> song assignments
# Per-level music assignment (which song plays on each level), indexed by
# level number. Value = song index into the extracted music set. Episode 4's
# secret-level entry reflects the game's special-music substitution.
_LEVEL_SONGS = {
    4: [0, 4, 3, 3, 2, 2, 4, 3, 1, 1, 1, 2, 2, 2, 2, 2, 2, 1, 3, 5],
    5: [11, 5, 7, 9, 10, 9, 10, 9, 10, 9, 10, 3, 13, 4, 12, 2, 6, 1, 0, 8],
    6: [5, 3, 1, 8, 8, 8, 7, 2, 7, 1, 3, 2, 1, 4, 4, 6, 2, 0, 0, 0],
}


def parse_level_songs():
    return _LEVEL_SONGS.get(EP, [0] * 20)


# ----------------------------------------- driver simulation (for preview)
def sim_voice(events, nframes, flags, kind):
    """Mirror src/music.c per-frame state -> list of (period, vol, duty).
    kind: 'p' pulse (envelope+vibrato+duty), 't' triangle, 'n' noise."""
    frames = [(0, 0, 0.5)] * nframes
    duty = {0x00: 0.125, 0x40: 0.25, 0x80: 0.5, 0xC0: 0.25}[flags & 0xC0]
    vib = flags & 1
    for fs, fe, entry in events:
        per = entry & (0x1F if kind == "n" else 0x7FF)
        vel = (entry >> 11) & 3
        shape = (entry >> 13) & 3
        base = VOL_TBL[vel]
        nvol = base
        for age, fr in enumerate(range(fs, fe)):
            if kind == "p":
                soft = bool(entry & NOTE_SOFT_ATTACK)
                # Four-frame 2/4/6/8 ramp; stepped to avoid 6502 mul/divide.
                if soft and age < 4:
                    vol = min(base, 2 + age * 2)
                    env_age = 0
                else:
                    env_age = age - 4 if soft else age
                    if shape == 1:
                        vol = max(1, base - (env_age >> 3))
                    elif shape == 2:
                        vol = max(0, base - (env_age >> 1))
                    else:
                        vol = base
                p = per
                if vib:
                    # Pitch-relative four-step LFO: center,+,center,- at 5Hz.
                    # Depth scales with pitch so it stays audible across notes.
                    phase = (fr // 3) & 3
                    depth = (per >> 8) + 1
                    off = depth if phase == 1 else (-depth if phase == 3 else 0)
                    if (per + off) >> 8 == per >> 8:
                        p = per + off
                frames[fr] = (p, vol, duty)
            elif kind == "t":
                frames[fr] = (per, 15, duty)
            else:  # noise: driver-side decay envelope
                frames[fr] = (per, nvol, duty)
                st = NOI_STEP[min(shape, 2)]
                nvol = nvol - st if nvol > st else 0
    return frames


def write_wav(path, song, rate=44100):
    p1 = sim_voice(song["ev_p1"], song["nframes"], song["fl_p1"], "p")
    p2 = sim_voice(song["ev_p2"], song["nframes"], song["fl_p2"], "p")
    p3 = sim_voice(song["ev_p3"], song["nframes"], song["fl_p3"], "p")
    p4 = sim_voice(song["ev_p4"], song["nframes"], song["fl_p4"], "p")
    tr = sim_voice(song["ev_tr"], song["nframes"], 0x80, "t")
    no = sim_voice(song["ev_no"], song["nframes"], 0x80, "n")
    spf = rate / 60.0
    total = int(song["nframes"] * spf)
    samples = bytearray()
    ph = [0.0, 0.0, 0.0, 0.0, 0.0]  # p1, p2, p3, p4, triangle
    lfsr, lacc = 0x4001, 0.0
    for n in range(total):
        fr = min(int(n / spf), song["nframes"] - 1)
        v = 0.0
        for i, arr in enumerate((p1, p2, p3, p4)):
            per, vol, duty = arr[fr]
            if per and vol:
                f = NES_CLOCK / (16.0 * (per + 1))
                ph[i] = (ph[i] + f / rate) % 1.0
                a = 0.032 * vol
                v += a if ph[i] < duty else -a
        per, vol, _ = tr[fr]
        if per:
            f = NES_CLOCK / (32.0 * (per + 1))
            ph[4] = (ph[4] + f / rate) % 1.0
            t = 4.0 * ph[4] - 1.0 if ph[4] < 0.5 else 3.0 - 4.0 * ph[4]
            v += 0.30 * t
        per, vol, _ = no[fr]
        if vol:
            steps = NES_CLOCK / NOI_TIMER[per & 0x0F] / rate
            lacc += steps
            while lacc >= 1.0:
                lacc -= 1.0
                tap = 6 if per & 0x10 else 1
                bit = (lfsr ^ (lfsr >> tap)) & 1
                lfsr = (lfsr >> 1) | (bit << 14)
            v += (0.030 * vol) * (1.0 if lfsr & 1 else -1.0)
        samples += struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32000))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(samples))


# ------------------------------------------------------------------ emit C
def main():
    imfs = sorted(AUD.glob("music_*.imf"),
                  key=lambda p: int(p.stem.split("_")[1]))
    if not imfs:
        raise SystemExit(f"no music_*.imf in {AUD}")

    songs = []
    for path in imfs:
        recs = parse_imf(path)
        notes, rhythm, total_ticks = extract_notes(recs)
        ch_p1, ch_tri, ch_p2, ch_p3, ch_p4, perc = \
            assign_voices(notes, total_ticks)
        nframes = max(1, ticks_to_frame(total_ticks))

        def mel(ch, conv, keep_dyn):
            if ch is None:
                return [], 0
            ns = audible(notes[ch])
            return melodic_events(ns, nframes, conv, keep_dyn), \
                channel_flags(ns)

        ev_p1, fl_p1 = mel(ch_p1, pulse_period, True)
        ev_p2, fl_p2 = mel(ch_p2, pulse_period, True)
        ev_p3, fl_p3 = mel(ch_p3, pulse_period, True)  # MMC5 pulse 3
        ev_p4, fl_p4 = mel(ch_p4, pulse_period, True)  # MMC5 pulse 4
        ev_tr, _ = mel(ch_tri, tri_period, False)
        ev_no = noise_events(notes, perc, rhythm, nframes)

        s = {"name": path.stem, "nframes": nframes,
             "ch_p1": ch_p1, "ch_p2": ch_p2, "ch_p3": ch_p3, "ch_p4": ch_p4,
             "ch_tri": ch_tri, "perc": perc,
             "ev_p1": ev_p1, "ev_p2": ev_p2, "ev_p3": ev_p3, "ev_p4": ev_p4,
             "ev_tr": ev_tr, "ev_no": ev_no,
             "fl_p1": fl_p1, "fl_p2": fl_p2, "fl_p3": fl_p3, "fl_p4": fl_p4,
             "rhythm": len(rhythm)}
        for tag, ev, fl in (("p1", ev_p1, fl_p1), ("p2", ev_p2, fl_p2),
                            ("p3", ev_p3, fl_p3), ("p4", ev_p4, fl_p4),
                            ("tr", ev_tr, 0x80), ("no", ev_no, 0x80)):
            blob, canon = encode_voice(ev, nframes, fl)
            s[f"blob_{tag}"] = blob
            s[f"canon_{tag}"] = canon
        songs.append(s)

    # --- optional song subset: when built level numbers are passed as CLI
    # args, only those levels' assigned songs get real data; the rest become
    # silent 1-frame stubs so a big soundtrack (keen5=14, keen6=9 songs) fits
    # the 4 music banks. Song INDICES are preserved, so music_song_bank[] and
    # the per-voice directories stay valid for every song index. ---
    built = [int(a) for a in sys.argv[1:]]
    if built:
        ls = parse_level_songs()
        needed = {ls[n] for n in built if 0 <= n < len(ls)}
        for i, s in enumerate(songs):
            if i in needed:
                continue
            for t in ("p1", "p2", "p3", "p4", "tr", "no"):
                # keep each voice's original flags so the emitted-array
                # self-test (which checks flags against s["fl_*"]) still passes
                fl = 0x80 if t in ("tr", "no") else s[f"fl_{t}"]
                s[f"blob_{t}"], s[f"canon_{t}"] = encode_voice([], 1, fl)[0], []
                s[f"ev_{t}"] = []  # also empty the event list (preview WAV sim)
            s["nframes"] = 1
        print(f"music subset: songs {sorted(needed)} kept real, "
              f"{len(songs) - len(needed)} stubbed (built levels {built})")

    # first-fit bin packing into free banks (a song never straddles banks)
    remain = {b: BANK_SIZE for b in FREE_BANKS}
    for s in songs:
        size = sum(len(s[f"blob_{t}"])
                   for t in ("p1", "p2", "p3", "p4", "tr", "no"))
        for b in FREE_BANKS:
            if remain[b] >= size:
                s["bank"] = b
                remain[b] -= size
                break
        else:
            raise SystemExit(f"music data overflows banks {FREE_BANKS}")

    level_songs = parse_level_songs()
    nsongs = len(songs)
    level_songs = [v if 0 <= v < nsongs else 0 for v in level_songs]

    def carray(name, bank, blob):
        vals = ", ".join(str(b) for b in blob)
        return (f'__attribute__((used, section(".prg_rom_{bank}"))) '
                f"const unsigned char {name}[{len(blob)}] = {{ {vals} }};\n")

    tags = ("p1", "p2", "p3", "p4", "tr", "no")
    c = ["// generated by gen_music.py -- format spec in tools/gen_music.py\n"]
    for i in range(nsongs):
        c.append("extern const unsigned char "
                 + ", ".join(f"mus{i}_{t}[]" for t in tags) + ";\n")
    for i, s in enumerate(songs):
        for t in tags:
            c.append(carray(f"mus{i}_{t}", s["bank"], s[f"blob_{t}"]))
    dir_bank0 = songs[0]["bank"] if songs else FREE_BANKS[0]
    c.append(f'__attribute__((used, section(".prg_rom_{dir_bank0}"))) '
             "const unsigned char music_song_bank[] = { "
             + ", ".join(str(s["bank"]) for s in songs) + " };\n")
    # the per-voice song directories live in the FIRST music bank, not the
    # contested fixed region (keen5: 4 x 14 x 2B = 112B): kmusic_play maps
    # MUSIC_DIR_BANK, copies the four blob pointers, then maps the song bank
    dir_bank = songs[0]["bank"] if songs else FREE_BANKS[0]
    for t, nm in zip(tags, ("p1", "p2", "p3", "p4", "tri", "noi")):
        c.append(f'__attribute__((used, section(".prg_rom_{dir_bank}"))) '
                 f"const unsigned char *const music_song_{nm}[] = {{ "
                 + ", ".join(f"mus{i}_{t}" for i in range(nsongs)) + " };\n")
    c.append(f'__attribute__((used, section(".prg_rom_{dir_bank0}"))) '
             "const unsigned char music_level_song[] = { "
             + ", ".join(str(v) for v in level_songs) + " };\n")
    (GEN / "musicdata.c").write_text("".join(c))

    h = ["#ifndef GEN_MUSIC_H\n#define GEN_MUSIC_H\n",
         f"#define MUSIC_NUM_SONGS {nsongs}\n",
         f"#define MUSIC_NUM_LEVELS {len(level_songs)}\n",
         f"#define MUSIC_DIR_BANK {dir_bank} /* holds the song directories */\n",
         "extern const unsigned char music_song_bank[];\n",
         "extern const unsigned char *const music_song_p1[];\n",
         "extern const unsigned char *const music_song_p2[];\n",
         "extern const unsigned char *const music_song_p3[];\n",
         "extern const unsigned char *const music_song_p4[];\n",
         "extern const unsigned char *const music_song_tri[];\n",
         "extern const unsigned char *const music_song_noi[];\n",
         "extern const unsigned char music_level_song[]; // by level number\n",
         "#endif\n"]
    (GEN / "music.h").write_text("".join(h))

    # ---- self-test: re-parse emitted C, decode, compare (frame-exact)
    text = (GEN / "musicdata.c").read_text()
    ok = True
    for i, s in enumerate(songs):
        for t in tags:
            m = re.search(rf"mus{i}_{t}\[\d+\] = \{{ ([\d, ]+) \}};", text)
            blob = bytes(int(x) for x in m.group(1).split(","))
            fl, events, total = decode_voice(blob)
            want_fl = {"p1": s["fl_p1"], "p2": s["fl_p2"],
                       "p3": s["fl_p3"], "p4": s["fl_p4"],
                       "tr": 0x80, "no": 0x80}[t]
            if (fl != want_fl or events != s[f"canon_{t}"]
                    or total != s["nframes"]):
                ok = False
                print(f"SELF-TEST FAIL: song {i} voice {t} mismatch")
    if not ok:
        raise SystemExit(1)

    # ---- preview WAVs (default set + any extra ids given on the CLI)
    want = {0, 1, 3, 4} | {int(a) for a in sys.argv[1:]}
    for i in sorted(want & set(range(nsongs))):
        write_wav(BUILD / f"music_preview_song{i}.wav", songs[i])

    # ---- report
    drum_names = {DRUM_THUD[0]: "kick", DRUM_TOM[0]: "tom",
                  DRUM_SNARE[0]: "snare", DRUM_CYMBAL[0]: "cymbal",
                  DRUM_HAT[0]: "hat"}
    total_bytes = 0
    for i, s in enumerate(songs):
        size = sum(len(s[f"blob_{t}"]) for t in tags)
        total_bytes += size
        dcnt = {}
        for _, _, e in s["ev_no"]:
            nm = drum_names.get(e & 0x1F, "?")
            dcnt[nm] = dcnt.get(nm, 0) + 1
        vels = [0] * 4
        envs = [0] * 3
        soft = 0
        for t in ("p1", "p2", "p3", "p4"):
            for _, _, e in s[f"ev_{t}"]:
                vels[(e >> 11) & 3] += 1
                envs[min((e >> 13) & 3, 2)] += 1
                soft += bool(e & NOTE_SOFT_ATTACK)
        # OPL voices carried: melodic pulses/triangle actually assigned a
        # channel, plus 1 if any percussive channel feeds the noise voice
        voices = sum(1 for k in ("ch_p1", "ch_p2", "ch_p3", "ch_p4", "ch_tri")
                     if s[k] is not None) + (1 if s["perc"] else 0)

        def dch(fl):
            return {0x00: "12.5", 0x40: "25", 0x80: "50"}.get(fl & 0xC0, "?") \
                + ("+vib" if fl & 1 else "")

        print(f"song {i} ({s['name']}): {s['nframes']/60.0:5.1f}s "
              f"p1=ch{s['ch_p1']}({len(s['ev_p1'])}n,{dch(s['fl_p1'])}) "
              f"p2=ch{s['ch_p2']}({len(s['ev_p2'])}n,{dch(s['fl_p2'])}) "
              f"p3=ch{s['ch_p3']}({len(s['ev_p3'])}n,{dch(s['fl_p3'])}) "
              f"p4=ch{s['ch_p4']}({len(s['ev_p4'])}n,{dch(s['fl_p4'])}) "
              f"tri=ch{s['ch_tri']}({len(s['ev_tr'])}n) "
              f"noise=ch{s['perc']}({len(s['ev_no'])}hits {dcnt}) "
              f"voices={voices} vel{vels} env{envs} soft={soft} "
              f"{size}B bank {s['bank']}"
              + (f" RHYTHM-MODE {s['rhythm']} hits" if s["rhythm"] else ""))
    used = {b: BANK_SIZE - r for b, r in remain.items() if r < BANK_SIZE}
    print(f"total {total_bytes} bytes; bank usage {used}")
    print(f"level->song: {level_songs}")
    print("self-test: emitted arrays decode back to source events: OK")


if __name__ == "__main__":
    main()
