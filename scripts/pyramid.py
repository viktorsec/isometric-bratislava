#!/usr/bin/env python3
"""Build web tile pyramids from the stitched tile grid.

`stitch.py` emits one big tile per source frame — for the 2026-08 capture, 3145
PNGs of 1744 x 1744, 14 GB. That grid is a fine archival format and a terrible
delivery format: a viewer would have to pull the lot to show anything at all.

This script re-cuts the same pixels into the pyramid every slippy-map viewer
expects: square tiles at a series of halving resolutions, so the client only
ever fetches tiles that are both on screen and at roughly screen resolution.

    tiles/<x>_<y>.png        ->  web/tiles/raw/<z>/<x>_<y>.webp
                                 web/tiles/info.js

A second pyramid is built whenever `tiles-processed/` holds any processed
tiles (the pixel-art re-renders put back together by `reassemble.py`). It is
the same grid with those tiles substituted in and every tile still missing
taken from `tiles/`, so the viewer can toggle between the two without any gaps:

    tiles-processed/<x>_<y>.png  ->  web/tiles/processed/<z>/...

`--pixel` adds further layers on top of that same grid, each one the processed
grid run through `pixelate.py` at a different pixel size — the 8-bit renderings
the viewer can switch between. They cost no extra source files, only pyramid
tiles, because the transform happens as the sources are read.

Every pyramid shares one geometry — processed tiles must match their source tile
pixel for pixel — so `info.js` describes the levels once and lists the layers.

Nothing here ever holds a whole level. The finest one is cut row by row from a
handful of decoded source tiles, and every coarser level is built from the four
children of each of its tiles, read back off disk. Peak memory is a few tiles
per worker, independent of the size of the mosaic — which for the 2026-08
capture is 148,240 x 64,528, 9.6 gigapixels. Holding even the half-resolution
version of that, as the previous version did, would be 7.2 GB.
"""

import argparse
import json
import math
import os
import re
import shutil
import sys
import time
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

import pixelate

Image.MAX_IMAGE_PIXELS = None

NAME_RE = re.compile(r"^(\d+)_(\d+)$")
SAVE_OPTS = {
    "webp": lambda q: {"format": "WEBP", "quality": q, "method": 4},
    "jpeg": lambda q: {"format": "JPEG", "quality": q, "optimize": True,
                       "progressive": True, "subsampling": 1},
}


def scan(src: Path) -> dict[tuple[int, int], Path]:
    """Map (column, row) -> file for every `<x>_<y>.<ext>` in `src`."""
    found: dict[tuple[int, int], Path] = {}
    if not src.is_dir():
        return found
    for p in sorted(src.iterdir()):
        if p.is_dir() or p.name.startswith("."):
            continue
        m = NAME_RE.match(p.stem)
        if not m:
            continue
        key = (int(m.group(1)), int(m.group(2)))
        if key in found:
            raise SystemExit(f"two files claim tile {key}: {found[key]}, {p}")
        found[key] = p
    return found


def check_complete(grid: dict[tuple[int, int], Path], src: Path) -> tuple[int, int]:
    """Require a full rectangular grid; return its (cols, rows)."""
    if not grid:
        raise SystemExit(f"no <x>_<y> tiles found in {src}/")
    cols = max(x for x, _ in grid) + 1
    rows = max(y for _, y in grid) + 1
    missing = [(x, y) for y in range(rows) for x in range(cols)
               if (x, y) not in grid]
    if missing:
        raise SystemExit(f"grid is {cols}x{rows} but {len(missing)} tiles are "
                         f"missing, first {missing[0]}")
    return cols, rows


def level_dims(w: int, h: int, tile: int) -> list[tuple[int, int]]:
    """Dimensions per level, coarsest first; level 0 fits in one tile."""
    dims = [(w, h)]
    while dims[0][0] > tile or dims[0][1] > tile:
        pw, ph = dims[0]
        dims.insert(0, (math.ceil(pw / 2), math.ceil(ph / 2)))
    return dims


