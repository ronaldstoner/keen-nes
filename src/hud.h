// Keen 4/5/6 NES port — persistent in-game HUD (score / ammo / lives).
// MIT License; see LICENSE.
#ifndef HUD_MOD_H
#define HUD_MOD_H

// Draw the HUD with sprites at the top-left of the screen.
// Call once per frame AFTER oam_clear() and BEFORE player/actor drawing so
// the HUD's OAM entries come first and win sprite priority (stay on top).
void hud_draw(void);

// Enable/disable the HUD (nonzero = on). When off, hud_draw() emits nothing.
void hud_set(unsigned char on);

// Difficulty selector (EASY/NORMAL/HARD -> g_difficulty), the port's
// only menu. Call once between the title screen and level_load(0);
// blocks until the player confirms and returns with rendering off.
void difficulty_select(void);

// Game-flow bookend screens (banked; static text via the selector's font).
// Each blocks until a fresh A/Start press, then returns with rendering off
// so the caller can restart the title flow. Stop the music before calling
// (they run from bank 6 and must not touch the audio drivers).
void gameover_show(void); // "GAME OVER" (lives exhausted)
void ending_show(void);   // "CONGRATULATIONS" (last demo level finished)

// Reset persistent player stats (lives/ammo/score/quest/keys/extra-life
// threshold) for a fresh game. Call between the selector and level_load(0).
void newgame_reset(void);

#endif
