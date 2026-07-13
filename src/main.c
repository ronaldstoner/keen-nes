// Keen 4/5/6 NES port — MMC5 (iNES mapper 5 / ExROM) engine: 4-way scrolling
// camera over a converted Keen level with near-lossless per-8x8 ExRAM
// extended-attribute backgrounds. MIT License; see LICENSE.
//
// GLOBAL MMC5 BANKING CONVENTION (every mapper access relies on it):
// The reset stub (src/mmc5/reset_keen.s) programs PRG mode 3 (four 8KB banks:
// $8000 switchable via $5114, $A000/$C000/$E000 = the 24KB fixed region) and
// CHR 1KB mode. Engine bank-select calls go through the mmc5_compat.c shim:
// set_prg_bank(bank, 0x80) -> $5114, set_chr_mode_0..5() -> the CHR set-A regs
// $5120-$5127 (8x8 sprites make the PPU fetch BG *and* sprites from set A).
// Level blobs map at the $8000 switchable window; code that reads a blob must
// stay in the fixed region (banked $8000 code would unmap the blob under
// itself). WRAM ($6000-$7FFF: all C data + the soft stack) is unlocked once in
// the reset stub via $5102/$5103 + page $5113; nothing re-locks it at runtime.
#include <ines.h>
#include <mapper.h>
#include <neslib.h>
#include <nesdoug.h>
#include <peekpoke.h>
#include "level_fmt.h"
#include "gen/levels.h"
#include "gen/player.h"
#include "player.h"
#include "actors.h"
#include "sfx.h"
#include "gen/sfx.h"
#include "hud.h"
#include "title.h"
#include "music.h"
#include "gen/music.h"
#include "mmc5/mmc5.h"
#include "map.h"
#define ITEM_STRIDE 5u   // MMC5 item record: x,y,type,empty_mt(u16)

// PRG size generated into levels.h (PRG_ROM_KB: grows past 128KB when the
// level banks — which skip the reserved music/sfx/title banks 7-12 —
// spill past bank 6). Two-step expansion as for CHR below.
#define PRG_ROM_KB_X(kb) MAPPER_PRG_ROM_KB(kb)
PRG_ROM_KB_X(PRG_ROM_KB);
// 8KB PRG-RAM (WRAM at $6000-$7FFF): C .data/.bss/.noinit + soft stack
// live there (src/mmc5.ld includes c-in-prg-ram.ld), ending the 2KB
// internal-RAM squeeze. Advertised in the iNES header; NOT battery-backed
// (that's a later milestone via MAPPER_PRG_NVRAM_KB).
MAPPER_PRG_RAM_KB(8);
// CHR = per-level region banks + sprites + title; size generated into
// levels.h (TOTAL_CHR_KB, power of two). Two-step expansion so the macro
// argument is expanded before MAPPER_CHR_ROM_KB stringifies it.
#define CHR_ROM_KB_X(kb) MAPPER_CHR_ROM_KB(kb)
CHR_ROM_KB_X(TOTAL_CHR_KB);
MAPPER_USE_MIRRORED_NAMETABLE;
MAPPER_USE_VERTICAL_MIRRORING;

// camera in world pixels, clamped to [min, max] per axis
static unsigned int cam_x, cam_y;
// The PPU sees disp_cam, published after the matching nametable + ExRAM seam
// packet has committed during vblank.
static unsigned int disp_cam_x, disp_cam_y;
static unsigned int min_x, min_y, max_x, max_y;

// nesdoug vram buffer fill (bytes); the buffer caps at 128 and silently
// drops overflow, so seam drawing must yield when it runs hot (a missed
// NMI leaves the previous frame's batch unflushed)
extern volatile unsigned char VRAM_INDEX;
// Horizontal catch-up yields here. The ungated vertical seam handling runs
// FIRST (<=2 rows, ~74B) so this leaves room for it plus a column while keeping
// the buffer under the 128B cap (74 + one 34B column + backdrop < 128).
#define VRAM_BUDGET 96

// Last drawn tile window edges (8px tile coords). drawn_left is u16: Slug
// Village is 146 mt wide = 292 tile columns (>255); a u8 wraps and corrupts
// the horizontal seam once the camera passes column 256.
static unsigned int drawn_left;
static unsigned int drawn_top; // K5 maps reach 500 vertical tile rows
// World tile row whose seam-exit restore is deferred one frame (0xFFFF =
// none): see the vertical seam catch-up in the main loop.
static unsigned int seam_restore = 0xFFFFu;

// ---------------------------------------------------------------------------
// MMC5 ExRAM seam staging. The seam renderer writes nametable tile-ids
// through the nesdoug VRAM buffer as usual (flushed in NMI); the parallel
// per-8x8 ExRAM bytes are STAGED here during the loop body and flushed to
// $5C00 in the post-ppu_wait_nmi vblank window with the proven mode-flip
// ($5104=2 so out-of-frame writes land, then back to $5104=1). Offsets are
// the nametable coarse offset ntrow_of(ty)*32 + (tx & 31) = the exact key
// the PPU's extended-attribute fetch uses (vram_addr & 0x3FF).
// ---------------------------------------------------------------------------
#define EXS_MAX 192
// Staged ExRAM writes, split into precomputed byte arrays so the vblank flush
// is a straight (zp),y store with NO per-cell 16-bit pointer math. exs_lo/exs_hi
// ARE the target address bytes: lo = off & 0xFF, hi = 0x5C + (off>>8) folds the
// $5C00 ExRAM page base in at stage time. The old exram_flush() reconstructed
// 0x5C00+off from a u16 exs_off[] every iteration -- the -Oz codegen was ~75
// cyc/write; the asm blast (src/exram_blast.s) is ~33. NON-static + volatile so
// exram_blast.s can reference them and LTO can't dead-store the stages away
// (their only reader is the asm). See src/exram_blast.s.
volatile unsigned char exs_lo[EXS_MAX];
volatile unsigned char exs_hi[EXS_MAX];
volatile unsigned char exs_val[EXS_MAX];
volatile unsigned char exs_n;
// FAST CONTIGUOUS FILLS (the fix for grey-on-fast-fall): the vertical seam's two
// heavy pieces are each 32 CONSECUTIVE $5C00 cells, so exram_blast fills them with
// the pointer set ONCE + a y-loop (~11 cy/cell) instead of 32 slow singletons
// (~33 cy). Folding both out of the slow path stops a pure-vertical frame's flush
// from overrunning vblank -- the overrun left $5104 at 2 (RAM mode) into active
// render, so the whole background lost per-cell attributes and went GREY.
//  - blank row (blank_on): the overscan seam row = 32 cells of one constant.
//  - restore row(s) (row_n): the real row scrolling into view; draw_row scatters
//    its 32 cells (col order) into row_ex[slot], anim_add preserved. <=2/frame.
volatile unsigned char blank_lo, blank_hi, blank_val, blank_on;
volatile unsigned char row_ex[64];       // 2 slots x 32 cells
volatile unsigned char row_base_lo[2], row_base_hi[2], row_n;
// A restore COLUMN is 30 cells at STRIDE 32 (offset ntr*32+col, ntr 0..29).
// draw_column scatters them into col_ex[ntr]; exram_blast fills $5C00+col with a
// +32 stride y-loop (~15cy/cell vs 33). Halving the column flush keeps a DIAGONAL
// frame's vblank work (neslib nametable flush + this blast) under the ~2273cy
// budget -> $5104 back to 1 before render even when the game is <60fps. <=1
// restore column/frame (cam_x moves <=1 tile/frame); col_on=0 -> none.
volatile unsigned char col_ex[30], col_idx, col_on;
extern unsigned char exram_blast(void); // returns 1 after the bounded commit
static void exs_push(unsigned int off, unsigned char val) {
  if (exs_n < EXS_MAX) {
    exs_lo[exs_n] = (unsigned char)off;
    exs_hi[exs_n] = (unsigned char)(0x5Cu + (off >> 8));  // off <= 959 -> hi 0x5C..0x5F
    exs_val[exs_n] = val;
    ++exs_n;
  }
}

