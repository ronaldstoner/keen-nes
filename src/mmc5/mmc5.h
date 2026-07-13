// MMC5 (iNES mapper 5 / ExROM) register map + inline helpers for the
// llvm-mos NES/keen-nes port.  Project-local platform: pair with
// src/mmc5/mmc5_hello.ld (linker + iNES-2.0 mapper-5 header) and
// src/mmc5/reset_mmc5.s (reset stub that programs a known power-on state).
//
// Every register here is a plain store to a write-only register, so the
// helpers are static-inline volatile pokes; the multiplier and IRQ status
// are the only readable regs.
#ifndef _MMC5_H_
#define _MMC5_H_
#include <stdint.h>

// ---- register addresses ----------------------------------------------
#define MMC5_PRG_MODE     (*(volatile uint8_t *)0x5100) // 0..3 (see below)
#define MMC5_CHR_MODE     (*(volatile uint8_t *)0x5101) // 0=8K 1=4K 2=2K 3=1K
#define MMC5_PRG_PROTECT1 (*(volatile uint8_t *)0x5102) // write 2 ..
#define MMC5_PRG_PROTECT2 (*(volatile uint8_t *)0x5103) // .. and 1 to unlock RAM
#define MMC5_EXRAM_MODE   (*(volatile uint8_t *)0x5104) // 0/1 PPU, 2 RAM, 3 ROM
#define MMC5_NT_MAPPING   (*(volatile uint8_t *)0x5105) // 4x2bit: 0/1 CIRAM,2 EXRAM,3 fill
#define MMC5_FILL_TILE    (*(volatile uint8_t *)0x5106)
#define MMC5_FILL_ATTR    (*(volatile uint8_t *)0x5107)
#define MMC5_PRG_RAM_BANK (*(volatile uint8_t *)0x5113) // $6000-$7FFF RAM page (3 bits)
#define MMC5_PRG_BANK0    (*(volatile uint8_t *)0x5114) // see mode table
#define MMC5_PRG_BANK1    (*(volatile uint8_t *)0x5115)
#define MMC5_PRG_BANK2    (*(volatile uint8_t *)0x5116)
#define MMC5_PRG_BANK3    (*(volatile uint8_t *)0x5117) // $E000-$FFFF, always ROM
// CHR bank set A (sprites), 8 regs; set B (background), 4 regs; + upper bits
#define MMC5_CHR_A0       (*(volatile uint8_t *)0x5120)
#define MMC5_CHR_A7       (*(volatile uint8_t *)0x5127)
#define MMC5_CHR_B0       (*(volatile uint8_t *)0x5128)
#define MMC5_CHR_B3       (*(volatile uint8_t *)0x512B)
#define MMC5_CHR_UPPER    (*(volatile uint8_t *)0x5130) // 2 high bits, latched on A/B write
#define MMC5_VSPLIT       (*(volatile uint8_t *)0x5200)
#define MMC5_IRQ_SCANLINE (*(volatile uint8_t *)0x5203) // compare target
#define MMC5_IRQ_STATUS   (*(volatile uint8_t *)0x5204) // W: bit7=enable; R: b7 pend b6 inframe (read acks)
#define MMC5_MUL_LO       (*(volatile uint8_t *)0x5205) // W: multiplicand  R: product low
#define MMC5_MUL_HI       (*(volatile uint8_t *)0x5206) // W: multiplier    R: product high
#define MMC5_EXRAM        ((volatile uint8_t *)0x5C00)  // 1KB ExRAM window

// ---- PRG bank modes ($5100) ------------------------------------------
#define MMC5_PRG_MODE_32K 0   // one 32K bank via $5117
#define MMC5_PRG_MODE_16K 1   // $8000 <-$5115(16K)  $C000<-$5117(16K)
#define MMC5_PRG_MODE_16_8 2  // $8000<-$5115(16K) $C000<-$5116(8K) $E000<-$5117(8K)
#define MMC5_PRG_MODE_8K  3   // four 8K banks via $5114..$5117
// bit7 of a PRG bank reg selects ROM(1) / PRG-RAM(0) for $5114..$5116.
#define MMC5_PRG_ROM      0x80

