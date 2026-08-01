# isometric-bratislava

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Preparing Reference Tiles

Turn the overlapping frames into a grid of tiles that line up:

```bash
.venv/bin/python scripts/stitch.py --cache offsets.json --jobs 12 \
    --preview preview.jpg --preview-width 6000
```

Simply cropping each frame on a fixed grid does not work, for three reasons.

**The frames are not where they were asked to be.** Earth Studio's keyframes miss
the nominal camera positions by up to a few hundred pixels, so the offset between
two neighbours has to be measured. `stitch.py` phase-correlates a grid of windows
across each pair's overlap to get it.

**The camera is perspective.** Ground at the bottom of a frame is nearer the
camera than ground at the top, so it is imaged at a larger scale. The same
building therefore appears at a slightly different scale in each of the two
frames that see it, and no amount of shifting will line both up.

**Per-frame corrections compound.** Give every frame its own affine and that
scale gradient forces each one to be 1.5% larger than its neighbour. Over 84
columns it reaches 3.4×, and tiles end up sampling far outside their frames.

So the correction is fitted once, for the whole capture. Every frame is the same
camera in the same pose, only moved, so a single homography `Phi` describes all
of them; each frame then needs nothing but its own position and a small damped
correction. Tiles are cut from frame centres, where the leftover error is
smallest, and sized so neighbours butt together exactly. Details in
[CAPTURE.md](CAPTURE.md#one-projection-shared-by-every-frame).

Seams come out at **0.31 px median, 95% under 2 px** on the 85 × 37 capture. The
rest is relief displacement: the ground is not the flat plane `Phi` assumes, so a
hill or a tower is seen from a different angle in each frame, and no global warp
can fix that. Frames are never all held in memory, and the measurement pass is
cached and resumable.

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
grid. Sizes must divide the source tile (1648 px), which keeps the pixel grid
aligned across the whole mosaic — nothing lands on the seams.

Zoom stops where one image pixel covers one device pixel — 50% on a 2x screen,
100% on a 1x one. Past that there is nothing further to show. Append `?zoom=N`
to the URL to lift the ceiling to N times that for inspecting the tiles
themselves; 8-bit layers are then magnified with nearest neighbour.

`pixelate.py` also runs standalone on any folder of images, where it can use
filters that the tiled path cannot (sharpening and Lanczos reach across a tile
edge, so they would show at the seams):

```bash
.venv/bin/python scripts/pixelate.py subtiles/processed -o subtiles/pixelart \
    --block 4 --filter lanczos --sharpen 0.9
```