// ---------------------------------------------------------------------------
// Background tile animation (TILEINFO chains). An 8x8 cell animates
// iff its ExRAM bank is in [anim_base, anim_base+anim_nbanks): its F per-phase
// patterns sit in consecutive 4KB banks, so bank + anim_phase points at the
// current phase (palette bits untouched; the emit guarantees base+F-1 < 64 so
// the add never carries). We keep a compact list of the ON-SCREEN animated
// cells (their ExRAM address + phase-0 byte), maintained 1:1 with the seam
// ExRAM writes; every anim tick a rotating slice of the list is rewritten to
// phase anim_phase, staged through the same mode-flip flush.
// ---------------------------------------------------------------------------
// cold anim helpers bank to the HUD bank (6) via a trampoline (like the
// palette/cam-bounds cold helpers). MAIN_COLD + lvl_bank are (re)defined
// later too, guarded by #ifndef; hoisted here for the anim block.
#ifndef MAIN_COLD
#define MAIN_COLD __attribute__((noinline, section(".prg_rom_6.text")))
#endif
extern const unsigned char lvl_bank[]; // src/gen/leveldata_mmc5.c
#define IS_ANIM(ex) ((unsigned char)(((ex) & 0x3Fu) - anim_base) < anim_nbanks)
// Compact list of the ON-SCREEN animated cells: their ExRAM offset + phase-0
// byte. Maintained 1:1 with the seam ExRAM writes (a seam column/row deletes
// its nametable column/row's entries then re-adds the incoming cells' animated
// ones; a full redraw rebuilds it). The phase tick iterates just this list
// (~50 cells) instead of scanning the 960-cell screen -- keeps F:~60. Sized for
// the densest keen4 screen (Descendents Cave ~60 on-screen animated cells).
#define AMAX 128
// Each on-screen animated cell, stored as the PRE-ENCODED ExRAM address bytes
// (lo = off & 0xFF, hi = 0x5C + (off >> 8)) — exactly what anim_tick emits to
// exs_lo/exs_hi. Same 256 bytes as the old u16 anim_off[], but the anim_del
// scan compares single bytes (the u16 load+mask per entry was the cost) and
// the tick copies two bytes with no shift/add.
static unsigned char anim_lo[AMAX];      // ExRAM addr low byte of each cell
static unsigned char anim_hi[AMAX];      // ExRAM addr high byte (0x5C-based)
static unsigned char anim_e0[AMAX];      // its phase-0 ExRAM byte
static unsigned char anim_n;             // list length
static unsigned char anim_phase;         // global phase 0..anim_frames-1
static unsigned char anim_timer;         // frames until the next phase step
static unsigned char anim_cur;           // rotating refresh cursor into the list
// Cells refreshed per frame. Bounds the per-frame ExRAM flush (the seam already
// stages up to ~63; a 53-cell anim burst on top overran vblank -> the mode-flip
// tail bled into active render). A rotating slice covers the whole list within
// a few frames (< the phase period), so every cell reaches the current phase
// with at most a few frames' lag (a subtle roll).
#define ANIM_SLICE 16
// remove list entries matching the offset selector, keeping order (in-place
// compaction). key 0 => nametable COLUMN (off & 31)==v; 1 => nametable ROW
// (off>>5)==v; 2 => exact offset ==v. Fixed (WRAM-only: works from the seam's
// mt_bank context — no bank switch needed, unlike the bank-6 cold helpers).
__attribute__((noinline)) static void anim_del(unsigned char key,
                                               unsigned int v) {
  unsigned char i = 0;
  unsigned char t0, t1 = 0; // byte match targets, hoisted out of the scan:
  if (key == 0) {           // COLUMN: off&31 == v      <=> lo&31 == v
    t0 = (unsigned char)v;
  } else if (key == 1) {    // ROW: off>>5 == v (off = row*32+col)
    t0 = (unsigned char)(0x5Cu + ((unsigned char)v >> 3)); // hi == 5C+(v>>3)
    t1 = (unsigned char)(((unsigned char)v & 7u) << 5);    // lo&E0 == (v&7)<<5
  } else {                  // EXACT offset
    t0 = (unsigned char)v;
    t1 = (unsigned char)(0x5Cu + (v >> 8));
  }
  while (i < anim_n) {
    unsigned char hit;
    if (key == 0)
      hit = (unsigned char)(anim_lo[i] & 31u) == t0;
    else if (key == 1)
      hit = anim_hi[i] == t0 && (unsigned char)(anim_lo[i] & 0xE0u) == t1;
    else
      hit = anim_lo[i] == t0 && anim_hi[i] == t1;
    if (hit) {
      --anim_n;
      anim_lo[i] = anim_lo[anim_n];
      anim_hi[i] = anim_hi[anim_n];
      anim_e0[i] = anim_e0[anim_n];
      if (anim_cur > anim_n)
        anim_cur = 0;
    } else {
      ++i;
    }
  }
}
static void anim_add(unsigned int off, unsigned char ex) {
  if (anim_n < AMAX) {
    anim_lo[anim_n] = (unsigned char)off;
    anim_hi[anim_n] = (unsigned char)(0x5Cu + (off >> 8));
    anim_e0[anim_n] = ex;
    ++anim_n;
  }
}
// reset the list (a full redraw rebuilds it). Cold; banked to bank 6.
MAIN_COLD static void anim_reset_b(void) {
  anim_n = 0;
  anim_cur = 0;
  anim_phase = 0;
  anim_timer = anim_speed;
}
// refresh a rotating slice of the on-screen animated cells to the CURRENT phase
// (called every frame). Fills only the ExRAM-flush budget the seam left this
// frame (ANIM_FLUSH_CAP total = the seam's proven ~63-write vblank envelope):
// on a heavy diagonal-scroll frame the seam takes the budget and anim defers
// (its cursor doesn't advance -> those cells refresh a frame or two later),
// which keeps the flush within vblank AND holds F:~60. Bank 6; inline push.
#define ANIM_FLUSH_CAP 64
MAIN_COLD static void anim_tick_b(void) {
  unsigned char k, ph = anim_phase, n = exs_n, c = anim_cur, nn = anim_n;
  if (!nn)
    return;
  for (k = 0; k < ANIM_SLICE; ++k) {
    if (n >= ANIM_FLUSH_CAP)
      break;
    if (c >= nn)
      c = 0;
    exs_lo[n] = anim_lo[c];  // pre-encoded at anim_add — plain byte copies
    exs_hi[n] = anim_hi[c];
    exs_val[n] = (unsigned char)(anim_e0[c] + ph);
    ++n;
    ++c;
  }
  anim_cur = c;
  exs_n = n;
}
// bank-6 trampolines (map the HUD bank, run the cold body, restore the level
// bank). Both bodies touch only WRAM/exs, so no level-blob access is lost.
static void anim_reset(void) {
  set_prg_bank(6, 0x80);
  anim_reset_b();
  set_prg_bank(lvl_bank[g_level], 0x80);
}
static void anim_tick(void) {
  set_prg_bank(6, 0x80);
  anim_tick_b();
  set_prg_bank(lvl_bank[g_level], 0x80);
}
// stage one seam ExRAM cell: push it, and if it animates add it to the on-screen
// list (the seam's anim_del already cleared this column/row's old entries). A
// cell scrolling in shows phase 0 until the next phase step (<= anim_speed
// frames), hidden at the masked-left / overscan incoming edge.
static void stage_ex(unsigned int off, unsigned char ex) {
  if (IS_ANIM(ex))
    anim_add(off, ex);
  exs_push(off, ex);
}

typedef unsigned char map_t;

// shared seam scratch: static so the hot loops use absolute,x addressing
// instead of soft-stack pointers (column draw was ~8k cycles with locals)
static unsigned char seam_buf[35]; // 17 metatiles x 2 tiles + slack
static unsigned char seam_ex[35];  // parallel per-8x8 ExRAM bytes
static unsigned int seam_mt[19];   // u16 metatile indices for a seam strip

// ---------------------------------------------------------------------------
// VERTICAL-SCROLL OVERSCAN SEAM (30-row nametable + vertical mirroring). A
// 240px view over a 30-row (240px) nametable can show 30 full rows only when
// scroll_y is tile-aligned; with a fine-Y offset the PPU shows 30 full rows
// PLUS a partial 31st row whose coarse-Y wraps at 30 -> it ALIASES nametable
// row `coarse` (the top window row). That aliased row is always split across
// the top 8px and bottom 8px overscan and NEVER the visible 224px middle, so
// we keep it BLACK: its cells point at an all-zero CHR bank (SEAM_BLANK_EX,
// gen_mmc5_rom) whose pixels are all color 0 = backdrop = $0F black. This black
// overscan band eats zero visible playfield.
// The seam nametable row = ntrow_of(cam_y>>3); the draw paths blacken it as
// they touch it, and the vertical seam catch-up restores the row that leaves
// the seam position (below) so a black row never scrolls into the middle.
static unsigned char seam_ntr;       // nametable row (0..29) kept black this frame
// gem-door/switch cold code banked to bank 26 (WRAM-only, frees the fixed region)
#define GEM_BANK26 __attribute__((noinline, section(".prg_rom_26.text")))

// picked items: bitmap over item-TABLE INDICES (the record's empty_mt
// byte supplies the overlay art, so no coordinate list is needed).
// 288-bit capacity covers K4's densest converted demo level: 282 records in
// L4 after its 112 collectible lifewater-drop tiles are represented faithfully.
static unsigned char picked_bm[36];
static const unsigned char bit8[8] = {1, 2, 4, 8, 16, 32, 64, 128};
#define PICKED(i) (picked_bm[(i) >> 3] & bit8[(i)&7])
// Bounding box of cells actually picked in this level. Seam strips outside it
// cannot possibly need an empty-cell overlay, so they skip the item-table
// lower-bound search (and, before the first pickup, its bank switch) entirely.
static unsigned char picked_any, picked_min_x, picked_max_x;
static unsigned char picked_min_y, picked_max_y;

// item-scan cursor: index of the first item with x >= Keen's window left
// edge (the item table is x-sorted). Amortized O(1) per frame; the old
// full-table scan cost ~3k cycles/frame on item-heavy maps (Bloogwaters:
// 57 items) and busted the frame budget on 70Hz double-tic frames.
static unsigned int item_lo;
// Previous player origin for swept pickup collision.  A frame can move several
// pixels (and an overloaded frame used to make that visually look larger), so
// testing only the final box can tunnel past a collectible.  The previous and
// current boxes form a conservative swept box; present_screen invalidates it
// across respawns/doors so a teleport can never collect a path of items.
static unsigned int item_prev_x, item_prev_y;
static unsigned char item_prev_valid;
static unsigned int item_sx0, item_sy0, item_sx1, item_sy1;

MAIN_COLD static unsigned char cell_visible_b(unsigned int tx,
                                               unsigned int ty) {
  return !(tx + 1u < drawn_left || tx > drawn_left + 32u ||
           ty + 1u < drawn_top || ty > drawn_top + 29u);
}

