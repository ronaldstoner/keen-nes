// Keen 4/5/6 NES port — persistent in-game HUD (score / lives / ammo / keygems),
// drawn as a top-left SPRITE overlay (like Keen's floating score box). The
// 4-way vertical scroller wraps the camera through all 30 nametable rows, so no
// BG row can be reserved for a fixed HUD; row 1 is the score (up to 8 digits,
// right-aligned) and row 2 is three icon+number groups (lives / ammo / keygems).
// MIT License; see LICENSE.
//
// SPRITE/SCANLINE BUDGET: row 2 stays <= 8 sprites (3 icons + <=5 digits) so it
// never breaks the 8-per-scanline PPU limit; the 8-digit score row can touch
// that limit only once score passes 10,000,000.
//
// BANKING RULE: everything here except the tiny trampolines (hud_draw,
// difficulty_select) and hud_set runs from switchable PRG bank 6 (HUD_BANKED).
// Banked code here must NEVER call ksfx_play/kmusic_* (those drivers restore R6
// to the LEVEL bank, unmapping the caller). ksfx_frame_hud is the deliberate
// exception: it restores bank 6 for the death/bookend path.

#include <neslib.h>
#include <mapper.h>
#include <peekpoke.h>
#include "player.h"
#include "gen/player.h"
#include <nesdoug.h>
#include "level_fmt.h"
#include "gen/statusdata.h"
#include "hud.h"
#include "sfx.h"
#include "mmc5/mmc5.h"

#define HUD_PRG_BANK 6
#define HUD_BANKED __attribute__((noinline, section(".prg_rom_6.text")))
#define HUD_RO __attribute__((section(".prg_rom_6.rodata")))

extern const unsigned char lvl_bank[]; // src/gen/leveldata_mmc5.c

// FPS/OAM instrumentation: tracked by player.c (cheap WRAM counters) but no
// longer drawn — the debug readout competed with gameplay sprites for the
// 8-per-scanline budget. g_loops counts game-loop iterations per 60 vblanks
// (60 = no dropped frames).
unsigned char g_loops;          // incremented by player_update each loop
unsigned char g_oam_used;       // sprites used last sampled frame (of 64)

#define HUD_SCORE_Y 9
// 8x16 sprite mode: every HUD digit is an 8x16 sprite (glyph on top, blank
// bottom), so a HUD row occupies 16 scanlines for the 8-sprites-per-scanline
// limit, not 8. Row 1 (y=9) spans scanlines 9..24; row 2 must start at >=25 or
// the two rows' sprites stack and blow the per-scanline budget (both rows at
// y9/y18 put 15 on one line). Pushing row 2 to y=26 keeps each row's
// per-scanline count to its own digit count (<=8), same as the old 8x8 HUD.
#define HUD_ROW2_Y 26
#define HUD_SCORE_X 8 // leftmost of the 8 digit cells; rightmost at 8+7*8
// Row-2 icon+number groups (icon glyph then its digits), left to right:
//   lives  @ x=8    ammo @ x=40    keygems @ x=88
#define HUD_LIFE_X 8
#define HUD_AMMO_X 40
#define HUD_GEM_X 88
#define HUD_PAL 0 // palette 0: white digits + white stat icons

static unsigned char hud_on = 1;

// Cached digit arrays — recomputed only when the underlying stat changes
// (division/modulo is expensive on the 6502, so don't redo it every frame).
// The score needs no digit cache at all: pl_score_d[] IS the digits;
// only the leading-zero skip is cached, keyed on pl_score_gen.
static unsigned char sh_gen;                 // pl_score_gen shadow
static unsigned char sh_ammo, sh_lives;
static unsigned char hud_valid;              // 0 => shadows/digits not built
static unsigned char score_first;            // first score digit to draw (<= 5)
static unsigned char ammo_d[2], lives_d[2];  // two digits each

void hud_set(unsigned char on) { hud_on = on; }

// Split an 8-bit value (0..99 expected; higher values clamp to 99) into two
// digits using repeated subtraction instead of division.
HUD_BANKED static void hud_digits2(unsigned char v, unsigned char *out) {
  unsigned char tens = 0;
  if (v > 99)
    v = 99;
  while (v >= 10) {
    v -= 10;
    ++tens;
  }
  out[0] = tens;
  out[1] = v;
}

