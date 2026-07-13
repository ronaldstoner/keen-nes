/* Keen-on-MMC5 link script. The banked-8 PRG layout (switchable $8000 window
 * for level blobs + 24KB fixed region at $A000-$FFFF) is mapper-agnostic; the
 * iNES header (mapper 5, in src/mmc5/ines-mmc5.ld) and the reset stub
 * (src/mmc5/reset_keen.s, programs the MMC5 registers) select MMC5.
 *
 * C .data/.bss/.noinit + soft stack live in the 8KB PRG-RAM at $6000-$7FFF
 * (c-in-prg-ram.ld); on MMC5 that window is unlocked in the reset stub via
 * $5102/$5103 + page $5113. The init-c-in-prg-ram.o $A001 write in .init.30 is
 * a harmless no-op on MMC5 ($A000-$BFFF is fixed ROM). */
INCLUDE common.ld
INCLUDE c-in-prg-ram.ld
INCLUDE src/mmc5/prg-rom-banked-8-mmc5.ld

/* The reused nes-mmc3 platform script names CHR placement pages only through
 * 255.  MMC5's $5130 extends those 8-bit bank registers to 10 bits, and the
 * 512KB build pins the shared text font at 1KB page 510.  Without this output
 * section, LTO keeps the C symbol but the linker has nowhere to place its
 * .chr_rom_510 input section; the emitted ROM contains zeros there and the
 * difficulty/GAME OVER/demo-complete screens are black. */
MEMORY {
  chr_rom_510 : ORIGIN = 0x0107f800, LENGTH = 0x400
}
SECTIONS {
  .chr_rom_510 : { KEEP(*(.chr_rom_510 .chr_rom_510.*)) } >chr_rom_510
}
