// World map (GAMEMAPS level 0): walk, enter stages, fence open, planted flags.
//
// BANKING: walk body is bank 6. MT_FLAGS/map_cell remaps $8000, so solid probes
// go through FIXED map_blocked_fixed (restores bank 6 before rts). Enter/fence
// tables live in bank 26; flag art + map_flags_draw in bank 6.
#include <mapper.h>
#include <neslib.h>
#include "level_fmt.h"
#include "gen/levels.h"
#include "gen/player.h"
#include "player.h"
#include "map.h"

#define MAP_TAB __attribute__((noinline, section(".prg_rom_26.text")))
#define MAP_COLD __attribute__((noinline, section(".prg_rom_6.text")))
#define MAP_DRAW __attribute__((noinline, section(".prg_rom_26.text")))

unsigned char g_on_map;
unsigned char levels_done[MAP_MAX_GAME_LV + 1];
unsigned int map_pos_x, map_pos_y;
unsigned char map_have_pos;
unsigned char pl_map_enter; // 0 none; else ROM slot

// Compass: 0N 1NE 2E 3SE 4S 5SW 6W 7NW
static unsigned char map_dir;
static unsigned char map_frame; // 0 walk1, 1 walk2, 2 stand
static unsigned char map_anim;

// ~18 units/tic @60Hz (Galaxy map walk is ~24 @70Hz).
#define MAP_SPEED 18

extern const unsigned char lvl_bank[];
extern const unsigned char lvl_game_no[];
extern const unsigned char *const ms_mapkeen[8][3];
extern const unsigned char *const ms_mapflag;
extern const unsigned char map_flag_x[], map_flag_y[], map_flag_lv[];

MAP_COLD static void map_newgame_b(void) {
  unsigned char i;
  for (i = 0; i <= MAP_MAX_GAME_LV; ++i)
    levels_done[i] = 0;
  map_have_pos = 0;
  map_pos_x = map_pos_y = 0;
  pl_map_enter = g_on_map = 0;
  map_dir = 2;
  map_frame = 2;
  map_anim = 0;
}

// Map Keen: position is tile << 8 (no combat clip inset).
MAP_COLD static void map_player_place_b(void) {
  if (map_have_pos) {
    pl_x = map_pos_x;
    pl_y = map_pos_y;
  } else {
    pl_x = (unsigned int)g_spawn_x << 8;
    pl_y = (unsigned int)g_spawn_y << 8;
  }
  pl_vx = pl_vy = 0;
  pl_on_ground = 1;
  pl_pogo = pl_pole = pl_ledge = 0;
  pl_look_off = pl_map_enter = 0;
  map_frame = 2;
  map_anim = 0;
}

// Replace fence cells with baked empty metatiles for completed levels.
MAP_TAB static void map_apply_done_b(void) {
  unsigned char i;
  ov_reset();
  for (i = 0; i < MAP_N_FENCE; ++i) {
    unsigned char lv = map_fence_lv[i];
    if (!levels_done[lv])
      continue;
    ov_add(map_fence_x[i], map_fence_y[i],
           (unsigned int)map_fence_mt_lo[i] |
               ((unsigned int)map_fence_mt_hi[i] << 8));
  }
}

// Enter tile at (mx,my) -> ROM slot, or 0. Ship always enterable; other
// finished stages refuse re-entry.
MAP_TAB static unsigned char map_scan_enter_b(unsigned char mx,
                                             unsigned char my) {
  unsigned char i;
  for (i = 0; i < MAP_N_ENTER; ++i) {
    if (map_enter_x[i] == mx && map_enter_y[i] == my) {
      unsigned char glv = map_enter_lv[i];
      unsigned char slot;
      if (glv > MAP_MAX_GAME_LV)
        return 0;
      slot = map_rom_of_lv[glv];
      if (slot == 0xFF || slot == (unsigned char)MAP_ROM_SLOT)
        return 0;
      if (glv != (unsigned char)MAP_SHIP_LV && levels_done[glv])
        return 0;
      return slot;
    }
  }
  return 0;
}

// FIXED: MT_FLAGS remaps $8000; restore bank 6 before return.
__attribute__((noinline)) static unsigned char
map_blocked_fixed(unsigned int x, unsigned int y) {
  unsigned char mx = (unsigned char)((x + (MAPKEEN_W_U >> 1)) >> 8);
  unsigned char my = (unsigned char)((y + (MAPKEEN_H_U >> 1)) >> 8);
  unsigned char s =
      (mx >= g_w || my >= g_h) ? 1u : (unsigned char)(MT_FLAGS(mx, my) & 7u);
  *(volatile unsigned char *)0x5114 = 0x86;
  return s;
}

// D-pad -> compass 0..7, or 0xFF if none.
MAP_COLD static unsigned char map_pad_dir(unsigned char pad) {
  unsigned char u = pad & PAD_UP, d = pad & PAD_DOWN;
  unsigned char l = pad & PAD_LEFT, r = pad & PAD_RIGHT;
  if (u && r)
    return 1;
  if (d && r)
    return 3;
  if (d && l)
    return 5;
  if (u && l)
    return 7;
  if (u)
    return 0;
  if (r)
    return 2;
  if (d)
    return 4;
  if (l)
    return 6;
  return 0xFF;
}

