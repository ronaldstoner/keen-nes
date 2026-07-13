// Level loader: switch PRG bank R6 to the level blob, install header
// pointers, and switch the background CHR banks to the level's tiles.
// Levels are split into 1-4 camera-X regions, each with its own 4KB CHR
// set and its own tl/tr/bl/br metatile tables (pal + collision are
// global); level_region_update() swaps both when the camera crosses a
// region boundary.
#include <mapper.h>
#include <neslib.h>
#include <string.h>
#include "level_fmt.h"
#include "gen/levels.h"

// ===========================================================================
// MMC5 EXRAM level reader.
// Parses the frozen 88-byte blob header, reads the u16 map + hot render-AoS /
// cold collision-SoA metatiles from the multi-bank blob, and copies
// the entity tables + palette to WRAM so the hot game loop reads them without
// bank juggling. Background CHR is per-8x8 via ExRAM (main.c's seam renderer);
// this file owns the DATA side (map/collision/entities/palette).
// ===========================================================================
#include <stdint.h>
#include "mmc5/mmc5.h"

unsigned char g_w, g_h, g_spawn_x, g_spawn_y;
const unsigned char *g_ent[8];
unsigned char g_nent[8];
unsigned int g_nitems;
unsigned char g_pal[16];
unsigned char g_level;
unsigned char g_region;                 // stays 0 (single palette set)
volatile unsigned char g_difficulty = 1; // 0 easy / 1 normal / 2 hard
// background tile-animation region (header bytes 14/15/36/37 -> main.c ticks)
unsigned char anim_base, anim_nbanks, anim_frames, anim_speed;

// gem-door + switch tables (header byte 38 extension; see level_fmt.h). The raw
// section bytes are copied verbatim to WRAM; the accessor macros index them.
unsigned char ext_raw[EXT_RAW_MAX];
unsigned char gd_n, sw_n;
unsigned char *sw_base = ext_raw + 2;   // switch records (past the gem-door recs)

extern const unsigned char *const lvl_blob_refs[];
extern const unsigned char lvl_bank[];
// per-level sprite CHR bank (1KB units) + metasprite image (bank + $8000 ptr);
// gen_mmc5_rom packs each level's Keen+enemies (+poses where they fit) into
// its own sprite bank and a matching ms_wram image.
extern const unsigned char lvl_spr_pages[][8];
extern const unsigned char lvl_pole_page[];
extern const unsigned char lvl_ledge_page[];
extern const unsigned char lvl_overlay_slot[];
extern const unsigned char lvl_ms_bank[];
extern const unsigned char *const lvl_ms_ref[];
extern unsigned char ms_wram[];          // gen/player.c (always-mapped WRAM)
static unsigned char spr_pages[8], pole_page, ledge_page, overlay_slot, chr_overlay;

static unsigned char base_bank;          // first PRG bank of the level blob
static unsigned int off_map;             // MAP section byte-offset (blob<64KB)
// Collision top/flags as $8000-window POINTERS. realign_mt bank-aligns the MT
// table and asserts 10*N <= 8KB, so top[N]/flags[N] live wholly in mt_bank:
// top[m] = $8000+8N+m, flags[m] = $8000+9N+m (9N-1 <= 8191 by the same
// assert). The hot readers write the constant mt_bank instead of deriving
// bank = base_bank + (off>>13) per call (rd8's 16-bit shift chain).
static const unsigned char *mt_top_p, *mt_flags_p;
#if EPISODE == 5
static unsigned int rows_off[256];       // Korath III Base is 250 rows
#else
static unsigned int rows_off[128];       // K4/K6 demo maps are below 128 rows
#endif
// MMC5 seam fast path: the first 8*N bytes are interleaved render records
// {4 tile ids, 4 ExRAM attrs}; top[N],flags[N] follow. gen_mmc5_rom aligns the
// whole 10*N table to one bank. One m<<3 locates a render record.
unsigned char mt_bank;
#define MMC5_ENT_ARENA 1632
static unsigned char ent_arena[MMC5_ENT_ARENA]; // WRAM entity tables
static const unsigned char ent_rec[8] = {5, 3, 4, 3, 3, 2, 2, 4};

