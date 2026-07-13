; MMC5 reset stub for the keen-nes project-local MMC5 platform.
;
; On power-on MMC5 defaults to PRG mode 3 with $5117 = last 8KB ROM bank, so
; the reset vector at $FFFC (top of ROM) resolves correctly.  But $5114-$5116
; default to bank 0 with bit7 = 0 (PRG-RAM!), so $8000-$DFFF would map to RAM.
; This stub -- which executes from the top ROM bank ($E000-$FFFF, already
; mapped) -- first programs the four PRG banks to ROM 0..3 (a linear 32KB
; image), then sets a known CHR / ExRAM / nametable / IRQ state, then enters
; the C runtime.  Also provides RTI stubs for the NMI/IRQ vectors so a stray
; interrupt is harmless (the proof ROM leaves both disabled).
;
; Register writes match the reference-emulator + NESdev semantics exactly.

.section .reset,"axR",@progbits
.globl _reset
_reset:
  sei
  cld
  ldx #$ff
  txs

  ; --- PRG: mode 3 (four 8KB banks), map ROM banks 0,1,2,3 linearly ---
  lda #3
  sta $5100            ; $5100 PRG mode 3
  lda #$80             ; bit7 = ROM select, bank 0
  sta $5114            ; $8000-$9FFF <- ROM bank 0
  lda #$81
  sta $5115            ; $A000-$BFFF <- ROM bank 1
  lda #$82
  sta $5116            ; $C000-$DFFF <- ROM bank 2
  lda #$83
  sta $5117            ; $E000-$FFFF <- ROM bank 3 (reset code lives here)

  ; --- CHR: 4KB banking (BG uses ExRAM extended attrs, but set a sane mode) ---
  lda #1
  sta $5101            ; $5101 CHR mode 1 = 4KB

  ; --- nametables: all four PPU pages -> CIRAM (vertical layout 0,1,0,1) ---
  lda #$44
  sta $5105            ; $5105 NT mapping

  ; --- ExRAM: start as general RAM so the C setup can preload attributes;
  ;     C flips it to extended-attribute mode (1) before enabling rendering.
  ;     (In modes 0/1 CPU writes outside active rendering store 0, so the
  ;     load-in-mode-2-then-switch dance is mandatory -- see hello_mmc5.c.) ---
  lda #2
  sta $5104            ; $5104 ExRAM mode 2 (RAM)

  ; --- disable the MMC5 scanline IRQ ---
  lda #0
  sta $5204            ; $5204 bit7 = 0 -> IRQ disabled
  sta $5203            ; target 0 (never matches anyway)

  jmp _start

; Harmless interrupt stubs (proof ROM never enables NMI/IRQ).
.globl nmi
.globl irq
nmi:
irq:
  rti
