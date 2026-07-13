#ifndef LEVEL_FMT_H
#define LEVEL_FMT_H

// ===========================================================================
// MMC5 EXRAM LEVEL FORMAT — the MMC5 migration format.
// (The blob header's version byte is 2 / MMC5_VERSION; see the header table.)
// ===========================================================================
// Emitted by tools/gen_mmc5_level.py into assets/converted/ck4/levelNN/ as
// TWO raw little-endian files. The emitted bytes are near-lossless.
//
//   mmc5.bin      the level blob (header + sections below)
//   mmc5_chr.bin  unique-tile CHR-ROM, 4KB banks of 256 tiles (16B each)
//
// The following are NOT emitted or read: regions, bands, per-region CHR bases,
// per-band ART/DECODE, region_band_chr/astride, and the old per-metatile 16x16
// palette (g_mt_pal) — palette is now per-8x8 via the ExRAM byte.
// Entity/spawn/door/collision SEMANTICS are unchanged; only the item table's
// empty-metatile field widened u8->u16 (below).
//
// WHY MMC5: ExRAM extended-attribute mode (mode 1) gives every 8x8 cell its
// OWN 6-bit CHR 4KB-bank number + 2-bit palette. A cell's full 8x8 tile =
// (ExRAM_bank * 256 + nametable_tile_id). That lifts two crushes: the plain
// nametable's 256-distinct-tiles-per-screen wall AND (via the u16 map below)
// the 256-metatiles-per-level wall (Border Village needs 782). No CHR merging:
// every distinct 8x8 tile is emitted verbatim.
//
// --- CHR (mmc5_chr.bin) ---
// n_banks 4KB banks, 256 tiles/bank, NES 2bpp (8 lo-plane + 8 hi-plane).
// Global tile index gi -> byte offset (gi>>8)*4096 + (gi&0xFF)*16.
// The nametable tile-id is gi&0xFF; the CHR 4KB bank is gi>>8. keen4: 5..10
// banks/level (all < 64, so 6 ExRAM bank bits suffice; chr_upper=0).
//
// --- ExRAM byte (per 8x8 cell), the fetch key ---
//   bit 7..6  palette 0..3   (selects among the 4 palettes of the ACTIVE set)
//   bit 5..0  CHR 4KB bank low 6 bits (0..63)
// Reference fetch (see gen_mmc5_proof.render_reference):
//   bank = (ExRAM & 0x3F) | (chr_upper << 6);  pal = ExRAM >> 6
//   pattern row = CHR[(bank<<12) | (tile_id<<4) | fineY]
//
// --- mmc5.bin header (fixed 88 bytes, little-endian) ---
//   0   u8   magic = 0x4D ('M')
//   1   u8   version = 2  (== MMC5_VERSION below; gen_mmc5_level.py writes it)
//   2   u8   w  (metatile columns)
//   3   u8   h  (metatile rows)
//   4   u8   spawn_x (metatile)
//   5   u8   spawn_y (metatile)
//   6   u8   spawn_dir (signed facing, 0/1/0xFF)
//   7   u8   backdrop_nes ($3F00 shared backdrop, NES color id; == entry 0
//            of every palette in every set)
//   8   u16  n_metatiles  N  (1..65536)
//   10  u16  n_chr_banks     (4KB banks in mmc5_chr.bin: static + anim region)
//   12  u8   n_palsets       (>=1; palette SETS / camera-X spans)
//   13  u8   chr_upper       ($5130 value; 0 for keen4, see >64 plan)
//   14  u8   anim_frames  F  (global phase count, 1 = no animation; see
//            "BACKGROUND TILE ANIMATION" below)
//   15  u8   anim_speed      (60Hz frames per phase step, authentic TILEINFO)
//   16  u32  off_rows        (byte offset from blob start; all sections u32
//   20  u32  off_map          because the blob spans multiple 8KB PRG banks)
//   24  u32  off_mt
//   28  u32  off_palsets
//   32  u32  off_spanbounds
//   36  u8   anim_base       (ABSOLUTE 4KB bank of the anim region; the low
//            byte of the old reserved u32. gen_mmc5_rom offsets it += the
//            level's CHR bank offset, like the ExRAM bank fields.)
//   37  u8   anim_nbanks     (anim region size in 4KB banks; 0 = none)
//   38  u16  reserved (0)
//   40  8 x { u32 off, u16 count }  entity directory, in this fixed order:
//            items, bloogs, blets, babs, plats, fplats, blocks, doors
//            (40+i*6). Records: see entity tables below.
//   88  end of header
//
// --- sections (in this order; each begins at its header offset) ---
// ROWS  @off_rows: h x u16 = byte-offset within the MAP section of row my's
//       first cell (== my*w*2). Precomputed so the engine avoids a my*w
//       multiply. (Every keen4 map < 64KB.)
// MAP   @off_map: w*h x u16, row-major, metatile index 0..N-1. THE HOT PATH:
//       u16 per cell (was u8) — this is the load-bearing change that removes
//       the 256-metatile cap. Engine cell read = MAP[ROWS[my] + mx*2] (u16).
// MT    @off_mt: metatile table as STRUCT-OF-ARRAYS — 10 parallel u8 arrays
//       of N entries, contiguous, in this order (field k base = off_mt+k*N):
//         k0 tl     k1 tr     k2 bl     k3 br     nametable tile-id (u8)
//         k4 tl_ex  k5 tr_ex  k6 bl_ex  k7 br_ex  ExRAM byte (pal<<6|bank)
//         k8 top    k9 flags  collision (UNCHANGED semantics, u16-indexed)
//       SoA (not 10-byte structs) so field access = field_base + mt_index
//       (16-bit add, NO runtime multiply): the engine precomputes the 10
//       field bases once at load.
//       Subtile order is TL,TR,BL,BR (q = hy*2 + hx). top/flags use the
//       TILEINFO encoding (top = TILEINFO top code;
//       flags bit0 right,1 bottom,2 left,3 misc==3(deadly), 4 POLE). bit4 =
//       MT_FLAG_POLE: the metatile is a climbable pole; src/player.c's pole
//       state machine grabs it on Up/Down and climbs. gen_mmc5_level.py sets it
//       from ti0.misc(f)&0x7F==1.
// PALSETS @off_palsets: n_palsets x 16 bytes. Each set = 4 palettes x 4 NES
//       color ids; entry 0 of each palette == backdrop_nes. The ExRAM 2
//       palette bits index the 4 palettes of the CURRENTLY-LOADED set.
// SPANBOUNDS @off_spanbounds: (n_palsets-1) x u16 = ascending camera-X PIXEL
//       boundaries. The engine loads palette set s while cam_x is in span s
//       (set 0 for cam_x < bound[0], etc.). keen4 ships n_palsets=1 (one
//       global set covers each level near-lossless), so SPANBOUNDS is EMPTY
//       and the engine always loads set 0. The mechanism is defined for future
//       multi-biome levels; if used, cells within a span-boundary camera
//       zone must be palette-tear-safe.
// ENTITY TABLES @ their directory offsets (after all the above). Records:
//   items :  x u8, y u8, type u8, empty_mt u16   (5B) — empty_mt WIDENED to
//            u16 (the metatile the cell reverts to on pickup, now u16 space)
//   bloogs:  x, y, mindiff                        (3B)
//   blets :  x, y, color, mindiff                 (4B)
//   babs  :  x, y, mindiff                        (3B)
//   plats :  x, y, dir (signed)                   (3B)
//   fplats:  x, y                                 (2B)
//   blocks:  x, y                                 (2B)
//   doors :  x, y, destx, desty                   (4B)
//   Same tables/fields/sort (ascending x) and difficulty (mindiff <=
//   g_difficulty spawns) convention as the source level format.
//
// --- BACKGROUND TILE ANIMATION (authentic TILEINFO chains) ---
// Every keen4 demo level animates background cells (waterfalls, flames,
// torches, water drops, shimmer): 117..328 animated cells/level, cycle
// lengths 2/3/4 tics from the DOS TILEINFO bg/fg chains. Restored on MMC5 by
// CHR-bank swapping in ExRAM:
//   * An animated 8x8 cell's F per-phase patterns are packed into F
//     CONSECUTIVE 4KB banks (the "anim region" appended after the static
//     banks). Phase k of a length-L cell lives in bank anim_base + block*F +
//     (k mod L), at the cell's nametable tile-id. The region is
//     ceil(#anim tiles / 256) blocks of F banks each (block = slot>>8).
//   * The cell's ExRAM byte (in the MT tl_ex..br_ex fields) carries the
//     PHASE-0 bank = the block base. So an ExRAM bank in
//     [anim_base, anim_base + anim_nbanks) ⟺ the cell animates (static cells
//     never reference the anim region). No extra per-metatile field needed.
//   * RUNTIME (src/level.c reads the header fields; src/main.c ticks): one
//     global phase counter anim_phase (0..F-1) advances every anim_speed
//     frames. Each tick, every ON-SCREEN animated cell's ExRAM byte is
//     rewritten to (phase0_byte + anim_phase) — same low-6-bit bank + block,
//     so it points at phase anim_phase's bank; palette bits (7..6) unchanged.
//     base+F-1 < 64 is enforced so the add never carries into the palette.
//   * On-screen animated cells are tracked in a compact list (each entry: the
//     cell's ExRAM address + its phase-0 byte), maintained 1:1 with the seam
//     ExRAM writes; the tick iterates just this list. The rewrites go through
//     the SAME $5104=2->write->1 mode-flip staging path as the seam ExRAM writes.
// F = lcm of the level's cycle lengths, or if >8 the F<=8 covering the most
// cells (keen4 = 4 for all levels; Border Village's 4 cycle-3 cells approximate
// to F=4). anim_speed = the dominant (most-visible) TILEINFO speed in 60Hz
// frames.
//
// >64 CHR BANKS PLAN (keen4 never hits it; documented for the contract):
// $5130's upper 2 bits are GLOBAL, so >64 banks (>16384 tiles) can't be
// addressed per-cell. Plan: partition CHR into 64-bank groups selected by
// camera-X SPAN via a per-span chr_upper (reuse SPANBOUNDS + a parallel
// per-span chr_upper table), with boundary-zone CHR sharing — set $5130 =
// span's chr_upper on the same cam_x switch that loads
// the palette set. Header's single chr_upper is the global constant used
// when n_palsets==1.
//
// 6502 NOTES: all multi-byte fields little-endian; every table byte-aligned;
// no field needs a runtime multiply on the hot path (MAP is u16-indexed via
// the precomputed ROWS table; MT via precomputed field bases). The u16 MAP
// read is the one place that widened from the earlier u8 map — the engine's
// cell fetch and seam renderer must load a u16 (two bytes) per cell.
#define MMC5_MAGIC        0x4D
#define MMC5_VERSION      2
#define MMC5_HDR          88
#define MMC5_OFF_W        2
#define MMC5_OFF_H        3
#define MMC5_OFF_SPAWN_X  4
#define MMC5_OFF_SPAWN_Y  5
#define MMC5_OFF_SPAWN_D  6
#define MMC5_OFF_BACKDROP 7
#define MMC5_OFF_NMETA    8   /* u16 */
#define MMC5_OFF_NBANKS   10  /* u16 */
#define MMC5_OFF_NPALSETS 12  /* u8  */
#define MMC5_OFF_CHRUPPER 13  /* u8  */
#define MMC5_OFF_ANIMF    14  /* u8: anim frame count F (1 = none) */
#define MMC5_OFF_ANIMSPD  15  /* u8: 60Hz frames per phase step */
#define MMC5_OFF_ROWS     16  /* u32 */
#define MMC5_OFF_MAP      20  /* u32 */
#define MMC5_OFF_MT       24  /* u32 */
#define MMC5_OFF_PALSETS  28  /* u32 */
#define MMC5_OFF_SPANS    32  /* u32 */
#define MMC5_OFF_ANIMBASE 36  /* u8: absolute 4KB bank of the anim region */
#define MMC5_OFF_ANIMNBK  37  /* u8: anim region size in 4KB banks (0=none) */
#define MMC5_OFF_ENTDIR   40  /* 8 x {u32 off, u16 count} */
/* Pre-flattened per-8x8 planes (gen_mmc5_rom.append_planes): one TILES byte
 * and one EXRAM byte per 8px cell, row-major, pitch = w*2, packed so no
 * plane row straddles an 8KB bank; a per-row directory (bank-from-base u8 +
 * $8000-window addr u16) lives in the u16-addressable region. The seam
 * renderer block-copies strips from these; metatile records stay for
 * collision and single-cell rewrites. */