// Pure-WRAM pickup preparation lives in the cold bank.  Keeping the swept-box
// arithmetic out of item_pickup matters because that function must remain in
// fixed ROM while the level item table occupies the $8000 bank.
MAIN_COLD static void item_sweep_b(void) {
  item_sx0 = pl_x; item_sy0 = pl_y;
  item_sx1 = pl_x + 240u; item_sy1 = pl_y + 496u;
  if (item_prev_valid) {
    if (item_prev_x < item_sx0) item_sx0 = item_prev_x;
    if (item_prev_y < item_sy0) item_sy0 = item_prev_y;
    if (item_prev_x + 240u > item_sx1) item_sx1 = item_prev_x + 240u;
    if (item_prev_y + 496u > item_sy1) item_sy1 = item_prev_y + 496u;
  }
  item_prev_x = pl_x; item_prev_y = pl_y; item_prev_valid = 1;
}
static void item_sweep(void) {
  set_prg_bank(6, 0x80);
  item_sweep_b();
  set_prg_bank(lvl_bank[g_level], 0x80);
}

// overlay picked-item replacement cells onto a fetched strip of cells:
// is_row: strip runs along x at row 'fixed'; else along y at column 'fixed'.
// The item table is x-sorted: binary-search the first record in the
// strip's x-range (~8 u8 iterations), then walk while x <= x1 testing the
// picked bit — bounded work per strip regardless of how much was picked.
// MMC5: item records are 5 bytes (empty_mt is u16) and the strip holds u16
// metatile indices (seam_mt). Overlays each picked item's empty-cell metatile
// index onto the fetched strip.
GEM_BANK26 static void apply_picked_b(unsigned char is_row, unsigned char fixed,
                                      unsigned char base, unsigned char nmt) {
  unsigned int lo = 0, hi = g_nitems;
  unsigned char x0, x1;
  const unsigned char *it;
  if (is_row) {
    x0 = base;
    x1 = (unsigned char)(base + nmt - 1);
  } else {
    x0 = fixed;
    x1 = fixed;
  }
  if (x1 < picked_min_x || x0 > picked_max_x)
    return;
  if (is_row) {
    if (fixed < picked_min_y || fixed > picked_max_y)
      return;
  } else {
    unsigned char y1 = (unsigned char)(base + nmt - 1);
    if (y1 < picked_min_y || base > picked_max_y)
      return;
  }
  while (lo < hi) { // lower bound: first item with x >= x0
    unsigned int mid = lo + ((hi - lo) >> 1);
    if (g_items[mid * 5u] < x0)
      lo = mid + 1;
    else
      hi = mid;
  }
  it = g_items + ((unsigned int)lo * 5u);
  for (; lo < g_nitems; ++lo, it += 5) {
    unsigned char d;
    if (it[0] > x1)
      break; // x-sorted: past the strip
    if (!PICKED(lo))
      continue;
    if (is_row) {
      if (it[1] != fixed)
        continue;
      d = (unsigned char)(it[0] - base);
    } else {
      d = (unsigned char)(it[1] - base);
    }
    if (d < nmt)
      seam_mt[d] = (unsigned int)it[3] | ((unsigned int)it[4] << 8);
  }
}

// This is seam-only work over WRAM (entity arena + picked bitmap + seam_mt),
// so keep its sizeable u16 search body out of the critically-full fixed ROM.
static void apply_picked(unsigned char is_row, unsigned char fixed,
                         unsigned char base, unsigned char nmt) {
  if (!picked_any)
    return;
  set_prg_bank(26, 0x80);
  apply_picked_b(is_row, fixed, base, nmt);
  set_prg_bank(lvl_bank[g_level], 0x80);
}

// Pickup-only bookkeeping is cold and pure WRAM, so keep its comparisons in
// bank 26 rather than spending the last fixed-region bytes on the seam guard.
GEM_BANK26 static void picked_note_b(unsigned char x, unsigned char y) {
  if (!picked_any) {
    picked_any = 1;
    picked_min_x = picked_max_x = x;
    picked_min_y = picked_max_y = y;
    return;
  }
  if (x < picked_min_x) picked_min_x = x;
  if (x > picked_max_x) picked_max_x = x;
  if (y < picked_min_y) picked_min_y = y;
  if (y > picked_max_y) picked_max_y = y;
}

// ty -> nametable row (0..29) = ty mod 30, as a 256-entry low-byte LUT (replaces the
// old subtract loop on the seam hot path — nt_addr/draw_row/draw_column/
// set_scroll all hit it). The shipped Keen 4 levels top out at 84 metatile
// rows, so every valid 8px world row is <168; 180 leaves seam lookahead room.
// K5 reaches 500 tile rows: 256 mod 30 = 16, so a set high byte adds 16
// modulo 30 after the lookup. The table lives in fixed rodata: WRAM is full
// (ms_wram alone is 4.5KB), so a boot-computed WRAM copy would overflow it.
static const unsigned char ntrow_lut[
#if EPISODE == 5
256
#else
180
#endif
] = {
     0, 1, 2, 3, 4, 5, 6, 7, 8, 9,10,11,12,13,14,15,
    16,17,18,19,20,21,22,23,24,25,26,27,28,29, 0, 1,
     2, 3, 4, 5, 6, 7, 8, 9,10,11,12,13,14,15,16,17,
    18,19,20,21,22,23,24,25,26,27,28,29, 0, 1, 2, 3,
     4, 5, 6, 7, 8, 9,10,11,12,13,14,15,16,17,18,19,
    20,21,22,23,24,25,26,27,28,29, 0, 1, 2, 3, 4, 5,
     6, 7, 8, 9,10,11,12,13,14,15,16,17,18,19,20,21,
    22,23,24,25,26,27,28,29, 0, 1, 2, 3, 4, 5, 6, 7,
     8, 9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,
    24,25,26,27,28,29, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9,
    10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,
    26,27,28,29
#if EPISODE == 5
    ,
     0, 1, 2, 3, 4, 5, 6, 7, 8, 9,10,11,12,13,14,15,
    16,17,18,19,20,21,22,23,24,25,26,27,28,29, 0, 1,
     2, 3, 4, 5, 6, 7, 8, 9,10,11,12,13,14,15,16,17,
    18,19,20,21,22,23,24,25,26,27,28,29, 0, 1, 2, 3,
     4, 5, 6, 7, 8, 9,10,11,12,13,14,15
#endif
};
static unsigned char ntrow_of(unsigned int ty) {
  unsigned char r = ntrow_lut[(unsigned char)ty];
#if EPISODE == 5
  if (ty & 0x100u) {
    r += 16u; // 256 mod 30
    if (r >= 30u)
      r -= 30u;
  }
#endif
  return r;
}

// nametable VRAM address of world 8px-tile (tx may exceed 255 on wide maps)
static unsigned int nt_addr(unsigned int tx, unsigned int ty) {
  unsigned int nt = (tx & 32u) ? 0x400u : 0;
  return 0x2000u + nt + ((unsigned int)ntrow_of(ty) << 5) + (tx & 31u);
}


// Scatter a restore column's 30 ExRAM cells (decoded into seam_ex) into col_ex,
// indexed by nametable row ntr, for exram_blast's stride-32 fast fill. anim_add
// preserved. WRAM-only -> bank 6. The seam-row cell is already SEAM_BLANK_EX in
// seam_ex (draw_column sets it), so the blank is carried through.
MAIN_COLD static void col_ex_stage_b(unsigned char col, unsigned int ty) {
  unsigned char exi = (unsigned char)(ty & 1);
  unsigned char ntr = ntrow_of(ty);
  unsigned char i;
  anim_del(0, col);
  for (i = 0; i < 30u; ++i) {
    unsigned char ex = seam_ex[exi++];
    if (IS_ANIM(ex))
      anim_add((unsigned int)ntr * 32u + col, ex);
    col_ex[ntr] = ex;
    if (++ntr >= 30u)
      ntr = 0;
  }
  col_idx = col;
  col_on = 1;
}
static void col_ex_stage(unsigned char col, unsigned int ty) {
  if (!col_on) {
    set_prg_bank(6, 0x80);
    col_ex_stage_b(col, ty);
    set_prg_bank(lvl_bank[g_level], 0x80);
  } else {
    // >1 restore column this frame (rare catch-up): slow per-cell path.
    unsigned char exi = (unsigned char)(ty & 1);
    unsigned char ntr = ntrow_of(ty);
    unsigned char i;
    anim_del(0, col);
    for (i = 0; i < 30u; ++i) {
      stage_ex((unsigned int)ntr * 32u + col, seam_ex[exi++]);
      if (++ntr >= 30u)
        ntr = 0;
    }
  }
}
// MMC5 draw one full tile column (world 8px-tile x = tx): emit nametable
// tile-ids through the VRAM buffer AND stage the parallel per-8x8 ExRAM
// bytes at the matching coarse offsets. Left/right subtile fields of the
// metatile are selected by tx parity. tx is u16 (wide maps >255 columns).
static void draw_column(unsigned int tx) {
  unsigned int ty = cam_y >> 3;
  unsigned char my0 = (unsigned char)(ty >> 1);
  unsigned char mx = (unsigned char)(tx >> 1);
  unsigned char odd = (unsigned char)(tx & 1u);
  unsigned char kt = odd ? 1 : 0;   // tr : tl  (top subtile field)
  unsigned char kb = odd ? 3 : 2;   // br : bl  (bottom subtile field)
  unsigned char col = (unsigned char)(tx & 31u);
  unsigned char nmt, i;
  const unsigned char *src;
  unsigned char remaining;
  unsigned int tyy;
  if (tx >= (unsigned int)g_w * 2u)
    return;
  nmt = 16;
  if ((unsigned char)(my0 + nmt) > g_h)
    nmt = g_h - my0;
  map_col_read16(mx, my0, nmt, seam_mt);  // map_cell readers apply gem-door overrides
  apply_picked(0, mx, my0, nmt);
  // decode top/bottom subtiles for all nmt cells with the MT bank mapped once
  mmc5_seam_decode(kt, kb, nmt, seam_mt, seam_buf, seam_ex);
  for (i = (unsigned char)(nmt << 1); i < 32; ++i) {
    seam_buf[i] = 0;
    seam_ex[i] = 0;
  }
  // The seam row (top window row, ntrow_of(ty) == ntrow_of(cam_y>>3)) is the
  // FIRST cell of every column strip and is always in the top/bottom overscan.
  // Point its ExRAM at the all-zero bank so it renders BLACK (any tile through a
  // zero CHR bank = all color 0 = backdrop $0F); the nametable tile stays real,
  // so restoring the cell (when the seam moves) needs only its ExRAM back. This
  // keeps horizontal scroll from leaving the aliased wrap row visible.
  seam_ex[ty & 1] = SEAM_BLANK_EX;
  // emit 30 tiles from strip offset (ty & 1), split at the NT row wrap
  src = seam_buf + (ty & 1);
  remaining = 30;
  tyy = ty;
  while (remaining) {
    unsigned char run = 30 - ntrow_of(tyy);
    if (run > remaining)
      run = remaining;
    multi_vram_buffer_vert((const char *)src, run, nt_addr(tx, tyy));
    src += run;
    tyy += run;
    remaining -= run;
  }
  // stage the 30 cells' ExRAM via the stride-32 fast fill (bank 6). col fixed,
  // ntrow wraps at 30; anim_del + anim_add handled in col_ex_stage.
  col_ex_stage(col, ty);
}


