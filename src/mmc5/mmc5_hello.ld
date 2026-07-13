/* Project-local llvm-mos platform for MMC5 (mapper 5) -- proof-ROM layout.
 *
 * Additive: reuses the nes-mmc3 clang driver (toolchain + search paths) but
 * REPLACES its linker script (pass -Tsrc/mmc5/mmc5_hello.ld).  The iNES
 * header is emitted by src/mmc5/ines-mmc5.ld with mapper 5 hardcoded, so we
 * never touch crt0's baked __mapper=4.
 *
 * Memory model for the proof ROM: a plain fixed 32KB PRG image at
 * $8000-$FFFF (reset stub programs the four MMC5 8KB PRG banks 0..3 to make
 * that mapping) and up to 1MB CHR-ROM as one linear .chr_rom blob (ExRAM
 * extended attributes select the 4KB bank per tile at render time).  PRG
 * sizes come from MAPPER_PRG_ROM_KB / MAPPER_CHR_ROM_KB in the .c source.
 *
 * nes.ld supplies the zp/ram/chr_rom regions, the .text/.rodata/.data/.bss
 * SECTIONS and the power-of-two asserts; we add the PRG-ROM region, the
 * reset/vector tail and our own OUTPUT_FORMAT. */

INCLUDE nes.ld
INCLUDE c-in-ram.ld

/* --- fixed 32KB PRG at $8000 (the reset stub maps banks 0..3 as ROM) --- */
__prg_rom_lma    = 0x8000;
__prg_rom_offset = 0;

MEMORY {
  prg_rom (rx) : ORIGIN = 0x8000, LENGTH = __prg_rom_size * 1024
}
REGION_ALIAS("c_readonly", prg_rom)
REGION_ALIAS("prg_rom_fixed", prg_rom)

SECTIONS {
  .prg_rom_fixed : { *(.prg_rom_fixed .prg_rom_fixed.*) } >prg_rom_fixed
}

/* reset + NMI/IRQ vectors ($FFFA) live at the top of the fixed region */
SECTIONS {
  .reset (ABSOLUTE(.) < 0xe000 ? 0xe000 : .) : {
    *(.reset .reset.*)
  } >prg_rom_fixed
  .vectors 0xfffa : {
    SHORT(nmi) SHORT(_reset) SHORT(irq)
  } >prg_rom_fixed
}

OUTPUT_FORMAT {
  INCLUDE src/mmc5/ines-mmc5.ld
  FULL(prg_rom)
  FULL(chr_rom)
}