HUD_BANKED static void hud_draw_b(void) {
  unsigned char i, x, gems;

  // Refresh cached digits only for stats that changed since last frame.
  if (!hud_valid || sh_gen != pl_score_gen) {
    sh_gen = pl_score_gen;
    // skip leading zeros, keep at least 3 digits (stop at index 5)
    for (i = 0; i < 5 && pl_score_d[i] == 0; ++i)
      ;
    score_first = i;
  }
  if (!hud_valid || sh_ammo != pl_ammo) {
    sh_ammo = pl_ammo;
    hud_digits2(sh_ammo, ammo_d);
  }
  if (!hud_valid || sh_lives != pl_lives) {
    sh_lives = pl_lives;
    hud_digits2(sh_lives, lives_d);
  }
  hud_valid = 1;

  // keygems held: popcount of the 4-bit pl_keys mask (item types 0..3).
  {
    unsigned char k = pl_keys;
    gems = (unsigned char)((k & 1) + ((k >> 1) & 1) + ((k >> 2) & 1) +
                           ((k >> 3) & 1));
  }

  // Score row: right-aligned in 8 cells, leading zeros skipped.
  x = HUD_SCORE_X + (unsigned char)(score_first << 3);
  for (i = score_first; i < 8; ++i, x += 8)
    oam_spr(x, HUD_SCORE_Y, font_tile[pl_score_d[i]], HUD_PAL);

  // Second row: three icon+number stat groups, well clear of the score row's
  // 16px (8x16) span so they never stack on a scanline. <= 8 sprites here so
  // the 8-per-scanline limit holds with no flicker.
  //  lives:   [head] D            (tens shown only when >= 10)
  oam_spr(HUD_LIFE_X, HUD_ROW2_Y, HUD_ICON_LIFE, HUD_PAL);
  x = HUD_LIFE_X + 10;
  if (lives_d[0]) {
    oam_spr(x, HUD_ROW2_Y, font_tile[lives_d[0]], HUD_PAL);
    x += 8;
  }
  oam_spr(x, HUD_ROW2_Y, font_tile[lives_d[1]], HUD_PAL);
  //  ammo:    [shot] DD
  oam_spr(HUD_AMMO_X, HUD_ROW2_Y, HUD_ICON_AMMO, HUD_PAL);
  oam_spr(HUD_AMMO_X + 10, HUD_ROW2_Y, font_tile[ammo_d[0]], HUD_PAL);
  oam_spr(HUD_AMMO_X + 18, HUD_ROW2_Y, font_tile[ammo_d[1]], HUD_PAL);
  //  keygems: [gem]  D            (0..4 held)
  oam_spr(HUD_GEM_X, HUD_ROW2_Y, HUD_ICON_GEM, HUD_PAL);
  oam_spr(HUD_GEM_X + 10, HUD_ROW2_Y, font_tile[gems], HUD_PAL);
}

// Fixed-region trampoline: the HUD body runs from bank 6 every frame
// (two mapper writes per frame ≈ 24 cycles; the level bank is restored
// for the seam/audio code that follows in the main loop).
void hud_draw(void) {
  if (!hud_on)
    return;
  set_prg_bank(HUD_PRG_BANK, 0x80);
  hud_draw_b();
  set_prg_bank(lvl_bank[g_level], 0x80);
}

// ---------------------------------------------------------------------
// Text-screen font infrastructure (shared by the difficulty selector).
// The 8x8 font sits in the last 2KB of CHR ROM (gen/statusdata.h, emitted by
// tools/gen_status.py) and is swapped into the bg (and, for the selector,
// sprite) CHR windows while a text screen is up.

// NT0 address of (column, row)
#define NTA(col, row) (0x2000u + ((row) * 32u) + (col))
#define ST_GLYPH(c) ((unsigned char)((c) - STATUS_GLYPH_LO))

HUD_RO static const char s_easy[] = "EASY";
HUD_RO static const char s_normal[] = "NORMAL";
HUD_RO static const char s_hard[] = "HARD";

HUD_BANKED static void st_text(unsigned int adr, const char *s) {
  vram_adr(adr);
  while (*s)
    vram_put(ST_GLYPH(*s++));
}

