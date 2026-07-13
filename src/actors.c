// Enemy actors + per-episode bestiary (#if EPISODE, from gen/levels.h) in the
// level blob's three enemy slots:
//   EP6: Bloog (walker) / Blooglet (walker) / Babobba (hopper)
//   EP4: Poison Slug (walker) / Mad Mushroom (bouncer) / Lick (hopper)
//   EP5: Sparky (walker+charge) / Ampton (walker) / Slicestar (v-bouncer)
// Walkers move N units/tic, kill on contact, and turn at walls and ledges.
#include <mapper.h>
#include <neslib.h>
#include <nesdoug.h>
#include "level_fmt.h"
#include "gen/levels.h"
#include "gen/player.h"
#include "player.h"
#include "actors.h"

// In the MMC5 ExRAM build the near-full fixed region needs the space; draw_actor
// + actors_draw are pure WRAM/OAM (no level-blob access), so they bank to a
// dedicated cold code bank (26 — free below the fixed region; the HUD bank 6 is
// itself full). main.c maps ACT_DRAW_BANK around the single actors_draw call and
// restores the level bank.
#define ACT_DRAW_BANK 26
#define ACT_BANK6 __attribute__((noinline, section(".prg_rom_26.text")))

// Shared fall physics, rescaled for the 60Hz tic to match the player's arc
// (odd-tic gravity 4->5, terminal 70->82; see src/player.c GRAVITY_HIGH).
#define ACT_GRAV 5
#define ACT_TERM 82

// --- slot 1: generic walker (EP6 Bloog / EP4 Poison Slug) ---
#if EPISODE == 4
#define MAX_BLOOGS 8        // slugs (Slug Village places 10; extras dropped)
#define BLOOG_SPEED 9       // 8 u/tic @70Hz, x7/6 for 60Hz
#define BLOOG_TURN_CHANCE 0x10 // turn-toward-Keen probability (not scaled)
#define ACTION_TICS 7       // 8-tic action @70Hz, x6/7 for 60Hz
#define WLK_MS ms_slug
#define WLK_MS_STUN ms_slugstun
#define BLOOG_BW (SLUG_W * 16)
#define BLOOG_BH (SLUG_H * 16)
#elif EPISODE == 5
// Sparky: patrols, and after a "search" pause charges at Keen's level.
// Walk 0x80 units per 8-tic action = 16 u/tic; charge 0x80 per 4 tics =
// 32 u/tic.
#define MAX_BLOOGS 6        // Ion Vent places 7 across all difficulties
#define BLOOG_SPEED 19         // 16 u/tic @70Hz, x7/6
#define SPARKY_CHARGE_SPEED 37 // 32 u/tic @70Hz, x7/6
#define ACTION_TICS 7          // 8-tic action @70Hz, x6/7
#define SPARKY_CHARGE_ANIM 3   // charge action frames 4-tic @70Hz, x6/7
#define SPARKY_SEARCH_CHANCE 0x40 // search probability
#define SPARKY_SEARCH_TICS 41  // 8 search actions x 6 tics @70Hz, x6/7
#define SPARKY_PREP_TICS 41    // 3 cycles x 4 actions x 4 tics @70Hz, x6/7
#define SPARKY_SEARCH_Y 0x100  // search Y radius: feet within +-0x100 units
#define WLK_MS ms_sparky
#define WLK_MS_STUN ms_sparkystun
#define BLOOG_BW (SPARKY_W * 16)
#define BLOOG_BH (SPARKY_H * 16)
#else
#define MAX_BLOOGS 4
#define BLOOG_SPEED 15      // 13 u/tic @70Hz, x7/6
#define BLOOG_TURN_CHANCE 0x20 // probability, not scaled
#define ACTION_TICS 9       // 10-tic action @70Hz, x6/7
#define WLK_MS ms_bloog
#define WLK_MS_STUN ms_bloogstun
#define BLOOG_BW (BLOOG_W * 16)
#define BLOOG_BH (BLOOG_H * 16)
#endif

static unsigned int bx[MAX_BLOOGS], by[MAX_BLOOGS]; // top-left, units
static unsigned char bdir[MAX_BLOOGS];              // 0=R 1=L
static unsigned char banim[MAX_BLOOGS], btic[MAX_BLOOGS];
static unsigned char bstun[MAX_BLOOGS];
static unsigned char nbloogs;
#if EPISODE == 5
// Sparky state machine: 0 walk, 1 search (paused, scans for Keen),
// 2 prep-charge windup, 3 charge; bst_tic counts the state down
static unsigned char bst[MAX_BLOOGS], bst_tic[MAX_BLOOGS];
#endif

// --- axis platforms (12 units/tic, reverse on info-plane block markers;
// dir 0=up 1=right 2=down 3=left) ---
#define MAX_PLATS 6
// --- slot 2: EP6 Blooglet walker / EP4 Mad Mushroom bouncer ---
#if EPISODE == 4
// slot 2 is a GENERIC "flyer/bouncer" slot: the level blob's blets record
// (x,y,color,mindiff) reuses the color byte as a TYPE discriminator so one
// table hosts several critters in the slot. Types:
//   0 Mad Mushroom  1 Skypest  2 Bounder.
// gen_mmc5_level.py routes info 21 -> 0, 8/45/46 -> 1,
// 12 -> 2. Bounding box + sprite table are selected per-actor by type.
#define BL_MUSH 0
#define BL_SKY  1
#define BL_BND  2
#define MAX_BLETS 16       // mushroom+skypest+bounder mix (Cave hard = 15)
#define BLET_BW (MUSH_W * 16)
#define BLET_BH (MUSH_H * 16)
#define MUSH_HOP_VY (-47)  // hop velY (-40 x7/6)
#define MUSH_JUMP_VY (-79) // big-jump velY, every 3rd bounce (-68 x7/6)
#define MUSH_NUM_JUMPS 3   // bounces per big jump
// Skypest: gentle 2D drifter, accel 1/odd-tic toward its x/y direction capped
// at 20 units/tic; random direction flips; bounces off walls/ceiling/floor.
// Lethal on contact, NOT killed by shots (knockback only), squished by a pogo
// stomp.
#define SKY_VMAX 23          // accel X/Y vel limit 20, x7/6
#define SKY_TAKEOFF_VY (-19) // takeoff velY (-16 x7/6)
#define SKY_CEIL_VY 9        // hit-ceiling velY (8 x7/6)
// Bounder: a heavy bouncing ball; gravity, bounce -50 on
// landing, every 3rd bounce randomly picks a horizontal direction at 24
// units/tic. Lethal on contact, stunnable (death-hop -32).
#define BND_BOUNCE_VY (-58)  // bounce velY (-50 x7/6)
#define BND_XVEL 28          // bounce/ride velX (24 x7/6)
#define BND_CYCLE 3          // bounces per direction pick
#define BND_STUN_VY (-37)    // death-hop velY (-32 x7/6)
// slot-2 types 3/4: Wormmouth + Mimrock (ground critters lethal ONLY during
// their attack, harmless otherwise).
#define BL_WORM 3
#define BL_MIM  4
// Wormmouth: ground worm, walks 16 u/tic. Bites (lethal) when Keen is roughly
// level and ahead in a window; only the bite kills.
#define WORM_SPEED 19            // move velX 16, x7/6
#define WORM_BITE_TICS 41        // 5 bite actions, x6/7 (48 @70Hz)
#define WORM_BITE_YR 0x100       // bite Y radius
#define WORM_BITE_MIN 0x80       // bite min X
#define WORM_BITE_MAX 0x180      // bite max X
#define WORM_PEEP_XR 0x300       // peep X radius (face Keen when far)
// Mimrock: inert rock -> sneaks toward Keen -> bonk-jumps at him; lethal ONLY
// airborne. Walk 0x40/6tic ~= 11 u/tic.
#define MIM_WALK_SPEED 13        // ~11 u/tic @70Hz, x7/6
#define MIM_JUMP_VX 23           // jump velX (20 x7/6)
#define MIM_JUMP_VY (-47)        // jump velY (-40 x7/6)
#define MIM_WAKE_XR 0x300        // wait X radius
#define MIM_JUMP_XR 0x400        // jump X radius
#define MIM_YR 0x500             // wait/jump Y radius
#define MIM_STUN_VY (-19)        // death-hop velY (-16 x7/6)
#elif EPISODE == 5
#define MAX_BLETS 6        // Amptons (Def Tunnel Vlook places 6 on hard)
#define BLET_SPEED 19      // walk 16 u/tic @70Hz, x7/6
#define BLET_TICS 7        // 8-tic action @70Hz, x6/7
#define BLET_BW (AMPTON_W * 16)
#define BLET_BH (AMPTON_H * 16)
#else
#define MAX_BLETS 4
#define BLET_SPEED 30      // 26 u/tic @70Hz, x7/6
#define BLET_TICS 4        // 5-tic action @70Hz, x6/7
#define BLET_BW (BLET_W * 16)
#define BLET_BH (BLET_H * 16)
#endif
#define PLAT_SPEED 14      // 12 u/tic @70Hz, x7/6
#define PLAT_BW (PLAT_W * 16)
#define PLAT_BH (PLAT_H * 16)
unsigned int pfx[MAX_PLATS], pfy[MAX_PLATS];
signed char pf_dx[MAX_PLATS], pf_dy[MAX_PLATS]; // this tic's delta
static unsigned char pfdir[MAX_PLATS];
static unsigned char pfkind[MAX_PLATS];  // 0=axis, 1=fall platform
static unsigned int pforig[MAX_PLATS];   // fall plat origin y
static int pfvel[MAX_PLATS];             // fall plat y velocity
static unsigned char nplats_;
unsigned char plat_ridden;               // set by player each tic