// MMC5 build the row strip for world row ty at the camera's left seam
// column: fills seam_buf (nametable tile-ids) AND seam_ex (per-8x8 ExRAM
// bytes) for 17 metatiles (34 subtiles). Left/right subtile fields chosen
// by ty parity. Shared by draw_row and draw_screen_full.
static void build_row_strip(unsigned int ty) {
  unsigned char mx0 = (unsigned char)((cam_x >> 3) >> 1);
  unsigned char my = (unsigned char)(ty >> 1);
  unsigned char lowbit = (unsigned char)(ty & 1);
  unsigned char kl = lowbit ? 2 : 0;   // bl : tl  (left subtile field)
  unsigned char kr = lowbit ? 3 : 1;   // br : tr  (right subtile field)
  unsigned char nmt, i;
  nmt = 17;
  if ((unsigned char)(mx0 + nmt) > g_w)
    nmt = g_w - mx0;
  map_row_read16(mx0, my, nmt, seam_mt);  // map_cell readers apply gem-door overrides
  apply_picked(1, my, mx0, nmt);
  // decode left/right subtiles for all nmt cells with the MT bank mapped once
  mmc5_seam_decode(kl, kr, nmt, seam_mt, seam_buf, seam_ex);
  for (i = (unsigned char)(nmt << 1); i < 34; ++i) {
    seam_buf[i] = 0;
    seam_ex[i] = 0;
  }
}

// draw one full tile row: nametable via the VRAM buffer + stage ExRAM
// noinline: the vertical seam catch-up has 4 call sites; a single shared copy
// keeps the near-full fixed region from overflowing (LTO would otherwise inline).
// Cheap blacken of the overscan seam row (nametable row seam_ntr): its real tiles
// are never seen in the top/bottom overscan, so we skip the map read + nametable
// redraw a full draw_row did and just record its 32 contiguous ExRAM cells
// (base..base+31) as an all-zero-CHR blank for exram_blast's fast fill. WRAM-only
// -> bank 6. Drops the row's anim entries so the phase tick can't un-blacken it.
MAIN_COLD static void blacken_seam_row_b(void) {
  unsigned int base = (unsigned int)seam_ntr * 32u;
  anim_del(1, seam_ntr);
  blank_lo = (unsigned char)base;
  blank_hi = (unsigned char)(0x5Cu + (base >> 8));
  blank_val = SEAM_BLANK_EX;
  blank_on = 1;
}
static void blacken_seam_row(void) {
  set_prg_bank(6, 0x80);
  blacken_seam_row_b();
  set_prg_bank(lvl_bank[g_level], 0x80);
}
// Scatter a restore row's 32 ExRAM cells (decoded into seam_ex by build_row_strip)
// into a fast-fill slot: row_ex[slot][col] for offset ntr*32+col. anim_add is
// preserved (animated cells scrolling in still register). WRAM-only -> bank 6.
MAIN_COLD static void row_ex_stage_b(unsigned char ntr, unsigned char tx) {
  unsigned int base = (unsigned int)ntr * 32u;
  volatile unsigned char *rex = row_ex + ((unsigned int)row_n << 5);
  unsigned char exi = (unsigned char)(tx & 1);
  unsigned char txx = tx;
  unsigned char i;
  anim_del(1, ntr);
  for (i = 0; i < 32u; ++i) {
    unsigned char ex = seam_ex[exi++];
    unsigned char col = (unsigned char)(txx & 31);
    if (IS_ANIM(ex))
      anim_add(base + col, ex);
    rex[col] = ex;
    ++txx;
  }
  row_base_lo[row_n] = (unsigned char)base;
  row_base_hi[row_n] = (unsigned char)(0x5Cu + (base >> 8));
  ++row_n;
}
static void row_ex_stage(unsigned char ntr, unsigned char tx) {
  if (row_n < 2u) {
    set_prg_bank(6, 0x80);
    row_ex_stage_b(ntr, tx);
    set_prg_bank(lvl_bank[g_level], 0x80);
  }
}
// draw_row handles RESTORE rows only (real content scrolling into the visible
// playfield); the overscan seam row is blackened by blacken_seam_row with no
// nametable redraw. ExRAM lands via the row_ex fast-fill scatter.
__attribute__((noinline)) static void draw_row(unsigned int ty) {
  unsigned int tx = cam_x >> 3;
  const unsigned char *src;
  unsigned char remaining;
  unsigned int txx;
  if (ty >= (unsigned int)g_h * 2u)
    return;
  build_row_strip(ty);             // real tiles + ExRAM
  src = seam_buf + (tx & 1u);
  remaining = 33;
  txx = tx;
  while (remaining) {
    unsigned char run = (unsigned char)(32u - (txx & 31u));
    if (run > remaining)
      run = remaining;
    multi_vram_buffer_horz((const char *)src, run, nt_addr(txx, ty));
    src += run;
    txx += run;
    remaining -= run;
  }
  row_ex_stage(ntrow_of(ty), (unsigned char)tx);
}



// MMC5 full redraw with rendering off: writes nametable tile-ids (direct
// $2007) AND ExRAM bytes (mode-2 so out-of-frame writes land) for the whole
// 32x30 screen, then returns ExRAM to extended-attribute mode 1.
static void draw_screen_full(void) {
  unsigned int ty, tx, left = cam_x >> 3;
  unsigned int top = cam_y >> 3;
  anim_reset();                     // rebuild the on-screen animated-cell list from scratch
  seam_ntr = ntrow_of(top);         // top window row = overscan seam (kept black)
  MMC5_EXRAM_MODE = MMC5_EXRAM_RAM;
  for (ty = top; ty < top + 30u && ty < (unsigned int)g_h * 2u; ++ty) {
    const unsigned char *src, *sex;
    unsigned char ntr = ntrow_of(ty), k;
    build_row_strip(ty);
    if (ntr == seam_ntr)            // aliased overscan seam row -> ExRAM black
      for (k = 0; k < 34; ++k)
        seam_ex[k] = SEAM_BLANK_EX;  // (real tiles stay in seam_buf)
    src = seam_buf + (left & 1u);
    sex = seam_ex + (left & 1u);
    k = 0;
    for (tx = left; tx < left + 33u && tx < (unsigned int)g_w * 2u; ++tx, ++k) {
      unsigned int off = (unsigned int)ntr * 32u + (tx & 31u);
      unsigned char ex = sex[k];
      vram_adr(nt_addr(tx, ty));
      vram_put(src[k]);
      if (IS_ANIM(ex)) {
        anim_add(off, ex);                        // rebuild on-screen list
        ex = (unsigned char)(ex + anim_phase);    // phase 0 here (fresh reset)
      }
      MMC5_EXRAM[off] = ex;
    }
  }
  MMC5_EXRAM_MODE = MMC5_EXRAM_EXATTR;
  drawn_left = left;
  drawn_top = top;
  seam_restore = 0xFFFFu; // full redraw: no deferred seam-exit restore pending
}

// Bank-6 cold-code helpers (also re-declared with cam_bounds below; the
// identical macro + extern are hoisted here for the palette/death helpers).
extern const unsigned char lvl_bank[]; // src/gen/leveldata_mmc5.c
#ifndef MAIN_COLD
#define MAIN_COLD __attribute__((noinline, section(".prg_rom_6.text")))
#endif