def half(im: Image.Image) -> Image.Image:
    """Exact 2x2 box reduction, odd edges replicated.

    Written out rather than left to `resize(..., BOX)` because the filter has
    to be strictly local. Each coarse tile here is built from its four children
    alone, so a filter whose support crossed the tile boundary would put a
    visible line down every seam of every level.

    `resize` is local only when both dimensions are even. Ask it for
    `ceil(w/2)` out of an odd `w` and the ratio is a shade under 2, so the
    support slides against the pixel grid all the way across the image — fine
    for one pass over a whole level, wrong when the level is assembled from
    pieces. A 2x2 mean with the odd last row or column doubled is local by
    construction, which makes tile-by-tile and whole-level reduction agree
    exactly.
    """
    a = np.asarray(im, dtype=np.uint16)
    if a.shape[0] % 2:
        a = np.concatenate([a, a[-1:]], axis=0)
    if a.shape[1] % 2:
        a = np.concatenate([a, a[:, -1:]], axis=1)
    h, w = a.shape[0] // 2, a.shape[1] // 2
    a = a.reshape(h, 2, w, 2, -1).sum((1, 3))
    return Image.fromarray(((a + 2) // 4).astype(np.uint8))


# --------------------------------------------------------------------------- #
# workers
# --------------------------------------------------------------------------- #
#
# Both phases are per-output-row tasks handed to a process pool, and neither
# ever holds more than a few tiles. That is the whole reason this file is
# shaped the way it is: the previous version built each coarse level by
# halving the level above *as one image*, which is fine for the 6 x 6 pilot
# (a 4944 x 4944 buffer) and impossible for the 85 x 37 capture, where the
# half-resolution mosaic alone is 74,120 x 32,264 — 7.2 GB, before counting the
# source cache or the copy `resize` makes.

_P: dict = {}


def _init_top(paths, stw, sth, width, height, tile, outdir, ext, save_kw,
              save_top, sharp, block, palette_name, processed, cache_tiles):
    _P.update(paths=paths, stw=stw, sth=sth, width=width, height=height,
              tile=tile, outdir=outdir, ext=ext, save_kw=save_kw,
              save_top=save_top, sharp=sharp, block=block,
              processed=processed, cache_tiles=cache_tiles,
              cache=OrderedDict(),
              palette=pixelate.FIXED_PALETTES[palette_name]() if block else None)


def _source(sx, sy):
    """One decoded source tile, transformed, from a small LRU.

    The cache is deliberately small — a handful of tiles, not the two full
    source rows the single-process version could afford. An output row sweeps
    left to right touching at most two source rows at a time, so six tiles is
    enough to never re-read within a row, and a source tile ends up decoded
    about `source_tile / pyramid_tile` times over the build. At 0.04 s a PNG
    that is a couple of minutes across the whole capture, against 1.5 GB per
    worker to avoid it.
    """
    cache = _P["cache"]
    im = cache.pop((sx, sy), None)
    if im is None:
        im = Image.open(_P["paths"][(sx, sy)]).convert("RGB")
        im.load()
        if _P["block"] and (sx, sy) in _P["processed"]:
            im = pixelate.pixelate(im, _P["block"], _P["palette"])
    cache[(sx, sy)] = im
    while len(cache) > _P["cache_tiles"]:
        cache.popitem(last=False)
    return im


def _sources_under(x0, y0, x1, y1):
    stw, sth = _P["stw"], _P["sth"]
    return [(sx, sy)
            for sy in range(y0 // sth, (y1 - 1) // sth + 1)
            for sx in range(x0 // stw, (x1 - 1) // stw + 1)]


def _cut_row(ty):
    """Write every finest-level tile in output row `ty`, cut from the sources."""
    tile, stw, sth = _P["tile"], _P["stw"], _P["sth"]
    width, height = _P["width"], _P["height"]
    y0, y1 = ty * tile, min((ty + 1) * tile, height)
    n = 0
    for tx in range(math.ceil(width / tile)):
        x0, x1 = tx * tile, min((tx + 1) * tile, width)
        im = Image.new("RGB", (x1 - x0, y1 - y0))
        for sy in range(y0 // sth, (y1 - 1) // sth + 1):
            for sx in range(x0 // stw, (x1 - 1) // stw + 1):
                ox, oy = sx * stw, sy * sth
                cx0, cy0 = max(x0, ox), max(y0, oy)
                cx1, cy1 = min(x1, ox + stw), min(y1, oy + sth)
                im.paste(_source(sx, sy).crop(
                    (cx0 - ox, cy0 - oy, cx1 - ox, cy1 - oy)),
                    (cx0 - x0, cy0 - y0))
        kw = _P["save_top"]
        if _P["sharp"] is not None and not any(
                s in _P["sharp"] for s in _sources_under(x0, y0, x1, y1)):
            kw = _P["save_kw"]
        im.save(_P["outdir"] / f"{tx}_{ty}.{_P['ext']}", **kw)
        n += 1
    return n


def _init_coarse(indir, outdir, ext, save_kw, tile, above):
    _P.update(indir=indir, outdir=outdir, ext=ext, save_kw=save_kw, tile=tile,
              above=above)


def _shrink_row(ty):
    """Write one coarse-level row from the four children of each tile.

    Reading the children back off disk rather than keeping the finer level in
    memory is what bounds this: a coarse tile needs 2x2 of them, 3 MB, and
    nothing else. They were just written, so they are still in the page cache.
    """
    tile, ext = _P["tile"], _P["ext"]
    acols, arows = _P["above"]
    n = 0
    for tx in range((acols + 1) // 2):
        big = Image.new("RGB", (2 * tile, 2 * tile))
        w = h = 0
        for dy in (0, 1):
            for dx in (0, 1):
                cx, cy = 2 * tx + dx, 2 * ty + dy
                if cx >= acols or cy >= arows:
                    continue
                child = Image.open(_P["indir"] / f"{cx}_{cy}.{ext}")
                child.load()
                big.paste(child, (dx * tile, dy * tile))
                w = max(w, dx * tile + child.width)
                h = max(h, dy * tile + child.height)
        half(big.crop((0, 0, w, h))).save(
            _P["outdir"] / f"{tx}_{ty}.{ext}", **_P["save_kw"])
        n += 1
    return n


def build(grid, geom, out: Path, tile: int, ext: str, save_kw, save_top=None,
          sharp=None, block=None, palette_name="rgb332", processed=(),
          jobs=1, cache_tiles=6) -> None:
    """Cut one full pyramid from `grid` into `out`.

    `block` and `processed` describe an optional per-source-tile transform —
    `pixelate.pixelate` at that block size, applied only to tiles in
    `processed`. Passed as data rather than as a callable because it has to
    cross into worker processes. It has to be a transform that treats each
    pixel independently of its neighbours across the tile edge, or the source
    tile borders would show in the output.

    `save_top` overrides the encoder for the finest level only, where lossy
    artefacts around hard edges would be visible; coarser levels are smooth
    again after halving and do not need it. `sharp` narrows that override to
    the output tiles drawing on those source tiles, so a layer that is part
    pixel art and part photography does not pay lossless prices for the
    photography (which is the one thing lossless compresses worst).
    """
    save_top = save_top or save_kw
    _cols, stw, sth, width, height, dims, counts = geom
    top = len(dims) - 1

    # Wiping first keeps a shrunken grid from leaving orphaned tiles behind.
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # --- level `top`: cut straight from the source grid ---------------------
    zdir = out / str(top)
    zdir.mkdir()
    tcols, trows = counts[top]
    done = 0
    t0 = time.time()
    with ProcessPoolExecutor(
            jobs, initializer=_init_top,
            initargs=(grid, stw, sth, width, height, tile, zdir, ext, save_kw,
                      save_top, sharp, block, palette_name, set(processed),
                      cache_tiles)) as ex:
        # Contiguous rows per worker, so the source cache is not thrown away
        # at every task boundary.
        for n in ex.map(_cut_row, range(trows),
                        chunksize=max(1, math.ceil(trows / jobs))):
            done += n
            print(f"\r    level {top}: {done}/{tcols * trows} tiles",
                  end="", flush=True)
    print(f"  ({time.time() - t0:.0f}s)")

    # --- remaining levels: each built from the one above, off disk ----------
    for z in range(top - 1, -1, -1):
        zdir = out / str(z)
        zdir.mkdir()
        tcols, trows = counts[z]
        with ProcessPoolExecutor(
                jobs, initializer=_init_coarse,
                initargs=(out / str(z + 1), zdir, ext, save_kw, tile,
                          counts[z + 1])) as ex:
            list(ex.map(_shrink_row, range(trows),
                        chunksize=max(1, math.ceil(trows / jobs))))
        print(f"    level {z}: {tcols * trows} tiles "
              f"({dims[z][0]}x{dims[z][1]})")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tiles", type=Path, default=Path("tiles"),
                    help="input grid from stitch.py (default: tiles/)")
    ap.add_argument("--processed", type=Path, default=Path("tiles-processed"),
                    help="processed tiles overriding the input grid "
                         "(default: tiles-processed/)")
    ap.add_argument("--out", type=Path, default=Path("web/tiles"),
                    help="output pyramids (default: web/tiles/)")
    ap.add_argument("--tile-size", type=int, default=512,
                    help="pyramid tile edge in px (default: 512)")
    ap.add_argument("--format", choices=sorted(SAVE_OPTS), default="webp")
    ap.add_argument("--quality", type=int, default=82)
    ap.add_argument("--jobs", "-j", type=int, default=min(8, os.cpu_count() or 1),
                    help="worker processes (default: min(8, cpus))")
    ap.add_argument("--cache-tiles", type=int, default=6,
                    help="decoded source tiles held per worker (default: 6); "
                         "raising it trades memory for fewer re-decodes")
    ap.add_argument("--raw-only", action="store_true",
                    help="skip the processed pyramid even if tiles exist")
    ap.add_argument("--pixel", default="",
                    help="comma-separated pixel sizes for extra 8-bit layers, "
                         "e.g. 1,2,4 (1 = palette only, no block reduction)")
    ap.add_argument("--palette", default="rgb332",
                    help="palette for the 8-bit layers (default: rgb332, the "
                         "standard 256-colour 8-bit palette)")
    args = ap.parse_args()

    blocks = [int(b) for b in args.pixel.split(",") if b.strip()]
    if any(b < 1 for b in blocks):
        raise SystemExit("--pixel sizes must be >= 1")
    # Only fixed palettes: one fitted to the image would differ per tile, and
    # the difference would land on the seams.
    if blocks and args.palette not in pixelate.FIXED_PALETTES:
        raise SystemExit(f"--palette must be one of "
                         f"{', '.join(sorted(pixelate.FIXED_PALETTES))}")

    tile = args.tile_size
    if tile & (tile - 1):
        raise SystemExit("--tile-size must be a power of two")
    ext = "jpg" if args.format == "jpeg" else args.format
    save_kw = SAVE_OPTS[args.format](args.quality)

    grid = scan(args.tiles)
    cols, rows = check_complete(grid, args.tiles)

    # Layers must share one geometry, tile for tile, so the raw grid sets it.
    sizes = {Image.open(p).size for p in grid.values()}
    if len(sizes) != 1:
        raise SystemExit(f"tiles in {args.tiles}/ are not uniform in size, so "
                         f"they do not form a grid: {sorted(sizes)}")
    stw, sth = sizes.pop()

    # The processed set is partial by design: whatever has not been re-rendered
    # yet keeps its raw tile, so the layer is always a complete grid.
    processed = {} if args.raw_only else scan(args.processed)
    stray = sorted(k for k in processed if k not in grid)
    if stray:
        print(f"warning: ignoring {len(stray)} processed tile(s) outside the "
              f"{cols}x{rows} grid, first {stray[0]}")
        processed = {k: v for k, v in processed.items() if k in grid}

    # A processed tile of the wrong size is one that was re-rendered against an
    # older stitch. It cannot be scaled into place — it is not just a different
    # size but a different framing — so treat it as not yet done and say so,
    # rather than failing the whole build over it.
    stale = sorted(k for k, p in processed.items()
                   if Image.open(p).size != (stw, sth))
    if stale:
        print(f"warning: {len(stale)} processed tile(s) do not match the "
              f"{stw}x{sth} raw tile and predate the current stitch — falling "
              f"back to raw for {', '.join(f'{x}_{y}' for x, y in stale)}. "
              f"Re-render them to bring them back.")
        processed = {k: v for k, v in processed.items() if k not in stale}

    # id, label, grid, pixel-block (None = untransformed), is-pixel-art
    layers = [("raw", "Raw", grid, None, False)]
    base = grid
    if processed:
        base = dict(grid)
        base.update(processed)
        layers.append(("processed", "Processed", base, None, False))

    # The 8-bit layers re-render the processed layer — but only the tiles that
    # were actually re-rendered. The raw photography filling the rest of the
    # grid is left alone: reducing it would just be a low-res aerial photo, not
    # pixel art, and it would misread as work that has been done.
    if blocks and not processed:
        print(f"warning: no processed tiles, so the 8-bit layers would have "
              f"nothing to render — skipping {args.pixel}")
        blocks = []
    for b in blocks:
        label = "8-bit" if b == 1 else f"8-bit {b}px"
        layers.append((f"pixel{b}", label, base, b, True))

    width, height = cols * stw, rows * sth

    # A pixel grid that does not divide the tile would land differently in each
    # tile, and the offset would show as a hard line down every seam.
    bad = [b for b in blocks if stw % b or sth % b]
    if bad:
        raise SystemExit(f"--pixel {bad} does not divide the {stw}x{sth} tile")

    dims = level_dims(width, height, tile)
    counts = [(math.ceil(w / tile), math.ceil(h / tile)) for w, h in dims]
    geom = (cols, stw, sth, width, height, dims, counts)
    per_layer = sum(c * r for c, r in counts)

    print(f"source:   {cols}x{rows} grid of {stw}x{sth} -> {width}x{height}")
    if processed:
        print(f"processed: {len(processed)}/{cols * rows} tiles from "
              f"{args.processed}/, the rest fall back to {args.tiles}/")
    else:
        print(f"processed: none in {args.processed}/, building the raw layer only")
    print(f"pyramid:  {len(dims)} levels, {tile}px {args.format} q{args.quality}, "
          f"{per_layer} tiles x {len(layers)} layer(s)")

    args.out.mkdir(parents=True, exist_ok=True)

    # A layer that existed last build and does not now would otherwise sit
    # there with the old geometry. `info.js` no longer lists it so the viewer
    # would not ask for it, but it is stale data on disk that looks current —
    # and after a re-stitch it is a whole obsolete mosaic.
    keep = {layer_id for layer_id, *_ in layers}
    for d in sorted(args.out.iterdir()):
        if d.is_dir() and d.name not in keep:
            print(f"  removing stale layer {d.name}/")
            shutil.rmtree(d)

    started = time.time()
    # Flat 256-colour art is both the worst case for a lossy codec (ringing on
    # every hard edge) and the best case for a lossless one, so it pays twice.
    lossless = ({"format": "WEBP", "lossless": True, "method": 4}
                if args.format == "webp" else None)
    for layer_id, label, layer_grid, block, px in layers:
        print(f"  {label.lower()}:")
        build(layer_grid, geom, args.out / layer_id, tile, ext, save_kw,
              save_top=lossless if px else None,
              sharp=set(processed) if px else None,
              block=block, palette_name=args.palette, processed=set(processed),
              jobs=args.jobs, cache_tiles=args.cache_tiles)

    info = {
        "width": width,
        "height": height,
        "tileSize": tile,
        "maxLevel": len(dims) - 1,
        "ext": ext,
        "levels": [{"w": w, "h": h, "cols": c, "rows": r}
                   for (w, h), (c, r) in zip(dims, counts)],
        # `pixel` tells the viewer to magnify with nearest neighbour: smoothing
        # an 8-bit layer past 1:1 would undo the whole point of it.
        "layers": [{"id": i, "label": l, **({"pixel": True} if px else {})}
                   for i, l, _, _, px in layers],
    }
    (args.out / "info.json").write_text(json.dumps(info, indent=2) + "\n")
    # Also as a script, so the viewer works over file:// where fetch() cannot.
    (args.out / "info.js").write_text(
        "window.MOSAIC_INFO = " + json.dumps(info) + ";\n")

    total = sum(p.stat().st_size for p in args.out.rglob(f"*.{ext}"))
    print(f"\nwrote {total / 1e6:.1f} MB to {args.out}/ "
          f"in {time.time() - started:.0f}s")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