static unsigned int blx[MAX_BLETS], bly[MAX_BLETS];
static unsigned char bldir[MAX_BLETS], blanim[MAX_BLETS], bltic[MAX_BLETS];
static unsigned char blstun[MAX_BLETS];
static unsigned char nblets_;
#if EPISODE == 4
static int blvy[MAX_BLETS];             // mushroom/skypest/bounder y velocity
static int blvx[MAX_BLETS];             // skypest/bounder x velocity
static unsigned char bljumps[MAX_BLETS]; // bounce counter (mush/bnd); skypest
                                         // reuses it as y-direction (0 dn,1 up)
static unsigned char bltype[MAX_BLETS];  // BL_MUSH / BL_SKY / BL_BND
// Bounder this-tic movement delta, so Keen can RIDE one like a moving platform
// (a harmless bouncing ball Keen stands on and bounces off, NOT a contact-kill
// — it uses the platform-ride path). player.c reads these for the ridden
// Bounder.
signed char bl_bdx[MAX_BLETS], bl_bdy[MAX_BLETS];
// per-type collision/draw box (units); indexed by bltype
// (0 Mush, 1 Skypest, 2 Bounder, 3 Wormmouth, 4 Mimrock)
static const unsigned int bl_bw[5] = {MUSH_W * 16, SKYPEST_W * 16,
                                       BOUNDER_W * 16, WORM_W * 16, MIM_W * 16};
static const unsigned int bl_bh[5] = {MUSH_H * 16, SKYPEST_H * 16,
                                       BOUNDER_H * 16, WORM_H * 16, MIM_H * 16};
#elif EPISODE == 6
static unsigned char blcolor[MAX_BLETS];
#endif

// --- slot 3: EP6 Babobba hopper / EP4 Lick hopper ---
#if EPISODE == 4
#define MAX_BABS 5         // licks
#define BAB_BW (LICK_W * 16)
#define BAB_BH (LICK_H * 16)
// Lick constants
#define LICK_LONG_RADIUS 0x300 // radius, not scaled
#define LICK_LONG_VX 37     // 32 x7/6
#define LICK_LONG_VY (-37)  // -32 x7/6
#define LICK_SHORT_VX 19    // 16 x7/6
#define LICK_SHORT_VY (-19) // -16 x7/6
#define LICK_FLAME_Y 0x100  // flame if Keen within this vertical range (radius)
#define LICK_FLAME_AHEAD 0x180 // ...and ahead window (radius, not scaled)
#define LICK_FLAME_TICS 27  // 8 flame actions x 4 tics @70Hz, x6/7
#define LICK_SIT_TICS 17    // land + hop windup 20 @70Hz, x6/7
#elif EPISODE == 5
// Slicestar (north): uses the axis-platform logic — a vertical 12 units/tic
// bouncer between block markers / solid tiles. Invincible (shots splash),
// kills on touch.
#define MAX_BABS 4
#define BAB_BW (SLICE_W * 16)
#define BAB_BH (SLICE_H * 16)
#else
#define MAX_BABS 3
#define BAB_BW (BAB_W * 16)
#define BAB_BH (BAB_H * 16)
#endif
static unsigned int babx[MAX_BABS], baby[MAX_BABS];
static int babvy[MAX_BABS];
static unsigned char babdir[MAX_BABS], babstate[MAX_BABS]; // 0 sit 1 air
static unsigned char babtic[MAX_BABS], babstun[MAX_BABS], nbabs_;
#if EPISODE == 4
static int babvx[MAX_BABS];             // lick hop x velocity
#endif

// --- active window: only objects within the camera + ~4 tiles run. main.c
// refreshes it once per frame; inactive actors get no tic, no draw and no
// probes, which keeps the per-frame map_cell (banked) probe count bounded by
// what's near the screen. Window kept in metatile coords so the per-actor test
// is four u8 compares on the position high bytes (the u16 box test cost ~120
// cycles per call, ~2.4k/frame with a dozen actors)
static unsigned char awx0, awx1, awy0, awy1;
void actors_set_window(unsigned int cam_px, unsigned int cam_py) {
  unsigned char cx = (unsigned char)(cam_px >> 4);
  unsigned char cy = (unsigned char)(cam_py >> 4);
  awx0 = cx > 8 ? cx - 8 : 0; // 4-tile window + widest actor slack
  awx1 = cx + 16 + 4;
  awy0 = cy > 8 ? cy - 8 : 0;
  awy1 = cy + 15 + 4;
}
static unsigned char awake(unsigned int x, unsigned int y) {
  unsigned char tx = (unsigned char)(x >> 8), ty = (unsigned char)(y >> 8);
  return tx >= awx0 && tx <= awx1 && ty >= awy0 && ty <= awy1;
}

// walker ledge/wall probe cache: ground walkers re-probe the map only
// when the leading-edge tile column, feet row or direction changed
// (2 banked probes per tile crossed instead of 2 per 70Hz tic)
static unsigned char bpr_mx[MAX_BLOOGS], bpr_key[MAX_BLOOGS];
static unsigned char bpr_blk[MAX_BLOOGS];
// blocked-turn cooldown: a walker boxed in on both sides would otherwise
// flip direction every tic, thrashing the (direction-keyed) probe cache
static unsigned char bcool[MAX_BLOOGS];
#if EPISODE != 4
static unsigned char blcool[MAX_BLETS];
#endif
// awake verdicts cached by the tic loops (they run every frame), so the
// draw pass culls with a byte test instead of recomputing the window
static unsigned char bawk[MAX_BLOOGS], blawk[MAX_BLETS];
static unsigned char babawk[MAX_BABS], pfawk[MAX_PLATS];
#if EPISODE != 4
static unsigned char blpr_mx[MAX_BLETS], blpr_key[MAX_BLETS];
static unsigned char blpr_blk[MAX_BLETS];
#endif

static unsigned char is_block(unsigned char mx, unsigned char my) {
  const unsigned char *p = g_blocks;
  unsigned char i;
  for (i = 0; i < g_nblocks; ++i, p += 2)
    if (p[0] == mx && p[1] == my)
      return 1;
  return 0;
}

static unsigned char rnd_state = 0xA5;
static unsigned char rnd(void) { // 8-bit LFSR (x^8+x^6+x^5+x^4+1)
  unsigned char b = ((rnd_state >> 0) ^ (rnd_state >> 2) ^
                     (rnd_state >> 3) ^ (rnd_state >> 4)) & 1;
  rnd_state = (rnd_state >> 1) | (b << 7);
  return rnd_state;
}

// u8 coords: MAP_CELL truncates to u8 anyway, and u8 params shave the
// high-byte handling off every call site (fixed PRG region is full)
static unsigned char btop_at(unsigned char mx, unsigned char my) {
  return MT_TOP(mx, my); // per-band solidity (band selected by row my)
}
static unsigned char bflags_at(unsigned char mx, unsigned char my) {
  return MT_FLAGS(mx, my);
}

