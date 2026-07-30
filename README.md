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
