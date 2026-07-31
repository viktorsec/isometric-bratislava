#!/usr/bin/env python3
"""Reassemble processed sub-tiles back into the full-size tiles they came from.

`subtiles/<pair>/` holds overlapping square crops cut from a strip of adjacent
tiles; an external process re-renders each crop independently, which means the
returned images disagree with each other twice over:

  * **photometrically** — each crop was exposed on its own, so neighbours differ
    by a constant gain per channel;
  * **structurally** — the re-render invents different detail in the overlap.

Averaging the overlaps would leave visible banding, and a hard cut would leave a
visible seam. So this does what a panorama stitcher does: Brown-Lowe gain
compensation to remove the photometric step, then multi-band (Laplacian pyramid)
blending across Voronoi seams so the residual structural difference is traded
across spatial frequencies instead of appearing as an edge.

    subtiles/processed/r<r>_c<c>_x<X>_y<Y>_s<S>.jpeg
        -> tiles-processed/<name>.png   (one per source tile)

Crop filenames carry their origin in strip pixels, so the processed images may
be at any uniform scale of the original crops; the canvas is built at the
processed resolution and resampled down to the source tile size at the end.
"""

import argparse
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

NAME_RE = re.compile(r"^r(\d+)_c(\d+)_x(\d+)_y(\d+)_s(\d+)$")


def load_crops(src):
    """Return [(x, y, array)] in strip coordinates plus the crop size."""
    crops, size = [], None
    for path in sorted(src.iterdir()):
        m = NAME_RE.match(path.stem)
        if not m:
            continue
        _, _, x, y, s = (int(v) for v in m.groups())
        img = Image.open(path).convert("RGB")
        if img.width != img.height:
            sys.exit(f"{path.name}: crop is not square ({img.width}x{img.height})")
        if size is None:
            size = (s, img.width)
        elif (s, img.width) != size:
            sys.exit(f"{path.name}: crop size differs from the rest")
        crops.append((x, y, np.asarray(img, dtype=np.float64)))
    if not crops:
        sys.exit(f"no crops named r<r>_c<c>_x<X>_y<Y>_s<S> in {src}")
    return crops, size


def gain_compensate(placed, side, sigma_n=10.0, sigma_g=0.1):
    """Brown-Lowe per-image gains, solved per channel over pairwise overlaps.

    Minimises sum over overlapping pairs of N_ij (g_i I_ij - g_j I_ji)^2, with a
    prior pulling every gain to 1 so the system stays well-posed and the overall
    exposure of the mosaic does not drift.
    """
    n = len(placed)
    gains = np.ones((n, 3))
    for ch in range(3):
        A = np.zeros((n, n))
        b = np.zeros(n)
        prior = np.zeros(n)
        for i in range(n):
            xi, yi, ai = placed[i]
            for j in range(i + 1, n):
                xj, yj, aj = placed[j]
                x0, y0 = max(xi, xj), max(yi, yj)
                x1 = min(xi + side, xj + side)
                y1 = min(yi + side, yj + side)
                if x1 <= x0 or y1 <= y0:
                    continue
                npix = (x1 - x0) * (y1 - y0)
                mi = ai[y0 - yi:y1 - yi, x0 - xi:x1 - xi, ch].mean()
                mj = aj[y0 - yj:y1 - yj, x0 - xj:x1 - xj, ch].mean()
                w = npix / sigma_n**2
                A[i, i] += w * mi * mi
                A[j, j] += w * mj * mj
                A[i, j] -= w * mi * mj
                A[j, i] -= w * mi * mj
                prior[i] += npix
                prior[j] += npix
        # The prior carries the same pixel weight as the data term, otherwise the
        # trivial all-zero solution wins.
        A += np.diag(prior / sigma_g**2)
        b += prior / sigma_g**2
        gains[:, ch] = np.linalg.solve(A, b)
    return gains


def voronoi_masks(placed, side, shape):
    """Hard label per pixel: the crop whose centre is nearest wins."""
    h, w = shape
    ys, xs = np.mgrid[0:h, 0:w]
    best = np.full((h, w), np.inf)
    label = np.zeros((h, w), dtype=np.int32)
    for i, (x, y, _) in enumerate(placed):
        cx, cy = x + side / 2.0, y + side / 2.0
        d = (xs - cx) ** 2 + (ys - cy) ** 2
        covered = (xs >= x) & (xs < x + side) & (ys >= y) & (ys < y + side)
        d = np.where(covered, d, np.inf)
        hit = d < best
        best[hit] = d[hit]
        label[hit] = i
    return label


def _reduce(a):
    a = _blur(a)
    return a[::2, ::2]


