# Capturing Bratislava as a near-orthographic dimetric mosaic

Process notes for producing a large stitched, near-orthographic image of downtown
Bratislava from Google Earth Studio.

## The problem

Google Maps' web client and Earth Studio both render with a **perspective** camera.
An isometric/dimetric image needs **parallel** projection. Earth Studio has no
orthographic mode, so the approach here is a long-lens approximation: narrow the
field of view until perspective convergence falls below the visible threshold, then
capture a grid of frames and stitch them.

This is an approximation, not a fix — see [Known limitations](#known-limitations).

## Camera settings

| Setting | Value | Notes |
|---|---|---|
| Altitude | `12000` m | Measured sweet spot for texture detail |
| Tilt | `63.43` | Earth Studio units — see convention note below. **The 2026-07 renders used `64.43`; see [As rendered](#as-rendered).** |
| Pan | `0` | Due north |
| Field of view | `1°` | Earth Studio's minimum — `fov` sits at relative 0 in the `.esp` |
| Resolution | `4096 × 4096` | Project settings |
| Format | PNG | JPEG artifacts hurt stitcher feature matching |
| Frame rate | any | Irrelevant to stills; only frame *count* matters |

### Tilt convention

**Earth Studio measures tilt from nadir (straight down), not from the horizon.**

Tilt `63.43` = **26.57° above horizontal** = `atan(0.5)` = the 2:1 dimetric angle.

This was discovered empirically: at tilt `60`, the camera sat 20.5 km from the point
it was photographing, which only makes sense at 30° above horizontal (`90 − 60`), not
60°. Every angle in this document is stated as **elevation above horizontal** unless
labelled as an Earth Studio value.

### Camera position is not the ground position

At 26.57° elevation the camera sits `altitude / tan(26.57°)` = **24,000 m** — exactly
twice the altitude — horizontally from the centre of frame. With pan 0 that offset is
due north, so the coordinates you type are ~0.216° of latitude south of what you
actually photograph.

Empirical anchor: **camera `47.9301, 17.1085` centres the frame on Hlavné námestie**
(≈ `48.1436, 17.1077`).

All coordinates in this document are **camera positions**, not ground positions.
Using a camera target instead would remove this offset entirely.

## Derived geometry

With `alt = 12000`, `elev = 26.57°`, `FOV = 1°`:

| Quantity | Formula | Value |
|---|---|---|
| Slant distance `D` | `alt / sin(elev)` | 26,833 m |
| Camera ground offset | `alt / tan(elev)` | 24,000 m |
| Frame width (cross-view, E–W) | `2 · D · tan(FOV/2)` | 468 m |
| Frame depth (along-view, N–S) | `width / sin(elev)` | 1,047 m |
| Ground sampling | `width / 4096` | 0.114 m/px |

Ground sampling of 0.114 m/px sits inside Google's source texture resolution
(roughly 0.10–0.15 m/texel in well-covered centres), so the render neither wastes
pixels on empty magnification nor discards available detail. This is why 12,000 m
tested best.

### Anisotropy

The footprint is **2.236× deeper than it is wide** (`1 / sin(26.57°)`), because the
oblique view stretches the ground along the viewing direction. Grid spacing must be
stretched by the same factor N–S, or northward steps overlap far more than eastward
ones.

The ratio for other angles: `1 / sin(elev)` — 2.0 at 30°, 1.41 at true isometric
(35.26°).

## Step size

Steps are chosen so a given building appears in **2–3 consecutive frames** in both
axes. Appearance count is `floor(footprint / step) + 1`.

| Axis | Step (degrees) | Ground | On screen | Appearances |
|---|---|---|---|---|
| Longitude | `0.0027°` | 200.5 m | 1,754 px | 2–3 |
| Latitude | `0.0040°` | 444.9 m | 1,740 px | 2–3 |

The two steps differ on the ground but are **equal on screen** — that is the actual
target. Degrees-to-metres at this latitude: 1° lat = 111,230 m, 1° lon = 74,266 m.

Both values stay at four decimal places, which keeps the grid readable.

### Tuning

Earlier iterations used 133.5 m spacing (buildings visible in 4+ frames — too much
redundancy) and equal metric spacing in both axes (5+ appearances northward, because
the anisotropy was not yet accounted for). Widening beyond ~267 m in the E–W axis
starts to leave the stitcher too little overlap to match on reliably.

**For the next capture, use latitude step `0.0042°` rather than `0.0040°`.** The
2.236 ratio was computed for the intended 26.57° elevation; at the 25.57° actually
rendered the correct ratio is 2.317. The measured pixel steps confirm it — see
[Square tiles](#square-tiles). This makes the output tiles square with no resampling.

## The grid

Two captures exist. **The August 2026 one, 85 × 37, is the current source** and is
what `frames/` holds; the 6 × 6 below was the July pilot and is kept because the
geometry notes and the [As rendered](#as-rendered) tilt record refer to it.

### August 2026 — 85 × 37, the whole city

3145 frames, 40 GB, listed row by row in [`rows.csv`](rows.csv).

| | |
|---|---|
| Rows (N→S) | 37, latitude `48.0080` down to `47.8640`, step `0.0040°` |
| Columns (W→E) | 85, longitude `17.0230` to `17.2498`, step `0.0027°` |
| Coverage | ~16.5 km N–S × ~17 km E–W of ground |
| Stitches to | 148,240 × 64,528 px — 9.6 gigapixels, 3145 tiles of 1744 px |

Same steps as the pilot, so the overlap is unchanged, but the measured step came
out **1767 × 1741 px — very nearly square**, against 1709 × 1648 in July. Something
about the camera changed between the two (most likely the tilt slip being
corrected); the practical effect is that squaring the tiles now costs 1.5% rather
than 3.6%.

This capture spans hills, river and farmland rather than one dense district, and
that is what forced the stitching model to be rewritten — see
[One projection](#one-projection-shared-by-every-frame).

### July 2026 — 6 × 6, the pilot

6 × 6, rows ordered **north to south** so row 1 is the top of the stitched image
(the camera faces north, so north renders at frame top).

**Latitudes (rows, N→S):**
`47.9421` · `47.9381` · `47.9341` · `47.9301` · `47.9261` · `47.9221`

**Longitudes (columns, W→E):**
`17.1004` · `17.1031` · `17.1058` · `17.1085` · `17.1112` · `17.1139`

| Row | Latitude | Frame 1 lon | Frame 6 lon |
|---|---|---|---|
| 1 (N) | 47.9421 | 17.1004 | 17.1139 |
| 2 | 47.9381 | 17.1004 | 17.1139 |
| 3 | 47.9341 | 17.1004 | 17.1139 |
| 4 | 47.9301 | 17.1004 | 17.1139 |
| 5 | 47.9261 | 17.1004 | 17.1139 |
| 6 (S) | 47.9221 | 17.1004 | 17.1139 |

Coverage: ~3.27 km N–S × 1.47 km E–W on the ground, stitching to roughly
**12,870 × 12,800 px** — square once the N–S compression is applied.

## As rendered

The first full 6 × 6 capture (July 2026, 36 frames) went out at tilt **`64.43`**
rather than the specified `63.43` — a one-digit slip. Decoded from the `.esp`
files, `rotationY` is exactly `0.35794444` × 180 = 64.43.

That puts the camera at **25.57° above horizontal** instead of 26.57°, so the
projection ratio is 2.09:1 rather than exactly 2:1 — 4.3% off true dimetric, which
is not visible. What does shift is the geometry:

| Quantity | Specified (26.57°) | As rendered (25.57°) |
|---|---|---|
| Slant distance | 26,833 m | 27,802 m |
| Camera ground offset | 24,000 m | 25,077 m |
| Frame width (E–W) | 468 m | 485 m |
| Frame depth (N–S) | 1,047 m | 1,124 m |
| Ground sampling | 0.114 m/px | 0.118 m/px |
| Anisotropy `1/sin(elev)` | 2.236 | 2.317 |

The larger camera offset means **every frame centre sits ~1,077 m north of plan**
(0.0097° of latitude), so the mosaic is centred that far north of Hlavné námestie
rather than on it. With 3.27 km of N–S coverage the main square is still well inside
the frame, just south of centre.

Overlap is unaffected in practice: appearance counts stay at 3 in both axes
(`485/200.5` and `1124/444.9`), and the step ratio of 2.219 against an ideal 2.317
is a 4% mismatch that costs nothing.

**Verdict: usable as-is.** Re-rendering 36 frames to recover a 1 km framing shift and
a 4% projection correction is not worth it. Fix the tilt on the next capture.

Two things that could not be confirmed from the `.esp`: the files carry no
`interpolation` / `easing` keys, so keyframe linearity is not verifiable offline —
check visually that frame spacing looks even across a row. Actual row latitudes are
`47.9422 / 47.9382 / …`, one ten-thousandth north of the values tabulated below;
spacing is exactly `0.0040°` throughout, so this is UI rounding and has no effect.

## Render procedure

One render per row, six rows, 36 frames total.

1. Set altitude, tilt, pan, FOV, and resolution as above. These never change.
2. Set the row's latitude as a static value.
3. Keyframe longitude: frame 1 = `17.1004`, frame 6 = `17.1139`.
4. **Set both keyframes to Linear interpolation.** Earth Studio eases by default,
   which bunches frames at the ends and spreads them through the middle — overlap
   would swing from ~80% at the edges to gaps in the centre.
5. Render frames 1–6.
6. Repeat for each row, changing only the latitude.

Scrub to the last frame and confirm longitude reads exactly `17.1139` before
rendering — an end keyframe placed one frame short truncates the sweep and duplicates
the final frame.

Keep Google's attribution in the export; it is required by the licence.

## Collecting the frames

Earth Studio writes one folder per render in download order, and every folder's
footage may be named identically (`bratislava2_0.jpeg` … `bratislava2_5.jpeg`).
Folder names carry no grid meaning: the script never uses them.

`scripts/collect_frames.py` discovers every `.esp` project under `raw-frames/`,
uses the sibling `footage/` directory for its frames, recovers the real camera
latitude and longitude keyframes, and flattens everything into one directory named
by true grid position:

```
frames/<x>_<y>.jpeg     x = column, 0 = west  -> increasing east
                        y = row,    0 = north -> increasing south
```

matching image orientation, since the camera faces north. It dry-runs by default:

```
python3 scripts/collect_frames.py            # show the mapping
python3 scripts/collect_frames.py --apply    # move
python3 scripts/collect_frames.py --apply --copy
```

Pass `--root <directory>` to collect another capture directory. By default output
goes to the `frames/` sibling of that input directory.

It refuses to run if two folders share a latitude, if rows have unequal frame counts,
if frame indices are not contiguous, or if any destination already exists.

## Stitching

`scripts/stitch.py` turns the overlapping frames into a regular, gapless tile
grid. It does **not** blend or feather — it resamples each frame's central region
into the tile that exactly abuts its neighbours, which is both simpler and better:
every tile comes from the middle of its frame, where relief displacement is
smallest.

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/stitch.py --cache offsets2.json --jobs 12 \
    --out tiles --preview preview2.jpg --preview-width 6000
```

The grid size is read from the frame filenames, so nothing needs telling how big
the capture is. `--region X0 Y0 NX NY` restricts the whole pipeline to a
sub-rectangle, which is how to try settings on a corner of a large capture
without waiting for all of it.

### How it works

1. **Coarse align** each adjacent pair with overlap-normalised cross-correlation on
   1/8-scale images. Plain phase correlation fails here: the offsets are ~40% of the
   frame width, and a Hann window destroys the very overlap being matched. Dividing
   the correlation by the overlap area removes the bias toward zero shift.
2. **Sample and fit.** Phase-correlate a 7 × 7 grid of 384 px windows spanning the
   overlap, and fit an affine to the resulting offset field. One window would only
   report the offset where it was placed.
3. **Screen and re-measure.** A pair that will not fit is almost never the imagery
   — it is the coarse align picking a wrong peak over water, forest or ploughed
   fields. Neighbouring pairs in the same row agree to a pixel or two, so their
   median is a far better starting offset than correlation, and re-measuring from
   it recovers essentially all of them. On the 2026-08 capture: 327 rejects of
   6168, **327 of 327 recovered**, nothing substituted.
4. **Fit the shared projection** `Phi` — one homography for the entire capture.
   See [One projection](#one-projection-shared-by-every-frame).
5. **Least-squares solve** for each frame's placement plus a small, damped affine
   correction, all pairs at once, imposed in a band about the shared edge so the
   residual is a distance in pixels exactly where a seam is visible.
6. **Resample and emit** uniform tiles through PIL's perspective transform.

Two self-tests on synthetic data run at startup: one pins the correlation
conventions (a sign or inverse error there silently correlates the wrong content
and returns plausible-looking noise), the other round-trips the projection fit and
the grid solve against a capture whose geometry is known — including a check that
the per-frame correction stays near a pure translation, which is what fails first
when the model is wrong.

### One projection, shared by every frame

The camera is perspective, so each frame is a perspective image of one ground plane
and two frames relate by a **homography — never by a translation**, and not by an
affine either. The measured pair relations:

| Neighbour | Measured relation (median over thousands of pairs) |
|---|---|
| East (x → x+1) | scale 1.00304 × 1.00000, **shear `dx += 0.0105·y`** |
| South (y → y+1) | **scale 1.01520 × 1.04490**, shear ~0 |

Both fall straight out of the geometry. Stepping east, image x-scale depends on
range and range depends on the image row, so the displacement shears down the frame.
Stepping south moves the overlap from one frame's near field into the next frame's
far field, so it changes scale — and the along-view term is twice the cross-view
one (4.5% against 1.5%), exactly the oblique 2:1 relationship.

The right model is not a transform per pair at all. **Every frame is the same
camera in the same pose, moved**, so a single homography describes the whole
capture:

    T_f(m) = Phi (m - delta_f)

Eight numbers for the mosaic, two per frame for where it sits. The pair relations
follow and are not free — for neighbours one step `D` apart, `A_ab = Phi ·
translate(D) · Phi⁻¹`, the same matrix for every pair in a direction. So `Phi` is
fitted from the median pair affine, 8 unknowns against thousands of measurements,
and it reproduces them to **0.07 px**.

#### Why not an affine per frame

That was the previous model and it does not survive a large capture. An affine
cannot be conjugated into itself by a translation: `A_ab` has a linear part of
`I + 0.0146`, and satisfying `T_a = A_ab · T_b` with affine `T` forces every
frame's scale to be its neighbour's times that factor. There is no way out of it:

| Affine per frame | Result on 85 × 37 |
|---|---|
| Let the scale follow the pairs | compounds 1.0146⁸⁴ ≈ **3.4×**; tiles sample **24,000 px** outside their frames |
| Hold the scale flat | every seam carries a **14 px ramp**, of slope exactly 0.0146 |

The 6 × 6 capture hid this completely — over five steps the same factor is 1.24×,
and the seams still closed to 1.7 px. A homography *is* closed under the
conjugation, so the same 0.0146 costs nothing: it was never a per-frame scale, it
is `Phi` being evaluated half a tile either side of the seam.

Upgrading the *pair* model to a homography does not help (loop closure 2.84 → 2.72
px) — the pair fits were never the problem.

#### What is left

Relief displacement. The ground is not the plane `Phi` assumes, and a hill or a
tower straddling a seam is imaged from two slightly different directions, so the
pair measurements **do not close their loops** — 1.2 px around a typical 2 × 2,
up to 22 px over the Malé Karpaty. No global warp of any kind can fix that.

Each frame therefore also carries a small affine correction, penalised toward
zero (`--prior`, default 1.0, in pixels of tile-corner drift per pixel of seam).
The penalty matters: corrections that vary smoothly across the grid are nearly
free in the pair constraints, and the non-closing loops drive them hard. Left
unpenalised the scale spread reaches **59%** and the grid breathes its tiles out
of their frames; at 1.0 it stays under 1% and the seams still close.

### Measured result (August 2026 capture, 85 × 37)

Step between tile centres. Each tile is cut from the middle of its frame, so the
two centres sit at the same place in their own frames and the step is the `s`
satisfying `A(c − s/2) = c + s/2` — the displacement at the frame centre corrected
for that, which is 26 px of the N–S figure and not cosmetic, since the step sets
the tile size and its error accumulates as crop drift across the grid.

| Step | dx | dy | spread |
|---|---|---|---|
| Horizontal (x → x+1) | **+1767.47 px** | ~0 px | 1762.4–1809.0 |
| Vertical (y → y+1) | ~0 px | **+1740.98 px** | 1738.5–1785.7 |

Cross-axis terms are under 1 px, confirming pan is exactly 0 and the grid is
axis-aligned.

- 6168 pairs measured in ~3 min on 12 workers; 327 rejects, all 327 recovered by
  re-measuring from the local median, **0 substituted**
- Shared projection reproduces the median pair relation to **0.07 px**
- Solve: 18,870 unknowns, 111,024 constraints, 3.9 s
- Solve residual: 0.57 px rms, 7.82 px max (in the seam bands)
- Seam error: **0.31 px median, 0.59 px mean**, 3.28 px p99, 7.62 px max —
  **95% of seams under 2 px, 100% under 5 px**
- Crop margin: 307 px at the tightest frame, of 1164 px nominal
- The Google Earth watermark at y≈3480 falls outside every crop and disappears
  without inpainting. The licence still requires attribution on anything published.

Better than the 6 × 6 capture's 1.68 px mean, over 87× the area — because the
shared projection removed a systematic error the small grid was too small to
expose, not because anything about the imagery improved.

The worst seams cluster around x=21–27, y=3–7, over the Malé Karpaty, where the
terrain is steepest. That is relief displacement and it is not a solvable
alignment error.

**The step is now square** (1767 × 1741, 1.5% apart) rather than the 1709 × 1648
of the July capture, so squaring the tiles costs almost nothing. This capture used
latitude step 0.0040° over 37 rows; the near-square result suggests the tilt was
also corrected — worth confirming from the `.esp` files if it matters.

### Square tiles

Default output is **1744 × 1744** (16 × 109), a mosaic of **148,240 × 64,528** —
9.6 gigapixels, which is why it is never assembled in memory.

The source step is 1767.47 × 1740.98, so squaring costs a **1.5% horizontal
squash** — against 3.6% for the July capture, whose latitude step under-shot the
projection anisotropy badly enough to matter. `--native` keeps 1767 × 1741 and
asks for no scale at all if fidelity matters more than tile shape.

The squash costs nothing extra to apply. It is a scale inside the shared
projection, so it rides along in the one interpolation the perspective correction
already required. That interpolation is a plain sampler with no prefilter of its
own, unlike `resize`, so tiles are rendered at 2× and box-reduced
(`--supersample`); at a 1.5% downscale that is cheap insurance rather than a
necessity, but it costs one pass over 4× the pixels, not a generation of quality.

The sub-pixel residual that remains is relief displacement, not a solvable
alignment error — a building straddling a seam has its roof displaced differently
in the two source frames. No global warp can fix it, homography or otherwise; only
a narrower FOV or a tighter crop reduces it. Tall towers and the hills show it
most.

**Colour, not geometry, is what is now visible.** At preview scale the mosaic
shows large patches of differing tint and season. Those are Google's own source
imagery boundaries — different aerial surveys meeting inside the photogrammetry —
and they do not align with the tile grid, so no stitching change will affect them.
Fixing them needs colour matching, which this script deliberately does not do.

## The web viewer

`scripts/pyramid.py` re-cuts the tile grid into the halving levels a slippy-map
viewer wants, and `web/` is the viewer.

```
.venv/bin/python scripts/pyramid.py --jobs 12
python3 -m http.server -d web 8000        # then open localhost:8000
```

For the 2026-08 capture that is **10 levels, 49,246 tiles, ~2.9 GB** of WebP,
from 148,240 × 64,528. The viewer holds a few dozen tiles at a time regardless.

Neither the pyramid nor the viewer has the grid size written into it — the
pyramid reads it from the tile filenames and writes `info.js`, and the viewer
reads that. Going from 6 × 6 to 85 × 37 needed no change to either beyond
memory:

- **No level is ever held whole.** The finest is cut row by row from a handful
  of decoded source tiles; every coarser level is built from the four children
  of each tile, read back off disk. The previous version halved each level as
  one image, which at this size is a 7.2 GB buffer. Peak is now a few tiles per
  worker — ~1 GB total at `--jobs 12`, and flat in the size of the mosaic.
- **The reduction had to become strictly local.** `resize(..., BOX)` is an
  exact 2 × 2 mean only when both dimensions are even; asked for `ceil(w/2)`
  from an odd `w` its support slides against the pixel grid all the way across
  the level. Harmless in one pass over a whole level, wrong when the level is
  assembled tile by tile — it would put a line down every seam. `half()` now
  does the 2 × 2 mean itself with the odd last row or column doubled, which is
  local by construction and bit-identical to the whole-level result.
- **Stale layer directories are removed.** A layer present in an earlier build
  and not the current one used to be left on disk with the old geometry.

### Re-running

`--cache offsets2.json` skips re-measuring, which dominates runtime. The cache is
versioned and records the settings the measurements were taken under; a stale one
is ignored with a notice rather than half-applied. A **partial** cache is resumed
rather than discarded, and the measurement pass flushes to it every minute — at
this scale, losing 40 minutes of measuring to a stray Ctrl-C is the single most
expensive mistake available. `--measure-only` stops after it.

`--jobs J` sets worker processes (default `min(8, cpus)`); each needs roughly
200 MB, so peak memory is set by this and not by the size of the capture.
`--region X0 Y0 NX NY` restricts everything to a sub-rectangle, tiles keeping
their global names. `--resume` skips tiles already written.

`--tile-size S` forces a square tile size, `--multiple M` changes the rounding
granularity (default 8), and `--native` disables squaring altogether. `--window`
and `--samples` set the correlation window and how many of them span each overlap;
`--supersample` the resampling factor; `--prior` how hard per-frame corrections
are held to the shared projection. `--format jpeg` trades a little quality for
much smaller files; the default PNG avoids a second generation of JPEG loss —
3145 tiles come to ~16 GB.

`--mosaic` is refused above `--mosaic-budget` megapixels (default 1000), because
the full mosaic is 9.6 Gpx. Use `--preview`, which is assembled from per-tile
thumbnails as they are written and so costs no extra memory.

Three implementation notes if this is ever modified:

- **The tile size enters the solve**, unlike in the old affine code. Continuity is
  still a property of the frame maps, but the constraints are imposed in a band
  about each shared edge, and where that edge falls depends on the tile size. So
  it is fixed before the solve rather than chosen after.
- **Constraints must stay near the seam.** The pair affine is a local truth,
  accurate where it was measured and nowhere else; imposing it at the far corners
  of both tiles is what produced 24,000 px of drift. Equally, they cannot be only
  *on* the seam line — that leaves the derivative across it unconstrained and the
  solve rank-deficient. Hence a band, a quarter tile either side.
- **A tile cannot be clipped back into its frame** the way a crop origin could:
  moving one breaks its joint with all four neighbours. A crop that runs off the
  frame or reaches the watermark is therefore a hard stop, not a nudge.

## Known limitations

**Perspective is approximated, not eliminated.** Near-to-far scale error across a
frame is `tan(FOV/2) / tan(elev)` = **±1.75%** at these settings.

Between frames this is now corrected exactly, not approximately: the shared
projection *is* the perspective, so the scale and shear it implies cost nothing to
satisfy, and seams closed from 10 px (translation) through 1.7 px (affine per
frame, small grids only) to 0.3 px. What survives is the part no reprojection can
reach, because it is not a property of the plane at all — **relief displacement**,
`h · tan(FOV/2)` at the frame edge, 0.22 m for a 25 m building, and **independent
of altitude**. A building straddling a seam has its roof placed differently in the
two frames that see it, so the roof can never be made to agree at the same time as
the ground. Only a narrower FOV or a tighter crop reduces it.

This is measurable: the pair relations do not close around a 2 × 2 loop, by 1.2 px
typically and up to 22 px over the hills. That number is the floor on seam error,
and no model — affine, homography, or a full bundle adjustment — gets below it
without modelling the terrain itself.

Within a frame the scale error is untouched — the mosaic is stitched from
perspective imagery, not reprojected into a parallel one, so a tile still runs
±1.75% in scale from one edge to the other. That is invisible as a gradient and only
mattered where two of them met.

**Pan is 0, not 45°.** Dimetric is a tilt *and* a heading. At pan 0 this is a 2:1
vertical compression of a head-on northward view; the classic dimetric look needs pan
`45` (or 135 / 225 / 315) so both building axes project symmetrically. Switching would
split the 24 km offset into 16,971 m north and 16,971 m east — camera roughly
`47.9910, 16.8792` for the same target — and needs re-anchoring by eye. The frame
footprint would then sit at 45° to the grid, giving serrated mosaic edges to crop.

**Facades are partly occluded.** At 26.57° elevation, a 20 m building hides everything
below ~14 m on the facade across a 12 m street, so Staré Mesto reads as mostly roofs
and upper storeys. Raising elevation opens the streets up at the cost of foreshortening
the walls; the visible-facade optimum for dense narrow streets is around 60°, where
nothing is occluded at all. This is a deliberate trade for the exact 2:1 ratio.

## The exact alternative

If stitch quality proves insufficient, the correct solution is a genuine orthographic
camera over the same data. Google's Photorealistic 3D Tiles (Map Tiles API) serve the
identical photogrammetry mesh as glTF; loaded in CesiumJS or three.js, an
`OrthographicFrustum` gives true parallel projection. Frames then tile with **zero**
parallax — step the camera target by exact metre offsets and adjacent renders align to
the pixel. LOD also becomes uniform across the image rather than falling off with
distance.

Earth Studio can get close to orthographic. It cannot get there.
