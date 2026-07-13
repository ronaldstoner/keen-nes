#ifndef ACTORS_H
#define ACTORS_H
void actors_init(void);
void actors_tic(void);
// refresh the active window (camera + 4 tiles); call once per frame
// before actors_tic so offscreen actors sleep
void actors_set_window(unsigned int cam_px, unsigned int cam_py);
unsigned char plat_under(unsigned int x0, unsigned int x1,
                         unsigned int feet, unsigned int tol);
extern unsigned int pfx[], pfy[];
extern signed char pf_dx[], pf_dy[];
extern unsigned char plat_ridden;
unsigned char actors_touch_player(void);
void actors_draw(unsigned int cam_px, unsigned int cam_py);
#endif
