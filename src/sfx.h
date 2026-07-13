#ifndef SFX_H
#define SFX_H
void ksfx_init(void);
void ksfx_play(unsigned char id);
void ksfx_frame(void);
void ksfx_frame_hud(void);  // same sequencer, restores PRG bank 6
void ksfx_frame_draw(void); // same sequencer, restores PRG bank 26 (draw bank)
void ksfx_stop(void);       // immediate pulse-2 silence/release
#endif
