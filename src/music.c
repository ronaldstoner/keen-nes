// NES music driver: plays gen_music.py-converted IMF streams across the full
// 2A03 APU plus the two MMC5 expansion pulses. Channels: pulse 1 (melody),
// pulse 2 (3rd voice, ducked while sfx.c owns the channel), triangle (bass),
// noise (percussion), and MMC5 pulse 3 ($5000) + pulse 4 ($5004) carrying two
// more OPL voices (countermelody / harmony). The MMC5 pulses use the 2A03
// pulse register layout (no sweep) and are enabled via $5015; on a non-MMC5
// cart those writes hit unmapped space as harmless no-ops, so the driver
// drives them unconditionally.
//
// Data format: see tools/gen_music.py.
//
// PULSE 2 / SFX ARBITRATION: sfx.c owns $4004-$4007 while an effect is playing
// (ksfx_active()); the driver then writes NOTHING to those registers but keeps
// advancing the pulse-2 stream and envelope, so the voice resumes at the
// frame-exact stream position when the effect ends (main.c calls ksfx_frame()
// before kmusic_sync(), so the hand-back happens the same frame it stops).
#include <mapper.h>
#include "gen/music.h"
#include "music.h"

#define APU_P1_CTRL (*(volatile unsigned char *)0x4000)
#define APU_P1_SWEEP (*(volatile unsigned char *)0x4001)
#define APU_P2_CTRL (*(volatile unsigned char *)0x4004)
#define APU_TRI_LIN (*(volatile unsigned char *)0x4008)
#define APU_TRI_LO (*(volatile unsigned char *)0x400A)
#define APU_TRI_HI (*(volatile unsigned char *)0x400B)
#define APU_NOI_CTRL (*(volatile unsigned char *)0x400C)
#define APU_NOI_PER (*(volatile unsigned char *)0x400E)
#define APU_NOI_LEN (*(volatile unsigned char *)0x400F)
// MMC5 expansion audio: two extra pulse channels (same interface as the
// 2A03 pulses, no sweep) plus the $5015 enable. $5000-$5003 = pulse 3,
// $5004-$5007 = pulse 4.
#define MMC5_P3_CTRL (*(volatile unsigned char *)0x5000)
#define MMC5_P4_CTRL (*(volatile unsigned char *)0x5004)
#define MMC5_SND_EN (*(volatile unsigned char *)0x5015)

extern const unsigned char lvl_bank[];   // src/level.c
extern unsigned char g_level;            // src/level.c
extern unsigned char ksfx_active(void);  // src/sfx.c: sfx owns pulse 2
extern volatile unsigned char FRAME_CNT1; // neslib NMI 60 Hz clock

typedef struct {
  const unsigned char *table; // note table ($8000-based, song bank)
  const unsigned char *start; // first event
  const unsigned char *pos;   // next event
  unsigned int entry;         // current note-table entry, 0 = rest
  unsigned char dur;          // frames left in current event
  unsigned char dirty;        // note changed, full APU rewrite pending
  unsigned char age;          // frames since note-on (envelope clock)
  unsigned char ctrl;         // pulse $4000 base: duty | 0x30
  unsigned char vib;          // pulse vibrato LFO enabled
  unsigned char hi;           // last written period high byte
  unsigned char vol;          // last written volume (0xFF = force)
} Voice;

// Order MUST match song_dir[] below. VP3/VP4 are the MMC5 expansion pulses.
enum { VP1, VP2, VP3, VP4, VTR, VNO, NVOICES };
static Voice voice[NVOICES];
static unsigned char song_bank;
static unsigned char playing = 0xFF; // 0xFF = stopped
static unsigned char noi_vol;        // current decaying drum volume
static unsigned char vib_t, vib_phase; // 5Hz center,+,center,- LFO
static unsigned char sfx_prev;
unsigned char music_frame_seen; // NMI frame clock, used by music_sync.s

static const unsigned char vol_tbl[4] = {4, 6, 8, 10}; // velocity -> volume
static const unsigned char noi_step[4] = {2, 3, 4, 6}; // drum decay / frame

static void silence(void) {
  APU_P1_CTRL = 0x30; // constant volume 0
  APU_TRI_LIN = 0x80; // linear counter reload 0
  APU_NOI_CTRL = 0x30;
  MMC5_P3_CTRL = 0x30; // MMC5 expansion pulses: constant volume 0
  MMC5_P4_CTRL = 0x30;
  noi_vol = 0;
  if (!ksfx_active()) // never touch $4004-$4007 under an active effect
    APU_P2_CTRL = 0x30;
}

