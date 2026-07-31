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

A 1616 px tile is too big to hand to an image-to-image model in one piece, so
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

Then serve `web/` and open <http://localhost:8777>:

```bash
python3 -m http.server 8000 --directory web
```

Drag to pan, scroll to zoom. Re-run `pyramid.py` whenever `tiles/` changes.

If `tiles-processed/` holds any processed tiles, a second pyramid is built
from them and a **Raw / Processed** toggle appears (also the `T` key). Tiles not
yet re-rendered fall back to their raw version, so the processed layer is always
a complete image. `--raw-only` skips it.