#define MMC5_OFF_PLANE_DIR    88 /* u16: row-directory blob offset */
#define MMC5_OFF_PLANE_NBANKS 90 /* u8: tiles-plane banks; EX plane follows */
#define MMC5_MT_FIELDS    10  /* 8B render AoS records + top[N] + flags[N] */
#define MMC5_EX_BANK_MASK 0x3F
#define MMC5_EX_PAL_SHIFT 6
#define MT_FLAG_POLE      0x10 /* metatile flags bit4: climbable pole */
#define MT_FLAG_GEMHOLD   0x20 /* metatile flags bit5: gem-holder (misc 7..10) */
#define MT_FLAG_SWITCH    0x40 /* metatile flags bit6: plat/bridge switch (misc 5/6/15) */
// GEM-DOOR + SWITCH extension section (gen_mmc5_level.py). Its blob offset is
// stored as a u16 at header byte 38 (the old reserved u16). 0 = none. Layout:
//   byte 0  n_gd (gem-door/holder records, <= MMC5_MAX_GD)
//   byte 1  n_sw (switch records,          <= MMC5_MAX_SW)
//   n_gd x 18B: hx,hy,color, placed_mt(u16), dx,dy0,nrows, open_mt[5](u16)
//   n_sw x  8B: sx,sy, off_mt(u16), on_mt(u16), tx,ty
// Placing the matching gem (pl_keys bit `color`) at holder (hx,hy) swaps that
// cell to metatile placed_mt and opens the door column at (dx,dy0..+nrows-1)
// to open_mt[row] -- per-row FULLY-OPEN art (each cell's DOS +1 anim chain
// walked to its terminal; the original replaces the door run plus its two
// ground tiles, height+2). A switch at (sx,sy) toggles its own art
// off_mt<->on_mt and the B-block marker at target (tx,ty).
#define MMC5_OFF_EXT      38   /* u16 blob offset of the gem-door/switch ext */
#define MMC5_MAX_GD       6    /* max gem-holder/door records per level */
#define MMC5_MAX_SW       4    /* max switch records per level */
#define MMC5_MAX_OVR      16   /* runtime cell overrides (door+holder+switch) */
// Runtime metatile-cell overrides: gem-place opens door cells + swaps the holder
// art, switch-flip swaps the switch art. map_cell()/the batch readers substitute
// ov_mt for a matching (ov_mx,ov_my) so BOTH collision (via MT_TOP/MT_FLAGS) and
// the seam renderer see the new cell — open tiles are non-solid, so passability
// is automatic. ov_n==0 (levels without) is a single branch on the hot path.
extern unsigned char ov_mx[MMC5_MAX_OVR], ov_my[MMC5_MAX_OVR];
extern unsigned int ov_mt[MMC5_MAX_OVR];
extern unsigned char ov_n;
/* MT-record bank + flat plane addressing (level.c) */
extern unsigned char mt_bank;      /* $80| bank holding the MT record table */
extern unsigned char pln_ex_delta; /* banks from tiles plane to EX plane */
extern unsigned int pln_pitch;     /* bytes per plane row (= w*2) */
extern unsigned char pln_cur_bank; /* set by pln_locate: $80| tiles bank */
extern unsigned char pln_rows_left;/* set by pln_locate: rows left in bank */
unsigned int pln_locate(unsigned int ty); /* -> $8000-window row address */
void ov_reset(void);
void ov_add(unsigned char mx, unsigned char my, unsigned int mt);
// ===========================================================================
// MMC5 EXRAM ENGINE API — the real engine consumes the frozen ExRAM blob
// (above) through these declarations. The map cell is a u16 metatile index;
// collision decodes through the MT struct-of-arrays (top/flags), read from
// the multi-bank blob via the banked $8000 window. Entity/item tables + the
// palette are copied to WRAM at load so the hot game loop reads them without
// bank juggling. (level.c provides these.)
// ===========================================================================
extern unsigned char g_w, g_h;
extern unsigned char g_spawn_x, g_spawn_y;
extern const unsigned char *g_ent[8];   // WRAM entity-table pointers
extern unsigned char g_nent[8];
extern unsigned int g_nitems;           // may exceed 255 (K4 L4 = 282 with drops)
#define g_items (g_ent[0])   // 5B: x,y,type,empty_mt(u16)
#define g_bloogs (g_ent[1])  // 3B: x,y,mindiff
#define g_blets (g_ent[2])   // 4B: x,y,color,mindiff
#define g_babs (g_ent[3])    // 3B: x,y,mindiff
#define g_plats (g_ent[4])   // 3B: x,y,dir
#define g_fplats (g_ent[5])  // 2B: x,y
#define g_blocks (g_ent[6])  // 2B: x,y
#define g_doors (g_ent[7])   // 4B: x,y,destx,desty
#define g_nbloogs (g_nent[1])
#define g_nblets (g_nent[2])
#define g_nbabs (g_nent[3])
#define g_nplats (g_nent[4])
#define g_nfplats (g_nent[5])
#define g_nblocks (g_nent[6])
#define g_ndoors (g_nent[7])
extern unsigned char g_pal[16];          // WRAM copy of palette set 0
extern unsigned char g_level;
extern unsigned char g_region;           // stays 0 (single palette set)
extern volatile unsigned char g_difficulty;
// background tile-animation state (level.c reads the header; main.c ticks it):
// anim_base = absolute base 4KB bank of the anim region, anim_nbanks its size
// in banks, anim_frames F the global phase count, anim_speed the 60Hz step.
// An ExRAM bank b animates iff (b - anim_base) < anim_nbanks (unsigned).
extern unsigned char anim_base, anim_nbanks, anim_frames, anim_speed;