void kmusic_init(void) {
  APU_P1_SWEEP = 0x08; // sweep off ($4015 enable is done by ksfx_init)
  MMC5_SND_EN = 0x03;  // $5015: enable MMC5 expansion pulse 3 + pulse 4
  silence();
}

void kmusic_stop(void) {
  playing = 0xFF;
  silence();
}

// voice index -> song directory array (order matches enum above)
static const unsigned char *const *const song_dir[NVOICES] = {
    music_song_p1, music_song_p2, music_song_p3, music_song_p4,
    music_song_tri, music_song_noi};

// Start the given GAME level's song. Safe to call any time a level is
// loaded (restores the level's bank A).
void kmusic_play(unsigned char game_level) {
  unsigned char i, song;
  Voice *v;
  const unsigned char *blobs[NVOICES];
  kmusic_stop();
  // music_level_song, music_song_bank AND the song directories live in
  // the first music bank (fixed-region relief: keen5's 14 songs cost
  // 112B of rodata); copy everything out before mapping the song bank
  set_prg_bank(MUSIC_DIR_BANK, 0x80);
  song = music_level_song[game_level];
  song_bank = music_song_bank[song];
  for (i = 0; i < NVOICES; ++i)
    blobs[i] = song_dir[i][song];
  set_prg_bank(song_bank, 0x80);
  for (i = 0, v = voice; i < NVOICES; ++i, ++v) {
    const unsigned char *blob = blobs[i];
    unsigned char flags = blob[0];
    v->table = blob + 2;
    v->start = v->table + ((unsigned int)blob[1] << 1);
    v->pos = v->start;
    v->dur = 1; // frame 1 decrements to 0 -> immediate fetch
    v->entry = 0;
    v->dirty = 0;
    v->age = 0;
    v->ctrl = (flags & 0xC0) | 0x30; // duty, length halt, constant volume
    v->vib = flags & 1;
    v->hi = 0xFF;  // force first HI write (also loads the length counter)
    v->vol = 0xFF; // force first volume write
  }
  set_prg_bank(lvl_bank[g_level], 0x80);
  vib_t = 0;
  vib_phase = 0;
  sfx_prev = 1; // force a pulse-2 rewrite on the first sfx-free frame
  music_frame_seen = FRAME_CNT1;
  playing = song;
}

// fetch the voice's next event; the song bank must be mapped at $8000
static void fetch(Voice *v) {
  const unsigned char *p = v->pos;
  unsigned char d = p[0];
  unsigned char idx;
  unsigned int entry = 0;
  if (d == 0) { // end of stream: loop
    p = v->start;
    d = p[0];
    if (d == 0) { // empty stream: rest forever
      d = 255;
      goto done;
    }
  }
  idx = p[1];
  p += 2;
  if (idx) {
    const unsigned char *e = v->table + ((unsigned int)(idx - 1) << 1);
    entry = (unsigned int)e[0] | ((unsigned int)e[1] << 8);
  }
done:
  v->pos = p;
  v->dur = d;
  v->entry = entry;
  v->dirty = 1;
  v->age = 0; // envelope retrigger (also on repeated identical notes)
}

// per-frame stepped volume for a pulse voice (constant-volume writes)
static unsigned char env_vol(const Voice *v) {
  unsigned char base = vol_tbl[(v->entry >> 11) & 3];
  unsigned char shape = (v->entry >> 13) & 3;
  unsigned char age = v->age;
  unsigned char drop;
  if (v->entry & 0x8000) { // soft OPL carrier: four-frame 2/4/6/8 attack
    if (age < 4) {
      unsigned char rise = 2 + (age << 1);
      return rise < base ? rise : base;
    }
    age -= 4; // decay/release timing begins after the attack
  }
  if (shape == 1) { // piano-decay, floor 1 (long echo tail)
    drop = age >> 3;
    return (drop + 1 >= base) ? 1 : base - drop;
  }
  if (shape == 2) { // pluck-fast-decay, fades out fully
    drop = age >> 1;
    return (drop >= base) ? 0 : base - drop;
  }
  return base; // organ-sustain
}

