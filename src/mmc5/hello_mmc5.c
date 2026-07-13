// "hello MMC5 ExRAM" -- minimal proof ROM for the keen-nes project-local
// MMC5 platform.  Renders one background screen using ExRAM extended-
// attribute mode (ExRAM mode 1): each 8x8 cell independently selects its
// 4KB CHR bank (ExRAM low 6 bits) AND its palette (ExRAM high 2 bits).
//
// The generated data (tools/gen_mmc5_proof.py -> gen_mmc5_data.c) puts a
// smooth plasma across the screen that needs 731 distinct 8x8 tiles -- far
// past the plain-nametable 256-tile-per-screen wall -- and lays the 4 palettes
// in an 8px diagonal stripe, finer than the standard 16x16 attribute grid.
// Rendering this correctly validates the whole stack: platform
// (linker/header/reset) + ExRAM extended-attribute rendering.
//
// KEY HARDWARE SUBTLETY: in ExRAM modes 0/1, CPU writes to $5C00-$5FFF
// while NOT actively rendering store 0 (the ExRAM belongs to the PPU).  So
// attributes must be loaded with ExRAM in mode 2 (general RAM) and only
// THEN switched to mode 1.  The reset stub leaves us in mode 2; we preload,
// then flip to mode 1 just before enabling rendering.
#include <stdint.h>
#include "mmc5.h"

#define PPUCTRL   (*(volatile uint8_t *)0x2000)
#define PPUMASK   (*(volatile uint8_t *)0x2001)
#define PPUSTATUS (*(volatile uint8_t *)0x2002)
#define PPUADDR   (*(volatile uint8_t *)0x2006)
#define PPUDATA   (*(volatile uint8_t *)0x2007)
#define PPUSCROLL (*(volatile uint8_t *)0x2005)

extern const unsigned char nt_tiles[960];
extern const unsigned char exram_attr[960];
extern const unsigned char bg_palettes[16];

static void ppu_addr(uint16_t a) {
  PPUADDR = (uint8_t)(a >> 8);
  PPUADDR = (uint8_t)a;
}

static void wait_vblank(void) {
  while (PPUSTATUS & 0x80) {
  }
  while (!(PPUSTATUS & 0x80)) {
  }
}

int main(void) {
  // PPU warm-up: two full frames before touching VRAM.
  PPUCTRL = 0;
  PPUMASK = 0;
  wait_vblank();
  wait_vblank();

  // --- palettes: 16 bytes straight to $3F00 (entry0 of each = backdrop) ---
  ppu_addr(0x3F00);
  for (uint8_t i = 0; i < 16; i++)
    PPUDATA = bg_palettes[i];

  // --- nametable $2000: 960 tile ids, then 64 (ignored) attribute bytes ---
  ppu_addr(0x2000);
  for (uint16_t i = 0; i < 960; i++)
    PPUDATA = nt_tiles[i];
  for (uint8_t i = 0; i < 64; i++)
    PPUDATA = 0;

  // --- ExRAM: preload per-cell (palette<<6 | bank) while in mode 2 (RAM) ---
  for (uint16_t i = 0; i < 960; i++)
    MMC5_EXRAM[i] = exram_attr[i];

  // Flip to extended-attribute mode now that ExRAM holds the attributes.
  mmc5_set_exram_mode(MMC5_EXRAM_EXATTR);

  // Scroll 0, nametable 0, background pattern table 0 (ExRAM overrides the
  // actual pattern bank per cell, but keep PPUCTRL well-defined).
  PPUCTRL = 0x00;
  PPUSCROLL = 0;
  PPUSCROLL = 0;

  wait_vblank();
  PPUMASK = 0x0A;   // show background (incl. leftmost 8px), no sprites

  for (;;) {
  }
  return 0;
}
