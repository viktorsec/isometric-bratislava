#!/usr/bin/env python3
"""Cut a run of adjacent tiles into overlapping square crops for re-rendering.

An image-to-image model can only take so many pixels at once, so a 1616 px tile
has to be sent in pieces. Cutting it into a non-overlapping grid would put a hard
edge everywhere two pieces meet: each piece is re-rendered independently, and
nothing constrains them to agree. Overlapping crops give `reassemble.py` a band
of shared content to blend across, which is what makes the seam disappear.

The tiles named on the command line are pasted into one horizontal strip and cut
at a fixed stride, so a crop that straddles a tile boundary carries content from
both sides and the boundary gets blended like any other seam.

    tiles/<x>_<y>.png  ->  subtiles/<name>+<name>/r<r>_c<c>_x<X>_y<Y>_s<S>.png

`X`/`Y` are the crop's origin in strip pixels; `reassemble.py` reads them back
out of the filename, so the crops can be re-rendered at any uniform scale as
long as the names survive.
"""

import argparse
import sys
from pathlib import Path

from PIL import Image

Image.MAX_IMAGE_PIXELS = None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="+", help="tile names, left to right (e.g. 3_4 4_4)")
    ap.add_argument("--tiles", type=Path, default=Path("tiles"))
    ap.add_argument("-o", "--out", type=Path, default=None,
                    help="output folder (default: subtiles/<name>+<name>/)")
    ap.add_argument("--size", type=int, default=808, help="crop edge in pixels")
    ap.add_argument("--overlap", type=float, default=0.5,
                    help="fraction of a crop shared with its neighbour (0-1)")
    args = ap.parse_args()

    if not 0 <= args.overlap < 1:
        sys.exit("--overlap must be in [0, 1)")

    images = []
    for name in args.names:
        path = args.tiles / f"{name}.png"
        if not path.exists():
            sys.exit(f"missing tile: {path}")
        img = Image.open(path).convert("RGB")
        if img.width != img.height:
            sys.exit(f"{path.name}: tile is not square ({img.width}x{img.height})")
        images.append(img)
    if len({im.size for im in images}) != 1:
        sys.exit("tiles differ in size")

    side = images[0].width
    strip = Image.new("RGB", (side * len(images), side))
    for i, img in enumerate(images):
        strip.paste(img, (i * side, 0))

    stride = max(1, int(round(args.size * (1 - args.overlap))))
    if args.size > min(strip.size):
        sys.exit(f"--size {args.size} exceeds the strip ({strip.width}x{strip.height})")
    xs = list(range(0, strip.width - args.size + 1, stride))
    ys = list(range(0, strip.height - args.size + 1, stride))
    # A stride that does not divide the strip evenly would leave an uncovered
    # margin; pull the last crop back flush against the far edge instead.
    for axis, extent in ((xs, strip.width), (ys, strip.height)):
        if axis[-1] + args.size < extent:
            axis.append(extent - args.size)

    out = args.out or Path("subtiles") / "+".join(args.names)
    out.mkdir(parents=True, exist_ok=True)
    for r, y in enumerate(ys):
        for c, x in enumerate(xs):
            crop = strip.crop((x, y, x + args.size, y + args.size))
            crop.save(out / f"r{r}_c{c}_x{x}_y{y}_s{args.size}.png")

    print(f"{len(xs) * len(ys)} crops of {args.size}px (stride {stride}) "
          f"from a {strip.width}x{strip.height} strip -> {out}/")


if __name__ == "__main__":
    main()