// update one pulse channel's registers; reg -> $4000 (p1) or $4004 (p2)
#define MUSIC_OUT __attribute__((noinline, section(".prg_rom_6.text")))
MUSIC_OUT static void pulse_write(Voice *v, volatile unsigned char *reg) {
  if (v->entry == 0) {
    if (v->dirty) {
      reg[0] = 0x30;
      v->vol = 0;
    }
  } else {
    unsigned char vol = env_vol(v);
    unsigned int per = v->entry & 0x7FF;
    unsigned char redo = v->dirty;
    if (v->vib) {
      unsigned char depth = (per >> 8) + 1;
      unsigned int pv = per;
      if (vib_phase == 1)
        pv += depth;
      else if (vib_phase == 3)
        pv -= depth;
      if ((pv & 0x700) == (per & 0x700)) { // never cross a hi-byte edge
        per = pv;
        if (vib_t == 0)
          redo = 1; // LFO just toggled: refresh the low period byte
      }
    }
    if (redo || vol != v->vol) {
      reg[0] = v->ctrl | vol;
      v->vol = vol;
    }
    if (redo) {
      unsigned char hi = (per >> 8) & 7;
      reg[2] = per & 0xFF;
      if (hi != v->hi) { // skip the HI write when unchanged: no phase click
        reg[3] = hi;
        v->hi = hi;
      }
    }
  }
  v->dirty = 0;
}

// Pure APU/WRAM half of the driver. Lives in the utility bank; kmusic_tick
// has already restored the level mapping before entering it.
MUSIC_OUT static void music_output_b(void) {
  unsigned char sfx_now;
  Voice *v;
  if (++vib_t >= 3) { // 4-step center,+,center,- cycle at 5Hz
    vib_t = 0;
    vib_phase = (vib_phase + 1) & 3;
  }

  sfx_now = ksfx_active();
  if (sfx_prev && !sfx_now) { // sfx released pulse 2: full state rewrite
    v = &voice[VP2];
    v->dirty = 1;
    v->hi = 0xFF;
    v->vol = 0xFF;
  }
  sfx_prev = sfx_now;

  pulse_write(&voice[VP1], (volatile unsigned char *)0x4000);
  // pulse 2 state always advances; register writes only while sfx.c isn't
  // playing an effect there (it owns $4004-$4007 then)
  if (!sfx_now)
    pulse_write(&voice[VP2], (volatile unsigned char *)0x4004);
  // MMC5 expansion pulses (countermelody / harmony): sfx.c never touches
  // them, so they always write.
  pulse_write(&voice[VP3], (volatile unsigned char *)0x5000);
  pulse_write(&voice[VP4], (volatile unsigned char *)0x5004);

  v = &voice[VTR]; // triangle: no volume control; vel/env bits unused
  if (v->dirty) {
    unsigned int e = v->entry;
    v->dirty = 0;
    if (e == 0) {
      APU_TRI_LIN = 0x80; // linear counter reload 0
    } else {
      unsigned char hi = (e >> 8) & 7;
      APU_TRI_LIN = 0xFF; // control flag on, linear reload 127
      APU_TRI_LO = e & 0xFF;
      if (hi != v->hi) {
        APU_TRI_HI = hi;
        v->hi = hi;
      }
    }
  }

  v = &voice[VNO]; // noise: drum hit + 2-5 frame driver-side volume ramp
  {
    unsigned int e = v->entry;
    if (v->dirty) {
      v->dirty = 0;
      if (e == 0) {
        APU_NOI_CTRL = 0x30;
        noi_vol = 0;
      } else {
        noi_vol = vol_tbl[(e >> 11) & 3];
        APU_NOI_CTRL = 0x30 | noi_vol;
        // Encoded noise bit 4 is the short/periodic LFSR selector.
        APU_NOI_PER = (e & 0x0F) | ((e & 0x10) << 3);
        APU_NOI_LEN = 0x08; // load length counter (halted, so it persists)
      }
    } else if (noi_vol) {
      unsigned char st = noi_step[(e >> 13) & 3];
      noi_vol = (noi_vol > st) ? noi_vol - st : 0;
      APU_NOI_CTRL = 0x30 | noi_vol;
    }
  }
}

__attribute__((noinline)) void kmusic_tick(void) {
  Voice *v;
  if (playing == 0xFF)
    return;

  // advance every voice one frame; fetch new events where a run expired
  // (voice_setup starts dur at 1 so the first frame fetches immediately)
  {
    unsigned char mapped = 0;
    for (v = voice; v != voice + NVOICES; ++v) {
      if (v->age != 255)
        v->age++; // envelope clock
      if (--v->dur == 0) {
        if (!mapped) {
          mapped = 1;
          set_prg_bank(song_bank, 0x80);
        }
        fetch(v);
      }
    }
    if (mapped)
      set_prg_bank(lvl_bank[g_level], 0x80);
  }

  // Two MMC5 register writes select the cold bank and restore the level bank.
  // This is roughly a dozen CPU cycles per video frame and does not touch the
  // event clock, so music remains frame-decoupled from gameplay load.
  set_prg_bank(6, 0x80);
  music_output_b();
  set_prg_bank(lvl_bank[g_level], 0x80);
}
