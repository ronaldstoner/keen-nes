; src/exram_blast.s -- MMC5 ExRAM write blast (the seam's per-frame vblank flush).
;
; Flushes staged ExRAM bytes to $5C00 under the $5104 mode-flip ($5104=2 so
; out-of-frame writes land, then back to $5104=1 = extended-attribute display).
;   1. exs_* singletons (columns / anim): (zp),y store per cell (~33 cy).
;   2. RESTORE ROWS (row_n): each 32 contiguous cells from row_ex[slot][0..31] to
;      $5C00+base, pointer-set-once y-loop (~11 cy/cell).
;   3. BLANK ROW (blank_on): the overscan seam row, 32 contiguous cells of one
;      constant, same y-loop.
; Folding the restore row(s) + blank row out of the slow ~33 cy/cell path is THE
; fix for grey-on-fast-fall: a pure-vertical frame's flush (was ~66 cells ~2200cy,
; overran vblank so $5104 stayed at 2 = RAM mode into active render -> whole
; background grey) drops to ~1000cy and fits vblank, so $5104 is back to 1 before
; the PPU renders.
;
; C prototype:  unsigned char exram_blast(void);
; Returns 1 after a complete commit (or when there was no work). The caller
; invokes this immediately after ppu_wait_nmi, and every path is bounded to fit
; that vblank window. Do NOT gate this with $5204 bit 6: MMC5's "in frame" flag
; is a scanline-detector state, not a vblank test, and remains asserted for the
; first few vblank scanlines. Using it here rejects every seam commit.
;   exs_n==0 AND row_n==0 AND blank_on==0: return (no mode flip).
;   else: $5104=2; flush singletons; fill restore rows; fill blank row; $5104=1;
;         exs_n=0; row_n=0; blank_on=0.
;
; Leaf routine: clobbers __rc2..__rc4 (scratch); __rc0/__rc1 (soft stack) untouched.
; Banked to bank 6 (.prg_rom_6.text) to relieve the near-full fixed region -- it
; touches only WRAM (exs_*/row_*/blank_*) + MMC5 regs, never the level blob, so
; the caller maps bank 6 around it (see main.c).

.globl exram_blast
.section .prg_rom_6.text.exram_blast,"axR",@progbits
exram_blast:
    lda exs_n
    ora row_n
    ora col_on
    ora blank_on
    bne .Lgo
    lda #1
    rts
.Lgo:
    lda #2
    sta $5104              ; MMC5_EXRAM_MODE = RAM (2): writes always land
    ldy #0                 ; (zp),y index stays 0 for the singleton stores
    ldx #0
    cpx exs_n
    beq .Lrows
.Lloop:
    lda exs_lo,x
    sta __rc2
    lda exs_hi,x
    sta __rc3
    lda exs_val,x
    sta (__rc2),y         ; *(0x5C00+off) = val
    inx
    cpx exs_n
    bne .Lloop
.Lrows:
    lda row_n
    beq .Lcol
    ldx #0                ; X = row_ex cursor (32 per slot, slots consecutive)
    lda #0
    sta __rc4             ; slot index
.Lrowslot:
    ldy __rc4
    lda row_base_lo,y
    sta __rc2
    lda row_base_hi,y
    sta __rc3
    ldy #0                ; Y = 0..31 within the row
.Lrowcell:
    lda row_ex,x
    sta (__rc2),y
    inx
    iny
    cpy #32
    bne .Lrowcell
    inc __rc4
    lda __rc4
    cmp row_n
    bne .Lrowslot
.Lcol:
    lda col_on
    beq .Lblank
    ; Four page-local strided loops are substantially faster than adjusting a
    ; 16-bit zero-page pointer after every store. Within each group of eight
    ; rows, col + row*32 is guaranteed <=255, so absolute,Y never page-crosses.
    ; This saves roughly 300 cycles on a diagonal seam frame -- the margin that
    ; keeps $5104 mode 2 from leaking past vblank on a real/cycle-exact PPU.
    ldy col_idx
    ldx #0
.Lcol5c:
    lda col_ex,x
    sta $5C00,y
    tya
    clc
    adc #32
    tay
    inx
    cpx #8
    bne .Lcol5c
    ldy col_idx
.Lcol5d:
    lda col_ex,x
    sta $5D00,y
    tya
    clc
    adc #32
    tay
    inx
    cpx #16
    bne .Lcol5d
    ldy col_idx
.Lcol5e:
    lda col_ex,x
    sta $5E00,y
    tya
    clc
    adc #32
    tay
    inx
    cpx #24
    bne .Lcol5e
    ldy col_idx
.Lcol5f:
    lda col_ex,x
    sta $5F00,y
    tya
    clc
    adc #32
    tay
    inx
    cpx #30
    bne .Lcol5f
.Lblank:
    lda blank_on
    beq .Ldone
    lda blank_lo
    sta __rc2
    lda blank_hi
    sta __rc3
    lda blank_val
    ldy #31               ; fill base+31 .. base+0 (32-aligned -> no page cross)
.Lbloop:
    sta (__rc2),y
    dey
    bpl .Lbloop
.Ldone:
    lda #1
    sta $5104              ; MMC5_EXRAM_MODE = EXATTR (1)
    lda #0
    sta exs_n             ; drain all
    sta row_n
    sta col_on
    sta blank_on
    lda #1
    rts
