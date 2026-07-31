#!/usr/bin/env python3
"""Turn faux-pixelart renders into real pixel art: coarse grid, fixed palette.

The AI tiles only look like pixel art. Every edge is antialiased and there are
thousands of near-identical midtones, so at 1:1 they read as a painting with a
blocky filter over it. Two operations fix that:

  * **Block reduction.** Average each `block x block` square down to one pixel
    and expand it straight back with nearest neighbour, so the image carries
    real, hard-edged pixels of a known size.
  * **Palette snap.** Quantise to a fixed palette. This is what actually sells
    the look — collapsing the midtones is what separates pixel art from a
    downscaled photo, and it also buys back some of the edge contrast that
    area-averaging costs.

`rgb332` is the standard 8-bit palette: 3 bits of red, 3 of green, 2 of blue,
evenly spaced. It is a fixed palette, not one fitted to the image, which is
also what makes it safe to apply tile by tile — every tile snaps the same
colour to the same entry, so nothing shows at the seams.

Used as a library by `pyramid.py`, and standalone on a folder of images:

    python scripts/pixelate.py subtiles/processed -o subtiles/pixelart --block 4
"""

import argparse
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter

EXTS = {".png", ".jpg", ".jpeg", ".webp"}
FILTERS = {"box": Image.BOX, "lanczos": Image.LANCZOS, "bilinear": Image.BILINEAR}


def _levels(n: int) -> list[int]:
    """`n` evenly spaced channel values spanning the full 0..255 range."""
    return [round(i * 255 / (n - 1)) for i in range(n)]


def _pack(colors: list[tuple[int, int, int]]) -> Image.Image:
    """A P-mode image carrying `colors` as its palette, for `quantize()`."""
    flat = [c for rgb in colors for c in rgb]
    flat += [0] * (768 - len(flat))
    pal = Image.new("P", (1, 1))
    pal.putpalette(flat)
    return pal


def rgb332() -> Image.Image:
    """The standard 256-colour 8-bit palette: 8 reds, 8 greens, 4 blues."""
    return _pack([(r, g, b) for r in _levels(8)
                            for g in _levels(8)
                            for b in _levels(4)])


def websafe() -> Image.Image:
    """The 216-colour web-safe cube, padded with a 40-step grey ramp."""
    cube = [(r, g, b) for r in _levels(6) for g in _levels(6) for b in _levels(6)]
    greys = [(v, v, v) for v in (round(i * 255 / 39) for i in range(40))]
    return _pack(cube + greys)


FIXED_PALETTES = {"rgb332": rgb332, "websafe": websafe}


def adaptive_palette(images, colors, sample_px=200_000):
    """A palette fitted to `images`, pooled so every tile agrees on it.

    Per-image palettes would make neighbouring tiles disagree about what "roof
    red" is, and the disagreement lands exactly on the seams.
    """
    per = max(1, sample_px // len(images))
    rng = np.random.default_rng(0)
    chunks = []
    for im in images:
        a = np.asarray(im, dtype=np.uint8).reshape(-1, 3)
        idx = rng.choice(len(a), size=min(per, len(a)), replace=False)
        chunks.append(a[idx])
    pool = np.concatenate(chunks)
    side = int(np.ceil(np.sqrt(len(pool))))
    pad = np.resize(pool, (side * side, 3))
    sample = Image.fromarray(pad.reshape(side, side, 3).astype(np.uint8))
    return sample.quantize(colors=colors, method=Image.MEDIANCUT,
                           dither=Image.Dither.NONE)


def pixelate(im, block=1, palette=None, dither=Image.Dither.NONE,
             filt=Image.BOX, sharpen=0.0, saturation=1.0, expand=True):
    """Block-reduce and palette-snap `im`, back at its original size.

    `block` must divide the image dimensions, and the caller must keep the
    block grid aligned with the image origin — that plus a *fixed* palette is
    what lets a mosaic be processed one tile at a time with no visible seams.
    `filt` other than BOX, and any `sharpen`, break that: their filter support
    reaches across the tile edge, where there is no neighbour to read. They are
    for standalone single images, not for tiles of a larger picture.
    """
    if saturation != 1.0:
        im = ImageEnhance.Color(im).enhance(saturation)
    small = im
    if block > 1:
        if im.width % block or im.height % block:
            raise ValueError(f"block {block} does not divide {im.size}")
        if sharpen > 0:
            im = im.filter(ImageFilter.UnsharpMask(
                radius=block * 1.5, percent=int(sharpen * 100), threshold=2))
        small = im.resize((im.width // block, im.height // block), filt)
        if sharpen > 0 and filt is not Image.BOX:
            small = small.filter(ImageFilter.UnsharpMask(
                radius=1, percent=int(sharpen * 60), threshold=3))
    if palette is not None:
        small = small.quantize(palette=palette, dither=dither).convert("RGB")
    if not expand or block == 1:
        return small
    return small.resize(im.size, Image.NEAREST)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path, help="directory of source images")
    ap.add_argument("-o", "--out", type=Path, required=True, help="output directory")
    ap.add_argument("--block", type=int, default=4,
                    help="pixel size in source px, 1 = palette only (default: 4)")
    ap.add_argument("--palette", default="rgb332",
                    help="rgb332 (default), websafe, adaptive, or none")
    ap.add_argument("--colors", type=int, default=64, help="adaptive palette size")
    ap.add_argument("--filter", choices=FILTERS, default="box")
    ap.add_argument("--sharpen", type=float, default=0.0, help="0-1.5")
    ap.add_argument("--saturation", type=float, default=1.0)
    ap.add_argument("--dither", choices=["none", "fs"], default="none")
    ap.add_argument("--native", action="store_true",
                    help="save at the reduced size instead of expanding back")
    args = ap.parse_args()

    paths = sorted(p for p in args.src.iterdir() if p.suffix.lower() in EXTS)
    if not paths:
        raise SystemExit(f"no images in {args.src}")
    args.out.mkdir(parents=True, exist_ok=True)

    images = [Image.open(p).convert("RGB") for p in paths]
    if args.palette == "adaptive":
        pal = adaptive_palette(images, args.colors)
    elif args.palette == "none":
        pal = None
    else:
        pal = FIXED_PALETTES[args.palette]()

    dither = (Image.Dither.FLOYDSTEINBERG if args.dither == "fs"
              else Image.Dither.NONE)
    for p, im in zip(paths, images):
        out = pixelate(im, args.block, pal, dither, FILTERS[args.filter],
                       args.sharpen, args.saturation, expand=not args.native)
        # PNG only: JPEG would smear ringing straight back into the hard edges.
        out.save(args.out / (p.stem + ".png"))
    print(f"wrote {len(paths)} tiles to {args.out} "
          f"(block {args.block}, palette {args.palette})")


if __name__ == "__main__":
    main()