// cached wall/ledge verdict for a walker's leading edge, keyed on
// (column, feet row, direction); left column 0 blocks like the old
// (int)nx < 256 edge stop
static unsigned char walk_blocked(unsigned char ahead_mx,
                                  unsigned char feet_my, unsigned char dir,
                                  unsigned char *pr_mx, unsigned char *pr_key,
                                  unsigned char *pr_blk) {
  unsigned char key = feet_my | (unsigned char)(dir << 7);
  unsigned char blocked;
  if (*pr_mx == ahead_mx && *pr_key == key)
    return *pr_blk;
  if (dir) // moving left
    blocked = ahead_mx == 0 || (bflags_at(ahead_mx, feet_my - 1) & 1) ||
              btop_at(ahead_mx, feet_my) == 0;
  else
    blocked = ahead_mx >= (unsigned char)(g_w - 1) ||
              (bflags_at(ahead_mx, feet_my - 1) & 4) ||
              btop_at(ahead_mx, feet_my) == 0;
  *pr_mx = ahead_mx;
  *pr_key = key;
  *pr_blk = blocked;
  return blocked;
}

#if EPISODE == 4
// falling-actor landing scan (feet at X midpoint): returns the new top y
// if a tile top lies between the old and new feet rows, else 0
static unsigned int land_at(unsigned int x, unsigned int y, unsigned int newy,
                            unsigned int bw, unsigned int bh) {
  unsigned char feet_new = (newy + bh) >> 8;
  unsigned char mx = (x + bw / 2) >> 8;
  unsigned char my = ((y + bh) >> 8) + 1;
  for (; my <= feet_new && my < g_h; ++my)
    if (btop_at(mx, my))
      return ((unsigned int)my << 8) - bh - 1;
  return 0;
}
#endif

// settle a spawn on the first floor at/below its row; returns top y units
static unsigned int settle_y(unsigned char sx, unsigned char my,
                             unsigned int bh) {
  while ((unsigned char)(my + 1) < g_h && btop_at(sx, my + 1) == 0)
    ++my;
  return (((unsigned int)my + 1) << 8) - bh - 1;
}

// Stays in the fixed region: it reads the level's spawn tables through
// R6 at $8000 (rules out $8000-window banks), and $A000-window (R7)
// relocation is a trap — see the note above level_load in level.c.
void actors_init(void) {
  const unsigned char *p;
  unsigned char i, k;
  // enemy tables carry every difficulty variant, x-sorted, with the
  // record's LAST byte = min difficulty; spawn iff <= g_difficulty
  i = 0;
  p = g_bloogs;
  for (k = g_nbloogs; k; --k, p += 3) {
    if (p[2] > g_difficulty)
      continue;
    bx[i] = (unsigned int)p[0] << 8;
    by[i] = settle_y(p[0], p[1], BLOOG_BH);
    bdir[i] = rnd() & 1;
    banim[i] = i & 3;
    btic[i] = 0;
    bstun[i] = 0;
    bawk[i] = 0;
    bpr_mx[i] = 0xFF; // probe cache invalid
    bcool[i] = 0;
#if EPISODE == 5
    bst[i] = 0;
    bst_tic[i] = 0;
#endif
    if (++i >= MAX_BLOOGS)
      break;
  }
  nbloogs = i;
  nplats_ = g_nplats > MAX_PLATS ? MAX_PLATS : g_nplats;
  p = g_plats;
  for (i = 0; i < nplats_; ++i, p += 3) {
    pfx[i] = (unsigned int)p[0] << 8;
    pfy[i] = (unsigned int)p[1] << 8;
    pfdir[i] = p[2];
    pfkind[i] = 0;
  }
  p = g_fplats;
  for (i = 0; i < g_nfplats && nplats_ < MAX_PLATS; ++i, ++nplats_, p += 2) {
    pfx[nplats_] = (unsigned int)p[0] << 8;
    pfy[nplats_] = (unsigned int)p[1] << 8;
    pforig[nplats_] = pfy[nplats_];
    pfkind[nplats_] = 1;
    pfvel[nplats_] = 0;
  }
  i = 0;
  p = g_babs;
  for (k = g_nbabs; k; --k, p += 3) {
    if (p[2] > g_difficulty)
      continue;
    babx[i] = (unsigned int)p[0] << 8;
#if EPISODE == 5
    baby[i] = (unsigned int)p[1] << 8; // slicestars float where placed
    babdir[i] = 0;                     // north spawn: start moving up
#else
    baby[i] = settle_y(p[0], p[1], BAB_BH);
#endif
    babstate[i] = 0;
    babtic[i] = 0;
    babstun[i] = 0;
    babawk[i] = 0;
#if EPISODE == 4
    babdir[i] = rnd() & 1;
    babvx[i] = 0;
    babvy[i] = 0;
#endif
    if (++i >= MAX_BABS)
      break;
  }
  nbabs_ = i;
  i = 0;
  p = g_blets;
#if EPISODE == 4
  // slot 2: mushroom/skypest/bounder, type in the color byte (p[2])
  for (k = g_nblets; k; --k, p += 4) {
    if (p[3] > g_difficulty)
      continue;
    blx[i] = (unsigned int)p[0] << 8;
    bldir[i] = rnd() & 1;
    blawk[i] = 0;
    bltype[i] = p[2];
    blvy[i] = 0;
    blvx[i] = 0;
    bljumps[i] = 0;
    blanim[i] = 0;
    bltic[i] = 0;
    blstun[i] = 0;
    if (bltype[i] == BL_SKY)
      bly[i] = (unsigned int)p[1] << 8; // floats where placed (no settle)
    else if (bltype[i] == BL_BND)
      bly[i] = ((unsigned int)p[1] << 8) - 128; // spawns above, then falls
    else // Mushroom / Wormmouth / Mimrock: settle on the floor at their box
      bly[i] = settle_y(p[0], p[1], bl_bh[bltype[i]]);
    if (++i >= MAX_BLETS)
      break;
  }
#else
  for (k = g_nblets; k; --k, p += 4) {
    if (p[3] > g_difficulty)
      continue;
    blx[i] = (unsigned int)p[0] << 8;
    bldir[i] = rnd() & 1;
    blawk[i] = 0;
    bly[i] = settle_y(p[0], p[1], BLET_BH);
#if EPISODE == 6
    blcolor[i] = p[2]; // EP5 Amptons have no color variants
#else
    blanim[i] = 0;
    bltic[i] = 0;
#endif
    blpr_mx[i] = 0xFF; // probe cache invalid
    blcool[i] = 0;
    blstun[i] = 0;
    if (++i >= MAX_BLETS)
      break;
  }
#endif
  nblets_ = i;
}

// platform movement, one tic; records per-platform deltas for riders
static void plats_tic(void) {
  unsigned char i;
  for (i = 0; i < nplats_; ++i) {
    unsigned int lead;
    pfawk[i] = awake(pfx[i], pfy[i]);
    pf_dx[i] = pf_dy[i] = 0;
    if (pfkind[i] == 1) { // fall platform (sit/fall/rise)
      if (plat_ridden == i + 1) {
        static unsigned char parity;
        parity ^= 1;
        if (parity) { // gravity high on alternate tics
          pfvel[i] += ACT_GRAV;
          if (pfvel[i] > ACT_TERM)
            pfvel[i] = ACT_TERM;
        }
        {
          unsigned int lead = (pfy[i] + PLAT_BH + pfvel[i]) >> 8;
          if (is_block(pfx[i] >> 8, lead)) {
            pfvel[i] = 0;
          } else {
            pfy[i] += pfvel[i];
            pf_dy[i] = pfvel[i];
          }
        }
      } else if (pfy[i] > pforig[i]) { // rise back
        pfvel[i] = 0;
        pfy[i] -= 12;
        pf_dy[i] = -12;
        if (pfy[i] < pforig[i])
          pfy[i] = pforig[i];
      }
      continue;
    }
    switch (pfdir[i]) {
    case 1: // right
      lead = (pfx[i] + PLAT_BW + PLAT_SPEED) >> 8;
      if (is_block(lead, pfy[i] >> 8))
        pfdir[i] = 3;
      else {
        pfx[i] += PLAT_SPEED;
        pf_dx[i] = PLAT_SPEED;
      }
      break;
    case 3: // left
      lead = (pfx[i] - PLAT_SPEED) >> 8;
      if (is_block(lead, pfy[i] >> 8))
        pfdir[i] = 1;
      else {
        pfx[i] -= PLAT_SPEED;
        pf_dx[i] = -PLAT_SPEED;
      }
      break;
    case 0: // up
      lead = (pfy[i] - PLAT_SPEED) >> 8;
      if (is_block(pfx[i] >> 8, lead)) {
        // B-blocks on both sides mean STOP, not reverse. A switch removes the
        // leading marker and the Lifter then continues upward in its preserved
        // direction.
        if (!is_block(pfx[i] >> 8, lead + 2u))
          pfdir[i] = 2;
      }
      else {
        pfy[i] -= PLAT_SPEED;
        pf_dy[i] = -PLAT_SPEED;
      }
      break;
    default: // down
      lead = (pfy[i] + PLAT_BH + PLAT_SPEED) >> 8;
      if (is_block(pfx[i] >> 8, lead)) {
        if (lead < 2u || !is_block(pfx[i] >> 8, lead - 2u))
          pfdir[i] = 0;
      }
      else {
        pfy[i] += PLAT_SPEED;
        pf_dy[i] = PLAT_SPEED;
      }
      break;
    }
  }
}

