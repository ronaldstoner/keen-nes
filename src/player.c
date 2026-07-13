// Keen player physics. MIT License; see LICENSE.
//
// Unit system: 256 units per 16px tile, 16 units per pixel. Integrates ONE tic
// per 60Hz frame with per-tic velocity deltas pre-scaled x7/6 and tic-count
// durations x6/7, so the original 70Hz speeds and jump/pogo arcs match within
// ~1px.
#include <neslib.h>
#include <nesdoug.h>
#include <mapper.h>
#include "level_fmt.h"
#include "gen/levels.h" // EPISODE (ck5 exit doors / keycard / fuses)
#include "gen/player.h"
#include "player.h"
#include "sfx.h"
#include "actors.h"
#include "gen/sfx.h"

// --- physics constants, rescaled for the 60Hz tic ---
// (per-tic velocity deltas x7/6, tic-count durations x6/7, gravity accel 4->5;
//  matching the 70Hz arc within ~1px: jump apex 67.5->68.8px, pogo
//  105->105.8px, walk 99.75px/s exact.)
#define GRAVITY_HIGH 5    // per odd tic (was 4 @70Hz)
#define VEL_TERMINAL 82   // units/tic (was 70)
#define JUMP_VEL (-47)    // units/tic (was -40)
#define JUMP_TIME 15      // tics of gravity suspension (was 18)
#define POGO_VEL (-56)    // was -48
#define POGO_TIME 20      // was 24
#define RUN_SPEED 28      // units/tic (was 24)
#define AIR_ACCEL 1       // units/tic^2 (x7/6 rounds back to 1)
#define GND_ACCEL 4       // was 3

// clip box (units), from the standing frame's spritetable entry
#define BOX_W (KEEN_CLIP_XH - KEEN_CLIP_XL) // ~240 = 15px
#define BOX_H (KEEN_CLIP_YH - KEEN_CLIP_YL) // ~496 = 31px

// LOOK-up/down + DEATH pose frames (FRAME_LOOKU/LOOKD1/LOOKD2/DEATH). The
// per-level sprite bank loads the real pose art where a level's enemy set
// leaves room; on dense levels those slots hold a STAND/JUMP fallback, so
// drawing FRAME_LOOKU there simply shows STAND — no runtime gate needed.

// volatile: prevents LTO from caching stale copies across the frame loop
volatile unsigned int pl_x, pl_y;   // clip box top-left, world units
volatile int pl_vx, pl_vy;          // units per tic
volatile unsigned char pl_on_ground, pl_pogo, pl_face; // face: 0=R 1=L
unsigned char pl_ammo = 5, pl_lives = 3;
unsigned char pl_dead, pl_level_done;
unsigned char pl_keycard; // ck5 security keycard (persists until level end)
#if EPISODE == 5
// pogo slammed cell (col, row) hard this tic — main.c checks for a fuse
unsigned char pl_fuse_x, pl_fuse_y, pl_fuse_hit;
#endif
// score as 8 unpacked BCD digits, MSB first (the 2A03 disables the 6502's
// decimal mode, so carries are manual digit loops; display needs no conversion)
unsigned char pl_score_d[8];
unsigned char pl_nextlife_d[8] = {0, 0, 0, 2, 0, 0, 0, 0}; // 20,000
unsigned char pl_score_gen;   // bumped on every award (HUD cache key)
unsigned char pl_keys;        // key gem bitmask (item types 0..3)
unsigned char pl_quest;       // quest items (K4 council / K6 items)
unsigned char pl_lifewater;   // Keen 4 lifewater drops; persists across levels
// door/gem/switch detection -> main.c (see player.h)
unsigned char pl_gem_hit, pl_switch_hit, pl_door;
unsigned int pl_door_x, pl_door_y;
static unsigned char tic_parity, jump_timer;
static unsigned char anim_timer, anim_frame;
static unsigned char jump_held_prev, pogo_held_prev;
static unsigned char up_prev; // door entry needs a fresh Up press

// look up/down (camera peek) state; pl_look_off is consumed by the camera in
// main.c. look_dir: 0 none, 1 up, 2 down.
volatile signed char pl_look_off;
static unsigned char look_dir, look_timer;

// pole / rope climbing. pl_pole: 0 = free, 1 = clinging to a pole. Grab on
// Up/Down near a pole tile (metatile flag bit MT_FLAG_POLE); jump (A) or step
// off the side to dismount, with a short re-grab cooldown so the dismount jump
// clears the pole. The generator packs the three SHINNY frames atomically
// wherever they fit; a level that cannot hold the full cycle gets the standing
// fallback in all three slots, so runtime logic stays branch-free.
unsigned char pl_pole;
static unsigned char pole_cd;   // tics before a jumped-off pole can be re-grabbed
unsigned char pl_ledge;         // 1 hang, 2 pull-up animation
static unsigned char ledge_timer, ledge_mx, ledge_my;
#define POLE_UP_SPEED 9         // pole climb-up velY (u/tic; x7/6 from 8)
#define POLE_DN_SPEED 28        // pole slide-down velY (x7/6 from 24)
#define POLE_JUMP_VY (-23)      // pole jump-off velY (x7/6 from -20)
#define POLE_GRAB_CD 16         // pole re-grab delay (x6/7 from 19)
static unsigned char solid_top_at(unsigned char mx, unsigned char my);
static unsigned char flags_at(unsigned char mx, unsigned char my);

// Called only after the existing X collision found the lower wall row. Byte
// coordinates keep the rare detector compact in the critically-full fixed
// bank; the map reads retain the level bank as required.
__attribute__((noinline)) static unsigned char
ledge_try(unsigned char mx, unsigned char upper, unsigned char lower,
          unsigned char face) {
  if ((flags_at(mx, upper) & 7u) || solid_top_at(mx, lower) != 1)
    return 0;
  pl_ledge = 1; ledge_timer = 10;
  ledge_mx = mx; ledge_my = lower;
  pl_y = (unsigned int)lower << 8;
  pl_face = face; pl_pogo = 0; pl_on_ground = 0;
  pl_vy = 0; anim_frame = FRAME_HANG;
  return 1;
}

