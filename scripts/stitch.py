#!/usr/bin/env python3
"""Reconcile overlapping frames into a regular, gapless tile grid.

The frames overlap heavily (~58% horizontally). This script measures the true
pixel step between neighbours, solves for a crop origin per frame, and emits
uniform tiles that butt together exactly — no blending, no feathering, no seams
in the geometric sense.

Why cropping rather than feature-based stitching:

  * Every tile comes from the centre of its frame, where relief displacement
    (roof positions shifting relative to their bases) is smallest.
  * A uniform tile size means the output is a clean grid, renderable as such.
  * The Google Earth watermark sits near y=3480, outside the central crop, so it
    disappears without any inpainting.

Pipeline:

  1. Coarse align each adjacent pair with overlap-normalised cross-correlation
     on 1/8-scale images (robust to the large ~1700 px offsets).
  2. Refine to sub-pixel with phase correlation on matching full-res windows.
  3. Least-squares solve for per-frame crop origins that best satisfy all
     pairwise constraints simultaneously, so alignment error is spread rather
     than accumulating along a row.
  4. Crop and write tiles; optionally assemble the full mosaic.

Convention throughout:  b[r, c] ~= a[r + dy, c + dx]
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None


# --------------------------------------------------------------------------- #
# correlation primitives
# --------------------------------------------------------------------------- #

def downsample(a, f):
    h = a.shape[0] // f * f
    w = a.shape[1] // f * f
    return a[:h, :w].reshape(h // f, f, w // f, f).mean((1, 3)).astype(np.float32)


def coarse_align(a, b):
    """Zero-padded cross-correlation normalised by overlap area.

    Plain correlation is biased toward zero shift (more overlap = bigger sum);
    dividing by the overlap count removes that, which matters here because the
    true offsets are a large fraction of the frame.
    """
    H, W = a.shape
    P = (2 * H, 2 * W)
    A = np.fft.rfft2(a - a.mean(), s=P)
    B = np.fft.rfft2(b - b.mean(), s=P)
    num = np.fft.irfft2(A * np.conj(B), s=P)
    ones = np.ones_like(a)
    O = np.fft.rfft2(ones, s=P)
    cnt = np.maximum(np.fft.irfft2(O * np.conj(O), s=P), 1.0)
    r = num / cnt
    r[cnt < 0.15 * a.size] = -np.inf          # ignore sliver overlaps
    pk = np.unravel_index(np.argmax(r), r.shape)
    dy = pk[0] - P[0] if pk[0] > H else pk[0]
    dx = pk[1] - P[1] if pk[1] > W else pk[1]
    return int(dy), int(dx)


def phase_align(A, B):
    """Sub-pixel residual for two nearly-aligned patches, with parabolic fit."""
    h, w = A.shape
    A = A.astype(np.float32) - A.mean()
    B = B.astype(np.float32) - B.mean()
    win = np.outer(np.hanning(h), np.hanning(w)).astype(np.float32)
    R = np.fft.rfft2(A * win) * np.conj(np.fft.rfft2(B * win))
    R /= np.abs(R) + 1e-9
    r = np.fft.irfft2(R, s=(h, w))
    pk = np.unravel_index(np.argmax(r), r.shape)
    peak = float(r[pk])

    def sub(c, lo, hi):
        den = lo - 2 * c + hi
        return 0.0 if den == 0 else float(np.clip((lo - hi) / (2 * den), -1, 1))

    y0, x0 = pk
    fy = y0 + sub(r[y0, x0], r[(y0 - 1) % h, x0], r[(y0 + 1) % h, x0])
    fx = x0 + sub(r[y0, x0], r[y0, (x0 - 1) % w], r[y0, (x0 + 1) % w])
    return (fy - h if fy > h / 2 else fy), (fx - w if fx > w / 2 else fx), peak


def selftest():
    """Pin down the sign conventions on synthetic data with a known shift."""
    rng = np.random.default_rng(0)
    n = rng.standard_normal((800, 900)).astype(np.float32)
    F = np.fft.rfft2(n)
    fy = np.fft.fftfreq(800)[:, None]
    fx = np.fft.rfftfreq(900)[None, :]
    # band-limited: image-like texture, but no global ramp (a ramp would pin
    # cross-correlation at zero shift regardless of the true offset)
    base = np.fft.irfft2(F * np.exp(-(fy ** 2 + fx ** 2) / (2 * 0.02 ** 2)),
                         s=(800, 900)).astype(np.float32)
    tdy, tdx = 37, -54
    a = base[200:600, 200:700]
    b = base[200 - tdy:600 - tdy, 200 - tdx:700 - tdx]
    cy, cx = coarse_align(a, b)
    py, px, _ = phase_align(a, b)
    assert (cy, cx) == (-tdy, -tdx), f"coarse convention: got ({cy},{cx})"
    assert abs(py + tdy) < 0.6 and abs(px + tdx) < 0.6, f"phase: ({py},{px})"


# --------------------------------------------------------------------------- #
# measurement
# --------------------------------------------------------------------------- #

def refine(a, b, dy0, dx0, wm_top, win):
    """Place matching windows from the coarse prior and measure the residual."""
    W = a.shape[1]
    by_lo, by_hi = max(0, -dy0), min(wm_top - win, wm_top - dy0 - win)
    bx_lo, bx_hi = max(0, -dx0), min(W - win, W - dx0 - win)
    if by_hi < by_lo or bx_hi < bx_lo:
        return None
    by, bx = (by_lo + by_hi) // 2, (bx_lo + bx_hi) // 2
    ay, ax = by + dy0, bx + dx0
    rdy, rdx, peak = phase_align(a[ay:ay + win, ax:ax + win],
                                 b[by:by + win, bx:bx + win])
    return dy0 + rdy, dx0 + rdx, peak


def measure(frames, n, wm_top, win, verbose=True):
    """Return {'h': [...], 'v': [...]} of ((x,y), dy, dx, peak) for each pair."""
    small = {k: downsample(v[:wm_top], 8) for k, v in frames.items()}
    out = {"h": [], "v": []}
    for kind, (sx, sy) in (("h", (1, 0)), ("v", (0, 1))):
        for y in range(n - sy):
            for x in range(n - sx):
                a, b = (x, y), (x + sx, y + sy)
                cy, cx = coarse_align(small[a], small[b])
                r = refine(frames[a], frames[b], cy * 8, cx * 8, wm_top, win)
                if r is None:
                    print(f"  warn: {a}->{b} no usable window", file=sys.stderr)
                    continue
                out[kind].append([list(a), r[0], r[1], r[2]])
                if verbose:
                    print(f"  {kind.upper()} {a}->{b}  dy={r[0]:+9.2f} dx={r[1]:+9.2f} "
                          f"peak={r[2]:.4f}", flush=True)
    return out


# --------------------------------------------------------------------------- #
# grid solve
# --------------------------------------------------------------------------- #

def solve_origins(pairs, n, src_w, src_h, centre):
    """Least-squares source-crop origins per frame.

    Continuity between horizontal neighbours needs
        ox[x+1,y] - ox[x,y] = src_w - dx_h
    and likewise vertically. The pairwise measurements disagree slightly (relief
    displacement varies with terrain), so the system is over-determined; solving
    it spreads the residual instead of letting it accumulate across a row.

    Note this works in SOURCE pixels and uses the measured step, not the output
    tile size — those differ whenever tiles are resampled to square. Using the
    output size here would break the butt joint.

    The two axes decouple, so we solve ox and oy separately.
    """
    idx = {(x, y): y * n + x for y in range(n) for x in range(n)}
    rows_x, rhs_x, rows_y, rhs_y = [], [], [], []

    def add(rows, rhs, a, b, val):
        r = np.zeros(n * n)
        r[idx[b]], r[idx[a]] = 1.0, -1.0
        rows.append(r)
        rhs.append(val)

    for (ax, ay), dy, dx, _ in pairs["h"]:
        a, b = (ax, ay), (ax + 1, ay)
        add(rows_x, rhs_x, a, b, src_w - dx)
        add(rows_y, rhs_y, a, b, -dy)
    for (ax, ay), dy, dx, _ in pairs["v"]:
        a, b = (ax, ay), (ax, ay + 1)
        add(rows_y, rhs_y, a, b, src_h - dy)
        add(rows_x, rhs_x, a, b, -dx)

    def solve(rows, rhs, target):
        gauge = np.ones(n * n) / (n * n)       # fix the free constant
        A = np.vstack(rows + [gauge])
        y = np.array(rhs + [target])
        sol, *_ = np.linalg.lstsq(A, y, rcond=None)
        resid = A[:-1] @ sol - y[:-1]
        return sol, resid

    ox, rx = solve(rows_x, rhs_x, centre[0])
    oy, ry = solve(rows_y, rhs_y, centre[1])
    return ox, oy, rx, ry


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--frames", type=Path, default=Path("frames"))
    p.add_argument("--out", type=Path, default=Path("tiles"))
    p.add_argument("-n", "--grid", type=int, default=6, help="grid size (default 6)")
    p.add_argument("--watermark-top", type=int, default=3300,
                   help="rows below this are excluded from matching (default 3300)")
    p.add_argument("--window", type=int, default=1200, help="refine window px")
    p.add_argument("--tile-size", type=int, default=None, metavar="S",
                   help="output square tile size in px; default is the smaller "
                        "measured step rounded to --multiple")
    p.add_argument("--multiple", type=int, default=8, metavar="M",
                   help="round the auto tile size to a multiple of M (default 8)")
    p.add_argument("--native", action="store_true",
                   help="keep the native non-square tile size (no resampling, "
                        "preserves the imagery's true aspect ratio)")
    p.add_argument("--cache", type=Path, default=None,
                   help="read/write measured offsets here to skip re-measuring")
    p.add_argument("--format", choices=("png", "jpeg"), default="png")
    p.add_argument("--quality", type=int, default=95)
    p.add_argument("--mosaic", type=Path, default=None,
                   help="also write the assembled mosaic here")
    p.add_argument("--preview", type=Path, default=None,
                   help="write a downscaled mosaic preview here")
    p.add_argument("--preview-width", type=int, default=2000)
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    selftest()
    n = args.grid

    missing = [f"{x}_{y}" for y in range(n) for x in range(n)
               if not list(args.frames.glob(f"{x}_{y}.*"))]
    if missing:
        raise SystemExit(f"missing frames: {', '.join(missing[:8])}")

    def path(x, y):
        return sorted(args.frames.glob(f"{x}_{y}.*"))[0]

    print(f"loading {n*n} frames from {args.frames}/ ...", flush=True)
    colour = {(x, y): Image.open(path(x, y)).convert("RGB")
              for y in range(n) for x in range(n)}
    fw, fh = colour[(0, 0)].size
    if any(im.size != (fw, fh) for im in colour.values()):
        raise SystemExit("frames differ in size")

    if args.cache and args.cache.exists():
        pairs = json.loads(args.cache.read_text())["pairs"]
        print(f"loaded offsets from {args.cache}")
    else:
        grey = {k: np.asarray(v.convert("L"), dtype=np.uint8) for k, v in colour.items()}
        print("measuring pairwise offsets...", flush=True)
        pairs = measure(grey, n, args.watermark_top, args.window, not args.quiet)
        del grey
        if args.cache:
            args.cache.write_text(json.dumps({"pairs": pairs}, indent=1))

    hs = np.array([[r[1], r[2]] for r in pairs["h"]])
    vs = np.array([[r[1], r[2]] for r in pairs["v"]])
    step_x = float(np.median(hs[:, 1]))
    step_y = float(np.median(vs[:, 0]))
    print(f"\nmeasured step: dx={step_x:.2f} (sd {hs[:,1].std():.2f}, n={len(hs)})  "
          f"dy={step_y:.2f} (sd {vs[:,0].std():.2f}, n={len(vs)})")
    print(f"cross terms:   dy_h={np.median(hs[:,0]):+.2f}  dx_v={np.median(vs[:,1]):+.2f}"
          "   (~0 means the grid is axis-aligned)")

    # Source extent per tile is the measured step — fractional, and independent of
    # the output tile size. One tile consumes exactly one step of source imagery.
    src_w, src_h = step_x, step_y
    if src_w > fw or src_h > fh:
        raise SystemExit(f"step {src_w:.1f}x{src_h:.1f} exceeds frame {fw}x{fh}")

    if args.native:
        out_w, out_h = int(round(src_w)), int(round(src_h))
    elif args.tile_size:
        out_w = out_h = args.tile_size
    else:
        m = max(1, args.multiple)
        out_w = out_h = int(round(min(src_w, src_h) / m)) * m
    if out_w <= 0 or out_h <= 0:
        raise SystemExit("computed a non-positive tile size")

    ox, oy, rx, ry = solve_origins(pairs, n, src_w, src_h,
                                   ((fw - src_w) / 2, (fh - src_h) / 2))
    print(f"source extent: {src_w:.2f} x {src_h:.2f} px per tile")
    print(f"tile size:     {out_w} x {out_h}"
          f"{' (native, no resampling)' if args.native else ''}")
    sx, sy = out_w / src_w, out_h / src_h
    aspect = sx / sy
    if abs(aspect - 1) > 1e-4:
        print(f"resample:      x{sx:.4f} horizontal, x{sy:.4f} vertical  ->  "
              f"{abs(aspect - 1) * 100:.1f}% aspect distortion")
    print(f"seam residual: x rms={np.sqrt((rx**2).mean()):.2f}px max={np.abs(rx).max():.2f}px  "
          f"y rms={np.sqrt((ry**2).mean()):.2f}px max={np.abs(ry).max():.2f}px  (source px)")

    ox = np.clip(ox, 0, fw - src_w)
    oy = np.clip(oy, 0, fh - src_h)
    keeps_wm = [(x, y) for y in range(n) for x in range(n)
                if oy[y * n + x] + src_h > args.watermark_top]
    print(f"watermark:     {'excluded from all tiles' if not keeps_wm else f'INSIDE {len(keeps_wm)} tile(s)'}")

    args.out.mkdir(parents=True, exist_ok=True)
    ext = "png" if args.format == "png" else "jpeg"
    save_kw = {} if args.format == "png" else {"quality": args.quality, "subsampling": 0}
    mosaic = Image.new("RGB", (out_w * n, out_h * n)) if (args.mosaic or args.preview) else None

    for y in range(n):
        for x in range(n):
            i = y * n + x
            # resize(box=...) takes a float rectangle, so the fractional origin and
            # fractional step are honoured exactly — no integer rounding drift — and
            # LANCZOS gives a properly antialiased rescale in the same step.
            box = (ox[i], oy[i], ox[i] + src_w, oy[i] + src_h)
            tile = colour[(x, y)].resize((out_w, out_h), Image.LANCZOS, box=box)
            tile.save(args.out / f"{x}_{y}.{ext}", **save_kw)
            if mosaic:
                mosaic.paste(tile, (x * out_w, y * out_h))
    print(f"\nwrote {n*n} tiles to {args.out}/  ({out_w}x{out_h} each)")

    if mosaic:
        print(f"mosaic:        {mosaic.width} x {mosaic.height} px")
        if args.mosaic:
            args.mosaic.parent.mkdir(parents=True, exist_ok=True)
            mosaic.save(args.mosaic, quality=args.quality)
            print(f"wrote {args.mosaic}")
        if args.preview:
            w = args.preview_width
            h = round(mosaic.height * w / mosaic.width)
            args.preview.parent.mkdir(parents=True, exist_ok=True)
            mosaic.resize((w, h), Image.LANCZOS).save(args.preview, quality=90)
            print(f"wrote {args.preview} ({w}x{h})")


if __name__ == "__main__":
    sys.exit(main())