// rider query: platform whose top is within [feet-tol, feet+tol] and
// overlaps [x0,x1]. Returns index+1 or 0.
// WRAM-only (pf* arrays) -> banked to cold bank 26 (player.c maps 26 around it)
ACT_BANK6 unsigned char plat_under(unsigned int x0, unsigned int x1,
                         unsigned int feet, unsigned int tol) {
  unsigned char i;
  for (i = 0; i < nplats_; ++i) {
    if (x1 < pfx[i] || pfx[i] + PLAT_BW < x0)
      continue;
    if (feet + tol >= pfy[i] && pfy[i] + tol >= feet)
      return i + 1;
  }
  return 0;
}

// shot at (x,y) units: stun the first overlapping live enemy. Map-free (only
// actor state + box overlaps), so banked to the cold draw bank (26) to relieve
// the full fixed region; the caller (player.c shots_tic) maps it around the call.
ACT_BANK6 unsigned char actors_shot_hit(unsigned int x, unsigned int y) {
  unsigned char i;
  for (i = 0; i < nbloogs; ++i) {
    if (!bawk[i] || bstun[i]) // sleeping actors: no collision
      continue;
    if (x + 128 > bx[i] && x < bx[i] + BLOOG_BW &&
        y + 128 > by[i] && y < by[i] + BLOOG_BH) {
      bstun[i] = 1;
      return 1;
    }
  }
  for (i = 0; i < nblets_; ++i) {
#if EPISODE == 4
    // slot-2 shot response by type: Mad Mushroom splashes unaffected;
    // Skypest is knocked back but not killed; Bounder is stunned +
    // death-hopped.
    if (!blawk[i] || ((bltype[i] == BL_BND || bltype[i] == BL_WORM ||
                       bltype[i] == BL_MIM) && blstun[i]))
      continue;
    if (x + 128 > blx[i] && x < blx[i] + bl_bw[bltype[i]] &&
        y + 128 > bly[i] && y < bly[i] + bl_bh[bltype[i]]) {
      // Mushroom/Skypest: shot splashes (not killed; Skypest dies to a pogo
      // stomp). Bounder: stunned + death-hop. Wormmouth/Mimrock: stunned.
      if (bltype[i] == BL_BND) {
        blstun[i] = 1;
        blvy[i] += BND_STUN_VY;
      } else if (bltype[i] == BL_WORM || bltype[i] == BL_MIM) {
        blstun[i] = 1;
      }
      return 1;
    }
#else
    if (!blawk[i] || blstun[i])
      continue;
    if (x + 128 > blx[i] && x < blx[i] + BLET_BW &&
        y + 128 > bly[i] && y < bly[i] + BLET_BH) {
      blstun[i] = 1;
      return 1;
    }
#endif
  }
  for (i = 0; i < nbabs_; ++i) {
    if (!babawk[i] || babstun[i])
      continue;
    if (x + 128 > babx[i] && x < babx[i] + BAB_BW &&
        y + 128 > baby[i] && y < baby[i] + BAB_BH) {
#if EPISODE == 5
      return 1; // Slicestar is invincible: the shot just splashes
#else
      babstun[i] = 1;
      return 1;
#endif
    }
  }
  return 0;
}

#if EPISODE == 4
// wall/ledge stop for a ground critter moving to nx (its leading edge tile is
// solid, or there's no floor at the feet ahead = a ledge). No probe cache
// (Wormmouth/Mimrock are few and active-window culled), so it just probes.
static unsigned char bl_walk_blocked(unsigned int nx, unsigned char feet_my,
                                     unsigned char dir, unsigned int bw) {
  unsigned char ahead = dir ? (unsigned char)(nx >> 8)
                            : (unsigned char)((nx + bw) >> 8);
  if (dir)
    return ahead == 0 || (bflags_at(ahead, feet_my - 1) & 1) ||
           btop_at(ahead, feet_my) == 0;
  return ahead >= (unsigned char)(g_w - 1) ||
         (bflags_at(ahead, feet_my - 1) & 4) || btop_at(ahead, feet_my) == 0;
}

// One generic bouncer that lands on the first tile top under its feet and
// re-bounces; shared by Mushroom and Bounder. Returns the landing y (top)
// or 0 (still airborne). Fell-out-of-map is parked near the bottom.
static void bl_gravity_land(unsigned char i, int hop_vy,
                            unsigned char grav_hi) {
  unsigned int newy, bw = bl_bw[bltype[i]], bh = bl_bh[bltype[i]];
  if (grav_hi) { // gravity on alternate tics
    blvy[i] += ACT_GRAV;
    if (blvy[i] > ACT_TERM)
      blvy[i] = ACT_TERM;
  }
  newy = bly[i] + blvy[i];
  if (blvy[i] > 0) { // falling: land on the first tile top under the feet
    unsigned int land = land_at(blx[i], bly[i], newy, bw, bh);
    if (land) {
      bly[i] = land;
      blvy[i] = hop_vy; // caller decides the bounce impulse
    } else
      bly[i] = newy;
    if ((bly[i] >> 8) >= (unsigned int)g_h) { // fell out: park near bottom
      bly[i] = ((unsigned int)g_h - 2) << 8;
      blvy[i] = 0;
    }
  } else
    bly[i] = newy;
}

