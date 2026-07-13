# Commander Keen NES port build (MMC5-only). MIT-licensed (code); game data
# NOT included. Requires your own original game files in original/ (see README),
# a python3 venv in .venv/ (pillow, py65), and llvm-mos in third_party/llvm-mos.
#
# Platform is MMC5 (iNES mapper 5 / ExROM): near-lossless per-8x8 ExRAM
# backgrounds, per-level sprite CHR banks, animated tiles, expansion audio.

LLVM_MOS := third_party/llvm-mos
# The nes-mmc3 clang driver is reused only for its toolchain + search paths;
# the link script (src/mmc5.ld) + iNES-2.0 header (src/mmc5/ines-mmc5.ld)
# select mapper 5, and src/mmc5/reset_keen.s programs the MMC5 registers.
CC := $(LLVM_MOS)/bin/mos-nes-mmc3-clang
PY := .venv/bin/python

all: build/keen4.nes build/keen4_ex.nes build/keen_ex.nes

third_party/unlzexe/unlzexe: third_party/unlzexe/unlzexe.c
	cc -w -O2 -o $@ $<

# --- keen4 MMC5 ExRAM PLAYABLE build: the REAL engine (main/level/player/
# actors/hud/title) on MMC5 with near-lossless per-8x8 ExRAM backgrounds.
# Backgrounds use the ExRAM level data (gen_mmc5_level -> gen_mmc5_rom, which
# also emits src/gen/levels.h + the sprite CHR); player/sfx/music/title/status
# use the normal generated data. mmc5_compat.c maps the engine's PRG/CHR bank
# calls to MMC5 registers (see its header for why the shim stays). ---
KEEN4_SRCS := src/mmc5/reset_keen.s src/exram_blast.s src/music_sync.s src/seam_decode.s src/mmc5/mmc5_compat.c src/main.c src/player.c \
        src/actors.c src/sfx.c src/hud.c src/level.c src/music.c \
        src/gen/musicdata.c src/title.c src/gen/leveldata_mmc5.c \
        src/gen/titledata.c src/gen/player.c src/gen/sfx.c
KEEN4_CFLAGS := -Oz -Tsrc/mmc5.ld -lneslib -lnesdoug

build/keen4.nes: third_party/unlzexe/unlzexe
	mkdir -p build src/gen assets/extracted/ck4 assets/converted/ck4
	# unlzexe is DOS-era: it parses paths with '\' separators, so a Unix output
	# path with '/' gets truncated to ~12 chars and misplaced. Run it INSIDE the
	# target dir with a bare 8.3 name, then rename. Falls back to copying the EXE
	# verbatim if it isn't LZEXE-compressed.
	@if [ ! -f assets/extracted/ck4/keen4_unpacked.exe ]; then \
	  ( cd assets/extracted/ck4 && ../../../third_party/unlzexe/unlzexe \
	      ../../../original/Commander_Keen_4/KEEN4E.EXE keen4unp.exe ) \
	    && mv -f assets/extracted/ck4/keen4unp.exe assets/extracted/ck4/keen4_unpacked.exe \
	    || cp original/Commander_Keen_4/KEEN4E.EXE assets/extracted/ck4/keen4_unpacked.exe; fi
	@if [ ! -f assets/extracted/ck4/EGAHEAD.BIN ]; then \
	  KEEN_EP=4 python3 tools/extract_tables.py; fi
	@if [ ! -d assets/extracted/ck4/gfx ]; then \
	  KEEN_EP=4 $(PY) tools/extract_assets.py; fi
	# Blob v2 uses renderer-ready metatile records. Regenerate deterministically
	# so a stale v1 converted asset can never be linked with the v2 reader.
	KEEN_EP=4 $(PY) tools/gen_mmc5_level.py 1 3
	KEEN_EP=4 $(PY) tools/gen_player_data.py
	KEEN_EP=4 $(PY) tools/gen_mmc5_rom.py 1 3
	KEEN_EP=4 $(PY) tools/gen_sfx.py
	KEEN_EP=4 $(PY) tools/gen_music.py 1 3
	KEEN_EP=4 $(PY) tools/gen_mmc5_title.py
	KEEN_EP=4 $(PY) tools/gen_status.py
	$(CC) $(KEEN4_CFLAGS) -o $@ $(KEEN4_SRCS) -Wl,-Map=build/keen4.map
	@ls -la $@

# Compatibility names used by older MMC5/ExRAM test workflows.  These are
# exact copies, never separately-linked ROMs, so an old artifact cannot hide
# behind the former "_ex" filename.
build/keen4_ex.nes: build/keen4.nes
	cp $< $@

build/keen_ex.nes: build/keen4.nes
	cp $< $@

keen4: build/keen4.nes build/keen4_ex.nes build/keen_ex.nes