def _blur(a):
    k = np.array([1.0, 4.0, 6.0, 4.0, 1.0]) / 16.0
    for axis in (0, 1):
        pad = [(0, 0)] * a.ndim
        pad[axis] = (2, 2)
        p = np.pad(a, pad, mode="edge")
        out = np.zeros_like(a)
        for t, kv in enumerate(k):
            sl = [slice(None)] * a.ndim
            sl[axis] = slice(t, t + a.shape[axis])
            out += kv * p[tuple(sl)]
        a = out
    return a


def _expand(a, shape):
    up = np.zeros(shape + a.shape[2:], dtype=a.dtype)
    up[::2, ::2] = a
    return 4.0 * _blur(up)


def multiband_blend(placed, side, shape, levels):
    """Laplacian-pyramid blend of every crop under its Voronoi mask."""
    h, w = shape
    label = voronoi_masks(placed, side, shape)

    shapes = [(h, w)]
    for _ in range(levels - 1):
        ph, pw = shapes[-1]
        shapes.append(((ph + 1) // 2, (pw + 1) // 2))

    acc = [np.zeros(s + (3,)) for s in shapes]
    wacc = [np.zeros(s) for s in shapes]

    for i, (x, y, arr) in enumerate(placed):
        img = np.zeros((h, w, 3))
        img[y:y + side, x:x + side] = arr
        cover = np.zeros((h, w))
        cover[y:y + side, x:x + side] = 1.0
        mask = (label == i).astype(np.float64) * cover

        # Laplacian pyramid of the (zero-padded) image, Gaussian of the mask.
        gi, gm = [img], [mask]
        for lv in range(1, levels):
            gi.append(_reduce(gi[-1])[:shapes[lv][0], :shapes[lv][1]])
            gm.append(_reduce(gm[-1])[:shapes[lv][0], :shapes[lv][1]])
        for lv in range(levels):
            lap = gi[lv] if lv == levels - 1 else gi[lv] - _expand(gi[lv + 1], shapes[lv])
            m = gm[lv]
            acc[lv] += lap * m[..., None]
            wacc[lv] += m

    out = None
    for lv in reversed(range(levels)):
        band = acc[lv] / np.maximum(wacc[lv], 1e-6)[..., None]
        out = band if out is None else band + _expand(out, shapes[lv])
    return np.clip(out, 0, 255)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path, help="folder of processed crops")
    ap.add_argument("-o", "--out", type=Path, default=Path("tiles-processed"))
    ap.add_argument("--names", nargs="+", required=True,
                    help="output tile names, left to right (e.g. 3_4 4_4)")
    ap.add_argument("--tile-size", type=int, default=1648,
                    help="edge of each source tile in strip pixels")
    ap.add_argument("--levels", type=int, default=7, help="blend pyramid levels")
    ap.add_argument("--no-gain", action="store_true", help="skip gain compensation")
    args = ap.parse_args()

    crops, (crop_src, crop_dst) = load_crops(args.src)
    scale = crop_dst / crop_src
    placed = []
    for x, y, arr in crops:
        px, py = x * scale, y * scale
        if abs(px - round(px)) > 1e-6 or abs(py - round(py)) > 1e-6:
            sys.exit(f"crop origin ({x},{y}) does not land on a pixel at scale {scale}")
        placed.append((int(round(px)), int(round(py)), arr))

    strip_w = args.tile_size * len(args.names)
    strip_h = args.tile_size
    w = int(round(strip_w * scale))
    h = int(round(strip_h * scale))
    print(f"{len(placed)} crops of {crop_src}px -> {crop_dst}px "
          f"(scale {scale:g}), canvas {w}x{h}")

    if not args.no_gain:
        gains = gain_compensate(placed, crop_dst)
        print("gains: min %.3f  max %.3f" % (gains.min(), gains.max()))
        placed = [(x, y, np.clip(a * g, 0, 255))
                  for (x, y, a), g in zip(placed, gains)]

    canvas = multiband_blend(placed, crop_dst, (h, w), args.levels)
    strip = Image.fromarray(canvas.astype(np.uint8))
    if (w, h) != (strip_w, strip_h):
        strip = strip.resize((strip_w, strip_h), Image.LANCZOS)

    args.out.mkdir(parents=True, exist_ok=True)
    for i, name in enumerate(args.names):
        x = i * args.tile_size
        tile = strip.crop((x, 0, x + args.tile_size, args.tile_size))
        path = args.out / f"{name}.png"
        tile.save(path)
        print(f"wrote {path} ({tile.width}x{tile.height})")


if __name__ == "__main__":
    main()