// slot 2 (generic critters): Mad Mushroom / Skypest / Bounder by type
static void blets_tic(void) {
  static unsigned char sparity;
  unsigned char i;
  sparity ^= 1;
  for (i = 0; i < nblets_; ++i) {
    blawk[i] = awake(blx[i], bly[i]);
    if (!blawk[i])
      continue;
    if (bltype[i] == BL_MUSH) {
      // Mad Mushroom: bounces in place forever, facing Keen; hop -40, every 3rd
      // landing a big -68 leap.
      unsigned char prev = (blvy[i] > 0);
      bldir[i] = (blx[i] < pl_x) ? 0 : 1;
      if (++bltic[i] >= 7) { // 8-tic action @70Hz, x6/7
        bltic[i] = 0;
        blanim[i] ^= 1;
      }
      bl_gravity_land(i, MUSH_HOP_VY, sparity);
      // detect a fresh landing (vy flipped from + to the hop impulse)
      if (prev && blvy[i] < 0) {
        if (++bljumps[i] >= MUSH_NUM_JUMPS) {
          bljumps[i] = 0;
          blvy[i] = MUSH_JUMP_VY; // every 3rd hop is a big leap
        }
      }
    } else if (bltype[i] == BL_SKY) {
      // Skypest: gentle 2D drift, random flips, bounce off geometry
      unsigned int bw = bl_bw[BL_SKY], bh = bl_bh[BL_SKY];
      unsigned char my, ahead;
      if (++bltic[i] >= 4) { // action frame (5-tic @70Hz, x6/7): random flips
        bltic[i] = 0;
        blanim[i] ^= 1;
        if (rnd() < 2)
          bldir[i] ^= 1;
        if (bljumps[i] == 0 && rnd() < 2) // moving down: flip up
          bljumps[i] = 1;
        else if (bljumps[i] && rnd() < 4) // moving up: flip down
          bljumps[i] = 0;
      }
      if (sparity) { // accel 1/odd-tic toward direction, cap +-20
        if (bldir[i]) {
          if (blvx[i] > -SKY_VMAX)
            --blvx[i];
        } else if (blvx[i] < SKY_VMAX)
          ++blvx[i];
        if (bljumps[i]) { // up
          if (blvy[i] > -SKY_VMAX)
            --blvy[i];
        } else if (blvy[i] < SKY_VMAX)
          ++blvy[i];
      }
      // X move + wall bounce
      {
        unsigned int nx = blx[i] + blvx[i];
        my = (unsigned char)((bly[i] + bh / 2) >> 8);
        ahead = blvx[i] < 0 ? (unsigned char)(nx >> 8)
                            : (unsigned char)((nx + bw) >> 8);
        if (blvx[i] < 0 && (ahead == 0 || (bflags_at(ahead, my) & 1))) {
          blvx[i] = 0;
          bldir[i] = 0; // turn right
        } else if (blvx[i] > 0 &&
                   (ahead >= (unsigned char)(g_w - 1) ||
                    (bflags_at(ahead, my) & 4))) {
          blvx[i] = 0;
          bldir[i] = 1; // turn left
        } else
          blx[i] = nx;
      }
      // Y move + ceiling/floor bounce
      {
        unsigned int ny = bly[i] + blvy[i];
        unsigned char mx = (unsigned char)((blx[i] + bw / 2) >> 8);
        if (blvy[i] < 0) { // rising: ceiling (bottom-solid tile)
          unsigned char head = (unsigned char)(ny >> 8);
          if ((int)ny < 256 || (bflags_at(mx, head) & 2)) {
            blvy[i] = SKY_CEIL_VY;
            bljumps[i] = 0; // now heading down
          } else
            bly[i] = ny;
        } else { // falling: floor top -> takeoff (preen simplified)
          unsigned char feet = (unsigned char)((ny + bh) >> 8);
          if (feet < g_h && btop_at(mx, feet)) {
            bly[i] = ((unsigned int)feet << 8) - bh - 1;
            blvy[i] = SKY_TAKEOFF_VY;
            bljumps[i] = 1; // heading up
          } else
            bly[i] = ny;
        }
      }
    } else if (bltype[i] == BL_WORM) {
      // Wormmouth: ground worm. Walks (turning at walls/ledges), faces Keen
      // when he's far (peep), and bites when Keen is level and just ahead;
      // ONLY the bite is lethal (see actors_touch_player). bljumps = state
      // (0 walk, 1 bite); bltic = timer.
      unsigned int bw = bl_bw[BL_WORM], bh = bl_bh[BL_WORM];
      if (bljumps[i]) { // biting: hold the lethal pose, then resume walking
        if (++bltic[i] >= WORM_BITE_TICS) {
          bltic[i] = 0;
          bljumps[i] = 0;
        }
      } else {
        int dx = (int)(pl_x - blx[i]);
        int dyf = (int)((pl_y + 496) - (bly[i] + bh)); // feet delta
        if (++bltic[i] >= 7) { // action frame (8-tic @70Hz, x6/7)
          bltic[i] = 0;
          blanim[i] ^= 1;
          if ((dx > WORM_PEEP_XR || dx < -WORM_PEEP_XR) && rnd() < 6)
            bldir[i] = (blx[i] < pl_x) ? 0 : 1; // peep: turn toward Keen
        }
        if (dyf > -WORM_BITE_YR && dyf < WORM_BITE_YR &&
            (bldir[i] ? (dx < -WORM_BITE_MIN &&
                         dx > -(int)(bw + WORM_BITE_MAX))
                      : (dx > WORM_BITE_MIN && dx < (int)(bw + WORM_BITE_MAX)))) {
          bljumps[i] = 1; // Keen level and ahead: bite
          bltic[i] = 0;
        } else {
          unsigned int nx = bldir[i] ? blx[i] - WORM_SPEED : blx[i] + WORM_SPEED;
          unsigned char feet_my = (bly[i] + bh + 1) >> 8;
          if (bl_walk_blocked(nx, feet_my, bldir[i], bw))
            bldir[i] ^= 1;
          else
            blx[i] = nx;
        }
      }
    } else if (bltype[i] == BL_MIM) {
      // Mimrock: inert rock -> sneaks toward Keen -> bonk-jumps at him. Lethal
      // ONLY while airborne (bljumps==2). bljumps = state (0 sit, 1 walk, 2
      // jump). Simplifications: wakes on proximity (no "Keen moving toward me"
      // gate); one jump then sit (no mid-air re-bounce).
      unsigned int bw = bl_bw[BL_MIM], bh = bl_bh[BL_MIM];
      int dx = (int)(pl_x - blx[i]);
      int dyf = (int)((pl_y + 496) - (bly[i] + bh));
      unsigned int adx = dx < 0 ? (unsigned int)-dx : (unsigned int)dx;
      unsigned int ady = dyf < 0 ? (unsigned int)-dyf : (unsigned int)dyf;
      if (bljumps[i] == 2) { // airborne bonk-jump (gravity + land -> sit)
        unsigned int newy;
        if (sparity) {
          blvy[i] += ACT_GRAV;
          if (blvy[i] > ACT_TERM)
            blvy[i] = ACT_TERM;
        }
        blx[i] += blvx[i];
        newy = bly[i] + blvy[i];
        if (blvy[i] > 0) {
          unsigned int land = land_at(blx[i], bly[i], newy, bw, bh);
          if (land) {
            bly[i] = land;
            blvy[i] = blvx[i] = 0;
            bljumps[i] = 0; // landed -> back to sit
            bltic[i] = 0;
          } else
            bly[i] = newy;
          if ((bly[i] >> 8) >= (unsigned int)g_h) { // fell out: park + sit
            bly[i] = ((unsigned int)g_h - 2) << 8;
            blvy[i] = blvx[i] = 0;
            bljumps[i] = 0;
          }
        } else
          bly[i] = newy;
      } else { // on the ground (sit or sneak): face Keen; the bonk-jump fires
               // directly when Keen is close enough, whatever the sub-state
        bldir[i] = (blx[i] < pl_x) ? 0 : 1; // face Keen
        if (ady <= MIM_YR && adx < MIM_JUMP_XR) {
          blvx[i] = bldir[i] ? -MIM_JUMP_VX : MIM_JUMP_VX;
          blvy[i] = MIM_JUMP_VY;
          bljumps[i] = 2; // in bonk range: leap at Keen (lethal airborne)
          bltic[i] = 0;
        } else if (bljumps[i] == 1) { // sneaking toward Keen
          if (++bltic[i] >= 5) { // 6-tic action @70Hz, x6/7
            bltic[i] = 0;
            blanim[i] ^= 1;
          }
          if (adx > MIM_JUMP_XR + 0x200 || ady > MIM_YR) {
            bljumps[i] = 0; // lost Keen -> back to inert
          } else {
            unsigned int nx =
                bldir[i] ? blx[i] - MIM_WALK_SPEED : blx[i] + MIM_WALK_SPEED;
            unsigned char feet_my = (bly[i] + bh + 1) >> 8;
            if (!bl_walk_blocked(nx, feet_my, bldir[i], bw))
              blx[i] = nx;
          }
        } else { // inert; wake to sneak when Keen nears (vertically level)
          if (ady <= MIM_YR && adx > MIM_WAKE_XR && adx < MIM_WAKE_XR + 0x300) {
            bljumps[i] = 1;
            bltic[i] = 0;
          }
        }
      }
    } else { // BL_BND Bounder: bouncing ball Keen can ride (harmless)
      unsigned char prev = (blvy[i] > 0);
      unsigned int oldx = blx[i], oldy = bly[i];
      if (++bltic[i] >= 7) { // 8-tic action @70Hz, x6/7
        bltic[i] = 0;
        blanim[i] ^= 1;
      }
      // horizontal move with wall stop
      if (blvx[i]) {
        unsigned int bw = bl_bw[BL_BND];
        unsigned int nx = blx[i] + blvx[i];
        unsigned char my = (unsigned char)((bly[i] + bl_bh[BL_BND] - 1) >> 8);
        unsigned char ahead = blvx[i] < 0 ? (unsigned char)(nx >> 8)
                                          : (unsigned char)((nx + bw) >> 8);
        unsigned char wall = blvx[i] < 0
                                 ? (ahead == 0 || (bflags_at(ahead, my) & 1))
                                 : (ahead >= (unsigned char)(g_w - 1) ||
                                    (bflags_at(ahead, my) & 4));
        if (wall) {
          blvx[i] = 0;
          bldir[i] ^= 1;
        } else
          blx[i] = nx;
      }
      bl_gravity_land(i, BND_BOUNCE_VY, sparity);
      if (prev && blvy[i] < 0) { // just bounced: maybe pick a new direction
        if (++bljumps[i] >= BND_CYCLE) {
          unsigned char r = rnd();
          bljumps[i] = 0;
          if (r < 100) {
            bldir[i] = 1;
            blvx[i] = -BND_XVEL;
          } else if (r < 200) {
            bldir[i] = 0;
            blvx[i] = BND_XVEL;
          } else
            blvx[i] = 0;
        }
      }
      // record this-tic delta for a rider (clamped to signed-char range)
      {
        int ddx = (int)blx[i] - (int)oldx, ddy = (int)bly[i] - (int)oldy;
        bl_bdx[i] = ddx < -127 ? -127 : ddx > 127 ? 127 : (signed char)ddx;
        bl_bdy[i] = ddy < -127 ? -127 : ddy > 127 ? 127 : (signed char)ddy;
      }
    }
  }
}