// Rare ledge state runs from bank 6. Detection itself piggybacks on the fixed
// horizontal collision scan below; once hanging, no map reads are needed.
__attribute__((noinline, section(".prg_rom_6.text"))) static void
ledge_tic_b(unsigned char pad) {
  if (pl_ledge == 1) {
    anim_frame = FRAME_HANG;
    if (ledge_timer) {
      --ledge_timer; // HANG1 lead-in: 12 DOS tics -> 10 at 60Hz
      return;
    }
    if ((pad & PAD_DOWN) ||
        (pl_face ? (pad & PAD_RIGHT) : (pad & PAD_LEFT))) {
      pl_ledge = 0; // down or away from the wall drops
      pl_vy = 1;
      return;
    }
    if ((pad & PAD_UP) ||
        (pl_face ? (pad & PAD_LEFT) : (pad & PAD_RIGHT))) {
      pl_ledge = 2;
      ledge_timer = 36; // four 10-tic actions, scaled x6/7 -> 4x9
      anim_frame = FRAME_PULL1;
    }
    return;
  }
  // Pull toward the prevalidated floor top. Motion is spread across the four
  // art stages; the final snap removes integer-rounding drift.
  if (ledge_timer > 27) anim_frame = FRAME_PULL1;
  else if (ledge_timer > 18) anim_frame = FRAME_PULL2;
  else if (ledge_timer > 9) anim_frame = FRAME_PULL3;
  else anim_frame = FRAME_PULL4;
  pl_y -= 14;
  pl_x += pl_face ? -7 : 7;
  if (--ledge_timer == 0) {
    pl_y = ((unsigned int)ledge_my << 8) - BOX_H - 1;
    pl_x = pl_face ? (((unsigned int)(ledge_mx + 1) << 8) - BOX_W - 1)
                   : ((unsigned int)ledge_mx << 8);
    pl_ledge = 0;
    pl_on_ground = 1;
    pl_vx = pl_vy = 0;
    anim_frame = FRAME_STAND;
  }
}

// score award: (code>>4) * 10^(code&15) points; extra life + threshold doubling
// when score >= the next-life threshold (starts 20,000). All awards are one
// digit times a power of ten, hence the nibble code. Body banked (fixed PRG is
// full); returns nonzero when an extra Keen was earned — the fixed wrapper
// plays the sound, because ksfx_play restores R6 to the LEVEL bank and would
// unmap this function.
__attribute__((noinline, section(".prg_rom_6.text"))) static unsigned char
score_add_b(unsigned char code) {
  unsigned char i = 7 - (code & 15);
  unsigned char d = code >> 4;
  for (;;) {
    d += pl_score_d[i];
    if (d < 10) {
      pl_score_d[i] = d;
      break;
    }
    pl_score_d[i] = d - 10;
    if (i == 0)
      break; // saturate at 99,999,999 (DOS i32 wraps far later; moot)
    d = 1;
    --i;
  }
  ++pl_score_gen;
  for (i = 0; i < 8; ++i) { // BCD compare, MSB first
    if (pl_score_d[i] > pl_nextlife_d[i])
      break; // above threshold
    if (pl_score_d[i] < pl_nextlife_d[i])
      return 0; // below: no extra life
  }
  ++pl_lives; // score >= next-life threshold: extra Keen + double the threshold
  {
    unsigned char c = 0;
    i = 8;
    while (i--) {
      unsigned char v = (unsigned char)(pl_nextlife_d[i] << 1) + c;
      c = 0;
      if (v >= 10) {
        v -= 10;
        c = 1;
      }
      pl_nextlife_d[i] = v;
    }
  }
  return 1;
}

extern const unsigned char lvl_bank[]; // src/gen/leveldata_mmc5.c

// look up / look down camera peek: 30 tics of animation before the camera
// moves. Requires grounded, standing still, pure up (or down) with no A
// (jump/pogo chord) and no B (shot aiming), so it can never fight aiming or the
// down+A pogo toggle. Banked to bank 6: pure RAM logic, no level-bank/sfx
// access (fixed region is critically full).
__attribute__((noinline, section(".prg_rom_6.text"))) static void
look_tic_b(unsigned char pad) {
  unsigned char dpad =
      pad & (PAD_UP | PAD_DOWN | PAD_LEFT | PAD_RIGHT | PAD_A | PAD_B);
  unsigned char want = 0;
  if (pl_on_ground && !pl_pogo && pl_vx == 0) {
    if (dpad == PAD_UP)
      want = 1;
    else if (dpad == PAD_DOWN)
      want = 2;
  }
  if (want && want == look_dir) {
    if (look_timer <= 26) // 30-tic DOS lead-in, x6/7 for the 60Hz tic
      ++look_timer;
    else if (want == 1) { // camera up, 1px/tic, DOS total 27px
      if (pl_look_off > -27)
        --pl_look_off;
    } else { // camera down, 1px/tic, DOS total 107px
      if (pl_look_off < 107)
        ++pl_look_off;
    }
  } else {
    look_dir = want; // restart the 30-tic lead-in on a direction change
    look_timer = 0;
    if (!want && pl_look_off) { // return spring: 3px/tic (DOS cap)
      if (pl_look_off < 0) {
        pl_look_off += (pl_look_off < -3) ? 3 : -pl_look_off;
      } else {
        pl_look_off -= (pl_look_off > 3) ? 3 : pl_look_off;
      }
    }
  }
}

// Covered-terrain sprite layering: DOS re-blits misc-0x80 fg tiles over ALL
// sprites — pole holes AND secret passages. pl_cov_mask bit (r<<2|c) = image
// cell (col+c, row+r) covers sprites, sampled each tic by cover_scan() (fixed
// region: MT_FLAGS needs the level bank). This bank-6 walker flips each
// ACTIVE-frame ms_wram record's OAM priority bit by its own cell, so Keen
// emerges from holes half-out like DOS; when EVERY record is covered,
// pl_cov_hide skips the draw entirely (the priority bit alone still leaks
// through the art's color-0 pixels). The previously-active frame's records
// are restored on frame change so stale bits never show. Ledge frames live
// in ROM (bank 26) and are skipped — hanging inside covered terrain is not
// a shipping layout.
static unsigned int pl_cov_mask;
static unsigned char pl_cov_hide;
static const unsigned char *cov_prev; // last frame walked (record state owner)
static unsigned char cov_dirty;       // some record still carries 0x20
// caches: cover_scan reruns only when Keen's cell / the override list change;
// cover_pri_b rewrites records only when its inputs change. Run
// unconditionally the two cost ~8% of a busy frame.
static unsigned char cov_cell_x = 0xFF, cov_cell_y = 0xFF, cov_ovgen = 0xFF;
static unsigned char cov_in_fx = 0xFF, cov_in_fy, cov_in_fr, cov_in_fc;
static unsigned int cov_in_mask;
// bit for image cell (r,c): (r<<2)|c indexed; a variable u16 shift lowers to
// a runtime loop (__ashlhi3) on the 6502, so both paths use tables/increments
__attribute__((section(".prg_rom_27")))
static const unsigned int cov_bit_b6[12] = {1, 2, 4, 0, 16, 32, 64, 0,
                                            256, 512, 1024, 0};