# --- keen5 / keen6: SAME MMC5 engine, episode selected by KEEN_EP. The tools
# (keenlib.py EPISODES, convert_nes/gen_mmc5_level spawn maps, actors.c/main.c
# #if EPISODE, gen_player_data, gen_mmc5_title TITLE_PIC) are all episode-aware;
# only the level-blob emitters take KEEN_EP + the level list. Demo scope is a
# small 2-level slice per episode (kept under the single-8KB-bank metatile cap
# of ~819 MTs and the CHR budget): keen5 = 1 (Ion Vent) + 13 (Korath III Base,
# the finale); keen6 = 2 (Guard Post 1) + 17 (BWB Megarocket, the finale) --
# keen6 L1 Bloogwaters is 923 MTs, over the cap, so Guard Post 1 stands in.
# Builds share src/gen, so they are NOT parallel-safe -- each target fully
# regenerates src/gen for its episode before linking. ---
build/keen5.nes: third_party/unlzexe/unlzexe
	mkdir -p build src/gen assets/extracted/ck5 assets/converted/ck5
	# see the ck4 target for why unlzexe runs inside the target dir (DOS paths)
	@if [ ! -f assets/extracted/ck5/keen5_unpacked.exe ]; then \
	  ( cd assets/extracted/ck5 && ../../../third_party/unlzexe/unlzexe \
	      ../../../original/Commander_Keen_5/KEEN5E.EXE keen5unp.exe ) \
	    && mv -f assets/extracted/ck5/keen5unp.exe assets/extracted/ck5/keen5_unpacked.exe \
	    || cp original/Commander_Keen_5/KEEN5E.EXE assets/extracted/ck5/keen5_unpacked.exe; fi
	@if [ ! -f assets/extracted/ck5/EGAHEAD.BIN ]; then \
	  KEEN_EP=5 python3 tools/extract_tables.py; fi
	@if [ ! -d assets/extracted/ck5/gfx ]; then \
	  KEEN_EP=5 $(PY) tools/extract_assets.py; fi
	KEEN_EP=5 $(PY) tools/gen_mmc5_level.py 1 13
	KEEN_EP=5 $(PY) tools/gen_player_data.py
	KEEN_EP=5 $(PY) tools/gen_mmc5_rom.py 1 13
	KEEN_EP=5 $(PY) tools/gen_sfx.py
	KEEN_EP=5 $(PY) tools/gen_music.py 1 13
	KEEN_EP=5 $(PY) tools/gen_mmc5_title.py
	KEEN_EP=5 $(PY) tools/gen_status.py
	$(CC) $(KEEN4_CFLAGS) -o $@ $(KEEN4_SRCS) -Wl,-Map=build/keen5.map
	@ls -la $@

keen5: build/keen5.nes

build/keen6.nes: third_party/unlzexe/unlzexe
	mkdir -p build src/gen assets/extracted/ck6 assets/converted/ck6
	# see the ck4 target for why unlzexe runs inside the target dir (DOS paths)
	@if [ ! -f assets/extracted/ck6/keen6_unpacked.exe ]; then \
	  ( cd assets/extracted/ck6 && ../../../third_party/unlzexe/unlzexe \
	      ../../../original/Commander_Keen_6/keen6.exe keen6unp.exe ) \
	    && mv -f assets/extracted/ck6/keen6unp.exe assets/extracted/ck6/keen6_unpacked.exe \
	    || cp original/Commander_Keen_6/keen6.exe assets/extracted/ck6/keen6_unpacked.exe; fi
	@if [ ! -f assets/extracted/ck6/EGAHEAD.BIN ]; then \
	  KEEN_EP=6 python3 tools/extract_tables.py; fi
	@if [ ! -d assets/extracted/ck6/gfx ]; then \
	  KEEN_EP=6 $(PY) tools/extract_assets.py; fi
	KEEN_EP=6 $(PY) tools/gen_mmc5_level.py 2 17
	KEEN_EP=6 $(PY) tools/gen_player_data.py
	KEEN_EP=6 $(PY) tools/gen_mmc5_rom.py 2 17
	KEEN_EP=6 $(PY) tools/gen_sfx.py
	KEEN_EP=6 $(PY) tools/gen_music.py 2 17
	KEEN_EP=6 $(PY) tools/gen_mmc5_title.py
	KEEN_EP=6 $(PY) tools/gen_status.py
	$(CC) $(KEEN4_CFLAGS) -o $@ $(KEEN4_SRCS) -Wl,-Map=build/keen6.map
	@ls -la $@

keen6: build/keen6.nes

# Build all three episodes in sequence (shared src/gen -> must be serial).
episodes:
	$(MAKE) keen4
	$(MAKE) keen5
	$(MAKE) keen6

# --- MMC5 platform proof (not the game; a standalone mapper self-check).
# "hello MMC5 ExRAM" renders one screen with >256 distinct 8x8 tiles AND
# per-8x8 palettes via ExRAM extended attributes -- both impossible under a
# plain nametable. ---
MMC5_CC := $(LLVM_MOS)/bin/mos-nes-mmc3-clang
MMC5_SRCS := src/mmc5/reset_mmc5.s src/mmc5/hello_mmc5.c src/mmc5/gen_mmc5_data.c

build/hello_mmc5.nes:
	mkdir -p build
	$(PY) tools/gen_mmc5_proof.py
	$(MMC5_CC) -Os -Tsrc/mmc5/mmc5_hello.ld -o $@ $(MMC5_SRCS) \
	  -Wl,-Map=build/hello_mmc5.map
	@ls -la $@

hello_mmc5: build/hello_mmc5.nes

clean:
	rm -rf build src/gen src/mmc5/gen_mmc5_data.c

.PHONY: all keen4 keen5 keen6 episodes clean hello_mmc5 \
        build/keen4.nes build/keen5.nes build/keen6.nes build/hello_mmc5.nes