// rendering off + font CHR in both bg windows + white-on-black palette +
// cleared NT0; used by the difficulty selector
HUD_BANKED static void st_screen_open(void) {
  unsigned int i;
  ppu_off();
  // Leave ExRAM extended-attribute mode (gameplay left $5104=1, which makes
  // the PPU fetch per-tile CHR bank + palette from ExRAM). Back to mode 0 so
  // the text screen renders normally: nametable attribute bytes + R0/R1 CHR.
  MMC5_EXRAM_MODE = MMC5_EXRAM_NT;
  MMC5_CHR_UPPER = STATUS_CHR_UPPER;
  // (tile N == tile N & 127; only indices 0..58 used, tile 0 = blank)
  set_chr_mode_0(STATUS_CHR_BANK);
  set_chr_mode_1(STATUS_CHR_BANK);
  // white-on-black text palette, written raw (neslib's pal_bg LUT wrecks
  // whites — see set_palettes_raw in main.c); bg AND sprite sets. $20 is
  // the port's canonical white (the converter palette map's white).
  vram_adr(0x3F00);
  for (i = 0; i < 32; ++i)
    vram_put(((i & 3) == 3) ? 0x20 : 0x0F);
  oam_clear(); // stale gameplay sprites must not ride over the overlay
  oam_size(0); // text screens (menu '>' cursor, bookends) use 8x8 sprites;
               // gameplay set 8x16 (level_chr_refresh). Every return to the
               // title passes through a bookend text screen, so this also
               // resets the mode before the next title (banked -> free of the
               // near-full fixed region).
  // clear NT0: 960 tiles + 64 attribute bytes (palette 0 everywhere)
  vram_adr(0x2000);
  for (i = 0; i < 1024; ++i)
    vram_put(0);
}

// ---------------------------------------------------------------------
// Difficulty selector — the port's only menu. Shown once, between the title
// screen and level 1. Sets g_difficulty (0/1/2), which actors_init's spawn
// filter honors.
// The cursor is a '>' font glyph drawn as a SPRITE (the font bank is
// mapped into a sprite CHR window too), so moving it needs no VRAM
// writes while rendering runs. Runs entirely from bank 11: no music or
// sfx is live yet, so nothing here can retarget R6 mid-loop.

HUD_RO static const char s_dtitle[] = "DIFFICULTY?";

HUD_BANKED static void diff_run(void) {
  unsigned char sel = 1; // default NORMAL
  unsigned char prev = 0xFF;
  st_screen_open();
  bank_bg(0);
  bank_spr(1);
  set_chr_mode_2(STATUS_CHR_BANK);     // '>' glyph visible to sprites
  set_chr_mode_3(STATUS_CHR_BANK + 1); // (tiles < 64, first 1KB window)
  st_text(NTA(10, 9), s_dtitle);
  st_text(NTA(13, 13), s_easy);
  st_text(NTA(13, 15), s_normal);
  st_text(NTA(13, 17), s_hard);
  scroll(0, 0);
  ppu_on_all();
  for (;;) {
    unsigned char pad;
    ppu_wait_nmi();
    pad = pad_poll(0);
    if (!(prev & PAD_UP) && (pad & PAD_UP) && sel)
      --sel;
    if (!(prev & PAD_DOWN) && (pad & PAD_DOWN) && sel < 2)
      ++sel;
    if (prev != 0xFF && !(prev & (PAD_START | PAD_A)) &&
        (pad & (PAD_START | PAD_A)))
      break;
    prev = pad;
    oam_set(0);
    // rows 13/15/17 -> y = row*8 - 1 (OAM y is line-1)
    oam_spr(88, (unsigned char)(103 + (sel << 4)), ST_GLYPH('>'), 0);
    oam_hide_rest();
  }
  g_difficulty = sel;
  oam_clear();
  ppu_off(); // caller loads the level and present_screen()s
}

// Fixed-region trampoline. Called before level_load(0), so R6 is
// restored to the boot default level bank on exit (harmless: level_load
// re-programs it immediately).
void difficulty_select(void) {
  set_prg_bank(HUD_PRG_BANK, 0x80);
  diff_run();
  set_prg_bank(lvl_bank[g_level], 0x80);
}

