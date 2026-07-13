// Keen-on-MMC5 banking layer. The engine issues PRG/CHR bank changes through
// six small primitives named set_prg_bank() and set_chr_mode_0..5(); these
// definitions map each DIRECTLY to the MMC5 registers (a strong def in a linked
// object also shadows the unused nes-mmc3 library versions of those names).
// These six are the ONLY mapper.h helpers the engine calls, and set_prg_bank is
// always called with addr_hi == 0x80 (the $8000 switchable window).
//
// WHY THIS LAYER STAYS (vs inlining mmc5.h's helpers at every call site):
//   1. mmc5.h's mmc5_set_chr_bg() writes CHR *set B* ($5128+). This engine uses
//      8x8 sprites, so the PPU fetches BOTH background and sprite patterns from
//      CHR *set A* ($5120-$5127) — set B is never consulted. Routing the "bg"
//      banks (R0/R1) to set A here is therefore the correct native MMC5 mapping;
//      the generic set-B helper would silently do nothing.
//   2. The two "bg" regs take a value in 1KB units with the low bit forced even,
//      preserving a single 2KB-aligned bg-CHR call from the engine as two 1KB
//      MMC5 pokes — one call site, no per-caller poke duplication (keeps the
//      near-full fixed region lean; inlining six pokes at dozens of hot call
//      sites would bloat it).
// The reset stub puts MMC5 in PRG mode 3 + CHR 1KB mode, so these are direct
// register pokes with no mode juggling.
#include <stdint.h>

// $8000-$9FFF switchable PRG window -> MMC5 $5114 (PRG bank reg 0).
// bit7 = ROM select. addr_hi is 0x80 throughout this engine (asserted below
// only informally; a non-0x80 caller would need $5115/$5116 instead).
__attribute__((leaf)) void set_prg_bank(char bank_id, char addr_hi) {
  (void)addr_hi;
  *(volatile uint8_t *)0x5114 = 0x80 | (uint8_t)bank_id;
}

// Background CHR (the two "bg" bank slots, value in 1KB units with the low bit
// forced even = 2KB-aligned). MMC5 1KB mode splits each 2KB slot into two 1KB
// regs of CHR set A: slot 0 -> $5120/1 ($0000-$07FF), slot 1 -> $5122/3
// ($0800-$0FFF). Set A (not B) because 8x8 sprites fetch BG from set A too.
void set_chr_mode_0(char chr_id) {
  uint8_t base = (uint8_t)chr_id & 0xFE;
  *(volatile uint8_t *)0x5120 = base;
  *(volatile uint8_t *)0x5121 = base + 1;
}
void set_chr_mode_1(char chr_id) {
  uint8_t base = (uint8_t)chr_id & 0xFE;
  *(volatile uint8_t *)0x5122 = base;
  *(volatile uint8_t *)0x5123 = base + 1;
}

// Sprite CHR: the four 1KB slots at $1000/$1400/$1800/$1C00 -> MMC5 CHR set A
// regs $5124-$5127 (1KB mode). 8x8 sprites use set A for all fetches.
void set_chr_mode_2(char chr_id) { *(volatile uint8_t *)0x5124 = (uint8_t)chr_id; }
void set_chr_mode_3(char chr_id) { *(volatile uint8_t *)0x5125 = (uint8_t)chr_id; }
void set_chr_mode_4(char chr_id) { *(volatile uint8_t *)0x5126 = (uint8_t)chr_id; }
void set_chr_mode_5(char chr_id) { *(volatile uint8_t *)0x5127 = (uint8_t)chr_id; }
