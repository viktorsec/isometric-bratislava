#!/usr/bin/env python3
"""Build web tile pyramids from the stitched tile grid.

`stitch.py` emits one big tile per source frame — 1648 x 1648 PNGs of ~5 MB.
That grid is a fine archival format and a terrible delivery format: a viewer
would have to pull 36 multi-megabyte files to show anything at all.

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

Memory is bounded by two source rows plus the half-resolution mosaic, not by
the full-resolution one, so this scales to grids much larger than 6 x 6.
"""

import argparse
import json
import math
import re
import shutil
import sys
import time
from collections import OrderedDict
from pathlib import Path

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
    """Exact 2x2 box reduction. Seamless: no filter support crosses tiles."""
    return im.resize((math.ceil(im.width / 2), math.ceil(im.height / 2)),
                     Image.BOX)


def build(grid, geom, out: Path, tile: int, ext: str, save_kw, tf=None,
          save_top=None, sharp=None) -> None:
    """Cut one full pyramid from `grid` into `out`.

    `tf` is an optional per-source-tile transform, called as `tf(im, (x, y))`
    so it can leave some tiles alone. It has to be one that treats each pixel
    independently of its neighbours across the tile edge — see
    `pixelate.pixelate` — or the tile borders would show in the output.

    `save_top` overrides the encoder for the finest level only, where lossy
    artefacts around hard edges would be visible; coarser levels are smooth
    again after halving and do not need it. `sharp` narrows that override to
    the output tiles drawing on those source tiles, so a layer that is part
    pixel art and part photography does not pay lossless prices for the
    photography (which is the one thing lossless compresses worst).
    """
    save_top = save_top or save_kw
    cols, stw, sth, width, height, dims, counts = geom
    top = len(dims) - 1

    # Wiping first keeps a shrunken grid from leaving orphaned tiles behind.
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # --- level `top`: cut straight from the source grid ---------------------
    # Output tiles are emitted row-major and are smaller than a source tile,
    # so a two-row cache of decoded sources is enough to never re-read a file.
    cache: OrderedDict[tuple[int, int], Image.Image] = OrderedDict()

    def source(sx: int, sy: int) -> Image.Image:
        im = cache.pop((sx, sy), None)
        if im is None:
            im = Image.open(grid[(sx, sy)]).convert("RGB")
            im.load()
            if tf is not None:
                im = tf(im, (sx, sy))
        cache[(sx, sy)] = im
        while len(cache) > 2 * cols:
            cache.popitem(last=False)
        return im

    def sources_under(x0: int, y0: int, x1: int, y1: int):
        return [(sx, sy)
                for sy in range(y0 // sth, (y1 - 1) // sth + 1)
                for sx in range(x0 // stw, (x1 - 1) // stw + 1)]

    def cut(x0: int, y0: int, x1: int, y1: int) -> Image.Image:
        im_out = Image.new("RGB", (x1 - x0, y1 - y0))
        for sy in range(y0 // sth, (y1 - 1) // sth + 1):
            for sx in range(x0 // stw, (x1 - 1) // stw + 1):
                ox, oy = sx * stw, sy * sth
                cx0, cy0 = max(x0, ox), max(y0, oy)
                cx1, cy1 = min(x1, ox + stw), min(y1, oy + sth)
                box = (cx0 - ox, cy0 - oy, cx1 - ox, cy1 - oy)
                im_out.paste(source(sx, sy).crop(box), (cx0 - x0, cy0 - y0))
        return im_out

    zdir = out / str(top)
    zdir.mkdir()
    tcols, trows = counts[top]
    # Built alongside so the next level down never touches the source files.
    lower = Image.new("RGB", dims[top - 1]) if top else None

    for ty in range(trows):
        y0, y1 = ty * tile, min((ty + 1) * tile, height)
        for tx in range(tcols):
            x0, x1 = tx * tile, min((tx + 1) * tile, width)
            im = cut(x0, y0, x1, y1)
            kw = save_top
            if sharp is not None and not any(
                    s in sharp for s in sources_under(x0, y0, x1, y1)):
                kw = save_kw
            im.save(zdir / f"{tx}_{ty}.{ext}", **kw)
            if lower is not None:
                lower.paste(half(im), (tx * tile // 2, ty * tile // 2))
        print(f"\r    level {top}: {(ty + 1) * tcols}/{tcols * trows} tiles",
              end="", flush=True)
    cache.clear()
    print()

    # --- remaining levels: halve the whole image, then cut ------------------
    cur = lower
    for z in range(top - 1, -1, -1):
        zdir = out / str(z)
        zdir.mkdir()
        tcols, trows = counts[z]
        for ty in range(trows):
            for tx in range(tcols):
                box = (tx * tile, ty * tile,
                       min((tx + 1) * tile, cur.width),
                       min((ty + 1) * tile, cur.height))
                cur.crop(box).save(zdir / f"{tx}_{ty}.{ext}", **save_kw)
        print(f"    level {z}: {tcols * trows} tiles ({cur.width}x{cur.height})")
        if z:
            cur = half(cur)


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

    # id, label, grid, transform, is-pixel-art
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
    pal = pixelate.FIXED_PALETTES[args.palette]()
    if blocks and not processed:
        print(f"warning: no processed tiles, so the 8-bit layers would have "
              f"nothing to render — skipping {args.pixel}")
        blocks = []
    for b in blocks:
        label = "8-bit" if b == 1 else f"8-bit {b}px"
        layers.append((f"pixel{b}", label, base,
                       lambda im, xy, b=b: pixelate.pixelate(im, b, pal)
                       if xy in processed else im, True))

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
    started = time.time()
    # Flat 256-colour art is both the worst case for a lossy codec (ringing on
    # every hard edge) and the best case for a lossless one, so it pays twice.
    lossless = ({"format": "WEBP", "lossless": True, "method": 4}
                if args.format == "webp" else None)
    for layer_id, label, layer_grid, tf, px in layers:
        print(f"  {label.lower()}:")
        build(layer_grid, geom, args.out / layer_id, tile, ext, save_kw, tf,
              save_top=lossless if px else None,
              sharp=set(processed) if px else None)

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
