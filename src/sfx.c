// Minimal APU sound-effect driver: plays pre-converted PC-speaker streams
// (period words at 140Hz; 0 = silence) on pulse channel 2, leaving pulse 1
// free for music. Priority = latest wins.
#include <mapper.h>
#include "gen/sfx.h"
#include "sfx.h"

// SFX data lives in PRG bank 8 (fixed region is full).  One frame consumes
// 2-3 source ticks; map bank 8 ONCE around the whole batch rather than around
// every 16-bit read (formerly 4-6 mapper writes/frame while an effect played).
extern const unsigned char lvl_bank[];
extern unsigned char g_level;
#define SFX_BANK 8
#define HUD_BANK 6

#define APU_P2_CTRL (*(volatile unsigned char *)0x4004)
#define APU_P2_SWEEP (*(volatile unsigned char *)0x4005)
#define APU_P2_LO (*(volatile unsigned char *)0x4006)
#define APU_P2_HI (*(volatile unsigned char *)0x4007)
#define APU_STATUS (*(volatile unsigned char *)0x4015)

static const unsigned short *cur;
static unsigned int cur_len, cur_pos;
static unsigned char acc;
static unsigned int last_period = 0xFFFF;

void ksfx_init(void) {
  APU_STATUS = 0x0F; // enable pulses (music will use pulse 1 later)
  APU_P2_SWEEP = 0x08;
}

void ksfx_play(unsigned char id) {
  set_prg_bank(SFX_BANK, 0x80);
  cur = sfx_data[id];
  cur_len = sfx_len[id];
  set_prg_bank(lvl_bank[g_level], 0x80);
  cur_pos = 0;
  last_period = 0xFFFF;
}

void ksfx_stop(void) {
  cur = 0;
  APU_P2_CTRL = 0x30; // constant volume 0: never leave a held pulse behind
}

// Bank 8 must already be visible at $8000.
static void tick_mapped(void) {
  unsigned int p;
  if (!cur)
    return;
  if (cur_pos >= cur_len) {
    cur = 0;
    APU_P2_CTRL = 0x30; // constant volume 0
    return;
  }
  p = cur[cur_pos];
  ++cur_pos;
  if (p == 0) {
    APU_P2_CTRL = 0x30;
  } else {
    APU_P2_CTRL = 0xB8; // duty 10, constant volume 8
    APU_P2_LO = p & 0xFF;
    if ((p >> 8) != (last_period >> 8))
      APU_P2_HI = (p >> 8) & 7; // avoid phase reset when high bits equal
    last_period = p;
  }
}

// nonzero while an effect is playing (music ducks pulse 2 off it)
unsigned char ksfx_active(void) { return cur != 0; }

// Advance the 140Hz stream at 60fps, restoring the caller's $8000 bank.
static void frame_to(unsigned char restore_bank) {
  if (!cur)
    return;
  set_prg_bank(SFX_BANK, 0x80);
  acc += 140 % 60;         // 20 per frame after the two whole ticks
  tick_mapped();
  tick_mapped();
  if (acc >= 60) {
    acc -= 60;
    tick_mapped();
  }
  set_prg_bank(restore_bank, 0x80);
}

// call once per gameplay frame
void ksfx_frame(void) {
  frame_to(lvl_bank[g_level]);
}

// Death/menu bodies execute from HUD bank 6 at $8000.  The fixed SFX driver
// can still batch bank-8 reads, but must restore bank 6 before returning.
void ksfx_frame_hud(void) {
  frame_to(HUD_BANK);
}

// Death fade runs from draw bank 26 (ms_frames lives there).
void ksfx_frame_draw(void) {
  frame_to(26);
}