// Bounder RIDE query: index+1 of an awake Bounder whose top is within
// [feet-tol, feet+tol] and overlaps [x0,x1]; 0 otherwise. Bounders carry Keen
// (bly = top to snap to, bl_bdx/bl_bdy = this-tic motion to inherit).
// WRAM-only (bl_* arrays) -> banked to cold bank 26; player.c maps 26 around
// its two calls.
ACT_BANK6 unsigned char bounder_under(unsigned int x0, unsigned int x1,
                                      unsigned int feet, unsigned int tol) {
  unsigned char i;
  for (i = 0; i < nblets_; ++i) {
    if (bltype[i] != BL_BND || !blawk[i])
      continue;
    if (x1 < blx[i] || blx[i] + bl_bw[BL_BND] < x0)
      continue;
    if (feet + tol >= bly[i] && bly[i] + tol >= feet)
      return i + 1;
  }
  return 0;
}
unsigned int bounder_top(unsigned char idx1) { return bly[idx1 - 1]; }
#else
static void blets_tic(void) {
  unsigned char i;
  for (i = 0; i < nblets_; ++i) {
    unsigned int nx, feet_my, ahead_mx;
    blawk[i] = awake(blx[i], bly[i]);
    if (!blawk[i] || blstun[i])
      continue;
    if (++bltic[i] >= BLET_TICS) { // per action-frame think
      bltic[i] = 0;
      blanim[i] = (blanim[i] + 1) & 3;
#if EPISODE == 6
      if (rnd() < 0x20) // Blooglet random turn toward Keen
        bldir[i] = (blx[i] < pl_x) ? 0 : 1;
#endif
      // EP5 Ampton: no random turn (walks between walls/ledges; pole
      // climbing and computer fiddling are skipped)
    }
    if (blcool[i]) { // just turned: pause briefly
      --blcool[i];
      continue;
    }
    nx = bldir[i] ? blx[i] - BLET_SPEED : blx[i] + BLET_SPEED;
    feet_my = (bly[i] + BLET_BH + 1) >> 8;
    ahead_mx = bldir[i] ? (nx >> 8) : ((nx + BLET_BW) >> 8);
    if (walk_blocked(ahead_mx, feet_my, bldir[i], &blpr_mx[i], &blpr_key[i],
                     &blpr_blk[i])) {
      bldir[i] ^= 1;
      blcool[i] = BLET_TICS;
    } else
      blx[i] = nx;
  }
}
#endif

#if EPISODE == 4
// Lick: sit ~20 tics, then either hop toward Keen (long hop vx 32 / vy -32
// when more than 0x300 units away, else short 16 / -16) or, when Keen is
// level (|dy| <= 0x100) and in the facing window, breathe flame for 32 tics.
// Only the flame is lethal. No wall clip while hopping; the flame kill box is
// the body box extended 0x180 units forward, so body contact during a flame
// kills even if Keen is slightly behind.
static void babs_tic(void) {
  static unsigned char parity;
  unsigned char i;
  parity ^= 1;
  for (i = 0; i < nbabs_; ++i) {
    babawk[i] = awake(babx[i], baby[i]);
    if (!babawk[i] || babstun[i])
      continue;
    if (babstate[i] == 0) { // grounded (hop land + next-hop windup)
      if (++babtic[i] >= LICK_SIT_TICS) {
        int dx = (int)(pl_x - babx[i]);
        int dy = (int)(pl_y - baby[i]);
        babtic[i] = 0;
        babdir[i] = (babx[i] < pl_x) ? 0 : 1; // face Keen
        if (dy >= -LICK_FLAME_Y && dy <= LICK_FLAME_Y &&
            (babdir[i] ? (dx > -(LICK_FLAME_AHEAD + BAB_BW))
                       : (dx < LICK_FLAME_AHEAD))) {
          babstate[i] = 2; // flame
        } else {
          unsigned int adx = dx < 0 ? (unsigned int)-dx : (unsigned int)dx;
          if (adx > LICK_LONG_RADIUS) {
            babvx[i] = babdir[i] ? -LICK_LONG_VX : LICK_LONG_VX;
            babvy[i] = LICK_LONG_VY;
          } else {
            babvx[i] = babdir[i] ? -LICK_SHORT_VX : LICK_SHORT_VX;
            babvy[i] = LICK_SHORT_VY;
          }
          babstate[i] = 1;
        }
      }
    } else if (babstate[i] == 2) { // flaming
      if (++babtic[i] >= LICK_FLAME_TICS) {
        babtic[i] = 0;
        babstate[i] = 0;
      }
    } else { // airborne hop; landing scheme shared with the EP6 Babobba
      unsigned int newy;
      if (parity) {
        babvy[i] += ACT_GRAV;
        if (babvy[i] > ACT_TERM)
          babvy[i] = ACT_TERM;
      }
      babx[i] += babvx[i];
      newy = baby[i] + babvy[i];
      if (babvy[i] > 0) { // landing check at feet, X midpoint
        unsigned int land = land_at(babx[i], baby[i], newy, BAB_BW, BAB_BH);
        if (land) {
          baby[i] = land;
          babstate[i] = 0;
          babtic[i] = 0;
        } else
          baby[i] = newy;
        if ((baby[i] >> 8) >= (unsigned int)g_h) { // fell out: respawn
          babstate[i] = 0;
          babtic[i] = 0;
          baby[i] = ((unsigned int)g_h - 2) << 8;
        }
      } else
        baby[i] = newy;
    }
  }
}
#elif EPISODE == 5
// Slicestar (north spawn): vertical bouncer at platform speed, reversing
// on info-plane block markers or solid tiles (axis-platform logic; the
// level author boxes each star with value-31 cells)
static void babs_tic(void) {
  unsigned char i;
  for (i = 0; i < nbabs_; ++i) {
    unsigned char mx, lead;
    babawk[i] = awake(babx[i], baby[i]);
    if (!babawk[i])
      continue;
    mx = (unsigned char)((babx[i] + BAB_BW / 2) >> 8);
    if (babdir[i] == 0) { // moving up
      lead = (unsigned char)((baby[i] - PLAT_SPEED) >> 8);
      if (baby[i] < 256u + PLAT_SPEED || is_block(mx, lead) ||
          (bflags_at(mx, lead) & 2)) // bottom-solid tile: bounce down
        babdir[i] = 1;
      else
        baby[i] -= PLAT_SPEED;
    } else { // moving down
      lead = (unsigned char)((baby[i] + BAB_BH + PLAT_SPEED) >> 8);
      if (lead >= g_h || is_block(mx, lead) ||
          btop_at(mx, lead)) // tile top: bounce up
        babdir[i] = 0;
      else
        baby[i] += PLAT_SPEED;
    }
  }
}
#else
// Babobba: sit 20 tics, hop toward Keen
static void babs_tic(void) {
  static unsigned char parity;
  unsigned char i;
  parity ^= 1;
  for (i = 0; i < nbabs_; ++i) {
    babawk[i] = awake(babx[i], baby[i]);
    if (!babawk[i] || babstun[i])
      continue;
    if (babstate[i] == 0) { // sitting
      if (++babtic[i] >= 17) { // 20-tic sit @70Hz, x6/7
        babtic[i] = 0;
        babstate[i] = 1;
        babdir[i] = (babx[i] < pl_x) ? 0 : 1;
        babvy[i] = -47; // -40 x7/6
      }
    } else { // airborne hop
      unsigned int newy;
      if (parity) {
        babvy[i] += ACT_GRAV;
        if (babvy[i] > ACT_TERM)
          babvy[i] = ACT_TERM;
      }
      babx[i] += babdir[i] ? (unsigned int)-14 : 14; // 12 x7/6
      newy = baby[i] + babvy[i];
      if (babvy[i] > 0) { // landing check at feet, X midpoint
        unsigned int feet_old = (baby[i] + BAB_BH) >> 8;
        unsigned int feet_new = (newy + BAB_BH) >> 8;
        unsigned int mx = (babx[i] + BAB_BW / 2) >> 8;
        unsigned int my;
        unsigned char landed = 0;
        for (my = feet_old + 1; my <= feet_new && my < (unsigned int)g_h; ++my)
          if (btop_at(mx, my)) {
            baby[i] = (my << 8) - BAB_BH - 1;
            babstate[i] = 0;
            landed = 1;
            break;
          }
        if (!landed)
          baby[i] = newy;
        if ((baby[i] >> 8) >= (unsigned int)g_h) { // fell out: respawn sitting
          babstate[i] = 0;
          baby[i] = ((unsigned int)g_h - 2) << 8;
        }
      } else
        baby[i] = newy;
    }
  }
}
#endif

