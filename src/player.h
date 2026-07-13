#ifndef PLAYER_MOD_H
#define PLAYER_MOD_H
extern volatile unsigned int pl_x, pl_y;
extern volatile int pl_vx, pl_vy;
extern volatile unsigned char pl_on_ground, pl_pogo, pl_face;
extern unsigned char pl_pole; // clinging to a climbable pole
extern unsigned char pl_ledge; // 1 hanging, 2 pulling onto a ledge
void player_init(void);
void player_update(unsigned char pad);
void player_draw(unsigned int cam_px, unsigned int cam_py);
void shots_draw(unsigned int cam_px, unsigned int cam_py);
extern unsigned char pl_ammo, pl_lives;
extern unsigned char pl_dead, pl_level_done;
extern unsigned char pl_keycard; // Keen 5 security keycard held
// Keen 5 fuse hook: player.c reports a hard pogo slam on cell
// (pl_fuse_x, pl_fuse_y); main.c breaks the fuse pseudo-item there
extern unsigned char pl_fuse_x, pl_fuse_y, pl_fuse_hit;
// camera peek offset in pixels (look up/down): -27 (up) .. +107 (down),
// ramped 1px/tic while looking, 3px/tic back
extern volatile signed char pl_look_off;
// score as 8 unpacked BCD digits, most significant first (2A03 disables decimal
// mode, so carries are propagated manually; digits double as the display form)
extern unsigned char pl_score_d[8];
extern unsigned char pl_nextlife_d[8]; // extra-Keen-at threshold (BCD)
extern unsigned char pl_score_gen;     // bumped on change (HUD cache key)
extern unsigned char pl_keys;          // gem bitmask (bit = item type 0..3)
extern unsigned char pl_quest;         // quest items grabbed (K4 members / K6 items)
extern unsigned char pl_lifewater;     // K4 raindrops held (0..99; 100 = 1UP)
// door/gem/switch hooks (main.c performs the VRAM + state changes; player.c
// only DETECTS on its collision cells so the cold action code stays banked).
// pl_gem_hit / pl_switch_hit = holder/switch index + 1 (0 = none this frame).
// pl_door + pl_door_x/y = a door teleport to run as a fade+cut transition.
extern unsigned char pl_gem_hit, pl_switch_hit, pl_door;
extern unsigned int pl_door_x, pl_door_y;
// award (hi nibble)*10^(lo nibble) points: all awards are one digit times a
// power of ten (item point values 100..5000)
void score_add(unsigned char code);
#endif