// write palettes directly: neslib's pal_bg/pal_spr push colors through a
// brightness LUT that remaps $3D->$20 and $20->$10, wrecking whites/greys.
// Body banked to bank 6 (WRAM/VRAM only, no level-blob read): cold
// (present_screen after a load/respawn), so it relieves the near-full
// fixed region. The trampoline restores the level bank for the
// draw_screen_full that follows in present_screen.
MAIN_COLD static void set_palettes_raw_b(const unsigned char *bg,
                                         const unsigned char *spr) {
  unsigned char i;
  vram_adr(0x3F00);
  for (i = 0; i < 16; ++i)
    vram_put(bg[i]);
  for (i = 0; i < 16; ++i)
    vram_put(spr[i]);
  vram_adr(0x0000);
}
static void set_palettes_raw(const unsigned char *bg,
                             const unsigned char *spr) {
  set_prg_bank(6, 0x80);
  set_palettes_raw_b(bg, spr);
  set_prg_bank(lvl_bank[g_level], 0x80);
}

// mark an item-table entry picked and rewrite its tiles + ExRAM in VRAM
// (shared by item pickup and the EP5 fuse-break path)
// Rewrite metatile cell (mtx,mty) in the visible nametable + ExRAM to metatile
// index mi. On-screen guard: nt_addr wraps mod 32/30, so writing an off-screen
// cell would corrupt an unrelated VISIBLE cell — skip it (the seam renderer
// redraws the true content when that cell scrolls in). Shared by item pickup,
// gem-door open, and switch toggle.
static void cell_write(unsigned int mtx, unsigned int mty, unsigned int mi) {
  unsigned int tx = mtx << 1, ty = mty << 1;
  unsigned char col = (unsigned char)(tx & 31);
  unsigned char r0, r1;
  unsigned char top2[2], bot2[2], topex[2], botex[2];
  unsigned int o0, o1, o2, o3;
  // nt_addr wraps at 32 columns / 30 rows.  Never let an off-resident logical
  // cell alias and overwrite an unrelated visible cell; its picked/override
  // state is authoritative and the seam renderer will apply it when streamed.
  {
    unsigned char visible;
    set_prg_bank(6, 0x80);
    visible = cell_visible_b(tx, ty);
    set_prg_bank(lvl_bank[g_level], 0x80);
    if (!visible) return;
  }
  r0 = ntrow_of(ty); r1 = ntrow_of(ty + 1);
  mmc5_seam_decode(0, 1, 1, &mi, top2, topex);     // tl,tr + tl_ex,tr_ex
  mmc5_seam_decode(2, 3, 1, &mi, bot2, botex);     // bl,br + bl_ex,br_ex
  o0 = (unsigned int)r0 * 32u + col;
  o1 = (unsigned int)r0 * 32u + (unsigned char)((col + 1) & 31);
  o2 = (unsigned int)r1 * 32u + col;
  o3 = (unsigned int)r1 * 32u + (unsigned char)((col + 1) & 31);
  multi_vram_buffer_horz((const char *)top2, 2, nt_addr(tx, ty));
  multi_vram_buffer_horz((const char *)bot2, 2, nt_addr(tx, ty + 1));
  anim_del(2, o0); anim_del(2, o1); anim_del(2, o2); anim_del(2, o3);
  stage_ex(o0, topex[0]); stage_ex(o1, topex[1]);
  stage_ex(o2, botex[0]); stage_ex(o3, botex[1]);
}
static void cell_pick(unsigned int idx) {
  const unsigned char *it = g_items + ((unsigned int)idx * 5u);
  picked_bm[idx >> 3] |= bit8[idx & 7];
  set_prg_bank(26, 0x80);
  picked_note_b(it[0], it[1]);
  set_prg_bank(lvl_bank[g_level], 0x80);
  cell_write(it[0], it[1],
             (unsigned int)it[3] | ((unsigned int)it[4] << 8)); // empty_mt
}

// ---- gem doors + switches (place a gem -> open its door; press a switch -> toggle its target) ----
// gem_place/switch_flip register cell overrides (level.c ov_*): map_cell() and the
// seam readers then substitute the new metatile, so BOTH collision (open tiles are
// non-solid -> passable) AND rendering follow with no player.c/seam changes. The
// one-shot visual refresh is a present_screen() forced-blank redraw (main-loop
// handler). These touch only WRAM -> banked to cold bank 26 (frees the fixed
// region); handle_gem_switch_door maps bank 26 around the calls.
static unsigned char gd_done_bm;                 // holders already placed
static unsigned char sw_on_bm;                   // switches flipped (block off)
static unsigned char sw_blk[MMC5_MAX_SW];        // resolved g_blocks slot (0xFF=?)
static unsigned char sw_bx[MMC5_MAX_SW], sw_by[MMC5_MAX_SW];
static unsigned char sw_ov[MMC5_MAX_SW];         // switch cell's ov slot (0xFF=?)

// reset per-level gem-door/switch runtime state (after level_load). Cold
// (once per level load / respawn) and WRAM-only (ov_reset is a fixed-region
// level.c helper, so it stays callable), so the body banks to cold bank 26 and
// only a tiny fixed trampoline remains — relieving the near-full fixed region.
GEM_BANK26 static void door_state_reset_b(void) {
  unsigned char i;
  ov_reset(); gd_done_bm = 0; sw_on_bm = 0;
  for (i = 0; i < MMC5_MAX_SW; ++i)
    sw_blk[i] = sw_ov[i] = 0xFF;
}
static void door_state_reset(void) {
  set_prg_bank(26, 0x80);
  door_state_reset_b();
  set_prg_bank(lvl_bank[g_level], 0x80);
}

// place the matching gem at holder gi (WRAM only). Registers the cell overrides
// (holder placed art + open door column) and returns the number of ov cells
// added (0 = already done); the caller cell_write's [ov_n-added .. ov_n).
GEM_BANK26 static unsigned char gem_place(unsigned char gi) {
  const unsigned char *r = GD_REC(gi);
  unsigned char k, dh = r[7];
  unsigned int openm = r[8] | ((unsigned int)r[9] << 8);
  if (gd_done_bm & bit8[gi])
    return 0;
  gd_done_bm |= bit8[gi];
  pl_keys &= (unsigned char)~(1u << r[2]);           // consume the matching gem
  ov_add(r[0], r[1], r[3] | ((unsigned int)r[4] << 8));  // holder -> placed art
  for (k = 0; k < dh; ++k)
    ov_add(r[5], (unsigned char)(r[6] + k), openm);      // open the door column
  return (unsigned char)(1 + dh);
}

// flip switch si: toggle its art + the target B-block marker (info ^= 0x1F). The
// player doesn't collide with B-blocks (they gate enemy platforms), so no shipped
// switch gates a player route. Returns the ov slot of the switch cell so the
// caller redraws it.
GEM_BANK26 static unsigned char switch_flip(unsigned char si) {
  const unsigned char *r = SW_REC(si);               // sx,sy,off(u16),on(u16),tx,ty
  unsigned char on = sw_on_bm & bit8[si];
  unsigned char tx = r[6], ty = r[7];
  unsigned int mt;
  unsigned char *bp = (unsigned char *)g_blocks;
  if (sw_blk[si] == 0xFF) {                          // resolve target slot once
    unsigned char j;
    for (j = 0; j < g_nblocks; ++j)
      if (bp[(unsigned int)j * 2u] == tx && bp[(unsigned int)j * 2u + 1] == ty) {
        sw_blk[si] = j; break;
      }
    sw_bx[si] = tx; sw_by[si] = ty;
  }
  if (sw_blk[si] != 0xFF) {
    unsigned int bo = (unsigned int)sw_blk[si] * 2u;
    if (on) { bp[bo] = sw_bx[si]; bp[bo + 1] = sw_by[si]; } // restore block
    else    { bp[bo] = 0xFF;      bp[bo + 1] = 0xFF; }      // remove block
  }
  sw_on_bm ^= bit8[si];
  mt = on ? (r[2] | ((unsigned int)r[3] << 8))       // going back to off-art
          : (r[4] | ((unsigned int)r[5] << 8));      // flipping to on-art
  if (sw_ov[si] == 0xFF) { sw_ov[si] = ov_n; ov_add(r[0], r[1], mt); }
  else ov_mt[sw_ov[si]] = mt;
  return sw_ov[si];
}

// camera bounds for the loaded level. Keep a 2-tile (32px) scroll margin
// inside the map's border ring of filler tiles on ALL four sides; without it
// the ring shows as garbage bars wherever the view reaches a map edge (our
// 240px view exposes more of it than DOS's 200px view did). Degenerate maps
// (too small for view+margin) pin the camera to the centered midpoint instead.
//
// Cold (level load / respawn only) and pure-WRAM (g_w/g_h in, min/max out —
// no blob deref), so it lives in the always-present HUD bank (6) to keep
// the near-full fixed region; a fixed trampoline maps the bank and restores
// the level bank. Reclaimed ~150B fixed for the per-band v2/v3 growth
// (keen4's fixed region is the tightest).
extern const unsigned char lvl_bank[]; // src/gen/leveldata_mmc5.c
#ifndef MAIN_COLD // (also defined earlier for the palette/death cold helpers)
#define MAIN_COLD __attribute__((noinline, section(".prg_rom_6.text")))
#endif
static unsigned int bnd_min, bnd_max;
MAIN_COLD static void cam_bound1(unsigned int m) {
  bnd_min = 32u;
  if (m >= 64u)
    m -= 32u;
  else
    bnd_min = m >>= 1; // degenerate: pin to the centered midpoint
  bnd_max = m;
}
MAIN_COLD static void cam_bounds_b(void) {
  cam_bound1(((unsigned int)g_w << 4) - 256u);
  min_x = bnd_min;
  max_x = bnd_max;
  cam_bound1(((unsigned int)g_h << 4) - 240u);
  min_y = bnd_min;
  max_y = bnd_max;
}
static void cam_bounds(void) { // fixed trampoline
  set_prg_bank(6, 0x80);
  cam_bounds_b();
  set_prg_bank(lvl_bank[g_level], 0x80);
}

