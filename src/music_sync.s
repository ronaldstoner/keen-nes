; Run music from elapsed 60 Hz NMI frames, not simulation-loop iterations,
; so the tempo stays DECOUPLED from gameplay slowdown: a dropped frame costs
; an extra tick here, never a slower song. Catch-up is capped at three ticks
; to prevent an unbounded burst after a debugger stop or long forced-blank
; transition. The tick count lives on the hardware stack across kmusic_tick
; because C may clobber all scratch registers.
.globl kmusic_sync
.globl kmusic_tick
.globl music_frame_seen
.globl FRAME_CNT1

.section .text.kmusic_sync,"axR",@progbits
kmusic_sync:
    lda music_frame_seen
    sta __rc2                  ; previous
    lda FRAME_CNT1
    sta music_frame_seen       ; consume all elapsed time immediately
    sec
    sbc __rc2                  ; elapsed, wraps naturally at 256
    beq .Ldone
    cmp #4
    bcc .Lhave_count
    lda #3
.Lhave_count:
    sta __rc3
.Ltick:
    pha                        ; C tick may clobber all scratch/register state
    jsr kmusic_tick
    pla
    sec
    sbc #1
    bne .Ltick
.Ldone:
    rts