// ---------------------------------------------------------------------
// Game-flow bookend screens: GAME OVER (lives exhausted) and the ENDING
// ("demo complete") screen. Both are static text screens reusing the
// difficulty selector's font machinery (st_screen_open/st_text +
// the STATUS_CHR font bank), run entirely from bank 6 and called from
// main()'s state machine via the fixed trampolines below. Like diff_run
// they must never call ksfx/kmusic (those drivers retarget R6 and would
// unmap this bank mid-function); the caller stops the music before invoking
// them.

HUD_RO static const char s_gameover[] = "GAME OVER";
HUD_RO static const char s_congrats[] = "CONGRATULATIONS";
HUD_RO static const char s_finished[] = "YOU FINISHED THE DEMO";
HUD_RO static const char s_credit[] = "COMMANDER KEEN";
HUD_RO static const char s_orig[] = "ORIGINAL GAME BY ID SOFTWARE";
HUD_RO static const char s_buy[] = "PLEASE BUY IT AND SUPPORT";
HUD_RO static const char s_gamedev[] = "GAME DEVELOPMENT";
HUD_RO static const char s_press[] = "PRESS START";

// Wait for a fresh A/Start press (release-then-press edge so a button held
// from the preceding death/pickup doesn't skip the screen instantly).
HUD_BANKED static void st_wait_start(void) {
  unsigned char prev = 0xFF;
  for (;;) {
    unsigned char pad;
    ppu_wait_nmi();
    // Let a death/level-end effect finish naturally on a static bookend.
    // This variant restores HUD bank 6 before returning.
    ksfx_frame_hud();
    pad = pad_poll(0);
    if (prev != 0xFF && !(prev & (PAD_START | PAD_A)) &&
        (pad & (PAD_START | PAD_A)))
      break;
    prev = pad;
  }
}

HUD_BANKED static void gameover_run(void) {
  st_screen_open();
  bank_bg(0);
  st_text(NTA(11, 13), s_gameover);
  st_text(NTA(10, 17), s_press);
  scroll(0, 0);
  ppu_on_all();
  st_wait_start();
  ksfx_stop(); // early dismissal must also release pulse 2 before title
  oam_clear();
  ppu_off(); // caller restarts the flow (title -> selector -> level 0)
}

HUD_BANKED static void ending_run(void) {
  st_screen_open();
  bank_bg(0);
  st_text(NTA(8, 4), s_congrats);
  st_text(NTA(5, 7), s_finished);
  st_text(NTA(9, 10), s_credit);
  st_text(NTA(2, 14), s_orig);     // id Software attribution
  st_text(NTA(3, 17), s_buy);
  st_text(NTA(8, 19), s_gamedev);
  st_text(NTA(10, 24), s_press);
  scroll(0, 0);
  ppu_on_all();
  st_wait_start();
  oam_clear();
  ppu_off();
}

void gameover_show(void) {
  set_prg_bank(HUD_PRG_BANK, 0x80);
  gameover_run();
  set_prg_bank(lvl_bank[g_level], 0x80);
}

void ending_show(void) {
  set_prg_bank(HUD_PRG_BANK, 0x80);
  ending_run();
  set_prg_bank(lvl_bank[g_level], 0x80);
}

// Reset the persistent player stats for a brand-new game (called between
// the difficulty selector and level_load(0) each time the flow restarts).
// Pure WRAM writes -> safe in bank 6. Camera / picked-items / actors are
// reset by the level_load + player_init + actors_init that follow.
HUD_BANKED static void newgame_reset_b(void) {
  unsigned char i;
  static const unsigned char nextlife[8] = {0, 0, 0, 2, 0, 0, 0, 0}; // 20,000
  pl_lives = 3;
  pl_ammo = 5;
  pl_quest = 0;
  pl_keys = 0;
  pl_lifewater = 0;
  pl_keycard = 0;
  for (i = 0; i < 8; ++i) {
    pl_score_d[i] = 0;
    pl_nextlife_d[i] = nextlife[i];
  }
  ++pl_score_gen; // invalidate the HUD digit caches
}

void newgame_reset(void) {
  set_prg_bank(HUD_PRG_BANK, 0x80);
  newgame_reset_b();
  set_prg_bank(lvl_bank[g_level], 0x80);
}