// clamped camera target for the current player position (pl_look_off =
// the look up/down peek, riding the same clamped path). Shared by cam_center
// (respawn/teleport snap) and the per-frame rate-limited follow in the main loop.
static unsigned int cam_tx, cam_ty;
static unsigned char cam_snap; // cam_center: center even while airborne
// pure-WRAM (player pos + bounds -> cam target). Banked to the HUD bank (6) via
// a trampoline to relieve the near-full fixed region (the background-animation
// restore needed ~130B).
MAIN_COLD static void cam_target_b(void)
{
  unsigned int px = pl_x >> 4;
  unsigned int t = (px > 124u) ? px - 124u : 0;
  unsigned int feet;
  if (t < min_x) t = min_x;
  if (t > max_x) t = max_x;
  cam_tx = t;
  // Vertical: track Keen's FEET, and only while he is SUPPORTED (ground /
  // pole / ledge; the map walker is always grounded) — jump and pogo arcs
  // hold the camera still, with a hard push only when the feet leave the
  // [36,172]px screen window. Feet rest at 143px from the frame top; the
  // look peek slides that to 170 (up) / 36 (down) at 1px/tic.
  feet = (pl_y + 496u) >> 4; // box bottom (KEEN_CLIP_YH; map keen: +31px)
  if (cam_snap || g_on_map || pl_on_ground || pl_pole || pl_ledge) {
    t = (feet > 143u) ? feet - 143u : 0;
    if (pl_look_off) {
      int t2 = (int)t + pl_look_off;
      t = (t2 < 0) ? 0 : (unsigned int)t2;
    }
  } else {
    t = cam_ty; // airborne: hold, except the push zones
    if (feet < t + 36u)
      t = (feet > 36u) ? feet - 36u : 0;
    else if (feet > t + 172u)
      t = feet - 172u;
  }
  if (t < min_y) t = min_y;
  if (t > max_y) t = max_y;
  cam_ty = t;
}
static void cam_target(void) {
  set_prg_bank(6, 0x80);
  cam_target_b();
  set_prg_bank(lvl_bank[g_level], 0x80);
}

// Per-frame camera easing is pure WRAM and too bulky for the saturated fixed
// bank.  It is separate from cam_target because cam_center needs an instant
// snap during loads/doors.
MAIN_COLD static void cam_step_b(void) {
  if (cam_x + 8u < cam_tx) cam_x += 8u;
  else if (cam_x > cam_tx + 8u) cam_x -= 8u;
  else cam_x = cam_tx;
  if (cam_y + 7u < cam_ty) cam_y += 7u;
  else if (cam_y > cam_ty + 7u) cam_y -= 7u;
  else cam_y = cam_ty;
}
static void cam_step(void) {
  set_prg_bank(6, 0x80);
  cam_step_b();
  set_prg_bank(lvl_bank[g_level], 0x80);
}

// center camera on the player, clamped to the level (spawn/respawn/door cut:
// center vertically even mid-air — the airborne hold must not pin a stale y)
static void cam_center(void) {
  cam_snap = 1;
  cam_target();
  cam_snap = 0;
  cam_x = cam_tx;
  cam_y = cam_ty;
}

// hardware scroll from the camera. X wraps the two horizontal nametables
// (512px). Y must be the mod-240 nametable position with the NT-Y bit
// clear: neslib's scroll() only accepts y < 480, and its y >= 240 path
// collapses through 8-bit math — for cam_y in [720,735] it emits the
// ILLEGAL scroll values 240-255 (a real PPU fetches attribute bytes as
// tile indices: garbage bars), and past 735 values that are simply wrong
// mod 240 (background shifted 16px against the 32px attr grid = every
// other metatile row wearing the wrong palette).
// ntrow_of already implements the engine's mod-30 row space.
static __attribute__((noinline)) void set_scroll(void) {
  unsigned char y = (unsigned char)(ntrow_of(disp_cam_y >> 3) << 3) |
                    ((unsigned char)disp_cam_y & 7u);
  scroll(disp_cam_x & 0x1FF, y);
}

// full redraw with rendering off: after level load or player respawn
static void present_screen(void) {
  ppu_off();                  // FIRST: CHR bank swaps tear mid-frame on
  level_region_update(cam_x); // real PPUs, so pick the region and
  level_chr_refresh();        // re-assert R0/R1 (the status screen
                              // borrows them) only once rendering is off
  set_palettes_raw(g_pal, spr_pal);
  exs_n = 0;                   // discard any staged seam ExRAM (full redraw)
  blank_on = 0;                // ...and any pending blank / restore-row/col fills
  row_n = 0;
  col_on = 0;
  draw_screen_full();         // mode-2 preload of nametable + ExRAM -> mode 1
  disp_cam_x = cam_x;
  disp_cam_y = cam_y;
  item_prev_valid = 0;
  set_scroll();
  ppu_on_all();
  ppu_mask(MASK_BG | MASK_SPR); // $18: mask left 8px (proven ExRAM seam rule)
}

extern const unsigned char lvl_game_no[]; // ROM slot -> game-level number

// Map <-> stage: fade palettes to black, ppu_off, then level_load/present.
// Body in bank 6 (WRAM/VRAM only).
MAIN_COLD static void screen_fadeout_b(void) {
  unsigned char step, i;
  unsigned char p[32];
  for (i = 0; i < 16; ++i) {
    p[i] = g_pal[i];
    p[16 + i] = spr_pal[i];
  }
  for (step = 0; step < 4; ++step) {
    for (i = 0; i < 32; ++i) {
      if ((p[i] & 0x30) != 0)
        p[i] = (unsigned char)(p[i] - 0x10);
      else
        p[i] = 0x0F;
    }
    multi_vram_buffer_horz((const char *)p, 16, 0x3F00);
    multi_vram_buffer_horz((const char *)(p + 16), 16, 0x3F10);
    set_scroll();
    ppu_wait_nmi();
    ppu_wait_nmi(); // ~8 frames total
  }
  oam_clear();
  ppu_off(); // level_load + present_screen run with rendering off
}
static void screen_fadeout(void) {
  set_prg_bank(6, 0x80);
  screen_fadeout_b();
  // Bank left at 6; level_load remaps $8000 for tables/blob next.
}

// Door transition: block input, hold Keen at the
// door and FADE HIM OUT (sprite palette to black), then INSTANT-CUT to the
// destination — no camera scroll (which revealed the map + stalled frames
// reloading while panning). The expensive reload happens off-screen in the
// present_screen forced blank. Body banked to draw bank 26 (ms_frames + WRAM/
// OAM/VRAM-buffer only; never retargets R6), like death_anim.
#define MAIN_DRAW __attribute__((noinline, section(".prg_rom_26.text")))
MAIN_DRAW static void door_fadeout_b(void) {
  unsigned char i, j, sp[16];
  int bx = (int)(((pl_x - KEEN_CLIP_XL) >> 4) - cam_x);
  int by = (int)(((pl_y - KEEN_CLIP_YL) >> 4) - cam_y);
  for (i = 0; i < 16; ++i)
    sp[i] = spr_pal[i];
  for (i = 0; i < 24; ++i) {
    oam_set(0);
    if (bx > -16 && bx < 248 && by > -16 && by < 240)
      oam_meta_spr((unsigned char)bx, (unsigned char)by,
                   ms_frames[FRAME_STAND][pl_face]);
    oam_hide_rest();
    if (i >= 6 && (i & 1) == 0) {        // darken Keen one luma step every 2 fr
      for (j = 1; j < 16; ++j)           // (entry 0 = the shared backdrop)
        sp[j] = (sp[j] & 0x30) ? (unsigned char)(sp[j] - 0x10) : 0x0F;
      multi_vram_buffer_horz((const char *)(sp + 1), 15, 0x3F11);
    }
    set_scroll();
    ppu_wait_nmi();
  }
}
// door transition: fade Keen out at the door, then
// INSTANT-CUT to the destination — reposition the camera + forced-blank full
// redraw, NO scroll (which revealed the map layout + stalled frames panning
// there). The expensive reload happens off-screen in present_screen's blank.
__attribute__((noinline)) static void door_transition(void) {
  set_prg_bank(26, 0x80);
  door_fadeout_b();              // input-locked walk-in + Keen fade-out (bank 26)
  set_prg_bank(lvl_bank[g_level], 0x80);
  pl_x = pl_door_x; pl_y = pl_door_y;
  pl_vx = pl_vy = 0; pl_on_ground = 0;
  cam_center();                  // reposition on the destination (instant cut)
  present_screen();              // forced-blank full redraw at the new location
}

// gem-place / switch-flip / door dispatch, extracted from the main loop so its
// cold code stays OUT of main's huge single function (LTO inflates main).
__attribute__((noinline)) static void handle_gem_switch_door(void) {
  unsigned char lb = lvl_bank[g_level];
  if (pl_gem_hit) {
    unsigned char added;
    set_prg_bank(26, 0x80);      // gem_place/switch_flip live in cold bank 26
    added = gem_place((unsigned char)(pl_gem_hit - 1));
    set_prg_bank(lb, 0x80);
    if (added) {                 // the map_cell override changed the door + holder
      present_screen();          // cells -> a forced-blank redraw shows them
      ksfx_play(SFX_OPENGEMDOOR);
    }
    pl_gem_hit = 0;
  }
  if (pl_switch_hit) {
    set_prg_bank(26, 0x80);
    switch_flip((unsigned char)(pl_switch_hit - 1));
    set_prg_bank(lb, 0x80);
    present_screen();            // redraw shows the toggled switch + block
    ksfx_play(SFX_NOAMMO);       // switch-flip feedback
    pl_switch_hit = 0;
  }
  if (pl_door) {
    pl_door = 0;
    door_transition();
  }
}


