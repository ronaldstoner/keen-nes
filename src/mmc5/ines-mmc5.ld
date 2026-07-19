/* iNES 2.0 header for MMC5 (mapper 5).  Self-contained: the mapper number
 * is hardcoded to 5 here so we do NOT depend on crt0's __mapper (which is a
 * hard global = 4 for the nes-mmc3 startfile we reuse).  PRG/CHR sizes come
 * from the MAPPER_PRG_ROM_KB / MAPPER_CHR_ROM_KB macros in the .c source.
 * Included from OUTPUT_FORMAT in src/mmc5/mmc5_hello.ld. */

BYTE(0x4e) BYTE(0x45) BYTE(0x53) BYTE(0x1a)   /* "NES\x1a" */

BYTE((__prg_rom_size / 16) & 0xff)            /* 5: PRG size low (16KB units) */
BYTE((__chr_rom_size / 8) & 0xff)             /* 6: CHR size low (8KB units)  */

BYTE(                                         /* 7 flags6 */
  ((DEFINED(__mirroring) ? __mirroring : 1) & 1) << 0 |   /* vertical default */
  (5 & 0xf) << 4                              /* mapper low nibble = 5 */
)
BYTE(                                         /* 8 flags7 */
  2 << 2 |                                    /* iNES 2.0 marker */
  ((5 >> 4) & 0xf) << 4                       /* mapper high nibble = 0 */
)
BYTE((5 >> 8) & 0xf)                          /* 8: mapper bits 8-11 | submapper */
BYTE(                                         /* 9: PRG/CHR size high nibbles */
  ((__prg_rom_size / 16) >> 8 & 0xf) << 0 |
  ((__chr_rom_size / 8)  >> 8 & 0xf) << 4
)
/* 10: PRG-(NV)RAM shift counts.  Low nibble = VOLATILE PRG-RAM, size =
 * 64 << shift, so 7 -> 8192 bytes = the 8KB at $6000-$7FFF that holds the C
 * runtime (.data/.bss/.noinit + soft stack; see mmc5.ld and the $5113 = 0
 * bank select in reset_keen.s).  High nibble = NVRAM, 0: no battery.
 * This MUST be non-zero.  The file is flagged iNES 2.0 (flags7 bit3), and
 * under 2.0 the PRG-RAM size comes from THIS byte, not the iNES 1.0 byte 8.
 * Leaving it 0 declares "no PRG-RAM": a strict emulator (Mesen) then maps
 * nothing at $6000-$7FFF, so every .bss read returns OPEN BUS (the address
 * high byte) and every write is discarded.  That reads as garbage in any
 * value loaded from RAM -- e.g. g_pal came back as sixteen $68 bytes at
 * $68B0, painting the whole background one palette. */
BYTE(7)
BYTE(0)   /* 11: CHR-(NV)RAM shift counts (CHR-ROM only)       */
BYTE(0)   /* 12: timing = NTSC */
BYTE(0)   /* 13: vs/extended console */
BYTE(0)   /* 14: misc roms */
BYTE(0)   /* 15: default expansion device */
