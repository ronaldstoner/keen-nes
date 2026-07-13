// Title screen: the original Keen title bitmap (converted by
// tools/gen_mmc5_title.py into src/gen/titledata.c), shown until Start.
// MIT License; see LICENSE.
#include <neslib.h>
#include <nesdoug.h>
#include <mapper.h>
#include <peekpoke.h>
#include "gen/levels.h"
#include "title.h"

// ===========================================================================
// MMC5 ExRAM TITLE: a single STATIC ExRAM extended-attribute screen — exactly
// like a level's spawn screen, just with no scroll/seam/collision. Every 8x8
// cell picks its own CHR tile (698 unique on this title,
// far past the 256-per-screen hardware nametable limit), its own CHR 4KB bank
// (ExRAM low 6 bits) and its own palette (ExRAM high 2 bits), so the whole
// title renders near-lossless with NO lossy tile reduction and NO raster
// trickery. Data: tools/gen_mmc5_title.py -> src/gen/titledata.c.
//
// title_blob (.prg_rom_12, $8000): [0..959] nametable tile-ids, [960..1919]
// ExRAM bytes (pal<<6 | ABSOLUTE 4KB bank, baked by the generator from
// levels.h CHR_ROM_KB), [1920..1935] the 16-byte bg palette.
#include "mmc5/mmc5.h"
#define TITLE_PRG_BANK 12
#define TITLE_BANKED __attribute__((noinline, section(".prg_rom_12.text")))
#define TB_EX  960
#define TB_PAL 1920

extern const unsigned char title_blob[];
extern const unsigned char title_chr_upper;

TITLE_BANKED static void title_run(void) {
  unsigned int i;

  // BG/sprite pattern-table convention (the BG bank is taken from ExRAM in
  // extended-attribute mode, so bank_bg is cosmetic here; no title sprites).
  bank_bg(0);
  bank_spr(1);
  MMC5_CHR_UPPER = title_chr_upper;    // $5130 selects title's 256KB window

  // Palettes written RAW via $2006/$2007 (neslib's pal_bg pushes colors
  // through a brightness LUT that wrecks whites/greys). The PPU is already
  // off (main() ppu_off'd before title_show), so direct VRAM writes are safe.
  // Cancel neslib's pending startup pal_clear.  Otherwise the first title NMI
  // replaces these raw colors with sixteen $0F entries. PAL_UPDATE is neslib's
  // one-byte NMI flag.
  __asm__ volatile("lda #0\nsta PAL_UPDATE");
  vram_adr(0x3F00);
  for (i = 0; i < 16; ++i)
    vram_put(title_blob[TB_PAL + i]);

  // Nametable tile-ids -> $2000, then 64 attribute bytes (ignored: the ExRAM
  // extended-attribute fetch overrides the attribute table).
  vram_adr(0x2000);
  for (i = 0; i < 960; ++i)
    vram_put(title_blob[i]);
  for (i = 0; i < 64; ++i)
    vram_put(0);

  // Per-8x8 ExRAM bytes -> $5C00. Flip $5104 to mode 2 (ExRAM = general RAM,
  // writes ALWAYS land regardless of frame phase — the proven preload rule),
  // fill the 32x30 window, then mode 1 (extended attributes) for display.
  MMC5_EXRAM_MODE = MMC5_EXRAM_RAM;
  for (i = 0; i < 960; ++i)
    MMC5_EXRAM[i] = title_blob[TB_EX + i];
  MMC5_EXRAM_MODE = MMC5_EXRAM_EXATTR;

  oam_clear();
  scroll(0, 0);
  // Static screen, no scroll -> no left-edge ExRAM seam, so the left column
  // stays visible ($0A = show BG incl. left 8px, sprites off).
  ppu_mask(0x0A);

  // wait for Start or A pressed, then released, before handing off
  {
    unsigned char pressed = 0;
    while (1) {
      unsigned char pad;
      ppu_wait_nmi();
      pad = pad_poll(0);
      if (pad & (PAD_START | PAD_A))
        pressed = 1;
      else if (pressed)
        break;
    }
  }

  ppu_off(); // caller redraws for gameplay (level CHR/palette/nametable; the
             // level_load ExRAM preload re-establishes $5104 mode 1)
}

// Fixed-region trampoline: map the title bank (code + blob at $8000), run.
void title_show(void) {
  set_prg_bank(TITLE_PRG_BANK, 0x80);
  title_run();
}