__attribute__((noinline, section(".prg_rom_27.text"))) static void
cover_pri_b(void) {
  const unsigned char *cur;
  unsigned char fx, fy, all = 1, any = 0;
  unsigned char *q;
  if (anim_frame > FRAME_POLED1) { // ledge poses: ROM records, no layering
    pl_cov_hide = 0;
    if (cov_dirty && cov_prev) {
      q = (unsigned char *)cov_prev;
      while (*q != 128) {
        q[3] &= (unsigned char)~0x20;
        q += 4;
      }
      cov_dirty = 0;
      cov_in_fx = 0xFF;
    }
    return;
  }
  cur = ms_frames_b6[anim_frame][pl_face];
  if (cov_dirty && cov_prev != cur) { // restore the frame we left
    q = (unsigned char *)cov_prev;
    while (*q != 128) {
      q[3] &= (unsigned char)~0x20;
      q += 4;
    }
    cov_dirty = 0;
    cov_in_fx = 0xFF;
  }
  cov_prev = cur;
  q = (unsigned char *)cur;
  if (!pl_cov_mask) {
    if (cov_dirty)
      while (*q != 128) {
        q[3] &= (unsigned char)~0x20;
        q += 4;
      }
    cov_dirty = 0;
    pl_cov_hide = 0;
    cov_in_fx = 0xFF;
    return;
  }
  fx = (unsigned char)((pl_x - KEEN_CLIP_XL) >> 4) & 15u;
  fy = (unsigned char)(pl_y >> 4) & 15u;
  // identical inputs since the last walk: records already correct
  if (fx == cov_in_fx && fy == cov_in_fy && pl_cov_mask == cov_in_mask &&
      anim_frame == cov_in_fr && pl_face == cov_in_fc)
    return;
  cov_in_fx = fx;
  cov_in_fy = fy;
  cov_in_mask = pl_cov_mask;
  cov_in_fr = anim_frame;
  cov_in_fc = pl_face;
  while (*q != 128) {
    // sample the 8x16 sprite's center: +4px x, +8px y into the sprite
    unsigned char r = (unsigned char)((fy + q[1] + 8u) >> 4);
    unsigned char c = (unsigned char)((fx + q[0] + 4u) >> 4);
    if (pl_cov_mask & cov_bit_b6[(r << 2) | c]) {
      q[3] |= 0x20;
      any = 1;
    } else {
      q[3] &= (unsigned char)~0x20;
      all = 0;
    }
    q += 4;
  }
  cov_dirty = any;
  pl_cov_hide = all;
}

// 3x3 cell scan of Keen's image window -> pl_cov_mask (bit r<<2|c). Runs in
// the fixed region (MT_FLAGS maps the level blob at $8000), but only when
// Keen crosses into a new cell or the override list changes (ov_n proxies
// door/switch cell swaps) — the mask is a pure function of those inputs.
static void cover_scan(void) {
  unsigned char bc = (unsigned char)((pl_x - KEEN_CLIP_XL) >> 8);
  unsigned char br = (unsigned char)(pl_y >> 8);
  unsigned char r;
  unsigned int m = 0, bit = 1;
  if (bc == cov_cell_x && br == cov_cell_y && ov_n == cov_ovgen)
    return;
  cov_cell_x = bc;
  cov_cell_y = br;
  cov_ovgen = ov_n;
  for (r = 0; r < 3; ++r, bit <<= 2) {
    unsigned char my = (unsigned char)(br + r);
    if (my >= g_h)
      break;
    if (flags_at(bc, my) & 0x80)
      m |= bit;
    bit <<= 1;
    if (flags_at((unsigned char)(bc + 1), my) & 0x80)
      m |= bit;
    bit <<= 1;
    if (flags_at((unsigned char)(bc + 2), my) & 0x80)
      m |= bit;
  }
  pl_cov_mask = m;
}

void score_add(unsigned char code) {
  unsigned char life;
  set_prg_bank(6, 0x80); // the HUD/status bank (hud.c HUD_PRG_BANK)
  life = score_add_b(code);
  set_prg_bank(lvl_bank[g_level], 0x80);
  if (life)
    ksfx_play(SFX_EXTRALIFE);
}

// --- neural stunner shots ---
#define MAX_SHOTS 2
#define SHOT_SPEED 75     // units/tic (x7/6 from 64)
static unsigned int shot_x[MAX_SHOTS], shot_y[MAX_SHOTS];
static unsigned char shot_dir[MAX_SHOTS];   // 0=R 1=L 2=U 3=D
static unsigned char shot_state[MAX_SHOTS]; // 0 off, 1 fly, 2+ hit timer
static unsigned char shot_tic[MAX_SHOTS];

extern unsigned char actors_shot_hit(unsigned int x, unsigned int y);
// Bounder ride (CK4: a bouncing ball Keen stands on and bounces with, like a
// moving platform). actors.c exposes the query plus the ridden Bounder's top
// and this-tic horizontal delta.
#if EPISODE == 4
extern unsigned char bounder_under(unsigned int x0, unsigned int x1,
                                   unsigned int feet, unsigned int tol);
extern unsigned int bounder_top(unsigned char idx1);
extern signed char bl_bdx[];
#endif

// Collision decode. Opened gem doors need no special-case here: map_cell()
// (level.c) substitutes the OPEN metatile for opened door cells, and the open
// tile is non-solid, so MT_TOP/MT_FLAGS naturally report it passable.
static unsigned char solid_top_at(unsigned char mx, unsigned char my) {
  return MT_TOP(mx, my); // per-band solidity (band selected by row my)
}
static unsigned char flags_at(unsigned char mx, unsigned char my) {
  return MT_FLAGS(mx, my);
}