// --- blob byte reads via the $8000 window: absolute blob offset ->
// bank = base_bank + (off>>13), addr = $8000 + (off & 0x1FFF). Each byte
// programs its own bank ($5114 direct, no function-call overhead) so a u16
// straddling a bank boundary is safe. rd8 leaves $5114 on whatever bank it
// last read; every level-data consumer goes through rd8, so no restore is
// needed (music/sfx set their own bank). ---
#define M5_PRG0 (*(volatile unsigned char *)0x5114)
static unsigned char rd8(unsigned int off) {
  M5_PRG0 = (unsigned char)(0x80 | (base_bank + (unsigned char)(off >> 13)));
  return *(const unsigned char *)(0x8000u + (off & 0x1FFFu));
}
static unsigned int rd16(unsigned int off) {
  return (unsigned int)rd8(off) | ((unsigned int)rd8(off + 1) << 8);
}

// runtime cell overrides (gem doors / holder / switch art) — see level_fmt.h
unsigned char ov_mx[MMC5_MAX_OVR], ov_my[MMC5_MAX_OVR];
unsigned int ov_mt[MMC5_MAX_OVR];
unsigned char ov_n;
void ov_reset(void) { ov_n = 0; }
void ov_add(unsigned char mx, unsigned char my, unsigned int mt) {
  if (ov_n >= MMC5_MAX_OVR)
    return;
  ov_mx[ov_n] = mx; ov_my[ov_n] = my; ov_mt[ov_n] = mt;
  ++ov_n;
}
// substitute an override metatile for cell (mx,my), else return the ROM cell d.
static unsigned int ov_apply(unsigned int d, unsigned char mx, unsigned char my) {
  unsigned char i;
  for (i = 0; i < ov_n; ++i)
    if (ov_mx[i] == mx && ov_my[i] == my)
      return ov_mt[i];
  return d;
}

// u16 metatile index at metatile coords: MAP[ROWS[my] + mx*2]
unsigned int map_cell(unsigned char mx, unsigned char my) {
  // MAP starts even, every row offset is even, and cells are u16, so this
  // read can never straddle an 8KB PRG-bank boundary. The generic rd16()
  // rewrites $5114 for each byte; collision uses this path many times/tic.
  // rows_off[] already includes off_map (folded at load), so the hot path
  // does NOT re-add it -- one fewer 16-bit add per collision probe.
  unsigned int off = rows_off[my] + ((unsigned int)mx << 1);
  const unsigned char *p;
  unsigned int d;
  M5_PRG0 = (unsigned char)(0x80 | (base_bank + (unsigned char)(off >> 13)));
  p = (const unsigned char *)(0x8000u + (off & 0x1FFFu));
  d = (unsigned int)p[0] | ((unsigned int)p[1] << 8);
  return ov_n ? ov_apply(d, mx, my) : d;
}
// collision decode: MT.top = field 8, MT.flags = field 9. One constant bank
// write + one indexed load — no per-call bank derivation.
unsigned char mmc5_mt_top(unsigned char mx, unsigned char my) {
  unsigned int m = map_cell(mx, my);
  M5_PRG0 = mt_bank;
  return mt_top_p[m];
}
unsigned char mmc5_mt_flags(unsigned char mx, unsigned char my) {
  unsigned int m = map_cell(mx, my);
  M5_PRG0 = mt_bank;
  return mt_flags_p[m];
}

// batch metatile-index fetches for the seam renderer (u16 dst). BATCH bank
// semantics: the $8000 window ($5114) is reprogrammed only when the cell's
// bank actually CHANGES, not per byte. off_map is even
// and the bank size (8192) is even, so a u16 cell never straddles a bank
// boundary (its low byte can't land on offset 0x1FFF) — both bytes are always
// in the currently-mapped bank, so one bank set per bank-run suffices. A
// column strip crosses at most ~2 banks (16 rows x w*2 apart); a row strip is
// contiguous and almost always one bank. Was 2 $5114 writes PER CELL.
void map_col_read16(unsigned char mx, unsigned char my, unsigned char n,
                    unsigned int *dst) {
  unsigned int base = (unsigned int)mx << 1;   // rows_off[] already has off_map
  unsigned char lastbank = 0xFF;
  while (n--) {
    unsigned int off = base + rows_off[my];
    unsigned char bk = (unsigned char)(base_bank + (unsigned char)(off >> 13));
    const unsigned char *p = (const unsigned char *)(0x8000u + (off & 0x1FFFu));
    unsigned int v;
    if (bk != lastbank) {
      M5_PRG0 = (unsigned char)(0x80 | bk);
      lastbank = bk;
    }
    v = (unsigned int)p[0] | ((unsigned int)p[1] << 8);
    *dst++ = ov_n ? ov_apply(v, mx, my) : v;   // opened door / swapped cells
    ++my;
  }
}
void map_row_read16(unsigned char mx, unsigned char my, unsigned char n,
                    unsigned int *dst) {
  unsigned int off = rows_off[my] + ((unsigned int)mx << 1); // rows_off has off_map
  unsigned char lastbank = 0xFF;
  while (n--) {
    unsigned char bk = (unsigned char)(base_bank + (unsigned char)(off >> 13));
    const unsigned char *p = (const unsigned char *)(0x8000u + (off & 0x1FFFu));
    unsigned int v;
    if (bk != lastbank) {
      M5_PRG0 = (unsigned char)(0x80 | bk);
      lastbank = bk;
    }
    v = (unsigned int)p[0] | ((unsigned int)p[1] << 8);
    *dst++ = ov_n ? ov_apply(v, mx, my) : v;
    ++mx;
    off += 2;
  }
}

