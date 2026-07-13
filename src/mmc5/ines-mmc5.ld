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
BYTE(0)   /* 10: PRG-(NV)RAM shift counts (none for the proof) */
BYTE(0)   /* 11: CHR-(NV)RAM shift counts (CHR-ROM only)       */
BYTE(0)   /* 12: timing = NTSC */
BYTE(0)   /* 13: vs/extended console */
BYTE(0)   /* 14: misc roms */
BYTE(0)   /* 15: default expansion device */