// slope-tile geometry: per-slope floor heights in pixels (units >> 4).
// Row = top-code & 7, column = x pixel within tile.
// Floor surface (units) = tile_top + height * 16.
static const unsigned char slope_px[8][16] = {
    {16, 16, 16, 16, 16, 16, 16, 16, 16, 16, 16, 16, 16, 16, 16, 16},
    {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    {0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7},
    {8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13, 14, 14, 15, 15},
    {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15},
    {7, 7, 6, 6, 5, 5, 4, 4, 3, 3, 2, 2, 1, 1, 0, 0},
    {15, 15, 14, 14, 13, 13, 12, 12, 11, 11, 10, 10, 9, 9, 8, 8},
    {15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0}};

#define NO_FLOOR 0xFFFFu

// floor surface Y (units) of metatile row my sampled at world x (units)
static unsigned int floor_at(unsigned int x_units, unsigned char my) {
  unsigned char t;
  if (my >= (unsigned int)g_h)
    return NO_FLOOR;
  t = solid_top_at(x_units >> 8, my);
  if (!t)
    return NO_FLOOR;
  return (my << 8) + ((unsigned int)slope_px[t & 7][(x_units >> 4) & 15] << 4);
}

// Banked body (bank 6, the HUD/status bank): init-only, touches no
// level-bank data (g_spawn_* are RAM), so the cold code doesn't eat the
// critically-full fixed region. The fixed wrapper restores the level
// bank (score_add pattern). Banked code must never call ksfx/kmusic.
__attribute__((noinline, section(".prg_rom_6.text"))) static void
player_init_b(void) {
  unsigned char s;
  pl_x = ((unsigned int)g_spawn_x << 8) + KEEN_CLIP_XL;
  // feet 1 unit above the spawn row's bottom boundary so the first fall
  // tic crosses into the floor row and the one-way top check fires
  pl_y = ((unsigned int)g_spawn_y << 8) + 256 - BOX_H - 1;
  pl_vx = pl_vy = 0;
  pl_on_ground = 0;
  pl_pogo = 0;
  pl_face = 0;
  // DOS reloads the level on death/entry: all transient player state
  // resets with it (the port respawns in place, so reset explicitly)
  jump_timer = 0;
  tic_parity = 0;
  jump_held_prev = pogo_held_prev = 1; // held buttons need a fresh press
  up_prev = PAD_UP;                    // ...doors too
  pl_pole = 0;
  pl_ledge = 0;
  pole_cd = 0;
  pl_cov_mask = 0;
  pl_cov_hide = 0;
  cov_cell_x = 0xFF; // cover caches: recompute on the first tic
  cov_ovgen = 0xFF;
  cov_in_fx = 0xFF;
  pl_gem_hit = pl_switch_hit = pl_door = 0;
  look_dir = 0;
  look_timer = 0;
  pl_look_off = 0;
  anim_timer = 0;
  anim_frame = FRAME_STAND;
  plat_ridden = 0;
  for (s = 0; s < MAX_SHOTS; ++s)
    shot_state[s] = 0; // live stunner shots die with the reload
  // pl_keys is cleared by main.c's level-entry AND death-reload paths (DOS
  // behavior: dying reloads the level fresh -- items respawn, gems reset,
  // doors re-close -- so gem state always belongs to the level session).
}

void player_init(void) {
  set_prg_bank(6, 0x80); // HUD/status bank (hud.c HUD_PRG_BANK)
  player_init_b();
  set_prg_bank(lvl_bank[g_level], 0x80);
}

// deadly tile contact (mt_flags bit 3) across Keen's box
static unsigned char touches_deadly(void) {
  // World metatile coordinates are u8 (level dimensions are u8); keeping
  // these loop bounds 16-bit made every compare/increment need two bytes.
  unsigned char x0 = pl_x >> 8, x1 = (pl_x + BOX_W) >> 8;
  unsigned char y0 = pl_y >> 8, y1 = (pl_y + BOX_H) >> 8;
  unsigned char tx, ty;
  for (ty = y0; ty <= y1; ++ty)
    for (tx = x0; tx <= x1; ++tx)
      if (flags_at(tx, ty) & 8)
        return 1;
  return 0;
}

// pole tile at metatile (mx,my)? (MT flags bit MT_FLAG_POLE)
static unsigned char is_pole(unsigned char mx, unsigned char my) {
  return (flags_at(mx, my) & MT_FLAG_POLE) != 0;
}

// One tic clinging to a pole. Up climbs, Down slides, A jumps off, Left/Right
// steps off at a floor. Returns with pl_pole cleared on dismount. Keen is
// X-snapped to the pole column at grab time.
static void pole_tic(unsigned char pad) {
  unsigned int mx = (pl_x + BOX_W / 2) >> 8;
  unsigned int cen; // Keen's vertical-center metatile row
  pl_vx = pl_vy = 0;
  // jump off (fresh A press): hop up and to the held side, brief re-grab lockout
  if ((pad & PAD_A) && !jump_held_prev) {
    pl_pole = 0;
    pl_vy = POLE_JUMP_VY;
    jump_timer = 9; // x6/7 from 10
    if (pad & PAD_LEFT) {
      pl_vx = -POLE_UP_SPEED;
      pl_face = 1;
    } else if (pad & PAD_RIGHT) {
      pl_vx = POLE_UP_SPEED;
      pl_face = 0;
    }
    pl_on_ground = 0;
    pole_cd = POLE_GRAB_CD;
    ksfx_play(SFX_JUMP);
    return;
  }
  if (pad & PAD_UP) { // climb up while a pole is still above the new center
    unsigned int newy = pl_y - POLE_UP_SPEED;
    cen = (newy + BOX_H / 2) >> 8;
    if (is_pole(mx, cen))
      pl_y = newy; // else: at the pole top, hold in place
  } else if (pad & PAD_DOWN) { // slide down
    unsigned int newy = pl_y + POLE_DN_SPEED;
    unsigned int nfeet = (newy + BOX_H) >> 8;
    cen = (newy + BOX_H / 2) >> 8;
    if (solid_top_at(mx, nfeet) == 1) { // reached a floor: dismount standing
      pl_y = (nfeet << 8) - BOX_H - 1;
      pl_pole = 0;
      pl_on_ground = 1;
    } else if (is_pole(mx, cen)) {
      pl_y = newy;
    } else { // slid past the pole bottom into open air: let go and fall
      pl_y = newy;
      pl_pole = 0;
    }
  }
  // step off to the side when a floor is underfoot
  if (pl_pole && (pad & (PAD_LEFT | PAD_RIGHT))) {
    unsigned int feet_my = ((pl_y + BOX_H) >> 8) + 1;
    if (solid_top_at(mx, feet_my)) {
      pl_pole = 0;
      pl_vx = (pad & PAD_RIGHT) ? POLE_UP_SPEED : -POLE_UP_SPEED;
      pl_face = (pad & PAD_RIGHT) ? 0 : 1;
    }
  }
}

// Try to grab a pole: Up grabs one overlapping the upper body, Down grabs one
// just below the feet. On grab, snap X to the pole column and enter pole mode.
// Requires no active re-grab cooldown.
static unsigned char try_grab_pole(unsigned char pad) {
  unsigned int mx;
  if (pl_pole || pole_cd)
    return 0;
  mx = (pl_x + BOX_W / 2) >> 8;
  if ((pad & PAD_UP) &&
      (is_pole(mx, pl_y >> 8) || is_pole(mx, (pl_y + BOX_H / 2) >> 8))) {
    ; // grab going up
  } else if ((pad & PAD_DOWN) && is_pole(mx, ((pl_y + BOX_H) >> 8) + 1)) {
    ; // grab going down
  } else {
    return 0;
  }
  pl_x = ((unsigned int)mx << 8) + 8; // center the clip box on the pole column
  pl_vx = pl_vy = 0;
  pl_on_ground = 0;
  pl_pogo = 0;
  jump_timer = 0;
  look_dir = 0; // a grab cancels any look-peek
  pl_look_off = 0;
  pl_pole = 1;
  return 1;
}

// one 70Hz physics tic
static void tic(unsigned char pad) {
  unsigned char jump_held = (pad & PAD_A) != 0;
  unsigned char pogo_held = (pad & PAD_B) != 0;
  int dir = 0;
  if (pad & PAD_LEFT)
    dir = -1;
  if (pad & PAD_RIGHT)
    dir = 1;

  if (pole_cd)
    --pole_cd;
  if (pl_ledge) {
    set_prg_bank(6, 0x80);
    ledge_tic_b(pad);
    set_prg_bank(lvl_bank[g_level], 0x80);
    jump_held_prev = jump_held;
    pogo_held_prev = pogo_held;
    return;
  }
  if (pl_pole) { // clinging to a pole: its own input handler owns this tic
    pole_tic(pad);
    // covered-terrain sprite priority (bank 6): the walk path's banked call
    // below is unreachable from this early return, so apply it here too
    cover_scan();
    if (pl_cov_mask || cov_dirty) {
      set_prg_bank(27, 0x80);
      cover_pri_b();
      set_prg_bank(lvl_bank[g_level], 0x80);
    }
    jump_held_prev = jump_held;
    pogo_held_prev = pogo_held;
    up_prev = pad & PAD_UP;
    return;
  }

  // control scheme: A = jump, B = shoot (with up/down aiming), down+A (fresh
  // press) = pogo toggle, usable on the ground or mid-air.
  {
    unsigned char shoot = (pogo_held && !pogo_held_prev); // B press
    unsigned char down_held = (pad & PAD_DOWN) != 0;
    if (jump_held && !jump_held_prev && down_held) {
      pl_pogo ^= 1;             // mount/dismount pogo
      jump_held_prev = 1;       // consume the press: no jump this tic
      if (pl_pogo && pl_on_ground) { // mounting on ground: first bounce
        pl_vy = POGO_VEL;
        jump_timer = POGO_TIME;
        pl_on_ground = 0;
        ksfx_play(SFX_POGO);
      }
    }
    if (shoot && !pl_ammo)
      ksfx_play(SFX_NOAMMO);
    if (shoot && pl_ammo) {
      unsigned char s;
      for (s = 0; s < MAX_SHOTS; ++s)
        if (!shot_state[s]) {
          --pl_ammo;
          if (pad & PAD_UP) {         // aim up (id shot dir 0)
            shot_dir[s] = 2;
            shot_x[s] = pl_x - KEEN_CLIP_XL + 80;
            shot_y[s] = pl_y - 160;
          } else if ((pad & PAD_DOWN) && !pl_on_ground) { // airborne: down
            shot_dir[s] = 3;
            shot_x[s] = pl_x - KEEN_CLIP_XL + 128;
            shot_y[s] = pl_y + BOX_H + 32;
          } else if (pl_face) {
            shot_dir[s] = 1;
            shot_x[s] = pl_x - KEEN_CLIP_XL - 128;
            shot_y[s] = pl_y + 64;
          } else {
            shot_dir[s] = 0;
            shot_x[s] = pl_x - KEEN_CLIP_XL + 256;
            shot_y[s] = pl_y + 64;
          }
          shot_state[s] = 1;
          shot_tic[s] = 0;
          ksfx_play(SFX_SHOOT);
          break;
        }
    }
  }
  pogo_held_prev = pogo_held;

  // horizontal accel (ground faster than air, cap RUN_SPEED)
  {
    signed char acc = pl_on_ground ? GND_ACCEL : AIR_ACCEL;
    if (dir > 0) {
      pl_face = 0;
      pl_vx += acc;
      if (pl_vx > RUN_SPEED)
        pl_vx = RUN_SPEED;
    } else if (dir < 0) {
      pl_face = 1;
      pl_vx -= acc;
      if (pl_vx < -RUN_SPEED)
        pl_vx = -RUN_SPEED;
    } else if (pl_on_ground) {
      // ground friction
      if (pl_vx > 0)
        pl_vx -= (pl_vx > GND_ACCEL) ? GND_ACCEL : pl_vx;
      else if (pl_vx < 0)
        pl_vx += (-pl_vx > GND_ACCEL) ? GND_ACCEL : -pl_vx;
    }
  }

  // jump start
  if (jump_held && !jump_held_prev && pl_on_ground && !pl_pogo) {
    pl_vy = JUMP_VEL;
    jump_timer = JUMP_TIME;
    pl_on_ground = 0;
    ksfx_play(SFX_JUMP);
  }
  jump_held_prev = jump_held;

  // variable jump: releasing A ends the boost phase
  if (jump_timer) {
    if (!jump_held)
      jump_timer = 0;
    else
      --jump_timer;
  } else {
    // gravity applies on odd tics only (original quirk)
    tic_parity ^= 1;
    if (tic_parity && !pl_on_ground) {
      pl_vy += GRAVITY_HIGH;
      if (pl_vy > VEL_TERMINAL)
        pl_vy = VEL_TERMINAL;
    }
  }

  // doors: FRESH Up press while standing on a door cell -> teleport.
  // Edge-detected: two-way door pairs land Keen inside the twin door's
  // window, and DOS's ~1s enter-door animation is what stops a held Up
  // from re-entering there; without an animation a held Up ping-ponged
  // between the pair at 70Hz. A fresh press per entry matches the felt
  // DOS behavior.
  if ((pad & PAD_UP) && !up_prev && pl_on_ground) {
    unsigned char i;
    unsigned int mx = (pl_x + BOX_W / 2) >> 8;
    unsigned int my = (pl_y + BOX_H) >> 8;
    unsigned char my0 = (unsigned char)(pl_y >> 8); // box top row
    // plat/bridge switch: a switch tile in Keen's midpoint COLUMN, anywhere
    // across his box rows (handles sit at body height, not underfoot),
    // toggles its target (sw_n <= 4, direct scan).
    for (i = 0; i < sw_n; ++i) {
      const unsigned char *r = SW_REC(i);
      if (r[0] == mx && r[1] >= my0 && r[1] <= (unsigned char)my) {
        pl_switch_hit = (unsigned char)(i + 1);
        break;
      }
    }
    for (i = 0; i < g_ndoors; ++i)
      if (g_doors[(unsigned int)i * 4] == mx && my >= g_doors[(unsigned int)i * 4 + 1] && my <= g_doors[(unsigned int)i * 4 + 1] + 4u) {
#if EPISODE == 5
        // ck5 exit doors (dest y 0): level complete; dest x 1 = security door,
        // which needs the keycard
        if (g_doors[(unsigned int)i * 4 + 3] == 0) {
          if (g_doors[(unsigned int)i * 4 + 2] && !pl_keycard) {
            ksfx_play(SFX_KEYCARD); // SOUND_NEEDKEYCARD
            break;
          }
          pl_level_done = 1;
          break;
        }
#endif
        // door: hand the destination to main.c, which runs the walk-in + fade
        // + INSTANT-CUT transition instead of scrolling the camera there (which
        // revealed the map + stalled frames).
        pl_door_x = ((unsigned int)g_doors[(unsigned int)i * 4 + 2] << 8) + KEEN_CLIP_XL;
        pl_door_y = ((unsigned int)g_doors[(unsigned int)i * 4 + 3] << 8) + 256 - BOX_H - 1;
        pl_door = 1;
        pl_vx = pl_vy = 0;
        break;
      }
  }
  up_prev = pad & PAD_UP;

  // look up / look down camera peek + covered-terrain sprite priority:
  // banked bodies (pure RAM logic, no map/sfx access — safe in bank 6; the
  // fixed region is over-full). cover_scan needs the level bank: run first.
  cover_scan();
  set_prg_bank(6, 0x80);
  look_tic_b(pad);
  set_prg_bank(lvl_bank[g_level], 0x80);
  if (pl_cov_mask || cov_dirty) {
    set_prg_bank(27, 0x80);
    cover_pri_b();
    set_prg_bank(lvl_bank[g_level], 0x80);
  }

  // --- X move + collision ---
  if (pl_vx) {
    unsigned int newx = pl_x + pl_vx;
    unsigned char y0 = pl_y >> 8, y1 = (pl_y + BOX_H) >> 8;
    unsigned char mx;
    unsigned char blocked = 0;
    unsigned char ty = y0; // also defined at the right map boundary
    // Standing ON a slope, the feet row must not wall-scan: mid-slope the
    // feet sit inside the slope tile's row, and a hill-crest fill tile with
    // solid sides in that row would stop the climb (Border Village hills).
    // The feet snap to the neighbor's higher surface on the next Y tic.
    if (pl_on_ground && y1 > y0) {
      unsigned char st =
          solid_top_at((unsigned char)((pl_x + BOX_W / 2) >> 8),
                       (unsigned char)((pl_y + BOX_H + 1) >> 8));
      if (st > 1)
        --y1;
    }
    if (pl_vx > 0) {
      mx = (newx + BOX_W) >> 8;
      if (mx >= g_w)
        blocked = 1;
      else {
        for (ty = y0; ty <= y1; ++ty)
          if (flags_at(mx, ty) & 4) { // tile's left side solid
            blocked = 1;
            break;
          }
        // walk-out level exit: the level ends when Keen's clip box crosses the
        // horizontal scroll bound (two-tile border, so scroll max + screen =
        // tile-to-unit(w-2)). Fires when the box enters column w-2 — reachable
        // only where the map leaves it open (exit corridors).
        if (mx >= (unsigned char)(g_w - 2)) {
          if (!blocked)
            pl_level_done = 1;
          blocked = 1;
        }
      }
      if (blocked) {
        pl_x = ((unsigned int)mx << 8) - BOX_W - 1;
        pl_vx = 0;
        // ledge grab: while falling toward a wall, the upper side cell is
        // empty and the next cell down has both a left wall and a floor top.
        // `ty` is the row that stopped the existing collision scan, so this
        // adds only the top-code confirmation instead of a second wall pass.
        if (pl_vy > 0 && (pad & PAD_RIGHT) && ty == y0 + 1u)
          ledge_try((unsigned char)mx, (unsigned char)y0, (unsigned char)ty, 0);
      } else
        pl_x = newx;
    } else {
      mx = newx >> 8;
      for (ty = y0; ty <= y1; ++ty)
        if (flags_at(mx, ty) & 1) { // tile's right side solid
          blocked = 1;
          break;
        }
      // left walk-out exit: box crossing into column 1 (min scroll bound =
      // 0x200, two-tile border). Solid columns 0/1 still wall exactly like the
      // old x<256 stop (probe above ran first).
      // `int` is signed 16-bit on the 6502. Casting world X to int made every
      // coordinate >= $8000 look negative, so Slug Village (spawn x=142,
      // $8E40 in player units) completed on the first LEFT step. Stay fully
      // unsigned; pl_x catches a near-zero subtraction before it can wrap.
      if (pl_x < 512u || newx < 512u) {
        if (!blocked)
          pl_level_done = 1;
        blocked = 1;
      }
      if (blocked) {
        pl_x = ((unsigned int)(mx + 1u) << 8);
        pl_vx = 0;
        if (pl_vy > 0 && (pad & PAD_LEFT) && ty == y0 + 1u)
          ledge_try((unsigned char)mx, (unsigned char)y0, (unsigned char)ty, 1);
      } else
        pl_x = newx;
    }
  }

  if (pl_ledge)
    return; // ledge owns Y until drop/pull completes

  // --- Y move + collision ---
  {
    unsigned int newy = pl_y + pl_vy;
    unsigned char x0 = pl_x >> 8, x1 = (pl_x + BOX_W) >> 8;
    unsigned int midx = pl_x + BOX_W / 2;
    unsigned char tx;
    if (pl_on_ground) {
      // riding a platform? inherit its motion and stay snapped to its top
      unsigned char pi;
      set_prg_bank(26, 0x80);   // plat_under is in cold bank 26
      pi = plat_under(pl_x, pl_x + BOX_W, pl_y + BOX_H + 1, 96);
      set_prg_bank(lvl_bank[g_level], 0x80);
      plat_ridden = pi;
      if (pi) {
        --pi;
        pl_x += pf_dx[pi];
        pl_y += pf_dy[pi]; // riders track fall/rise platforms too
        pl_y = pfy[pi] - BOX_H - 1;
        pl_vy = 0;
        goto y_done;
      }
#if EPISODE == 4
      { // riding a Bounder: inherit its X drift, stay snapped to its bouncing
        // top (so Keen bounces with it and can jump off) — CK4 Bounder ride
        unsigned char bi;
        set_prg_bank(26, 0x80);   // bounder_under is in cold bank 26
        bi = bounder_under(pl_x, pl_x + BOX_W, pl_y + BOX_H + 1, 96);
        set_prg_bank(lvl_bank[g_level], 0x80);
        if (bi) {
          pl_x += bl_bdx[bi - 1];
          pl_y = bounder_top(bi) - BOX_H - 1;
          pl_vy = 0;
          goto y_done;
        }
      }
#endif
    } else
      plat_ridden = 0;
    if (!pl_on_ground)
      plat_ridden = 0;
    if (pl_on_ground) {
      // grounded: follow the floor surface at the X midpoint (slopes).
      // Search the rows around the current feet row; snap feet to surface.
      unsigned int feet = pl_y + BOX_H + 1;
      unsigned char my0 = feet >> 8;
      unsigned int fy = NO_FLOOR, cand;
      // first surface WITHIN the +-64 snap window wins. The old cascade
      // committed to the topmost non-empty row, so a one-way top one
      // row above the feet (stacked shelf tiles, e.g. keen5 Security
      // Center's exit-door rows 11+12) shadowed the row the feet were
      // on: its surface is 256 units up, outside the snap, and the
      // bail-out dropped Keen every other tic — pl_on_ground flickered
      // 0/1 and door Up presses were randomly ignored. NO_FLOOR
      // (0xFFFF) self-rejects: 0xFFFF+64 wraps below any real feet.
      if (my0 > 0) {
        cand = floor_at(midx, my0 - 1); // ascended into row above
        if (cand + 64 >= feet && feet + 64 >= cand)
          fy = cand;
      }
      if (fy == NO_FLOOR) {
        cand = floor_at(midx, my0);
        if (cand + 64 >= feet && feet + 64 >= cand)
          fy = cand;
      }
      if (fy == NO_FLOOR) {
        cand = floor_at(midx, my0 + 1); // gentle descent
        if (cand + 64 >= feet && feet + 64 >= cand)
          fy = cand;
      }
      if (fy == NO_FLOOR) { // box edges on flat tops (ledge overhang)
        for (tx = x0; tx <= x1; ++tx)
          if (solid_top_at(tx, my0) == 1) {
            fy = (unsigned int)my0 << 8;
            break;
          }
      }
      if (fy != NO_FLOOR && fy + 64 >= feet && feet + 64 >= fy) {
        pl_y = fy - BOX_H - 1;
        pl_vy = 0;
      } else {
        pl_on_ground = 0; // walked off an edge
      }
    }
    if (!pl_on_ground && pl_vy >= 0) { // falling
      unsigned char feet_old = (pl_y + BOX_H) >> 8;
      unsigned char feet_new = (newy + BOX_H) >> 8;
      unsigned char landed = 0;
      unsigned char my;
#if EPISODE == 5
      unsigned char impact = (unsigned char)pl_vy; // vy before the snap
      unsigned char land_my = 0;
#endif
      for (my = feet_old; !landed && my <= feet_new; ++my) {
        unsigned int fy = floor_at(midx, my);
        if (fy == NO_FLOOR) { // flat-top check across the whole box width
          for (tx = x0; tx <= x1; ++tx)
            if (solid_top_at(tx, my) == 1) {
              fy = (unsigned int)my << 8;
              break;
            }
        }
        if (fy == NO_FLOOR)
          continue;
        // land if feet cross the surface this tic (one-way platforms)
        if (pl_y + BOX_H <= fy && newy + BOX_H >= fy) {
          pl_y = fy - BOX_H - 1;
          pl_vy = 0;
          landed = 1;
#if EPISODE == 5
          land_my = (unsigned char)my;
#endif
        }
      }
#if EPISODE == 4
      // land on a Bounder's top while falling (then ride it, above)
      if (!landed && pl_vy >= 0) {
        unsigned char bi;
        set_prg_bank(26, 0x80);   // bounder_under is in cold bank 26
        bi = bounder_under(pl_x, pl_x + BOX_W, newy + BOX_H, 80);
        set_prg_bank(lvl_bank[g_level], 0x80);
        if (bi) {
          pl_y = bounder_top(bi) - BOX_H - 1;
          pl_vy = 0;
          landed = 1;
        }
      }
#endif
      // platform catch while falling
      {
        unsigned char pi;
        set_prg_bank(26, 0x80);   // plat_under is in cold bank 26
        pi = plat_under(pl_x, pl_x + BOX_W, newy + BOX_H, 80);
        set_prg_bank(lvl_bank[g_level], 0x80);
        if (!landed && pi && pl_vy >= 0) {
          --pi;
          pl_y = pfy[pi] - BOX_H - 1;
          pl_vy = 0;
          landed = 1;
#if EPISODE == 5
          land_my = 0; // platform, never a fuse
#endif
        }
      }
      if (landed) {
        if (pl_pogo) { // pogo auto-bounce
#if EPISODE == 5
          // hard pogo slam (>= 0x30 units/tic): report the landed-on cell;
          // main.c breaks it if a fuse-top pseudo-item lives there
          if (impact >= 0x30 && land_my) {
            pl_fuse_x = (unsigned char)(midx >> 8);
            pl_fuse_y = land_my;
            pl_fuse_hit = 1;
          }
#endif
          pl_vy = POGO_VEL;
          jump_timer = jump_held ? POGO_TIME : POGO_TIME / 2;
          pl_on_ground = 0;
          ksfx_play(SFX_POGO);
        } else {
          if (!pl_on_ground)
            ksfx_play(SFX_LAND);
          pl_on_ground = 1;
        }
      } else {
        pl_y = newy;
        // falling out the bottom kills: clip box below the vertical scroll
        // bound (scroll max + screen = tile-to-unit(h-2)) — feet entering row
        // h-2 is death
        if ((unsigned char)((newy + BOX_H) >> 8) >= (unsigned char)(g_h - 2))
          pl_dead = 1;
      }
    } else if (!pl_on_ground) { // rising: ceiling check
      unsigned char head_new = newy >> 8;
      unsigned char bumped = 0;
      if ((int)newy < 256)
        bumped = 1;
      else
        for (tx = x0; tx <= x1; ++tx)
          if (flags_at(tx, head_new) & 2) { // bottom-solid
            bumped = 1;
            break;
          }
      if (bumped) {
        pl_vy = 0;
        jump_timer = 0;
        pl_y = ((unsigned int)(head_new + 1u) << 8);
      } else
        pl_y = newy;
    }
  y_done:;
  }

  // gem placing: standing on a gem-holder tile (MT_FLAG_GEMHOLD) while holding
  // the matching key gem places it — main.c consumes the gem, swaps the holder
  // art, and opens the door. No Up press needed (auto-places), so it runs
  // whenever Keen is grounded.
  if (pl_on_ground && !pl_gem_hit && gd_n) {
    unsigned char mx = (unsigned char)((pl_x + BOX_W / 2) >> 8);
    unsigned char my = (unsigned char)((pl_y + BOX_H) >> 8);
    unsigned char my0 = (unsigned char)(pl_y >> 8);
    unsigned char i;
    for (i = 0; i < gd_n; ++i) {          // gd_n <= 6: direct scan (the
      const unsigned char *r = GD_REC(i); // MT_FLAG_GEMHOLD flag gate costs
      if (r[0] == mx &&                   // more fixed code than it saves)
          r[1] >= my0 && r[1] <= my &&    // holders sit at body height
          (pl_keys & (unsigned char)(1u << r[2]))) {
        pl_gem_hit = (unsigned char)(i + 1);
        break;
      }
    }
  }

  // grab a pole if Up/Down is held near one (works grounded or mid-air)
  if ((pad & (PAD_UP | PAD_DOWN)))
    try_grab_pole(pad);
}

static void shots_tic(void) {
  unsigned char s;
  for (s = 0; s < MAX_SHOTS; ++s) {
    unsigned int mx, my;
    if (!shot_state[s])
      continue;
    if (shot_state[s] >= 2) { // hit flash counts down then despawns
      if (++shot_tic[s] >= 10) { // 12-tic flash @70Hz -> 10 @60Hz
        shot_tic[s] = 0;
        if (++shot_state[s] > 3)
          shot_state[s] = 0;
      }
      continue;
    }
    if (shot_dir[s] == 0)
      shot_x[s] += SHOT_SPEED;
    else if (shot_dir[s] == 1)
      shot_x[s] -= SHOT_SPEED;
    else if (shot_dir[s] == 2)
      shot_y[s] -= SHOT_SPEED;
    else
      shot_y[s] += SHOT_SPEED;
    if (++shot_tic[s] >= 5) // 6-tic frames @70Hz -> 5 @60Hz
      shot_tic[s] = 0;
    mx = (shot_x[s] + 64) >> 8;
    my = (shot_y[s] + 64) >> 8;
    {
      // wall/tile hit is a level-blob read (level bank); actors_shot_hit is in
      // the cold draw bank (26, map-free) — map it around only that call.
      unsigned char hit = mx >= (unsigned int)g_w || my >= (unsigned int)g_h ||
                          (flags_at(mx, my) & 5) || solid_top_at(mx, my) == 1;
      if (!hit) {
        set_prg_bank(26, 0x80);
        hit = actors_shot_hit(shot_x[s], shot_y[s]);
        set_prg_bank(lvl_bank[g_level], 0x80);
      }
      if (hit) {
        shot_state[s] = 2; // hit flash
        shot_tic[s] = 0;
        ksfx_play(SFX_SHOTHIT);
      }
    }
  }
}

extern unsigned char g_loops;
extern unsigned char g_oam_used;

void player_update(unsigned char pad) {
  ++g_loops; // FPS instrumentation (one increment per game-loop iteration)
  if ((g_loops & 15) == 0) { // sample OAM occupancy from last frame's
    unsigned char i, n = 0;  // shadow (we run before this frame's clear)
    const unsigned char *oam = (const unsigned char *)0x0200;
    for (i = 0; i < 64; ++i) {
      if (oam[(unsigned char)(i * 4)] >= 0xEF)
        break; // OAM fills front-to-back; first hidden slot = count
      ++n;
    }
    g_oam_used = n;
  }
  if (touches_deadly())
    pl_dead = 1;
  // ONE physics tic per 60Hz frame (was a 70/60 accumulator that ran two 70Hz
  // tics on every 6th frame — the periodic scroll hitch). Constants are pre-
  // scaled x7/6 so real-world motion is unchanged; uniform frames let the seam
  // catch up every frame (main.c), which itself smooths scrolling.
  actors_tic(); // platforms/enemies move first so riders track exactly
  tic(pad);
  shots_tic();

  // animation state
  if (pl_ledge) {
    ; // ledge_tic_b / grab detection selected HANG or PULL1..4
  } else if (pl_pole) {
    if (pad & PAD_DOWN) {
      // slide down: DOS facing-the-screen slide pose (one frame; the 1KB
      // pole overlay page can't hold the full 3-frame slide cycle)
      anim_frame = FRAME_POLED1;
      anim_timer = 0;
    } else if (pad & PAD_UP) {
      if (anim_frame < FRAME_POLE1 || anim_frame > FRAME_POLE3) {
        anim_frame = FRAME_POLE1; // (re)enter the climb cycle (e.g. off POLED)
        anim_timer = 0;
      }
      ++anim_timer;
      if (anim_timer < 5u)
        return;
      anim_timer = 0;
      if (++anim_frame > FRAME_POLE3)
        anim_frame = FRAME_POLE1;
    } else {
      anim_frame = FRAME_POLE1; // DOS pole-sit pose
      anim_timer = 0;
    }
  }
  else if (pl_pogo && !pl_on_ground)
    anim_frame = (pl_vy < 0) ? FRAME_POGO1 : FRAME_POGO2;
  else if (!pl_on_ground)
    anim_frame = (pl_vy < -8) ? FRAME_JUMP1 : (pl_vy < 8 ? FRAME_JUMP2 : FRAME_JUMP3);
  else if (look_dir == 1)
    anim_frame = FRAME_LOOKU;
  else if (look_dir == 2) // 6-tic lookDown1 lead-in (x6/7 -> 5), then lookDown2
    anim_frame = (look_timer <= 5) ? FRAME_LOOKD1 : FRAME_LOOKD2;
  else if (pl_vx) {
    if (++anim_timer >= 4) { // run-cycle step: 5-tic @70Hz -> 4 @60Hz
      anim_timer = 0;
      ++anim_frame;
    }
    if (anim_frame < FRAME_RUN1 || anim_frame > FRAME_RUN4)
      anim_frame = FRAME_RUN1;
  } else
    anim_frame = FRAME_STAND;
}

// player_draw + shots_draw are pure WRAM/OAM (no level-blob read), so they bank
// to the cold draw bank (26) with actors_draw to relieve the full fixed region;
// main.c maps bank 26 around all three and restores the level bank.
#define DRAW_BANK __attribute__((noinline, section(".prg_rom_26.text")))

// draw player metasprite relative to camera (pixels)
DRAW_BANK void player_draw(unsigned int cam_px, unsigned int cam_py) {
  unsigned int img_x = (pl_x - KEEN_CLIP_XL) >> 4;
  unsigned int img_y = (pl_y - KEEN_CLIP_YL) >> 4;
  int sx = (int)(img_x - cam_px);
  int sy = (int)(img_y - cam_py);
  if (pl_cov_hide)
    return; // fully behind covering terrain (hole / secret area): DOS-hidden
  if (sx < -24 || sx > 256 || sy < -32 || sy > 240)
    return;
  oam_meta_spr((unsigned char)sx, (unsigned char)sy,
               pl_ledge ? ms_ledge[anim_frame - FRAME_HANG][pl_face]
                        : ms_frames[anim_frame][pl_face]);
}

DRAW_BANK void shots_draw(unsigned int cam_px, unsigned int cam_py) {
  unsigned char s;
  for (s = 0; s < MAX_SHOTS; ++s) {
    int sx, sy;
    unsigned char frame;
    if (!shot_state[s])
      continue;
    sx = (int)((shot_x[s] >> 4) - cam_px);
    sy = (int)((shot_y[s] >> 4) - cam_py);
    if (sx < -16 || sx > 256 || sy < -16 || sy > 240)
      continue;
    if (shot_state[s] == 2) frame = 2;
    else if (shot_state[s] == 3) frame = 3;
    else frame = (shot_tic[s] >= 3) ? 1 : 0;
    oam_meta_spr((unsigned char)sx, (unsigned char)sy, ms_shot[frame]);
  }
}