// ===========================================================================
// IRQ vector target. On MMC5, backgrounds render per-cell via ExRAM extended
// attributes (no scanline-IRQ CHR banding is needed), so NO interrupt source
// is ever enabled: the MMC5 scanline IRQ is off
// ($5204=0), the APU frame IRQ is inhibited ($4017=$40), and the ExRAM title
// needs no raster IRQ either. A stray IRQ therefore cannot occur; this bare
// handler just returns. interrupt_norecurse saves A/X/Y + RTIs around it, and
// being non-weak it makes the linker skip libc's irq.s.obj parser.
// ---------------------------------------------------------------------------
// Bare RTI implementation lives in seam_decode.s. No IRQ source is enabled.


// Keen's death animation: the launch-up-and-off-the-top arc. Draws the
// real DEATH pose where the level's sprite
// bank has room for it (per-level packing, gen_mmc5_rom); on a dense level
// whose bank is full of enemies FRAME_DEATH's ms slot holds the JUMP-pose
// fallback, so it degrades to the old placeholder rather than garbling.
// Physics is frozen; Keen just arcs upward over the (frozen) scene.
//
// Body runs from draw bank 26: WRAM/OAM + ms_frames (also bank 26). Fixed
// region only pays the trampoline. Level song holds during the ~0.8s arc
// (kmusic_sync retargets R6); SFX_DIE is played by the fixed trampoline.
MAIN_DRAW static void death_anim_b(void) {
  int vy = -6; // screen px/frame; gravity pulls it down each frame
  int dy = 0;
  unsigned char i;
  int bx = (int)(((pl_x - KEEN_CLIP_XL) >> 4) - cam_x);
  int by = (int)(((pl_y - KEEN_CLIP_YL) >> 4) - cam_y);
  for (i = 0; i < 48; ++i) {
    int sy = by + dy;
    oam_set(0);
    if (sy > -16 && sy < 240 && bx > -16 && bx < 248)
      oam_meta_spr((unsigned char)bx, (unsigned char)sy,
                   ms_frames[FRAME_DEATH][pl_face]);
    oam_hide_rest();
    set_scroll();
    ppu_wait_nmi();
    // Advance the 140Hz death effect; restore bank 26 after the SFX bank read.
    ksfx_frame_draw();
    dy += vy;
    ++vy; // arc: up first, then fall past the bottom
  }
}
static void death_anim(void) {
  ksfx_play(SFX_DIE); // fixed region: retargets R6 to the sfx/level bank
  set_prg_bank(26, 0x80);
  death_anim_b();
  set_prg_bank(lvl_bank[g_level], 0x80);
}

#ifndef BOOT_LVL
#define BOOT_LVL 0 // production boots the first demo level; test flag overrides
#endif

// GAME-FLOW STATE MACHINE:
//   title -> difficulty select ->
//     HAS_WORLD_MAP: overworld (MAP_ROM_SLOT) <-> enter playable levels
//       finish a level   -> mark done, open fences, return to map
//       all playable done -> ENDING
//     else (linear demo): play level 0..NUM_LEVELS-1 in order
//       finish last level -> ENDING
//     death, lives left  -> respawn in place
//     death, no lives    -> GAME OVER -> title
// item pickup: Keen's clip box vs item cells. Extracted (noinline) so this big
// block stays out of main's one huge function (LTO inflates main otherwise).

MAIN_COLD static unsigned char item_reward_b(unsigned char t) {
  if (t == 15) {
    if (++pl_lifewater >= 100) {
      pl_lifewater = 0;
      ++pl_lives;
      return SFX_EXTRALIFE;
    }
    return SFX_ITEM;
  }
  if (t == 10) {
    ++pl_lives;
    return SFX_EXTRALIFE;
  }
  if (t == 11) {
    pl_ammo += (g_difficulty == 0) ? 8 : 5;
    return SFX_AMMO;
  }
  if (t < 4) {
    pl_keys |= bit8[t];
    return SFX_GEM;
  }
  if (t == 12) {
#if EPISODE == 5
    pl_keycard = 1;
    return SFX_EXTRALIFE;
#else
    pl_level_done = 1;
    ++pl_quest;
    return SFX_LEVELDONE;
#endif
  }
  return SFX_GEM;
}

// WRAM-only hit scan (bank 6). Apply + sfx stay fixed so $8000 banking stays
// the level blob (no execute-from-wrong-bank risk).
// hits[] is BSS (not stack): soft stack has little headroom above ms_wram.
#define ITEM_HIT_MAX 4
static unsigned int item_hits[ITEM_HIT_MAX];
static unsigned char item_hit_n;
MAIN_COLD static void item_scan_b(void) {
  const unsigned char *it;
  unsigned char ptx = pl_x >> 8, pty = pl_y >> 8;
  unsigned char xlo = (ptx >= 2) ? (unsigned char)(ptx - 2) : 0;
  unsigned char xhi = ptx + 2;
  unsigned char n = 0;
  unsigned int i;
  item_sweep_b();
  while (item_lo < g_nitems &&
         g_items[(unsigned int)item_lo * ITEM_STRIDE] < xlo)
    ++item_lo;
  while (item_lo &&
         g_items[(item_lo - 1) * ITEM_STRIDE] >= xlo)
    --item_lo;
  it = g_items + ((unsigned int)item_lo * ITEM_STRIDE);
  for (i = item_lo; i < g_nitems; ++i, it += ITEM_STRIDE) {
    unsigned int cx0, cy0;
    if (it[0] > xhi)
      break;
    if ((unsigned char)(it[1] - pty + 3) > 6)
      continue;
#if EPISODE == 5
    if (it[2] >= 13)
      continue;
#endif
    if (PICKED(i))
      continue;
    cx0 = (unsigned int)it[0] << 8;
    cy0 = (unsigned int)it[1] << 8;
    if (item_sx0 < cx0 + 256u && cx0 < item_sx1 &&
        item_sy0 < cy0 + 256u && cy0 < item_sy1) {
      if (n < ITEM_HIT_MAX)
        item_hits[n++] = i;
    }
  }
  item_hit_n = n;
}
__attribute__((noinline)) static void item_pickup(void) {
  unsigned char n, j;
  set_prg_bank(6, 0x80);
  item_scan_b();
  n = item_hit_n;
  set_prg_bank(lvl_bank[g_level], 0x80);
  for (j = 0; j < n; ++j) {
    unsigned int i = item_hits[j];
    const unsigned char *it = g_items + i * ITEM_STRIDE;
    unsigned char t = it[2];
    // Avoid overflowing the VRAM buffer on a busy seam frame (would corrupt
    // the next NMI flush and often freeze). Retry the pickup next frame.
    if (VRAM_INDEX >= VRAM_BUDGET)
      break;
    cell_pick(i);
    if (t >= 4 && t <= 9) {
      static const unsigned char pts[6] = {0x12, 0x22, 0x52, 0x13, 0x23, 0x53};
      score_add(pts[t - 4]);
      ksfx_play(SFX_ITEM);
    } else {
      unsigned char snd;
      set_prg_bank(6, 0x80);
      snd = item_reward_b(t);
      set_prg_bank(lvl_bank[g_level], 0x80);
      ksfx_play(snd);
    }
  }
}