// Seam decoder: one bank map and one m*8 address per metatile, followed by
// fixed-offset loads — the 6502 no longer performs four independent u16 indexes.
// Implemented as a leaf 6502 routine in seam_decode.s: llvm-mos's -Oz C lowering
// spills several u16 temporaries per record (~4k cycles/strip); the assembly
// follows the same ABI and keeps the record pointer in zero page.

void level_load(unsigned char n) {
  const unsigned char *B;
  unsigned int off_rows, off_mt, off_pal, N, fb, arena;
  unsigned char i;

  g_level = n;
  base_bank = lvl_bank[n];
  M5_PRG0 = 0x80 | base_bank;
  B = lvl_blob_refs[base_bank];          // $8000; anchors the bank sections

  g_w = B[MMC5_OFF_W];
  g_h = B[MMC5_OFF_H];
  g_spawn_x = B[MMC5_OFF_SPAWN_X];
  g_spawn_y = B[MMC5_OFF_SPAWN_Y];
  N = (unsigned int)B[MMC5_OFF_NMETA] | ((unsigned int)B[MMC5_OFF_NMETA + 1] << 8);
  off_rows = rd16(MMC5_OFF_ROWS);        // blob < 64KB: low 16 bits of the u32
  off_map = rd16(MMC5_OFF_MAP);
  off_mt = rd16(MMC5_OFF_MT);
  off_pal = rd16(MMC5_OFF_PALSETS);
  anim_frames = B[MMC5_OFF_ANIMF];       // background tile animation params
  anim_speed = B[MMC5_OFF_ANIMSPD];
  anim_base = B[MMC5_OFF_ANIMBASE];
  anim_nbanks = B[MMC5_OFF_ANIMNBK];

  fb = off_mt + (N << 3);                // collision arrays follow 8B records
  // window pointers into mt_bank (off_mt is bank-aligned -> fb & 0x1FFF = 8N)
  mt_top_p = (const unsigned char *)(0x8000u + (fb & 0x1FFFu));       // top[N]
  mt_flags_p = mt_top_p + N;                                          // flags[N]
  // MT is bank-aligned (gen_mmc5_rom.realign_mt): whole table in one bank.
  mt_bank = (unsigned char)(0x80u | base_bank |
                            (unsigned char)(off_mt >> 13));
  for (i = 0; i < g_h; ++i)              // ROWS[] (h x u16) -> WRAM, off_map
    rows_off[i] = off_map + rd16(off_rows + ((unsigned int)i << 1)); // folded in
  for (i = 0; i < 16; ++i)              // palette set 0 -> WRAM
    g_pal[i] = rd8(off_pal + i);

  // entity tables -> WRAM arena; g_ent[i] point into it (record-by-record
  // copy advances the arena cursor with no multiply)
  arena = 0;
  for (i = 0; i < 8; ++i) {
    unsigned int dir = MMC5_OFF_ENTDIR + (unsigned int)i * 6u;
    unsigned int eoff = rd16(dir);       // u32 off, low 16 (blob < 64KB)
    unsigned int cnt = rd16(dir + 4);
    unsigned char rec = ent_rec[i], r;
    g_ent[i] = ent_arena + arena;
    if (i == 0)
      g_nitems = cnt;
    g_nent[i] = (unsigned char)cnt;
    while (cnt--)
      for (r = 0; r < rec; ++r)
        ent_arena[arena++] = rd8(eoff++);
  }

  // gem-door + switch extension (header byte 38 = u16 blob offset; 0 = none).
  // Copy the raw section bytes; the level_fmt.h macros index them.
  gd_n = sw_n = 0;
  sw_base = ext_raw + 2;
  {
    unsigned int e = rd16(MMC5_OFF_EXT);
    if (e) {
      unsigned char j;
      gd_n = rd8(e);
      sw_n = rd8(e + 1);
      for (j = 0; j < EXT_RAW_MAX; ++j)   // fixed copy (unused tail is inert)
        ext_raw[j] = rd8(e + j);
      sw_base = ext_raw + 2 + (unsigned int)gd_n * 10u;
    }
  }

  // PER-LEVEL sprite CHR + metasprites: copy this level's ms_wram image (from
  // its own PRG bank) into always-mapped WRAM, then point set A ($5124-$5127)
  // at the level's 4KB sprite bank. The image's tile ids match that bank, so
  // Keen's LOOK/DEATH poses appear wherever the level's enemy set left room;
  // dense levels keep the STAND/JUMP fallback baked into the image.
  {
    const unsigned char *src = lvl_ms_ref[n];
    unsigned int j;
    M5_PRG0 = 0x80 | lvl_ms_bank[n];       // map the ms image bank at $8000
    for (j = 0; j < SPR_MS_LEN; ++j)
      ms_wram[j] = src[j];
    M5_PRG0 = 0x80 | base_bank;            // restore the level blob bank
  }
  for (i = 0; i < 8; ++i)
    spr_pages[i] = lvl_spr_pages[n][i];
  pole_page = lvl_pole_page[n];
  ledge_page = lvl_ledge_page[n];
  overlay_slot = lvl_overlay_slot[n];
  // CHR: sprites on set A ($5124-$5127 via the compat shim); bg is per-cell
  // via ExRAM (main.c). $5130 upper bits = 0 (all bg banks < 64).
  MMC5_CHR_UPPER = CHR_UPPER;
  level_chr_refresh();
  g_region = 0;
}