// gem-door + switch tables: level_load copies the raw extension section (header
// byte 38) verbatim into ext_raw and the accessors below index it. Keeping it as
// raw bytes (vs typed arrays) is ~570B smaller in level_load's fixed-region code.
// ext_raw layout: [0]=n_gd [1]=n_sw, then n_gd x 10B gem-door records, then
// n_sw x 8B switch records (see MMC5_OFF_EXT). Counts are 0 for levels without.
#define EXT_RAW_MAX (2 + MMC5_MAX_GD * 18 + MMC5_MAX_SW * 8)
extern unsigned char ext_raw[EXT_RAW_MAX];
extern unsigned char gd_n, sw_n;
extern unsigned char *sw_base;   // level_load sets = ext_raw+2+gd_n*10 (switch recs)
#define GD_REC(i) (ext_raw + 2 + (unsigned int)(i) * 18u)
#define gd_hx(i)     GD_REC(i)[0]
#define gd_hy(i)     GD_REC(i)[1]
#define gd_color(i)  GD_REC(i)[2]
#define gd_placed(i) (GD_REC(i)[3] | ((unsigned int)GD_REC(i)[4] << 8))
#define gd_dx(i)     GD_REC(i)[5]
#define gd_dy(i)     GD_REC(i)[6]
#define gd_dh(i)     GD_REC(i)[7]
#define gd_open(i)   (GD_REC(i)[8] | ((unsigned int)GD_REC(i)[9] << 8))
#define SW_REC(i) (sw_base + (unsigned int)(i) * 8u)
#define sw_sx(i)  SW_REC(i)[0]
#define sw_sy(i)  SW_REC(i)[1]
#define sw_off(i) (SW_REC(i)[2] | ((unsigned int)SW_REC(i)[3] << 8))
#define sw_on(i)  (SW_REC(i)[4] | ((unsigned int)SW_REC(i)[5] << 8))
#define sw_tx(i)  SW_REC(i)[6]
#define sw_ty(i)  SW_REC(i)[7]