// one 70Hz tic for all actors
void actors_tic(void) {
  unsigned char i;
  plats_tic();
  blets_tic();
  babs_tic();
#if EPISODE == 5
  // Sparky: patrol at 16 units/tic; sometimes pause to look around, and if
  // Keen's feet are level (+-0x100 units) on the looked-at side, wind up and
  // charge at 32 units/tic until a wall/ledge turns it around.
  for (i = 0; i < nbloogs; ++i) {
    unsigned int nx, feet_my, ahead_mx;
    unsigned char speed;
    bawk[i] = awake(bx[i], by[i]);
    if (!bawk[i] || bstun[i])
      continue;
    if (++btic[i] >= (bst[i] == 3 ? SPARKY_CHARGE_ANIM : ACTION_TICS)) {
      btic[i] = 0;
      banim[i] = (banim[i] + 1) & 3;
      if (bst[i] == 0 && banim[i] == 0 && rnd() < SPARKY_SEARCH_CHANCE) {
        bst[i] = 1; // stop and look around
        bst_tic[i] = SPARKY_SEARCH_TICS;
      }
    }
    if (bst[i] == 1) { // searching: stands still, scans left then right
      --bst_tic[i];
      if (bst_tic[i] == SPARKY_SEARCH_TICS / 2 || bst_tic[i] == 0) {
        unsigned char want_left = bst_tic[i] != 0;
        int dy = (int)(pl_y + 496) - (int)(by[i] + BLOOG_BH);
        if (dy >= -SPARKY_SEARCH_Y && dy < SPARKY_SEARCH_Y &&
            (want_left ? pl_x < bx[i] : pl_x > bx[i])) {
          bdir[i] = want_left; // face Keen and wind up the charge
          bst[i] = 2;
          bst_tic[i] = SPARKY_PREP_TICS;
        } else if (bst_tic[i] == 0)
          bst[i] = 0;
      }
      continue;
    }
    if (bst[i] == 2) { // charge windup (in place)
      if (--bst_tic[i] == 0)
        bst[i] = 3;
      continue;
    }
    if (bcool[i]) { // just turned at a wall/ledge: pause briefly
      --bcool[i];
      continue;
    }
    speed = bst[i] == 3 ? SPARKY_CHARGE_SPEED : BLOOG_SPEED;
    nx = bdir[i] ? bx[i] - speed : bx[i] + speed;
    feet_my = (by[i] + BLOOG_BH + 1) >> 8;
    ahead_mx = bdir[i] ? (nx >> 8) : ((nx + BLOOG_BW) >> 8);
    if (walk_blocked(ahead_mx, feet_my, bdir[i], &bpr_mx[i], &bpr_key[i],
                     &bpr_blk[i])) {
      bdir[i] ^= 1; // wall or ledge: turn
      bst[i] = 0;   // a charge ends at the wall
      bcool[i] = 3 * ACTION_TICS; // turn pause: 3 x 8 tics
    } else
      bx[i] = nx;
  }
#else
  for (i = 0; i < nbloogs; ++i) {
    unsigned int nx, feet_my, ahead_mx;
    bawk[i] = awake(bx[i], by[i]);
    if (!bawk[i] || bstun[i])
      continue;
    if (++btic[i] >= ACTION_TICS) { // per action-frame think
      btic[i] = 0;
      banim[i] = (banim[i] + 1) & 3;
      if (rnd() < BLOOG_TURN_CHANCE)
        bdir[i] = (bx[i] < pl_x) ? 0 : 1;
    }
    if (bcool[i]) { // just turned at a wall/ledge: pause briefly
      --bcool[i];
      continue;
    }
    nx = bdir[i] ? bx[i] - BLOOG_SPEED : bx[i] + BLOOG_SPEED;
    feet_my = (by[i] + BLOOG_BH + 1) >> 8;
    ahead_mx = bdir[i] ? (nx >> 8) : ((nx + BLOOG_BW) >> 8);
    if (walk_blocked(ahead_mx, feet_my, bdir[i], &bpr_mx[i], &bpr_key[i],
                     &bpr_blk[i])) {
      bdir[i] ^= 1; // wall or ledge: turn
      bcool[i] = ACTION_TICS;
    } else
      bx[i] = nx;
  }
#endif
}

// returns nonzero if any lethal actor overlaps Keen's clip box. Map-free
// (actor state + box overlaps only), so banked to the cold draw bank (26); the
// caller (main.c game loop) maps it around the call and restores the level bank.
ACT_BANK6 unsigned char actors_touch_player(void) {
  unsigned char i;
  for (i = 0; i < nbloogs; ++i) {
    if (!bawk[i] || bstun[i])
      continue;
    if (pl_x < bx[i] + BLOOG_BW && bx[i] < pl_x + 240 &&
        pl_y < by[i] + BLOOG_BH && by[i] < pl_y + 496)
      return 1;
  }
#if EPISODE == 4
  for (i = 0; i < nblets_; ++i) { // slot 2: mushroom/skypest/bounder/worm/mim
    unsigned int bw, bh;
    if (!blawk[i])
      continue;
    // The Bounder is ALWAYS harmless on contact — Keen rides it (player.c
    // bounder_under); it is never a touch-kill.
    if (bltype[i] == BL_BND)
      continue;
    // stunned Wormmouth/Mimrock and squished Skypest are harmless
    if (blstun[i] && (bltype[i] == BL_SKY || bltype[i] == BL_WORM ||
                      bltype[i] == BL_MIM))
      continue;
    // Wormmouth kills only while biting, Mimrock only while airborne (bonk);
    // both are harmless otherwise (attack-frame-only collision).
    if (bltype[i] == BL_WORM && bljumps[i] != 1)
      continue;
    if (bltype[i] == BL_MIM && bljumps[i] != 2)
      continue;
    bw = bl_bw[bltype[i]];
    bh = bl_bh[bltype[i]];
    if (pl_x < blx[i] + bw && blx[i] < pl_x + 240 &&
        pl_y < bly[i] + bh && bly[i] < pl_y + 496) {
      // Skypest is squished (not killed) by a pogo stomp from above
      if (bltype[i] == BL_SKY && pl_pogo && pl_vy > 0) {
        blstun[i] = 1; // becomes the harmless SQUASH sprite
        continue;
      }
      return 1;
    }
  }
  for (i = 0; i < nbabs_; ++i) { // Licks: only the flame is lethal
    unsigned int x0, x1;
    if (!babawk[i] || babstun[i] || babstate[i] != 2)
      continue;
    x0 = babx[i];
    x1 = babx[i] + BAB_BW;
    if (babdir[i])
      x0 = (x0 > LICK_FLAME_AHEAD) ? x0 - LICK_FLAME_AHEAD : 0;
    else
      x1 += LICK_FLAME_AHEAD;
    if (pl_x < x1 && x0 < pl_x + 240 &&
        pl_y < baby[i] + BAB_BH && baby[i] < pl_y + 496)
      return 1;
  }
#else
#if EPISODE == 5
  for (i = 0; i < nblets_; ++i) { // Amptons: lethal unless stunned
    if (!blawk[i] || blstun[i])
      continue;
    if (pl_x < blx[i] + BLET_BW && blx[i] < pl_x + 240 &&
        pl_y < bly[i] + BLET_BH && bly[i] < pl_y + 496)
      return 1;
  }
#endif
  for (i = 0; i < nbabs_; ++i) {
    if (!babawk[i] || babstun[i])
      continue;
    if (pl_x < babx[i] + BAB_BW && babx[i] < pl_x + 240 &&
        pl_y < baby[i] + BAB_BH && baby[i] < pl_y + 496)
      return 1;
  }
#endif
  return 0;
}