// ---- CHR bank modes ($5101) ------------------------------------------
#define MMC5_CHR_MODE_8K  0
#define MMC5_CHR_MODE_4K  1
#define MMC5_CHR_MODE_2K  2
#define MMC5_CHR_MODE_1K  3

// ---- ExRAM modes ($5104) ---------------------------------------------
#define MMC5_EXRAM_NT     0   // ExRAM usable as an extra nametable
#define MMC5_EXRAM_EXATTR 1   // *** extended attributes: per-tile bank+palette
#define MMC5_EXRAM_RAM    2   // ExRAM = general CPU RAM (writes always land)
#define MMC5_EXRAM_ROM    3   // ExRAM read-only

// ---- nametable-source helper: build $5105 from four 2-bit page sources
#define MMC5_NT(p0, p1, p2, p3) \
  ((uint8_t)(((p0) & 3) | (((p1) & 3) << 2) | (((p2) & 3) << 4) | (((p3) & 3) << 6)))
#define MMC5_NT_CIRAM0 0
#define MMC5_NT_CIRAM1 1
#define MMC5_NT_EXRAM  2
#define MMC5_NT_FILL   3

// ---- helpers ---------------------------------------------------------
static inline void mmc5_set_prg_mode(uint8_t m) { MMC5_PRG_MODE = m; }
static inline void mmc5_set_chr_mode(uint8_t m) { MMC5_CHR_MODE = m; }
static inline void mmc5_set_exram_mode(uint8_t m) { MMC5_EXRAM_MODE = m; }

// $6000-$7FFF PRG-RAM page.  Unlock RAM writes first with mmc5_ram_unlock().
static inline void mmc5_set_prg_ram_bank(uint8_t b) { MMC5_PRG_RAM_BANK = b & 7; }
static inline void mmc5_ram_unlock(void) { MMC5_PRG_PROTECT1 = 2; MMC5_PRG_PROTECT2 = 1; }
static inline void mmc5_ram_lock(void) { MMC5_PRG_PROTECT1 = 0; MMC5_PRG_PROTECT2 = 0; }

// PRG bank at a CPU slot.  Pass MMC5_PRG_ROM|bank for ROM, bare bank for RAM.
static inline void mmc5_set_prg_bank8000(uint8_t b) { MMC5_PRG_BANK0 = b; }
static inline void mmc5_set_prg_banka000(uint8_t b) { MMC5_PRG_BANK1 = b; }
static inline void mmc5_set_prg_bankc000(uint8_t b) { MMC5_PRG_BANK2 = b; }
static inline void mmc5_set_prg_banke000(uint8_t b) { MMC5_PRG_BANK3 = b; }

// CHR: background set B (4KB reg B0 covers $0000-$0FFF in 4KB mode) and the
// $5130 upper-2-bit extension (latched into the NEXT A/B write).
static inline void mmc5_set_chr_upper(uint8_t hi) { MMC5_CHR_UPPER = hi & 3; }
static inline void mmc5_set_chr_bg(uint8_t reg, uint8_t bank) {
  (*(volatile uint8_t *)(0x5128 + (reg & 3))) = bank;
}
static inline void mmc5_set_chr_spr(uint8_t reg, uint8_t bank) {
  (*(volatile uint8_t *)(0x5120 + (reg & 7))) = bank;
}

// Scanline IRQ: arm at a target line, or disable.  Read status to ack.
static inline void mmc5_irq_set(uint8_t scanline) {
  MMC5_IRQ_SCANLINE = scanline;
  MMC5_IRQ_STATUS = 0x80;      // enable
}
static inline void mmc5_irq_disable(void) { MMC5_IRQ_STATUS = 0x00; }
static inline uint8_t mmc5_irq_ack(void) { return MMC5_IRQ_STATUS; }

// Hardware 8x8->16 unsigned multiply (a*b), ~free vs a 6502 software mul.
static inline uint16_t mmc5_mul(uint8_t a, uint8_t b) {
  MMC5_MUL_LO = a;
  MMC5_MUL_HI = b;
  return (uint16_t)MMC5_MUL_LO | ((uint16_t)MMC5_MUL_HI << 8);
}

#endif // _MMC5_H_
