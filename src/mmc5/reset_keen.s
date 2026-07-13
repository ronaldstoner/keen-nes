; Keen-on-MMC5 reset stub. Executes
; from the top ROM bank ($E000-$FFFF, mapped by $5117=$FF on power-on), sets
; up the MMC5 banked layout, unlocks WRAM, then enters the C runtime.
;
; Banked-8 layout: PRG mode 3 (four 8KB banks).
;   $8000-$9FFF = switchable window, R6-equivalent (level blobs) -> bank 0.
;   $A000-$FFFF = 24KB fixed region -> banks __fixed_bank, +1, +2 (last).
; __fixed_bank = (__prg_rom_size/8) - 3 (from the banked linker script).
;
; WRAM: MMC5 uses $5102/$5103 unlock + $5113 bank (not $A001). The C
; soft stack lives in $6000-$7FFF from _start on, so WRAM MUST be writable
; before jmp _start. (init-c-in-prg-ram.o repeats a $A001 write in .init.30;
; that's a harmless no-op on MMC5 -- $A000-$BFFF is ROM here.)
;
; CHR: 1KB bank mode ($5101=3). Reset leaves the CHR regs in the compat-shim
; layout; GAMEPLAY then runs level_chr_refresh which sets 8x16 sprites (oam_size)
; and maps set A ($5120-$5127) = the two GLOBAL 4KB sprite banks (pages 0-7). The
; BACKGROUND does NOT use the CHR regs at all -- it is per-cell via ExRAM extended
; attributes (see below), so set B ($5128+) is never written.
;
; ExRAM: reset sets mode 0 (a SAFE boot default -- ExRAM not yet populated). The
; shipping engine renders backgrounds with mode 1 (extended attributes); each
; level's draw_screen_full preloads $5C00 with rendering off, then flips $5104=1.
;
; IRQ: MMC5 scanline IRQ disabled ($5204=0). The gameplay band IRQ is off
; (converter forces band_count=1); the title's scanline-IRQ writes are no-ops.

.section .reset,"axR",@progbits
.globl _reset
_reset:
  sei
  cld
  ldx #$ff
  txs

  ; $5100-$5105 are contiguous: initialize PRG/CHR modes, WRAM unlock,
  ; safe ExRAM mode, and vertical nametable mapping with one compact loop.
  ldx #5
.Linit5100_loop:
  lda .Linit5100,x
  sta $5100,x
  dex
  bpl .Linit5100_loop

  ; --- PRG: mode 3, $8000 window = level bank 0, fixed region at $A000+ ---
  lda #$80                 ; ROM bank 0 at $8000 (level blob window / R6)
  sta $5114
  ldx #mos8(__fixed_bank)
  ldy #1
.Lfixed_loop:
  txa
  ora #$80                 ; ROM select
  sta $5114,y              ; $A000-$FFFF = three consecutive fixed banks
  inx
  iny
  cpy #4
  bne .Lfixed_loop

  ; --- WRAM bank 0 + MMC5 scanline IRQ off ---
  lda #0
  sta $5113
  sta $5204
  sta $5203

  ldx #$ff                 ; preserve the neslib CRT reset-entry convention
  jmp _start

.Linit5100:
  .byte 3, 3, 2, 1, 0, $44

; Interrupt vectors point here via _reset.ld's .vectors (nmi/_reset/irq).
; nmi/irq symbols are provided by neslib + the engine (main.c defines irq;
; neslib defines nmi), so no stub is needed here.