// The screens (gameover_show/ending_show, hud.c bank 6) and the per-game
// stat reset (newgame_reset) are banked, so the whole bookend machinery
// costs the near-full fixed region almost nothing.
int main(void) {
  ppu_off();

  for (;;) {                 // whole-game loop: each pass is one playthrough
    unsigned char ending = 0; // set when the LAST demo level is completed

    // ms_wram is loaded PER LEVEL by level_load (each level's sprite bank has
    // its own tile ids); nothing draws metasprites before the first level_load
    // (title/menu use their own CHR + font_tile ids), so no boot copy here.

    // Sprite size: gameplay uses 8x16 (set in the banked level_chr_refresh);
    // title/menu/bookend text screens use 8x8, set in the banked st_screen_open
    // (hud.c) -- which every non-first title is preceded by -- and 8x8 is the
    // power-on default for the first title. So NO oam_size call sits in the
    // near-full fixed region.
    title_show(); // title screen; waits for Start

    difficulty_select(); // the port's only menu: EASY/NORMAL/HARD

    newgame_reset(); // fresh lives/score/ammo/quest/keys (banked)
    map_newgame();   // clear levels_done + saved map position
    { // clear collected-item state (static bitmap persists across games)
      unsigned char i;
      for (i = 0; i < sizeof(picked_bm); ++i)
        picked_bm[i] = 0;
      picked_any = 0;
      item_lo = 0;
    }

#if HAS_WORLD_MAP
    level_load(MAP_ROM_SLOT);
    g_on_map = 1;
#else
    level_load(BOOT_LVL);
    g_on_map = 0;
#endif
    // door_state_reset clears ov_*; map_apply_done must run after it.
    door_state_reset();
#if HAS_WORLD_MAP
    if (g_on_map)
      map_apply_done();
#endif
    kmusic_play(lvl_game_no[g_level]);
    bank_bg(0);
    bank_spr(1);
    cam_bounds();
    ksfx_init();
    kmusic_init();
    player_init();
#if HAS_WORLD_MAP
    if (g_on_map)
      map_player_place();
    else
#endif
      actors_init();
    cam_center();
    set_vram_buffer();
    present_screen();

    // Inhibit the APU frame IRQ (undefined power-on state). The MMC5 ExRAM
    // build uses NO gameplay IRQ (backgrounds are per-cell via ExRAM), so it
    // keeps IRQs masked — the title already returned with them disabled (sei).
    POKE(0x4017, 0x40);
    __asm__ volatile("sei");

  while (1) {
    unsigned char pad = pad_poll(0);
    unsigned int new_left;
    unsigned int new_top;

    // ----- mode-specific GAME LOGIC (map vs combat) -----
#if HAS_WORLD_MAP
    if (g_on_map) {
      // MAP MODE: walk + enter only. No combat sim.
      map_player_update(pad);
      if (pl_map_enter) {
        unsigned char slot = pl_map_enter;
        unsigned char i;
        pl_map_enter = 0;
        map_save_pos();
        g_on_map = 0;
        ksfx_play(SFX_MAPENTER); // map-enter whoosh (not level-exit)
        screen_fadeout();
        level_load(slot);
        door_state_reset();
        kmusic_play(lvl_game_no[g_level]);
        for (i = 0; i < sizeof(picked_bm); ++i)
          picked_bm[i] = 0;
        picked_any = 0;
        item_lo = 0;
        cam_bounds();
        player_init();
        actors_init(); // combat only
        cam_center();
        present_screen();
      }
      pl_look_off = 0;
      pl_dead = 0;
      pl_level_done = 0;
      pl_gem_hit = pl_switch_hit = pl_door = 0;
    } else
#endif
    {
      // COMBAT MODE
      {
        static unsigned char paused, start_prev;
        unsigned char start_now = pad & PAD_START;
        if (start_now && !start_prev)
          paused ^= 1;
        start_prev = start_now;
        if (paused) {
          ksfx_frame();
          kmusic_sync();
          ppu_wait_nmi();
          continue;
        }
      }

      actors_set_window(cam_x, cam_y);
      player_update(pad);
      if (pl_gem_hit || pl_switch_hit || pl_door)
        handle_gem_switch_door();

      if (pl_level_done) {
        unsigned char i;
        pl_level_done = 0;
#if HAS_WORLD_MAP
        // Brief complete flash, then fade back to map (fences + flags).
        for (i = 0; i < 30; ++i) {
          one_vram_buffer((i & 4) ? 0x30 : 0x0F, 0x3F00);
          ksfx_frame();
          ppu_wait_nmi();
        }
        map_mark_done(lvl_game_no[g_level]);
        if (map_all_playable_done()) {
          ending = 1;
          break;
        }
#if EPISODE == 5
        pl_keycard = 0;
#endif
        screen_fadeout();
        level_load(MAP_ROM_SLOT);
        g_on_map = 1;
        door_state_reset(); // before map_apply_done (ov_reset)
        map_apply_done();
        kmusic_play(lvl_game_no[g_level]);
        for (i = 0; i < sizeof(picked_bm); ++i)
          picked_bm[i] = 0;
        picked_any = 0;
        item_lo = 0;
        cam_bounds();
        player_init();
        map_player_place(); // no actors_init on map
        cam_center();
        present_screen();
#else
        for (i = 0; i < 90; ++i) {
          one_vram_buffer((i & 4) ? 0x30 : 0x0F, 0x3F00);
          ksfx_frame();
          ppu_wait_nmi();
        }
        if ((unsigned char)(g_level + 1) >= NUM_LEVELS) {
          ending = 1;
          break;
        }
#if EPISODE == 5
        pl_keycard = 0;
#endif
        level_load(g_level + 1);
        door_state_reset();
        kmusic_play(lvl_game_no[g_level]);
        for (i = 0; i < sizeof(picked_bm); ++i)
          picked_bm[i] = 0;
        picked_any = 0;
        item_lo = 0;
        cam_bounds();
        actors_init();
        pl_dead = 2;
#endif
      }
#if HAS_WORLD_MAP
      if (!g_on_map)
#endif
      {
        unsigned char touched;
        set_prg_bank(26, 0x80);
        touched = actors_touch_player();
        set_prg_bank(lvl_bank[g_level], 0x80);
        if (touched || pl_dead) {
          if (pl_dead != 2) {
            death_anim();
            if (pl_lives == 0)
              break;
            --pl_lives;
          }
          player_init();
          pl_dead = 0;
          cam_center();
          present_screen();
          // Skip seam/item/draw for this frame: a dead Keen must not collect
          // items or run more nested C (soft-stack headroom is tight).
          continue;
        }
      }
    }

    // ----- SHARED: camera + ExRAM seam (both modes need map scroll) -----
    cam_target();
    cam_step();
    new_left = cam_x >> 3;
    new_top = cam_y >> 3;
    seam_ntr = ntrow_of(new_top);
    // Deferred restore of the row that left the seam on an UPWARD step (set
    // below). The end-of-frame ExRAM blast + scroll publish can spill past
    // vblank on vertical-seam frames; the PPU then shows one frame with the
    // OLD Y scroll against the NEW ExRAM, and a same-frame restore of the old
    // seam row put content lines in the top overscan band while pogoing up.
    // Restoring one frame later keeps that row black through the mismatch
    // window (both frames it sits inside the black overscan band). Dropped if
    // the camera reversed and the row is the seam again (it must stay black).
    if (seam_restore != 0xFFFFu) {
      if (seam_restore != new_top)
        draw_row(seam_restore);
      seam_restore = 0xFFFFu;
    }
    if (drawn_top < new_top) {
      ++drawn_top;
      draw_row(drawn_top + 29u);
      blacken_seam_row();
    } else if (drawn_top > new_top) {
      --drawn_top;
      blacken_seam_row();
      seam_restore = drawn_top + 1u; // ex-seam row: restore NEXT frame
    }
    // Horizontal catch-up (drawn_left is u16: maps wider than 128 mt need it).
    while (drawn_left < new_left && VRAM_INDEX < VRAM_BUDGET) {
      ++drawn_left; // moved right: draw incoming right col
      draw_column(drawn_left + 32u);
    }
    while (drawn_left > new_left && VRAM_INDEX < VRAM_BUDGET) {
      // ExRAM is one 32-col page; 33rd window col aliases the first. Refresh
      // the old left edge as it becomes visible under the left mask strip.
      draw_column(drawn_left--);
    }

    if (anim_frames > 1) {
      if (--anim_timer == 0) {
        anim_timer = anim_speed;
        if (++anim_phase >= anim_frames)
          anim_phase = 0;
      }
      if (exs_n < 40u && !blank_on && !col_on && row_n == 0u)
        anim_tick();
    }

    // ----- combat-only world interactions -----
    if (!g_on_map) {
      item_pickup();
#if EPISODE == 5
      if (pl_fuse_hit) {
        const unsigned char *it = g_items;
        unsigned int i;
        unsigned char remaining = 0, broke = 0;
        pl_fuse_hit = 0;
        for (i = 0; i < g_nitems; ++i, it += ITEM_STRIDE) {
          if (it[2] < 13)
            continue;
          if (PICKED(i))
            continue;
          if (it[0] == pl_fuse_x &&
              (it[2] == 13 ? it[1] == pl_fuse_y
                           : (broke && it[1] == pl_fuse_y + 1))) {
            cell_pick(i);
            if (it[2] == 13)
              broke = 1;
          } else if (it[2] == 13)
            ++remaining;
        }
        if (broke) {
          ksfx_play(SFX_SHOTHIT);
          if (!remaining)
            pl_level_done = 1;
        }
      }
#endif
    }

    // ----- SHARED present: OAM + audio + ExRAM flush -----
    one_vram_buffer(g_pal[0], 0x3F00);
    oam_set(0);
    if (!g_on_map)
      hud_draw();
    if (g_on_map) {
      set_prg_bank(6, 0x80);
      map_flags_draw(disp_cam_x, disp_cam_y);
      set_prg_bank(26, 0x80);
      map_player_draw(disp_cam_x, disp_cam_y);
    } else {
      set_prg_bank(26, 0x80);
      player_draw(disp_cam_x, disp_cam_y);
      shots_draw(disp_cam_x, disp_cam_y);
      actors_draw(disp_cam_x, disp_cam_y);
    }
    set_prg_bank(lvl_bank[g_level], 0x80);
    oam_hide_rest();
    ksfx_frame();
    kmusic_sync();
    set_scroll();
    ppu_wait_nmi();
    if (!g_on_map)
      level_chr_overlay(pl_ledge ? 2u : pl_pole);
    *(volatile unsigned char *)0x5114 = 0x86;
    exram_blast();
    disp_cam_x = cam_x;
    disp_cam_y = cam_y;
    set_scroll();
    *(volatile unsigned char *)0x5114 = (unsigned char)(0x80u | lvl_bank[g_level]);
  } // while (1): exited only by break -> GAME OVER or ENDING

    kmusic_stop(); // silence the level song before the static screen
    // (bookend text screens draw NO sprites, so 8x16 vs 8x8 is moot -> the
    //  next loop's oam_size(0) before title_show resets the mode)
    if (ending)
      ending_show();   // finished the last demo level -> "CONGRATULATIONS"
    else
      gameover_show(); // out of lives -> "GAME OVER"
  } // for (;;): restart the whole flow at the title screen
}
