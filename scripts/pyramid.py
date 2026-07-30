#!/usr/bin/env python3
"""Build a web tile pyramid from the stitched tile grid.

`stitch.py` emits one big tile per source frame — 1703 x 1616 PNGs of ~5 MB.
That grid is a fine archival format and a terrible delivery format: a viewer
would have to pull 36 multi-megabyte files to show anything at all.

This script re-cuts the same pixels into the pyramid every slippy-map viewer
expects: square tiles at a series of halving resolutions, so the client only
ever fetches tiles that are both on screen and at roughly screen resolution.

    tiles/<x>_<y>.png        ->  web/tiles/<z>/<x>_<y>.webp
                                 web/tiles/info.js

Level `maxLevel` is full resolution; each level below halves both axes, down
to level 0, which is a single tile holding the whole mosaic. Edge tiles are
left partial rather than padded, so no bytes are spent on blank margins.

Memory is bounded by two source rows plus the half-resolution mosaic, not by
the full-resolution one, so this scales to grids much larger than 6 x 6.
"""

import argparse
import json
import math
import re
import sys
import time
from collections import OrderedDict
from pathlib import Path

from PIL import Image

Image.MAX_IMAGE_PIXELS = None

NAME_RE = re.compile(r"^(\d+)_(\d+)$")
SAVE_OPTS = {
    "webp": lambda q: {"format": "WEBP", "quality": q, "method": 4},
    "jpeg": lambda q: {"format": "JPEG", "quality": q, "optimize": True,
                       "progressive": True, "subsampling": 1},
}


def discover(src: Path) -> dict[tuple[int, int], Path]:
    """Map (column, row) -> file for every `<x>_<y>.<ext>` in `src`."""
    found = {}
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
    if not found:
        raise SystemExit(f"no <x>_<y> tiles found in {src}/")

    cols = max(x for x, _ in found) + 1
    rows = max(y for _, y in found) + 1
    missing = [(x, y) for y in range(rows) for x in range(cols)
               if (x, y) not in found]
    if missing:
        raise SystemExit(f"grid is {cols}x{rows} but {len(missing)} tiles are "
                         f"missing, first {missing[0]}")
    return found


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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tiles", type=Path, default=Path("tiles"),
                    help="input grid from stitch.py (default: tiles/)")
    ap.add_argument("--out", type=Path, default=Path("web/tiles"),
                    help="output pyramid (default: web/tiles/)")
    ap.add_argument("--tile-size", type=int, default=512,
                    help="pyramid tile edge in px (default: 512)")
    ap.add_argument("--format", choices=sorted(SAVE_OPTS), default="webp")
    ap.add_argument("--quality", type=int, default=82)
    args = ap.parse_args()

    tile = args.tile_size
    if tile & (tile - 1):
        raise SystemExit("--tile-size must be a power of two")
    ext = "jpg" if args.format == "jpeg" else args.format
    save_kw = SAVE_OPTS[args.format](args.quality)

    grid = discover(args.tiles)
    cols = max(x for x, _ in grid) + 1
    rows = max(y for _, y in grid) + 1

    sizes = {Image.open(p).size for p in grid.values()}
    if len(sizes) != 1:
        raise SystemExit(f"source tiles are not uniform: {sorted(sizes)}")
    stw, sth = sizes.pop()
    width, height = cols * stw, rows * sth

    dims = level_dims(width, height, tile)
    top = len(dims) - 1
    counts = [(math.ceil(w / tile), math.ceil(h / tile)) for w, h in dims]

    print(f"source:   {cols}x{rows} grid of {stw}x{sth} -> {width}x{height}")
    print(f"pyramid:  {len(dims)} levels, {tile}px {args.format} q{args.quality}, "
          f"{sum(c * r for c, r in counts)} tiles")

    args.out.mkdir(parents=True, exist_ok=True)
    started = time.time()

    # --- level `top`: cut straight from the source grid ---------------------
    # Output tiles are emitted row-major and are smaller than a source tile,
    # so a two-row cache of decoded sources is enough to never re-read a file.
    cache: OrderedDict[tuple[int, int], Image.Image] = OrderedDict()

    def source(sx: int, sy: int) -> Image.Image:
        im = cache.pop((sx, sy), None)
        if im is None:
            im = Image.open(grid[(sx, sy)]).convert("RGB")
            im.load()
        cache[(sx, sy)] = im
        while len(cache) > 2 * cols:
            cache.popitem(last=False)
        return im

    def cut(x0: int, y0: int, x1: int, y1: int) -> Image.Image:
        out = Image.new("RGB", (x1 - x0, y1 - y0))
        for sy in range(y0 // sth, (y1 - 1) // sth + 1):
            for sx in range(x0 // stw, (x1 - 1) // stw + 1):
                ox, oy = sx * stw, sy * sth
                cx0, cy0 = max(x0, ox), max(y0, oy)
                cx1, cy1 = min(x1, ox + stw), min(y1, oy + sth)
                box = (cx0 - ox, cy0 - oy, cx1 - ox, cy1 - oy)
                out.paste(source(sx, sy).crop(box), (cx0 - x0, cy0 - y0))
        return out

    zdir = args.out / str(top)
    zdir.mkdir(exist_ok=True)
    tcols, trows = counts[top]
    # Built alongside so the next level down never touches the source files.
    lower = Image.new("RGB", dims[top - 1]) if top else None

    for ty in range(trows):
        y0, y1 = ty * tile, min((ty + 1) * tile, height)
        for tx in range(tcols):
            x0, x1 = tx * tile, min((tx + 1) * tile, width)
            im = cut(x0, y0, x1, y1)
            im.save(zdir / f"{tx}_{ty}.{ext}", **save_kw)
            if lower is not None:
                lower.paste(half(im), (tx * tile // 2, ty * tile // 2))
        done = (ty + 1) * tcols
        print(f"\r  level {top}: {done}/{tcols * trows} tiles", end="", flush=True)
    cache.clear()
    print()

    # --- remaining levels: halve the whole image, then cut ------------------
    cur = lower
    for z in range(top - 1, -1, -1):
        zdir = args.out / str(z)
        zdir.mkdir(exist_ok=True)
        tcols, trows = counts[z]
        for ty in range(trows):
            for tx in range(tcols):
                box = (tx * tile, ty * tile,
                       min((tx + 1) * tile, cur.width),
                       min((ty + 1) * tile, cur.height))
                cur.crop(box).save(zdir / f"{tx}_{ty}.{ext}", **save_kw)
        print(f"  level {z}: {tcols * trows} tiles ({cur.width}x{cur.height})")
        if z:
            cur = half(cur)

    info = {
        "width": width,
        "height": height,
        "tileSize": tile,
        "maxLevel": top,
        "ext": ext,
        "levels": [{"w": w, "h": h, "cols": c, "rows": r}
                   for (w, h), (c, r) in zip(dims, counts)],
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
