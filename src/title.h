// Title screen: shows the original Keen title bitmap, waits for Start.
// MIT License; see LICENSE.
#ifndef TITLE_H
#define TITLE_H

// Draw the title screen (rendering must be off), turn rendering on, and
// block until Start is pressed and released. Returns with rendering off;
// the caller re-initializes level CHR banks, palettes and the nametable.
void title_show(void);

#endif
