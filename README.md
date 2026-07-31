# isometric-bratislava

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Preparing the tiles

Measure the overlaps and cut the gapless tile grid:

```bash
.venv/bin/python scripts/stitch.py --cache offsets.json --mosaic mosaic.jpg --preview preview.jpg
```

## Re-rendering tiles through an image model

`scripts/subtiles.py` pastes a run of adjacent tiles into a strip and cuts it
into overlapping square crops (default 808 px, 50% overlap). The overlap is what
later hides the seams — each crop comes back re-rendered independently.

```bash
.venv/bin/python scripts/subtiles.py 3_4 4_4        # -> subtiles/3_4+4_4/
```

Re-render the crops elsewhere, keeping the filenames (they carry each crop's
origin, so any uniform output scale is fine), then put them in one folder and
stitch them back:

```bash
.venv/bin/python scripts/reassemble.py subtiles/processed --names 3_4 4_4
```

That writes full-size tiles to `tiles-processed/`. It removes the per-crop
exposure differences with Brown-Lowe gain compensation and blends across Voronoi
seams with a Laplacian pyramid, so neither the crop grid nor the tile boundary
shows.

## Viewer

`tiles/` is too heavy to serve directly, so `scripts/pyramid.py` re-cuts it into
a zoom pyramid — square tiles at halving resolutions — that the page loads on
demand. Generate it into `web/tiles/`:

```bash
.venv/bin/python scripts/pyramid.py
```

Then serve `web/` and open <http://localhost:8000>:

```bash
python3 -m http.server 8000 --directory web
```

Drag to pan, scroll to zoom. Re-run `pyramid.py` whenever `tiles/` changes.

If `tiles-processed/` holds any processed tiles, a second pyramid is built
from them and a **Raw / Processed** switcher appears in the top right (the `T`
key cycles). Tiles not yet re-rendered fall back to their raw version, so the
processed layer is always a complete image. `--raw-only` skips it.

### 8-bit layers

`--pixel` adds one more layer per pixel size, each the processed layer run
through `scripts/pixelate.py`: block-reduced to that pixel size and snapped to
the standard 256-colour 8-bit palette (`rgb332` — 8 reds, 8 greens, 4 blues).
The AI renders only *look* like pixel art; this gives them real pixels of a
known size and a real palette.

Only the re-rendered tiles are treated. The raw photography filling the rest of
the grid passes through untouched, so the 8-bit layers show exactly how far the
re-render has got, and the border between the two is a tile edge.

```bash
.venv/bin/python scripts/pyramid.py --pixel 1,2,4
```

`1` is palette-only at full resolution, `2` and `4` halve and quarter the pixel
grid. Sizes must divide the source tile (1616 px), which keeps the pixel grid
aligned across the whole mosaic — nothing lands on the seams. The viewer draws
these layers with nearest-neighbour magnification.

`pixelate.py` also runs standalone on any folder of images, where it can use
filters that the tiled path cannot (sharpening and Lanczos reach across a tile
edge, so they would show at the seams):

```bash
.venv/bin/python scripts/pixelate.py subtiles/processed -o subtiles/pixelart \
    --block 4 --filter lanczos --sharpen 0.9
```
