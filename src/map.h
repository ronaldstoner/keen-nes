#ifndef KEEN_MAP_H
#define KEEN_MAP_H
// World map (GAMEMAPS level 0). Separate mode from combat: main.c runs a lean
// frame (no actors/items/HUD/combat physics) while g_on_map. Tables from
// gen_mmc5_rom (map_nodes.json).

#include "gen/levels.h"

// 1 while the overworld is loaded / running.
extern unsigned char g_on_map;

// levels_done[game_level]: 1 when that stage is finished. Index 0 unused.
// Ship (MAP_SHIP_LV) may be marked but stays re-enterable.
extern unsigned char levels_done[MAP_MAX_GAME_LV + 1];

// Saved overworld position (restored on return from a stage).
extern unsigned int map_pos_x, map_pos_y;
extern unsigned char map_have_pos;

// map_player_update sets this when A/B requests enter: 0 = none, else ROM slot.
extern unsigned char pl_map_enter;

void map_newgame(void);           // clear done flags + saved position
void map_apply_done(void);        // open fences for completed levels (after map load)
void map_mark_done(unsigned char glv);
unsigned char map_all_playable_done(void); // 1 if every non-map, non-ship ROM stage is done

void map_player_update(unsigned char pad); // 8-dir walk, FG solid, A/B enter
void map_player_place(void);               // spawn or restore map_pos_*
void map_save_pos(void);

// Bank 6 mapped: planted flags for completed levels (draw before map Keen).
void map_flags_draw(unsigned int cam_px, unsigned int cam_py);
// Bank 26 mapped: map Keen metasprite.
void map_player_draw(unsigned int cam_px, unsigned int cam_py);

#endif
