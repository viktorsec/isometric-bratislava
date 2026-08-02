# isometric-bratislava

A 148,240 × 64,528 aerial mosaic of Bratislava — 9.6 gigapixels, being redrawn square by square as isometric pixel
art, with a browser viewer to pan around it and to hand squares to an image
model and take them back.

```bash
make venv     # .venv + requirements.txt
make help     # every target
```

The `make` targets run from the project root and pick the right interpreter.
The commands behind them are given below, since most take arguments.

## Stitching Reference Frames

```bash
.venv/bin/python scripts/stitch.py --cache offsets.json --jobs 12 \
    --preview preview.jpg --preview-width 6000
```

Cropping each frame on a fixed grid does not work: Earth Studio's keyframes miss
their nominal camera positions by up to a few hundred pixels, the camera is
perspective (so a building is imaged at a different scale in each frame that
sees it), and giving every frame its own affine correction compounds — the scale
gradient makes each frame 1.5% larger than its neighbour, 3.4× over 84 columns.

So one homography `Phi` is fitted for the whole capture — every frame is the
same camera in the same pose, only moved — and each frame then needs only its
own position and a small damped correction. Tiles are cut from frame centres,
where the leftover error is smallest, and sized so neighbours butt together
exactly.

Seams come out at **0.31 px median, 95% under 2 px** on the 85 × 37 capture; the
rest is relief displacement, which no global warp can fix. Frames are never all
held in memory and the measurement pass is cached and resumable. Details in
[CAPTURE.md](CAPTURE.md#one-projection-shared-by-every-frame).

## Viewer

`tiles/` is too heavy to serve directly, so `pyramid.py` re-cuts it into square
tiles at halving resolutions that the page loads on demand:

```bash
make pyramid                       # tiles/ -> web/tiles/
make serve                         # http://localhost:8000  (PORT=8080)
```

`make viewer` does both; re-run `make pyramid` whenever `tiles/` changes.

| input | does |
|---|---|
| drag, scroll, double-click | pan, zoom, zoom in |
| `+` `-` `0` `1` | zoom in, out, fit, 1:1 |
| `G` | the export/import grid |
| `R` | the redrawn overlay |
| `T` | cycle renderings |
| `P` | prompt builder |

The URL keeps the position, zoom and rendering, so a view can be linked. Zoom
stops where one image pixel covers one device pixel; `?zoom=N` lifts that
ceiling by N for inspecting tiles.

`make serve` is `scripts/serve.py`, which is `http.server` over `web/` plus the
two paths that point outside it — `prompt.json` at the root and `redrawn-cells/`
for the grid to write into. It is stdlib only and needs no venv. A plain static
server still shows the mosaic, but not the prompt builder or imports.

## Redrawing

**G** draws the grid handed to the image model: 1024 px squares overlapping by
128 px on every side, which is what later hides the seams. Hovering one gives a
button that composes it at full resolution and downloads it as
`<layer>_c<col>_r<row>_x<x>_y<y>.png`.

Neighbours that are already redrawn are painted into that export over the
photography they were made from, so each shared 128 px band leaves as finished
pixel art for the model to continue rather than as photography it has to guess
at. The inner 48 px of the band fades out into the photography — most of the
strip still arrives at full strength to be copied, but the join is not a hard
line, which a model would otherwise read as an edge and draw back into its
output. The square's own re-render is left out — exporting a finished cell means
re-rolling it, and handing a model its own output back only entrenches whatever
it got wrong. Tick **Keep the redrawn edges** in the prompt builder (**P**,
composed from [prompt.json](prompt.json)) to tell it so.

Drop the re-rendered square back onto its cell and it is filed in
`redrawn-cells/` under the name it left with — the grid has to be on, and
dropping onto a cell that already holds one asks first. Any square image works;
a cell only ever keeps one file. **R** toggles them as an overlay over whichever
rendering is showing, so the drops accumulate into a view of how far the
re-render has got, with no pyramid to rebuild.

The older strip route is still what feeds `tiles-processed/`: `subtiles.py`
pastes adjacent tiles into a strip and cuts overlapping crops, and
`reassemble.py` puts the re-rendered crops back, removing per-crop exposure
differences with Brown-Lowe gain compensation and blending across Voronoi seams
with a Laplacian pyramid.

```bash
.venv/bin/python scripts/subtiles.py 3_4 4_4                     # -> subtiles/3_4+4_4/
.venv/bin/python scripts/reassemble.py subtiles/processed --names 3_4 4_4
```

Once `tiles-processed/` holds anything, `pyramid.py` builds a second pyramid
from it and a **Raw / Processed** switcher appears. Tiles not yet re-rendered
fall back to raw, so that rendering is always a complete image. `--raw-only`
skips it.

## Postprocessing

```bash
.venv/bin/python scripts/pyramid.py --pixel 1,2,4
```

One more rendering per pixel size, each the processed one run through
`pixelate.py`: block-reduced and snapped to the standard 256-colour palette
(`rgb332`). The AI renders only *look* like pixel art; this gives them real
pixels of a known size and a real palette. `1` is palette-only at full
resolution, `2` and `4` halve and quarter the pixel grid. Sizes must divide the
source tile (1648 px) so the pixel grid stays aligned across the mosaic and
nothing lands on a seam.

Only re-rendered tiles are treated — the raw photography passes through
untouched, so the border between the two is a tile edge.

`pixelate.py` also runs standalone, where it can use filters the tiled path
cannot (sharpening and Lanczos reach across a tile edge, so they would show at
the seams):

```bash
.venv/bin/python scripts/pixelate.py subtiles/processed -o subtiles/pixelart \
    --block 4 --filter lanczos --sharpen 0.9
```