// single palette set for keen4 (SPANBOUNDS empty): nothing to switch.
void level_region_update(unsigned int cam_x) { (void)cam_x; }

// re-assert this level's sprite CHR after any overlay. 8x16 sprites use BOTH
// pattern tables; build-time per-level packing preserves independent 8x8
// palette choices for the active cast. The background is per-cell via
// ExRAM extended attributes (bypasses both register sets), so pages 0-3 are
// free for the second sprite bank. Body banked to bank 6 (six register pokes,
// no level-blob read; cold = load/respawn/present/status-close) so the near-full
// fixed region pays only the trampoline; the two extra pages (0-1) 8x16 needs
// over the old 8x8 path cost the fixed region nothing.
__attribute__((noinline, section(".prg_rom_6.text"))) static void
level_chr_refresh_b(void) {
  unsigned char i;
  chr_overlay = 0;
  oam_size(1);                          // gameplay sprites are 8x16 (PPUCTRL b5);
                                        // set here (bank 6) so the fixed region
                                        // pays nothing. present_screen runs this
                                        // on load/respawn/door/gem/status-close.
  for (i = 0; i < 8; ++i)
    ((volatile unsigned char *)0x5120)[i] = spr_pages[i];
}
void level_chr_refresh(void) {
  set_prg_bank(6, 0x80);
  level_chr_refresh_b();
  // No bank-restore: both call sites re-map the PRG window immediately after
  // (level_load -> door_state_reset; present_screen -> set_palettes_raw), and
  // no level-blob read intervenes. Saves the fixed region a set_prg_bank.
}

// Switch the sprite pattern set only on pole-state transitions. The alternate
// set is a ROM-precomputed overlay: enemies/HUD retain identical tile slots;
// only currently-unused Keen run/jump slots become the three SHINNY frames.
// Thus climbing costs six mapper writes on grab/release, zero per-frame CHR
// uploads, and no enemy-animation cuts.
__attribute__((noinline, section(".prg_rom_6.text"))) static void
level_chr_overlay_b(void) {
  ((volatile unsigned char *)0x5120)[overlay_slot] =
      chr_overlay == 1 ? pole_page : chr_overlay == 2 ? ledge_page
                                                    : spr_pages[overlay_slot];
}
void level_chr_overlay(unsigned char kind) {
  if (kind == chr_overlay)
    return;
  chr_overlay = kind;
  set_prg_bank(6, 0x80);
  level_chr_overlay_b();
  set_prg_bank(lvl_bank[g_level], 0x80);
}
