; MMC5 hot metatile decoder.
;
; void mmc5_seam_decode(u8 k0, u8 k1, u8 nmt, const u16 *mi, u8 *buf, u8 *ex)
; llvm-mos ABI at entry:
;   A=k0, X=k1, __rc2=nmt, __rc4/5=mi, __rc6/7=buf, __rc8/9=ex
;
; Blob v2 render record: {tl,tr,bl,br,tl_ex,tr_ex,bl_ex,br_ex} (8 bytes).
; One m<<3 pointer calculation then four fixed-offset loads replaces the C
; compiler's heavily-spilled independent wide indexes.

.globl mmc5_seam_decode
.globl mt_bank
.section .text.mmc5_seam_decode,"axR",@progbits
mmc5_seam_decode:
    sta __rc10             ; k0
    stx __rc11             ; k1
    lda mt_bank             ; pre-encoded $80|bank by level_load
    sta $5114
    ldx __rc2              ; metatile count
.Lloop:
    ldy #0
    lda (__rc4),y          ; m low
    sta __rc12
    iny
    lda (__rc4),y          ; m high
    sta __rc13
    asl __rc12             ; m *= 8
    rol __rc13
    asl __rc12
    rol __rc13
    asl __rc12
    rol __rc13
    lda __rc13
    ora #$80
    sta __rc13             ; aligned table: record = $8000 + m*8

    ldy __rc10             ; tile k0
    lda (__rc12),y
    ldy #0
    sta (__rc6),y
    ldy __rc10             ; ex k0
    tya
    ora #4
    tay
    lda (__rc12),y
    ldy #0
    sta (__rc8),y

    ldy __rc11             ; tile k1
    lda (__rc12),y
    ldy #1
    sta (__rc6),y
    ldy __rc11             ; ex k1
    tya
    ora #4
    tay
    lda (__rc12),y
    ldy #1
    sta (__rc8),y

    lda __rc4             ; mi += 2 (carry is clear: final m-high ROL was 0)
    adc #2
    sta __rc4
    bcc .Lmiok
    inc __rc5
.Lmiok:
    clc                    ; buf += 2
    lda __rc6
    adc #2
    sta __rc6
    bcc .Lbufok
    inc __rc7
.Lbufok:
    clc                    ; ex += 2
    lda __rc8
    adc #2
    sta __rc8
    bcc .Lexok
    inc __rc9
.Lexok:
    dex
    bne .Lloop
.Ldone:
    rts

; Every possible IRQ source is disabled by reset/main.  Keep the vector target
; as the literal hardware-safe operation instead of paying the C interrupt
; prologue/epilogue (~38 fixed-bank bytes) for an unreachable empty handler.
.globl irq
.section .text.irq,"axR",@progbits
irq:
    rti