MAP_COLD static void map_player_update_b(unsigned char pad) {
  static unsigned char act_prev;
  unsigned char act_now, dir;
  unsigned int n;
  signed char dx = 0, dy = 0;

  pl_map_enter = 0;
  dir = map_pad_dir(pad);

  if (dir != 0xFF) {
    map_dir = dir;
    // Diagonals move on both axes.
    switch (dir) {
    case 0: dy = -1; break;
    case 1: dx = 1; dy = -1; break;
    case 2: dx = 1; break;
    case 3: dx = 1; dy = 1; break;
    case 4: dy = 1; break;
    case 5: dx = -1; dy = 1; break;
    case 6: dx = -1; break;
    default: dx = -1; dy = -1; break;
    }
    pl_face = (dx < 0) ? 1 : 0;

    // Axis-separated; solid probe is FIXED (restores bank 6).
    if (dx) {
      if (dx < 0) {
        if (pl_x >= MAP_SPEED) {
          n = pl_x - MAP_SPEED;
          if (!map_blocked_fixed(n, pl_y))
            pl_x = n;
        }
      } else {
        n = pl_x + MAP_SPEED;
        if (!map_blocked_fixed(n, pl_y))
          pl_x = n;
      }
    }
    if (dy) {
      if (dy < 0) {
        if (pl_y >= MAP_SPEED) {
          n = pl_y - MAP_SPEED;
          if (!map_blocked_fixed(pl_x, n))
            pl_y = n;
        }
      } else {
        n = pl_y + MAP_SPEED;
        if (!map_blocked_fixed(pl_x, n))
          pl_y = n;
      }
    }

    if (++map_anim >= 4) {
      map_anim = 0;
      map_frame = (map_frame == 0) ? 1 : 0;
    }
  } else {
    map_frame = 2;
    map_anim = 0;
  }

  act_now = pad & (PAD_A | PAD_B);
  pl_map_enter = (act_now && !act_prev) ? 0xFF : 0;
  act_prev = act_now;
}

// Planted flag for each completed stage (static pose). Call with bank 6.
// Placement: +0x60 x, -0x1E0 y in Galaxy map units relative to holder tile.
MAP_COLD void map_flags_draw(unsigned int cam_px, unsigned int cam_py) {
  unsigned char i;
  for (i = 0; i < MAP_N_FLAG; ++i) {
    unsigned char lv = map_flag_lv[i];
    int sx, sy;
    if (lv > MAP_MAX_GAME_LV || !levels_done[lv])
      continue;
    sx = (int)((((unsigned int)map_flag_x[i] << 8) + 0x60u) >> 4) -
         (int)cam_px;
    sy = (int)((((int)map_flag_y[i] << 8) - 0x1E0) >> 4) - (int)cam_py;
    if (sx < -32 || sx > 256 || sy < -40 || sy > 240)
      continue;
    oam_meta_spr((unsigned char)sx, (unsigned char)sy, ms_mapflag);
  }
}

MAP_DRAW void map_player_draw(unsigned int cam_px, unsigned int cam_py) {
  int sx = (int)((pl_x >> 4) - cam_px);
  int sy = (int)((pl_y >> 4) - cam_py);
  if (sx < -16 || sx > 256 || sy < -16 || sy > 240)
    return;
  oam_meta_spr((unsigned char)sx, (unsigned char)sy,
               ms_mapkeen[map_dir][map_frame]);
}

void map_newgame(void) {
  set_prg_bank(6, 0x80);
  map_newgame_b();
}

void map_mark_done(unsigned char glv) {
  if (glv && glv <= MAP_MAX_GAME_LV)
    levels_done[glv] = 1;
}

// 1 if every non-map, non-ship stage in this ROM is complete.
unsigned char map_all_playable_done(void) {
  unsigned char s;
  for (s = 0; s < NUM_LEVELS; ++s) {
    unsigned char gn = lvl_game_no[s];
    if (!gn || gn == (unsigned char)MAP_SHIP_LV)
      continue;
    if (!levels_done[gn])
      return 0;
  }
  return 1;
}

void map_save_pos(void) {
  map_pos_x = pl_x;
  map_pos_y = pl_y;
  map_have_pos = 1;
}

void map_player_place(void) {
  set_prg_bank(6, 0x80);
  map_player_place_b();
  set_prg_bank(lvl_bank[g_level], 0x80);
}

void map_apply_done(void) {
  set_prg_bank(26, 0x80);
  map_apply_done_b();
  set_prg_bank(lvl_bank[g_level], 0x80);
}

void map_player_update(unsigned char pad) {
  set_prg_bank(6, 0x80);
  map_player_update_b(pad);
  if (pl_map_enter == 0xFF) {
    // Sprite center tile.
    unsigned char mx =
        (unsigned char)((pl_x + (MAPKEEN_W_U >> 1)) >> 8);
    unsigned char my =
        (unsigned char)((pl_y + (MAPKEEN_H_U >> 1)) >> 8);
    set_prg_bank(26, 0x80);
    pl_map_enter = map_scan_enter_b(mx, my);
  }
  set_prg_bank(lvl_bank[g_level], 0x80);
}