// Draw one actor metasprite without OAM hazards:
// - fully inside the safe box with OAM room: fast neslib oam_meta_spr
// - near screen edges or OAM nearly full: per-part draw that clips parts
//   whose OAM byte coords would wrap (the old cast wrapped sx in [-48,-1]
//   to 208..255, painting ghost copies at the opposite screen edge).
// Gross offscreen culling is the callers' job (cached awake flags).
// Returns nonzero when OAM is full (SPRID wrapped): caller stops, so the
// 64-sprite budget can never wrap around and stomp the HUD/player.
ACT_BANK6 static unsigned char draw_actor(int sx, int sy,
                                          const unsigned char *ms) {
  unsigned char idx;
  const signed char *p;
  idx = oam_get();
  if (idx == 0) // HUD/player drew already, so 0 means SPRID wrapped: full
    return 1;
  if (idx <= 188 && sx >= 8 && sx <= 200 && sy >= 0 && sy <= 200) {
    // whole metasprite stays in 0..255 on both axes (enemy part offsets
    // are -5..51 in x, 0..24 in y) and 64B of OAM headroom remains
    oam_meta_spr((unsigned char)sx, (unsigned char)sy, ms);
    return 0;
  }
  for (p = (const signed char *)ms; p[0] != -128; p += 4) {
    int px = sx + p[0];
    int py = sy + p[1];
    if (px < 0 || px > 248 || py < 0 || py > 231)
      continue; // clip parts instead of letting the byte coord wrap
    oam_spr((unsigned char)px, (unsigned char)py, (unsigned char)p[2],
            (unsigned char)p[3]);
    idx += 4;
    if (idx == 0)
      return 1;
  }
  return 0;
}

// pass 0 draws actors fully on screen, pass 1 the edge stragglers, so
// when the sprite budget runs out the far/half-visible ones drop first
#define DRAW_PASS(sx) ((sx) >= 0 && (sx) <= 208 ? 0 : 1)

static unsigned char edge_pending; // pass 0 saw actors for pass 1

ACT_BANK6 void actors_draw(unsigned int cam_px, unsigned int cam_py) {
  unsigned char i, pass;
  edge_pending = 0;
  for (pass = 0; pass < 2; ++pass) {
    if (pass && !edge_pending)
      break; // nothing near the screen edges: skip the second sweep
    for (i = 0; i < nplats_; ++i) { // platforms first: Keen rides them
      int sx, sy;
      if (!pfawk[i]) // cull with the tic loop's cached window verdict
        continue;
      sx = (int)((pfx[i] >> 4) - cam_px);
      sy = (int)((pfy[i] >> 4) - cam_py);
      if (DRAW_PASS(sx) != pass) {
        edge_pending = 1; // only reachable in pass 0
        continue;
      }
      if (draw_actor(sx, sy, ms_platform))
        return;
    }
    for (i = 0; i < nbloogs; ++i) {
      int sx, sy;
      if (!bawk[i])
        continue;
      sx = (int)((bx[i] >> 4) - cam_px);
      sy = (int)((by[i] >> 4) - cam_py);
      if (DRAW_PASS(sx) != pass) {
        edge_pending = 1; // only reachable in pass 0
        continue;
      }
      if (draw_actor(sx, sy,
                     bstun[i] ? WLK_MS_STUN : WLK_MS[banim[i]][bdir[i]]))
        return;
    }
#if EPISODE == 4
    for (i = 0; i < nbabs_; ++i) { // Licks
      int sx, sy;
      unsigned char fr;
      if (!babawk[i])
        continue;
      sx = (int)((babx[i] >> 4) - cam_px);
      sy = (int)((baby[i] >> 4) - cam_py);
      if (DRAW_PASS(sx) != pass) {
        edge_pending = 1; // only reachable in pass 0
        continue;
      }
      if (babstun[i])
        fr = 4;
      else if (babstate[i] == 2) // flame frames alternate every 4 tics
        fr = 2 + ((babtic[i] >> 2) & 1);
      else
        fr = babstate[i]; // 0 grounded / 1 airborne
      if (draw_actor(sx, sy, ms_lick[fr][babdir[i]]))
        return;
    }
    for (i = 0; i < nblets_; ++i) { // slot 2: mushroom / skypest / bounder
      int sx, sy;
      const unsigned char *ms;
      if (!blawk[i])
        continue;
      sx = (int)((blx[i] >> 4) - cam_px);
      sy = (int)((bly[i] >> 4) - cam_py);
      if (DRAW_PASS(sx) != pass) {
        edge_pending = 1; // only reachable in pass 0
        continue;
      }
      if (bltype[i] == BL_SKY)
        ms = blstun[i] ? ms_skypestsquash
                       : ms_skypest[blanim[i] & 1][bldir[i]];
      else if (bltype[i] == BL_BND)
        ms = blstun[i] ? ms_bounderstun
                       : ms_bounder[blanim[i] & 1][bldir[i]];
      else if (bltype[i] == BL_WORM) // row 0 hint/walk, row 1 bite
        ms = blstun[i] ? ms_wormstun : ms_worm[bljumps[i] ? 1 : 0][bldir[i]];
      else if (bltype[i] == BL_MIM) // 0 sit, 1-2 walk, 3 bonk(airborne)
        ms = blstun[i] ? ms_mimstun
                       : ms_mim[bljumps[i] == 0   ? 0
                                : bljumps[i] == 2 ? 3
                                                  : 1 + (blanim[i] & 1)]
                               [bldir[i]];
      else
        ms = ms_mush[blanim[i] & 1][bldir[i]];
      if (draw_actor(sx, sy, ms))
        return;
    }
#elif EPISODE == 5
    for (i = 0; i < nbabs_; ++i) { // Slicestars (single spinning frame)
      int sx, sy;
      if (!babawk[i])
        continue;
      sx = (int)((babx[i] >> 4) - cam_px);
      sy = (int)((baby[i] >> 4) - cam_py);
      if (DRAW_PASS(sx) != pass) {
        edge_pending = 1; // only reachable in pass 0
        continue;
      }
      if (draw_actor(sx, sy, ms_slice[0][0]))
        return;
    }
    for (i = 0; i < nblets_; ++i) { // Amptons
      int sx, sy;
      if (!blawk[i])
        continue;
      sx = (int)((blx[i] >> 4) - cam_px);
      sy = (int)((bly[i] >> 4) - cam_py);
      if (DRAW_PASS(sx) != pass) {
        edge_pending = 1; // only reachable in pass 0
        continue;
      }
      if (draw_actor(sx, sy, blstun[i] ? ms_amptonstun
                                       : ms_ampton[blanim[i]][bldir[i]]))
        return;
    }
#else
    for (i = 0; i < nbabs_; ++i) {
      int sx, sy;
      unsigned char fr;
      if (!babawk[i])
        continue;
      sx = (int)((babx[i] >> 4) - cam_px);
      sy = (int)((baby[i] >> 4) - cam_py);
      fr = babstate[i] ? (babvy[i] < 0 ? 1 : 2) : 0;
      if (DRAW_PASS(sx) != pass) {
        edge_pending = 1; // only reachable in pass 0
        continue;
      }
      if (draw_actor(sx, sy, ms_bab[babstun[i] ? 0 : fr][babdir[i]]))
        return;
    }
    for (i = 0; i < nblets_; ++i) {
      int sx, sy;
      const unsigned char *const (*tbl)[2];
      if (!blawk[i])
        continue;
      sx = (int)((blx[i] >> 4) - cam_px);
      sy = (int)((bly[i] >> 4) - cam_py);
      tbl = blcolor[i] == 0 ? ms_blet_red : ms_blet_grn;
      if (DRAW_PASS(sx) != pass) {
        edge_pending = 1; // only reachable in pass 0
        continue;
      }
      if (draw_actor(sx, sy, tbl[blstun[i] ? 4 : blanim[i]][bldir[i]]))
        return;
    }
#endif
  }
}
