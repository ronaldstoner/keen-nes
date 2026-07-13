#ifndef MUSIC_H
#define MUSIC_H
void kmusic_init(void);
void kmusic_play(unsigned char game_level); // starts that level's song
void kmusic_stop(void);
void kmusic_sync(void);  // advance by elapsed 60 Hz NMI frames (bounded catch-up)
#endif