// u16 metatile index at metatile coords: MAP[my*w*2 + mx*2] (banked read)
unsigned int map_cell(unsigned char mx, unsigned char my);
#define MAP_CELL(mx, my) map_cell((unsigned char)(mx), (unsigned char)(my))

// collision decode: the metatile's top / flags byte (semantics unchanged;
// now u16-indexed, read from the blob).
unsigned char mmc5_mt_top(unsigned char mx, unsigned char my);
unsigned char mmc5_mt_flags(unsigned char mx, unsigned char my);
#define MT_TOP(mx, my)   mmc5_mt_top((unsigned char)(mx), (unsigned char)(my))
#define MT_FLAGS(mx, my) mmc5_mt_flags((unsigned char)(mx), (unsigned char)(my))

// batch metatile-INDEX fetches (u16) for the seam renderer
// Seam decoder (hot path): maps the single MT bank ONCE, then decodes nmt
// metatile indices mi[] into buf[] (tile-ids) + ex[] (ExRAM bytes), two
// subtiles per cell (field k0 then k1, interleaved). k0/k1 = top/bottom for a
// column, left/right for a row. No per-cell bank switch.
void mmc5_seam_decode(unsigned char k0, unsigned char k1, unsigned char nmt,
                      const unsigned int *mi, unsigned char *buf,
                      unsigned char *ex);

void level_load(unsigned char n);
void level_region_update(unsigned int cam_x);  // palette-span switch (no-op)
void level_chr_refresh(void);
void level_chr_overlay(unsigned char kind); // 0 normal, 1 pole, 2 ledge


#endif
