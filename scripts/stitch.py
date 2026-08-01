#!/usr/bin/env python3
"""Reconcile overlapping frames into a regular, gapless tile grid.

The frames overlap heavily (~58% horizontally). This script measures how each
adjacent pair maps onto the other, solves for a per-frame placement that best
satisfies every pair at once, and resamples uniform tiles that butt together
exactly — no blending, no feathering, no seams in the geometric sense.

Why resampling a crop rather than feature-based stitching:

  * Every tile comes from the centre of its frame, where relief displacement
    (roof positions shifting relative to their bases) is smallest.
  * A uniform tile size means the output is a clean grid, renderable as such.
  * The Google Earth watermark sits near y=3480, outside the central crop, so it
    disappears without any inpainting.

One projection, shared by every frame
-------------------------------------

Earth Studio's camera is perspective, so each frame is a perspective image of
(approximately) one ground plane, and two such images relate by a homography —
never by a translation, and not by an affine either. But every frame is the
*same* camera in the same pose, only moved, so one homography describes the
whole capture:

    T_f(m) = Phi (m - delta_f)

Eight numbers for the mosaic, two per frame for where that frame sits, and a
small damped affine correction per frame for the ground not being the plane
`Phi` assumes. `fit_projection` derives all of that and argues it out — in
particular why the obvious per-frame affine cannot work at this scale. Read it
before changing anything here.

Pipeline:

  1. Coarse align each adjacent pair with overlap-normalised cross-correlation
     on 1/8-scale images (robust to the large ~1700 px offsets).
  2. Sample the local offset on a grid of windows across the overlap by phase
     correlation, and fit an affine to that field.
  3. Screen the measured pairs, re-measuring the rejects from the local median
     — the coarse align is what fails, not the imagery — and substituting the
     median, down-weighted, for anything still unfittable.
  4. Fit the one shared projection `Phi` to the median pair relation.
  5. Least-squares solve for each frame's placement and its small correction,
     all pairs at once, so alignment error is spread rather than accumulated.
  6. Resample and write tiles; optionally a downscaled preview.

Working at capture scale
------------------------

A capture is thousands of 4096x4096 frames — the 2026-08 grid is 85 x 37 =
3145, forty gigabytes on disk. Three things follow, and they shape the code far
more than the mathematics does:

  * **Frames are never all resident.** A worker opens the two frames a pair
    needs, measures, and drops them. Grey is decoded straight out of the JPEG
    (`draft("L")`), which skips the colour convert and costs ~0.08 s a frame, so
    re-reading a frame for each of its pairs is cheaper than caching it.
  * **The solve is sparse.** 3145 frames is 18,870 unknowns against 111,000
    constraint rows; dense, that matrix is 17 TB. Sparse it is 2 M non-zeros,
    and LSQR converges on it in seconds.
  * **The mosaic is not assembled.** 85 x 37 tiles of 1744 px is 9.6 gigapixels,
    29 GB in memory. `--preview` downsamples each tile as it is written and
    pastes it into a small canvas instead; `--mosaic` is refused above
    `--mosaic-budget` megapixels.

Peak RSS is roughly `--jobs` x 200 MB plus a few hundred MB for the solve, so
the default 8 workers sit under 2 GB regardless of how large the capture is.

Conventions throughout:

    b[r, c] ~= a[r + dy, c + dx]          local, within one window
    p_a = A_ab @ p_b                      per pair, p = (x, y, 1) column
    p_frame = T_f @ p_tile                per frame, the thing solved for

`T_f` takes a tile's *own* coordinates, `[0, out_w] x [0, out_h]`, not mosaic
coordinates: the tile's offset into the mosaic cancels against `delta_f`. So
`T_f` is exactly the map PIL needs to render the tile, and the mosaic never has
to be represented anywhere.
"""

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import NamedTuple

import numpy as np
import scipy.sparse as sparse
from PIL import Image
from scipy.optimize import least_squares
from scipy.sparse.linalg import lsqr

Image.MAX_IMAGE_PIXELS = None

FRAME_RE = re.compile(r"(\d+)_(\d+)\.(jpe?g|png|tiff?|webp)$", re.I)


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


def texture(shape, seed=0):
    """Band-limited noise: image-like, but with no global ramp.

    A ramp would pin cross-correlation at zero shift regardless of the true
    offset, which would make the self-test pass for the wrong reason.
    """
    rng = np.random.default_rng(seed)
    F = np.fft.rfft2(rng.standard_normal(shape).astype(np.float32))
    fy = np.fft.fftfreq(shape[0])[:, None]
    fx = np.fft.rfftfreq(shape[1])[None, :]
    return np.fft.irfft2(F * np.exp(-(fy ** 2 + fx ** 2) / (2 * 0.02 ** 2)),
                         s=shape).astype(np.float32)


def selftest():
    """Pin down the conventions on synthetic data with known transforms.

    A sign error anywhere in here silently correlates the wrong content and
    returns plausible-looking noise, so both the local convention (shift) and
    the pair convention (affine, and which way round it composes) are checked.
    """
    base = texture((800, 900))
    tdy, tdx = 37, -54
    a = base[200:600, 200:700]
    b = base[200 - tdy:600 - tdy, 200 - tdx:700 - tdx]
    cy, cx = coarse_align(a, b)
    py, px, _ = phase_align(a, b)
    assert (cy, cx) == (-tdy, -tdx), f"coarse convention: got ({cy},{cx})"
    assert abs(py + tdy) < 0.6 and abs(px + tdx) < 0.6, f"phase: ({py},{px})"

    # Affine round trip. `want` is p_a = want @ p_b, so frame a is frame b
    # resampled through want's inverse: a(q) = b(want^-1 q).
    want = np.array([[1.0040, 0.0060, 24.0],
                     [-0.0025, 0.9955, -17.0],
                     [0.0, 0.0, 1.0]])
    size = (1200, 1200)
    src = Image.fromarray(
        np.clip(texture(size, 1) * 40 + 128, 0, 255).astype(np.uint8))
    inv = np.linalg.inv(want)
    fa = np.asarray(src.transform(size, Image.AFFINE, tuple(inv[:2].ravel()),
                                  Image.BICUBIC), np.float32)
    fb = np.asarray(src, np.float32)
    got, _, _ = fit_affine(sample_pair(fa, fb, -17, 24, size[0], 256, 3))
    err = np.abs(got - want)
    assert (err[:, :2] < 6e-4).all() and (err[:, 2] < 1.5).all(), \
        f"affine convention:\n{got}"


def selftest_solve():
    """Round-trip the projection fit and the grid solve on a known capture.

    These are the steps with no visible output of their own to check: a wrong
    sign or a transposed index still produces tiles, just misaligned ones, and
    at 3000 frames nobody is going to notice which.

    So: invent a homography and a grid of frames placed by it, derive the pair
    relations that geometry implies exactly, and confirm the pipeline recovers
    a placement whose seams close. Note the pair relations are built by
    composition rather than by measurement, so they close their loops exactly —
    which is the point. It isolates the solve from the imagery.
    """
    rng = np.random.default_rng(7)
    ow, oh, fw, fh = 300, 280, 2048, 2048
    nx, ny = 5, 4
    nodes = [(x, y) for y in range(ny) for x in range(nx)]
    # A homography with real perspective in it: g, h are what an affine model
    # cannot represent and what the whole design turns on.
    Phi = np.array([[1.0300, -0.0170, 749.0],
                    [0.0004, 0.9740, 764.0],
                    [3.0e-08, -9.0e-06, 1.0]])
    # ...and frames that do not sit exactly on the nominal grid, which is what
    # the per-frame part of the solve exists to recover.
    off = {f: rng.normal(0, 4, 2) for f in nodes}

    def shift(d):
        S = np.eye(3)
        S[:2, 2] = -d
        return S

    # Everything below is in a tile's own coordinates, [0, ow] x [0, oh],
    # because that is what `T` maps: the mosaic offset cancels against the
    # tile's origin in the mosaic.
    T_true = {f: Phi @ shift(off[f]) for f in nodes}

    rel = {}
    for (x, y) in nodes:
        for b in ((x + 1, y), (x, y + 1)):
            if b not in T_true:
                continue
            # The exact pair relation, reduced to an affine fitted over the
            # seam band — which is what `measure` produces from real imagery.
            horizontal = b[1] == y
            pts = edge_points(horizontal, ow, oh, k=5)
            pa = project(T_true[(x, y)], pts)
            pb = project(T_true[b], pts - pair_delta(horizontal, ow, oh))
            M = np.column_stack([pb, np.ones(len(pb))])
            cx, *_ = np.linalg.lstsq(M, pa[:, 0], rcond=None)
            cy, *_ = np.linalg.lstsq(M, pa[:, 1], rcond=None)
            rel[((x, y), b)] = np.array([cx, cy, [0.0, 0.0, 1.0]])

    got, rms = fit_projection(rel, ow, oh, fw, fh, verbose=False)
    assert rms < 0.05, f"projection fit residual {rms:.3f}px"
    # The gauge is pinned by a tile's centre landing at the frame's centre.
    err = np.abs(project(got, [[ow / 2, oh / 2]]) - np.array([fw / 2, fh / 2]))
    assert err.max() < 0.05, f"projection gauge off by {err.max():.3f}px"

    T, resid, _ = solve_frames(rel, nodes, ow, oh, {k: 1.0 for k in rel},
                               got, prior=1.0, fw=fw, fh=fh)
    assert np.abs(resid).max() < 0.2, f"solve residual {np.abs(resid).max():.3f}"
    seams = seam_error(T, rel, ow, oh)
    assert max(seams.values()) < 0.2, f"solve seam {max(seams.values()):.3f}"

    # Every tile must land where the truth puts it, up to the one thing no
    # measurement can see: a global affine re-gauging of the mosaic. Stretch
    # the whole mosaic slightly and every pair relation still holds, because
    # each pair only ever sees its neighbour — the tell is a placement error
    # that is exactly linear in the grid position. It is harmless (the tiles
    # still abut and the crops stay centred), so it is fitted and removed
    # before asking whether the per-frame offsets came back.
    corners = tile_corners(ow, oh)
    delta = np.array([(project(T[f], corners)
                       - project(T_true[f], corners)).mean(0) for f in nodes])
    G = np.array([[1.0, f[0], f[1]] for f in nodes])
    gauge, *_ = np.linalg.lstsq(G, delta, rcond=None)
    worst = np.abs(delta - G @ gauge).max()
    assert worst < 0.5, f"tile placement off by {worst:.2f}px"

    # And the correction must stay near a pure translation: the projection
    # already explains every bit of scale and shear here. An accordion shows
    # up as a linear part that wanders — it reached 59% on the real capture
    # before `Phi` and the prior between them stopped it.
    for f in nodes:
        N = T[f] @ np.linalg.inv(T_true[f])
        assert np.abs(N[:2, :2] / N[2, 2] - np.eye(2)).max() < 0.02, \
            f"correction at {f} is not near a translation:\n{N / N[2, 2]}"


# --------------------------------------------------------------------------- #
# measurement
# --------------------------------------------------------------------------- #

def sample_pair(a, b, dy0, dx0, wm_top, win, n):
    """Local offsets on an n x n grid of windows spanning the overlap.

    One window can only ever report the offset at one place. Since the frames
    do not differ by a translation, that number is right where it was taken and
    wrong everywhere else — in particular at the crop edge, which is the one
    place the error becomes visible. Sampling the whole overlap is what makes
    the affine fit possible, and it costs one FFT pair per window.

    Returns rows of `[x, y, dx, dy, peak]` in frame-b coordinates, where
    `dx, dy` is the total offset there, not a residual on the prior.
    """
    W = a.shape[1]
    by_lo, by_hi = max(0, -dy0), min(wm_top - win, wm_top - dy0 - win)
    bx_lo, bx_hi = max(0, -dx0), min(W - win, W - dx0 - win)
    if by_hi < by_lo or bx_hi < bx_lo:
        return []
    out = []
    for by in np.unique(np.linspace(by_lo, by_hi, n).round().astype(int)):
        for bx in np.unique(np.linspace(bx_lo, bx_hi, n).round().astype(int)):
            ay, ax = by + dy0, bx + dx0
            rdy, rdx, peak = phase_align(a[ay:ay + win, ax:ax + win],
                                         b[by:by + win, bx:bx + win])
            out.append([bx + win / 2, by + win / 2,
                        dx0 + rdx, dy0 + rdy, peak])
    return out


def fit_affine(samples, min_peak=0.05):
    """Least-squares affine through a sampled offset field.

    Returns `(A, residual, used)` where `p_a = A @ p_b` for homogeneous column
    `p = (x, y, 1)`, `residual` is the per-sample distance left over, and `used`
    counts the samples the fit kept.

    Windows landing on water, deep shadow or the sky match badly and would drag
    the fit, so samples are dropped twice: on the correlation peak first, then
    on distance from a provisional fit. One reweighting pass is enough — the
    survivors agree to about a pixel, so a second pass has nothing to remove.
    """
    s = np.array([r for r in samples if r[4] >= min_peak], float)
    if len(s) < 6:
        return None, None, 0
    x, y, dx, dy = s[:, 0], s[:, 1], s[:, 2], s[:, 3]
    M = np.column_stack([x, y, np.ones(len(s))])
    keep = np.ones(len(s), bool)
    for refit in (False, True):
        cx, *_ = np.linalg.lstsq(M[keep], dx[keep], rcond=None)
        cy, *_ = np.linalg.lstsq(M[keep], dy[keep], rcond=None)
        resid = np.hypot(dx - M @ cx, dy - M @ cy)
        if refit:
            break                       # `resid` and `keep` now match this fit
        # Median-based, so a handful of gross outliers cannot inflate the gate
        # that is supposed to catch them.
        keep = resid <= max(4 * np.median(resid[keep]), 1.0)
        if keep.sum() < 6:
            return None, None, 0
    A = np.array([[1 + cx[0], cx[1], cx[2]],
                  [cy[0], 1 + cy[1], cy[2]],
                  [0.0, 0.0, 1.0]])
    return A, resid[keep], int(keep.sum())


# --- worker side ----------------------------------------------------------- #

_W = {}


def _init_measure(paths, cfg):
    _W.update(paths=paths, cfg=cfg)


def load_grey(path):
    """Decode one frame straight to greyscale.

    `draft("L")` asks libjpeg for a luma-only decode, which skips the YCbCr to
    RGB convert and the colour planes entirely: ~0.08 s for a 4096 x 4096 frame
    against ~0.11 s, and 16 MB resident instead of 64 MB. Cheap enough that
    frames are re-read per pair rather than cached, which is what keeps peak
    memory independent of the size of the capture.
    """
    im = Image.open(path)
    try:
        im.draft("L", im.size)
    except (AttributeError, ValueError):
        pass                              # not a JPEG; convert() below copes
    return np.asarray(im.convert("L"), dtype=np.uint8)


def _fit_pair(a_img, b_img, dy0, dx0, a, b, prior):
    cfg = _W["cfg"]
    rows = sample_pair(a_img, b_img, dy0, dx0,
                       cfg.watermark_top, cfg.window, cfg.samples)
    A, resid, used = fit_affine(rows)
    return {"a": list(a), "b": list(b),
            "A": None if A is None else A.tolist(),
            "rms": None if A is None else float(np.sqrt((resid ** 2).mean())),
            "used": used, "n": len(rows), "prior": prior}


def _measure_frame(job):
    """Measure one frame against its east and south neighbours.

    Grouped by frame rather than by pair so the frame is decoded once for both
    of its outgoing pairs, and so a task is a useful unit of cache progress.
    """
    f, nbrs = job
    wm = _W["cfg"].watermark_top
    a = load_grey(_W["paths"][f])
    small_a = downsample(a[:wm], 8)
    out = []
    for b in nbrs:
        bi = load_grey(_W["paths"][b])
        cy, cx = coarse_align(small_a, downsample(bi[:wm], 8))
        out.append(_fit_pair(a, bi, cy * 8, cx * 8, f, b, "coarse"))
        del bi
    return out


def _remeasure_pair(job):
    """Re-fit one pair from a supplied offset instead of the coarse align."""
    a, b, dy0, dx0 = job
    return _fit_pair(load_grey(_W["paths"][a]), load_grey(_W["paths"][b]),
                     int(round(dy0)), int(round(dx0)), a, b, "median")


def measure(paths, jobs, todo, cfg, cache_path, cache_meta, have, verbose=True):
    """Fit an affine to every adjacent pair in `todo`, in parallel.

    Results are flushed to the cache as they arrive: at capture scale this pass
    is tens of minutes, and losing it to a stray Ctrl-C would be the single
    most expensive mistake available. A re-run picks up the pairs still
    missing.
    """
    tasks = {}
    for a, b in todo:
        tasks.setdefault(a, []).append(b)
    tasks = sorted(tasks.items())
    total = len(todo)

    done = bad = 0
    t0 = last = saved = time.time()
    with ProcessPoolExecutor(jobs, initializer=_init_measure,
                             initargs=(paths, cfg)) as ex:
        for res in ex.map(_measure_frame, tasks, chunksize=1):
            for r in res:
                have[(tuple(r["a"]), tuple(r["b"]))] = r
                bad += r["A"] is None
            done += len(res)
            now = time.time()
            if verbose and (now - last > 5 or done == total):
                rate = done / max(now - t0, 1e-9)
                print(f"  {done}/{total} pairs  {rate:.1f}/s  "
                      f"eta {(total - done) / max(rate, 1e-9) / 60:.1f} min  "
                      f"unfittable {bad}", flush=True)
                last = now
            if now - saved > 60:          # bounded loss, bounded rewrite cost
                write_cache(cache_path, cache_meta, have)
                saved = time.time()
    write_cache(cache_path, cache_meta, have)
    return have


def horizontal(a, b):
    """True if `b` is `a`'s east neighbour, false if it is the south one."""
    return a[1] == b[1]


def median_key(a, b):
    """Which group of pairs this one is compared against: direction, then row.

    Per row rather than globally because range — and with it scale — varies
    gently from north to south, so a pair's own row is a much tighter reference
    than the whole capture.
    """
    return ("e" if horizontal(a, b) else "s", a[1])


def median_for(med, a, b):
    """The local median pair affine, falling back to the whole direction.

    A row with nothing usable in it — all water, say — still needs a prior.
    """
    key = median_key(a, b)
    return med.get(key, med.get((key[0], None)))


def screen(records, max_dev, max_rms, nominal_weight, verbose=True):
    """Replace implausible pair measurements with the local median.

    Over a 17 km capture the phase correlation has to contend with the Danube,
    forest and farmland — surfaces with either no texture to lock onto or, in
    the case of ploughed fields and orchards, a periodic one that offers a
    strong wrong peak. A handful of such pairs among six thousand is expected,
    and a single one left in would tear the grid: the solve has no way to know
    the constraint is a lie and will bend a whole neighbourhood to satisfy it.

    The grid is regular, so the pairs of one row are near-identical to each
    other and their median is a good estimate of any one of them — see
    `median_key`. A pair that could not be fitted, fitted badly, or lands far
    from that median is replaced by it and down-weighted, which keeps the
    system full rank without letting a guess compete with a measurement.
    """
    rel, weight, subs = {}, {}, []
    groups = {}
    for pair in records:
        groups.setdefault(median_key(*pair), []).append(pair)

    med = {}
    for key, pairs in groups.items():
        good = [records[p]["A"] for p in pairs
                if records[p]["A"] is not None
                and records[p]["rms"] is not None
                and records[p]["rms"] <= max_rms]
        if good:
            med[key] = np.median(np.array(good, float), axis=0)
    if not med:
        raise SystemExit("no pair could be fitted at all — check --window, "
                         "--watermark-top and that the frames really overlap")
    for d in ("e", "s"):
        rows = [v for (dd, _), v in med.items() if dd == d]
        if rows:
            med[(d, None)] = np.median(np.array(rows, float), axis=0)

    for (a, b), r in sorted(records.items()):
        m = median_for(med, a, b)
        A = None if r["A"] is None else np.array(r["A"], float)
        why = None
        if A is None:
            why = "unfittable"
        elif r["rms"] is not None and r["rms"] > max_rms:
            why = f"rms {r['rms']:.1f}"
        elif m is not None:
            dev = float(np.hypot(A[0, 2] - m[0, 2], A[1, 2] - m[1, 2]))
            if dev > max_dev:
                why = f"dev {dev:.0f}px"
        if why is not None:
            if m is None:
                raise SystemExit(f"{a}->{b}: {why}, and no median to fall back on")
            rel[(a, b)], weight[(a, b)] = m.copy(), nominal_weight
            subs.append((a, b, why))
        else:
            rel[(a, b)], weight[(a, b)] = A, 1.0

    if verbose and subs:
        print(f"  substituted the local median for {len(subs)} of {len(rel)} "
              f"pairs (weight {nominal_weight}):")
        for a, b, why in subs[:12]:
            print(f"    {a}->{b}  {why}")
        if len(subs) > 12:
            print(f"    ... and {len(subs) - 12} more")
    return rel, weight, subs, med


def retry(paths, jobs, subs, med, records, cache_path, meta, cfg, verbose=True):
    """Re-measure rejected pairs from the local median instead of the coarse align.

    Nearly every rejection is the coarse stage, not the imagery. Over water,
    forest and ploughed farmland the 1/8-scale cross-correlation has plenty of
    competing peaks and picks a wrong one; the windows are then placed over
    unrelated ground, correlate nothing, and the pair is dropped as
    unfittable — on the 2026-08 capture, 327 pairs, every one of them a miss of
    thousands of pixels rather than a shortage of texture.

    The grid supplies a far better prior than correlation can: neighbouring
    pairs in the same row agree to a pixel or two, so their median places the
    windows correctly. Fed that, the same imagery fits on 45-49 windows of 49.

    This is a real measurement, not the substitution it replaces — only the
    starting offset is borrowed, and every window still has to agree about
    where the ground actually is. Pairs that fail even from the median prior
    fall back to substitution in the re-screen.
    """
    tasks = []
    for a, b, _why in subs:
        if records.get((a, b), {}).get("prior") == "median":
            continue                      # already tried this; don't loop
        m = median_for(med, a, b)
        if m is not None:
            tasks.append((a, b, m[1, 2], m[0, 2]))
    if not tasks:
        return 0

    print(f"  re-measuring {len(tasks)} of them from the local median "
          f"(the coarse align, not the imagery, is what failed)...", flush=True)
    rescued = 0
    with ProcessPoolExecutor(jobs, initializer=_init_measure,
                             initargs=(paths, cfg)) as ex:
        for r in ex.map(_remeasure_pair, tasks, chunksize=4):
            records[(tuple(r["a"]), tuple(r["b"]))] = r
            rescued += r["A"] is not None
    write_cache(cache_path, meta, records)
    if verbose:
        print(f"  {rescued} of {len(tasks)} now fit")
    return rescued


# --------------------------------------------------------------------------- #
# geometry
# --------------------------------------------------------------------------- #

def tile_corners(out_w, out_h):
    """A tile's corners in its own frame's tile coordinates, x then y."""
    return np.array([[0.0, 0.0], [out_w, 0.0], [0.0, out_h], [out_w, out_h]])


def edge_points(horizontal, out_w, out_h, k=3, span=0.25):
    """Points about the edge a tile shares with its east or south neighbour.

    Returned in the *first* tile's own coordinates — `[0, out_w] x [0, out_h]`
    — as `k` positions along the edge by three offsets across it, at the edge
    and `span` tiles either side. The neighbour's copy of the same ground is
    these points minus one step, which is what `pair_delta` returns.

    Where a pair constraint is imposed decides whether a large capture holds
    together, because a pair affine is only locally true — it is fitted over
    the overlap, and the overlap is where the two tiles meet. With the tile
    size equal to the step, the shared edge lands within a few pixels of the
    overlap's centre, so evaluating there is the affine carrying its own
    measurement. Evaluating at the far corners of both tiles instead — two
    tiles apart, nearly the whole frame — extrapolates it across ground where
    the true relation bows away from affine by +-1.75%, ~30 px per pair and
    systematic, which the chain then multiplies.

    A band and not the line itself, because the line alone does not determine
    the answer. The difference between the two placements is affine, so forcing
    it to vanish at two points of a line forces it to vanish along the whole
    line and says nothing about the derivative across — a chain of frames can
    then breathe, each rescaling normal to its seams with the translation
    absorbing it, at no cost to any constraint, and the solve comes out rank
    deficient. Sampling `span` either side pins it, from data rather than from
    a regulariser. A quarter tile is ~440 px, well inside the +-1164 px of
    overlap the affine was fitted over.
    """
    s = np.linspace(0.0, 1.0, k)
    d = (-span, 0.0, span)
    if horizontal:                        # vertical edge, tiles side by side
        return np.array([[(1 + o) * out_w, t * out_h] for t in s for o in d])
    return np.array([[t * out_w, (1 + o) * out_h] for t in s for o in d])


def pair_delta(horizontal, out_w, out_h):
    """The step from a tile to its east or south neighbour, in tile pixels."""
    return np.array([out_w, 0.0]) if horizontal else np.array([0.0, out_h])


def project(H, pts):
    """Apply a 3x3 homography to an (n, 2) array of points."""
    pts = np.atleast_2d(np.asarray(pts, float))
    q = H @ np.vstack([pts.T, np.ones(len(pts))])
    return (q[:2] / q[2]).T


def pair_step(A, centre):
    """How far apart two neighbouring tiles' centres sit, in frame pixels.

    Each frame's tile is cut from the middle of it, so the two tile centres are
    at the same place in their own frames and the step `s` is whatever makes
    the pair affine carry one to the other:

        A (c - s/2) = c + s/2   =>   s = 2 (I + L)^-1 (A c - c)

    which is the displacement at the frame centre, corrected for the two tile
    centres not being the same point. The correction is not cosmetic: on the
    2026-08 capture it is 26 px of the 1767 px N-S step, and the step sets the
    tile size, whose error accumulates as crop drift right across the grid.
    """
    d = A[:2, :2] @ centre[:2] + A[:2, 2] - centre[:2]
    return 2 * np.linalg.solve(np.eye(2) + A[:2, :2], d)


# --------------------------------------------------------------------------- #
# the shared projection
# --------------------------------------------------------------------------- #

def fit_projection(rel, out_w, out_h, fw, fh, verbose=True):
    """One homography, mosaic to frame, shared by every frame in the capture.

    This is the model, and getting it right is what makes a capture of this
    size hold together at all.

    Every frame is the same camera in the same pose, moved. So each is the same
    perspective image of the same ground plane, offset — mosaic to frame is one
    homography `Phi` composed with a translation of the mosaic:

        T_f(m) = Phi (m - delta_f)

    Eight numbers for the whole capture, plus two per frame for where it sits.
    The pair relations follow from it and are not free: for neighbours one step
    `D` apart, `A_ab = Phi . translate(D) . Phi^-1`, the same matrix for every
    pair in a direction. That is why `Phi` can be fitted from just the median
    eastward and southward pair affine, and why the fit is worth trusting — it
    is 8 unknowns against thousands of measurements.

    Why not per-frame affines, which is the obvious thing and what this script
    used to do. An affine cannot be conjugated into itself by a translation:
    the measured `A_ab` has a linear part of `I + 0.0146`, and satisfying
    `T_a = A_ab T_b` with affine `T` forces each frame's scale to be its
    neighbour's times that factor. Over 84 columns it compounds to 3.4x, tiles
    wander tens of thousands of pixels out of their frames, and clamping the
    scale flat instead leaves a 14 px ramp along every seam — the residual is
    a clean linear ramp of slope 0.0146, the shear it could not represent.
    A homography *is* closed under this conjugation, so the same 0.0146 costs
    nothing: it is not a per-frame scale at all, it is `Phi` being evaluated
    half a tile either side of the seam.

    The gauge — where the mosaic's origin sits — is fixed by asking that a
    tile's centre land at the frame's centre, which is what keeps crops clear
    of the frame edges and the watermark.
    """
    east = [A for (a, b), A in rel.items() if a[1] == b[1]]
    south = [A for (a, b), A in rel.items() if a[0] == b[0]]
    if not east or not south:
        raise SystemExit("need pairs in both directions to fit the projection")
    want = {True: np.median(east, axis=0), False: np.median(south, axis=0)}
    pts = {h: edge_points(h, out_w, out_h) for h in (True, False)}
    step = {h: pair_delta(h, out_w, out_h) for h in (True, False)}
    centre = np.array([fw / 2, fh / 2])
    mid = np.array([[out_w / 2, out_h / 2]])

    def residual(p):
        H = np.append(p, 1.0).reshape(3, 3)
        r = []
        for h, A in want.items():
            pa = project(H, pts[h])
            pb = project(H, pts[h] - step[h])
            r.append((pa - pb @ A[:2, :2].T - A[:2, 2]).ravel())
        # Gauge, weighted hard because it is a choice rather than a fit.
        r.append((project(H, mid)[0] - centre) * 100)
        return np.concatenate(r)

    p0 = np.array([1.0, 0.0, fw / 2 - out_w / 2,
                   0.0, 1.0, fh / 2 - out_h / 2, 0.0, 0.0])
    sol = least_squares(residual, p0, method="lm", xtol=1e-15, ftol=1e-15)
    Phi = np.append(sol.x, 1.0).reshape(3, 3)
    rms = float(np.sqrt((residual(sol.x)[:-2] ** 2).mean()))
    if verbose:
        L = Phi[:2, :2]
        print(f"  projection: scale {L[0, 0]:.5f},{L[1, 1]:.5f}  "
              f"shear {L[0, 1]:+.5f},{L[1, 0]:+.5f}  "
              f"perspective {Phi[2, 0]:+.3e},{Phi[2, 1]:+.3e}")
        print(f"  reproduces the median pair relation to {rms:.3f} px")
    return Phi, rms


# --------------------------------------------------------------------------- #
# grid solve
# --------------------------------------------------------------------------- #

def solve_frames(rel, nodes, out_w, out_h, weight, Phi, prior=1.0,
                 fw=None, fh=None):
    """Least-squares per-frame correction `N_f` on top of the shared `Phi`.

    `Phi` says where a frame's tile is if the capture were perfect. It is not:
    Earth Studio's keyframes land the camera a little off the nominal grid, and
    the ground is not the plane `Phi` assumes — over 17 km of hills the two
    disagree by enough to matter. So each frame carries a small affine `N_f`,
    applied in frame pixels after `Phi`, and

        T_f(m) = N_f Phi (m - delta_f)

    with the tile of frame `f` at `delta_f = (x, y) * tile`. Each pair asserts
    `T_a = A_ab T_b` across the band where it was measured; written at points,
    the residual is a distance in pixels, so least squares minimises the thing
    that is actually visible. `N_f` enters linearly, so this stays one sparse
    solve — the projection absorbed all the nonlinearity.

    `N_f` is written as identity plus `[M_f | t_f]` about the frame centre, and
    `M_f` — the part that scales, shears and rotates — is penalised toward
    zero with weight `prior`. Translation is left free.

    The prior is doing something specific. Corrections that vary smoothly and
    grow across the grid are nearly free in the pair constraints, and the
    measurements, which do not close their loops (median 1.2 px per 2x2 on the
    2026-08 capture, and that is relief displacement, not error), push hard in
    exactly that direction. Left alone the grid breathes; penalising `M_f`
    absolutely stops it, because unlike the affine-per-frame model nothing here
    has to compound — `Phi` already carries the systematic part, so an honest
    `M_f` is small. Scaled by the half-tile it becomes the displacement it
    would cause at a tile corner, so `prior` reads as pixels of drift against
    pixels of seam. At 1.0 the corrections stay under a pixel of scale while
    the seams close to a quarter of one.

    Unlike the affine model this needs no gauge fixing afterwards and no frame
    pinned: `Phi` fixes the mosaic, and `M_f -> 0` fixes the rest.
    """
    S = 0.5 * (out_w + out_h)
    centre = np.array([fw / 2, fh / 2]) if fw else project(Phi, [[out_w / 2,
                                                                  out_h / 2]])[0]
    col = {f: i * 6 for i, f in enumerate(nodes)}
    band = {h: edge_points(h, out_w, out_h) for h in (True, False)}
    step = {h: pair_delta(h, out_w, out_h) for h in (True, False)}
    q = {h: (project(Phi, band[h]), project(Phi, band[h] - step[h]))
         for h in (True, False)}

    rows, cols, vals, rhs, wts = [], [], [], [], []
    # Assembled a pair at a time rather than an entry at a time: 6000 pairs of
    # 18 points is two million non-zeros, and appending those individually in
    # Python costs more than the solve does.
    row = 0
    for (a, b), A in rel.items():
        w = weight[(a, b)]
        qa, qb = q[horizontal(a, b)]
        # Known part: where Phi alone puts the two, and how far that is from
        # what the pair says. N_f has to make up the difference.
        base = qa - qb @ A[:2, :2].T - A[:2, 2]
        n = len(qa)
        # [u_x, u_y, 1] — the row a 2x3 correction contributes at each point.
        Ua = np.column_stack([(qa - centre) / S, np.ones(n)])
        Ub = np.column_stack([(qb - centre) / S, np.ones(n)])
        for i in (0, 1):
            rows.append(np.repeat(row + 2 * np.arange(n) + i, 9))
            cols.append(np.tile(np.concatenate([col[a] + 3 * i + np.arange(3),
                                                col[b] + np.arange(6)]), n))
            vals.append((w * np.hstack([Ua, -A[i, 0] * Ub,
                                        -A[i, 1] * Ub])).ravel())
        rhs.append((-w * base).ravel())   # row-major: point-major, then x/y
        wts.append(np.full(2 * n, w))
        row += 2 * n

    n_pair_rows = row
    if prior > 0:
        idx = np.array([c + i * 3 + j
                        for c in col.values() for i in (0, 1) for j in (0, 1)])
        rows.append(row + np.arange(len(idx)))
        cols.append(idx)
        # (S/2) / S, the half-tile in M units.
        vals.append(np.full(len(idx), prior * 0.5))
        rhs.append(np.zeros(len(idx)))
        wts.append(np.ones(len(idx)))
        row += len(idx)

    M = sparse.csr_matrix((np.concatenate(vals),
                           (np.concatenate(rows), np.concatenate(cols))),
                          shape=(row, len(col) * 6))
    y = np.concatenate(rhs)
    wts = np.concatenate(wts)
    sol, istop, itn = lsqr(M, y, atol=1e-13, btol=1e-13,
                           iter_lim=max(20000, 4 * len(col) * 6))[:3]
    # Unweighted and pair rows only, so the number quoted is a distance in
    # frame pixels and is not flattered by the prior rows, which are not
    # measurements.
    resid = ((M @ sol - y) / wts)[:n_pair_rows]

    T = {}
    for f, i in col.items():
        m = sol[i:i + 6].reshape(2, 3)
        L = np.eye(2) + m[:, :2] / S
        N = np.eye(3)
        N[:2, :2] = L
        N[:2, 2] = m[:, 2] - m[:, :2] @ centre / S
        T[f] = N @ Phi          # correction after shared projection
    return T, resid, (istop, itn)


def seam_error(T, rel, out_w, out_h, samples=13):
    """Worst disagreement along each shared edge, in frame pixels.

    This is the number that matters: not how well the model fits where it was
    measured, but how far apart the two frames place the ground at the line
    where their tiles actually meet.
    """
    s = np.linspace(0.0, 1.0, samples)
    out = {}
    for (a, b), A in rel.items():
        horizontal = a[1] == b[1]
        if horizontal:
            m = np.column_stack([np.full(samples, out_w), s * out_h])
        else:
            m = np.column_stack([s * out_w, np.full(samples, out_h)])
        pa = project(T[a], m)
        pb = project(T[b], m - pair_delta(horizontal, out_w, out_h))
        out[(a, b)] = float(np.hypot(*(pa - pb @ A[:2, :2].T - A[:2, 2]).T).max())
    return out


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #

def resample(im, T, out_w, out_h, ss):
    """Sample one tile out of its frame through the frame's homography.

    PIL's PERSPECTIVE transform reads

        src = ((a x + b y + c) / (g x + h y + 1),
               (d x + e y + f) / (g x + h y + 1))

    for each output pixel, which is exactly `T` read as a 3x3 — so the crop,
    the squaring resample and the perspective correction all happen in the
    single interpolation, with no intermediate generation. `T` maps the tile's
    own coordinates, `[0, out_w] x [0, out_h]`, because the mosaic offset
    `delta_f` cancels against the tile's origin in the mosaic.

    The transform is a plain sampler with no prefilter, unlike `resize`, so it
    is run at `ss` times the output resolution and box-reduced. At the ~4%
    downscale squaring implies that is close to free insurance; it matters more
    if a future capture needs a larger one.
    """
    H = T @ np.diag([1.0 / ss, 1.0 / ss, 1.0])
    H = H / H[2, 2]
    tile = im.transform((out_w * ss, out_h * ss), Image.PERSPECTIVE,
                        tuple(H.ravel()[:8]), Image.BICUBIC)
    return tile.resize((out_w, out_h), Image.BOX) if ss > 1 else tile


_R = {}


def _init_render(paths, out_dir, ext, save_kw, out_w, out_h, ss, thumb, resume):
    _R.update(paths=paths, out_dir=out_dir, ext=ext, save_kw=save_kw,
              out_w=out_w, out_h=out_h, ss=ss, thumb=thumb, resume=resume)


def _render_tile(job):
    """Write one tile, and return its thumbnail if a preview was asked for.

    Only the thumbnail travels back through the pool. A tile is ~8 MB as raw
    pixels and there are thousands of them; the preview is assembled from the
    23-pixel versions instead, which is the difference between a bounded few
    hundred MB and the 25 GB the full mosaic would need.
    """
    f, T = job
    x, y = f
    dst = _R["out_dir"] / f"{x}_{y}.{_R['ext']}"
    ow, oh, thumb = _R["out_w"], _R["out_h"], _R["thumb"]
    if _R["resume"] and dst.exists():
        if not thumb:
            return f, None
        tile = Image.open(dst).convert("RGB")
    else:
        im = Image.open(_R["paths"][f]).convert("RGB")
        tile = resample(im, np.array(T, float), ow, oh, _R["ss"])
        tile.save(dst, **_R["save_kw"])
    if not thumb:
        return f, None
    small = tile.resize(thumb, Image.LANCZOS)
    return f, small.tobytes()


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #

CACHE_VERSION = 3


class Measurement(NamedTuple):
    """Everything a pair measurement depends on.

    One object rather than three loose ints, because the same three have to
    reach the worker processes, describe the cache, and decide whether a cache
    on disk still applies — and a value that drifted between those would either
    silently mix settings or throw away good measurements.
    """
    watermark_top: int
    window: int
    samples: int

    def cache_meta(self, fw, fh):
        return {"window": self.window, "samples": self.samples,
                "watermark_top": self.watermark_top, "frame": [fw, fh]}


def read_cache(path, meta):
    """Load whatever pair affines are on disk and still apply.

    Unlike earlier versions this does not insist the cache be complete: the
    measurement pass is long enough at capture scale that resuming a partial
    one matters more than the simplicity of an all-or-nothing check. Pairs the
    cache does not have are simply measured.
    """
    if not path or not path.exists():
        return {}
    data = json.loads(path.read_text())
    if data.get("version") != CACHE_VERSION:
        # v1 held one offset per pair, which cannot be upgraded: the affine
        # needs the whole sampled field, and that was never recorded. v2 has
        # affines but no record of the settings they were measured under.
        print(f"{path} is version {data.get('version', 1)}, this build writes "
              f"{CACHE_VERSION}; re-measuring", file=sys.stderr)
        return {}
    if data.get("meta") != meta:
        print(f"{path} was measured with different settings "
              f"({data.get('meta')}); re-measuring", file=sys.stderr)
        return {}
    return {(tuple(r["a"]), tuple(r["b"])): r for r in data["pairs"]}


def write_cache(path, meta, records):
    if not path:
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({
        "version": CACHE_VERSION,
        "meta": meta,
        "pairs": [records[k] for k in sorted(records)],
    }))
    tmp.replace(path)                     # never leave a half-written cache


# --------------------------------------------------------------------------- #
# frames
# --------------------------------------------------------------------------- #

def discover(frames_dir, region=None):
    """Map the frames directory to a grid, and check it is a complete one."""
    paths = {}
    for p in sorted(frames_dir.iterdir()):
        m = FRAME_RE.fullmatch(p.name)
        if m:
            paths[(int(m[1]), int(m[2]))] = p
    if not paths:
        raise SystemExit(f"no frames named <x>_<y>.<ext> in {frames_dir}/")
    nx = max(x for x, _ in paths) + 1
    ny = max(y for _, y in paths) + 1

    x0, y0, w, h = region or (0, 0, nx, ny)
    if x0 < 0 or y0 < 0 or x0 + w > nx or y0 + h > ny or w < 2 or h < 1:
        raise SystemExit(f"--region {x0} {y0} {w} {h} does not fit the "
                         f"{nx}x{ny} grid found in {frames_dir}/")
    nodes = [(x, y) for y in range(y0, y0 + h) for x in range(x0, x0 + w)]
    missing = [f for f in nodes if f not in paths]
    if missing:
        raise SystemExit(f"{len(missing)} missing frames, first: " +
                         ", ".join(f"{x}_{y}" for x, y in missing[:8]))
    return paths, (nx, ny), nodes


def frame_size(paths, nodes):
    """Frame dimensions, checked across the capture.

    `Image.open` reads the header only, so this is 3000 stats and no decodes —
    a second, against a pipeline that would otherwise fail thousands of frames
    in with an unhelpful error.
    """
    sizes = {}
    for f in nodes:
        with Image.open(paths[f]) as im:
            sizes.setdefault(im.size, []).append(f)
    if len(sizes) > 1:
        detail = "; ".join(f"{w}x{h}: {len(v)} frames ({v[0][0]}_{v[0][1]}...)"
                           for (w, h), v in sizes.items())
        raise SystemExit(f"frames differ in size — {detail}")
    return next(iter(sizes))


# --------------------------------------------------------------------------- #
# pipeline stages
# --------------------------------------------------------------------------- #

def measure_pairs(args, paths, nodes, cfg, meta):
    """Every adjacent pair's raw measurement, from the cache or freshly made."""
    nodeset = set(nodes)
    wanted = {(a, b)
              for a in nodes
              for b in ((a[0] + 1, a[1]), (a[0], a[1] + 1))
              if b in nodeset}

    have = {k: v for k, v in read_cache(args.cache, meta).items() if k in wanted}
    todo = sorted(wanted - set(have))
    if have:
        print(f"cache:         {len(have)} of {len(wanted)} pairs already measured")
    if todo:
        print(f"measuring {len(todo)} pairs on {args.jobs} workers "
              f"(~{2 * len(todo) / args.jobs * 0.25 / 60:.0f} min)...", flush=True)
        measure(paths, args.jobs, todo, cfg, args.cache, meta, have,
                not args.quiet)
    return have


def screen_pairs(args, paths, have, cfg, meta):
    """Screen the measurements, re-measuring the rejects where that helps.

    Returns `(rel, weight)`: the affine each pair asserts, and how much the
    solve should believe it.
    """
    print("\nscreening pair measurements...")
    screened = screen(have, args.max_dev, args.max_rms, args.nominal_weight,
                      not args.quiet)
    rel, weight, subs, med = screened
    if subs and not args.no_retry:
        if retry(paths, args.jobs, subs, med, have, args.cache, meta, cfg,
                 not args.quiet):
            rel, weight, subs, med = screen(have, args.max_dev, args.max_rms,
                                            args.nominal_weight, not args.quiet)
    print(f"  {sum(1 for w in weight.values() if w == 1.0)} of {len(rel)} pairs "
          f"measured, {len(subs)} substituted")
    return rel, weight


def choose_tile_size(args, rel, fw, fh):
    """The step between tile centres, and the output tile size it implies.

    Under a perspective camera there is no single step — the displacement
    varies across the frame, which is the whole point — but every tile is cut
    from the middle of its frame, so the step between tile *centres* is well
    defined, and it is what sets the output resolution.
    """
    centre = np.array([fw / 2, fh / 2, 1.0])
    steps = {k: pair_step(A, centre) for k, A in rel.items()}
    horiz = np.array([d for pair, d in steps.items() if horizontal(*pair)])
    vert = np.array([d for pair, d in steps.items() if not horizontal(*pair)])
    src_w, src_h = float(np.median(horiz[:, 0])), float(np.median(vert[:, 1]))
    print(f"\nmeasured step: dx={src_w:.2f}  dy={src_h:.2f}  (between tile centres)")
    print(f"               spread  dx {horiz[:, 0].min():.1f}..{horiz[:, 0].max():.1f}"
          f"  dy {vert[:, 1].min():.1f}..{vert[:, 1].max():.1f}"
          f"  cross-axis |dy|<={np.abs(horiz[:, 1]).max():.1f},"
          f" |dx|<={np.abs(vert[:, 0]).max():.1f}")
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
    return src_w, src_h, out_w, out_h


def report(args, T, rel, weight, resid, nodes, src_w, src_h, out_w, out_h,
           fw, fh):
    """Print what the solve achieved, and refuse to render a grid that drifted."""
    print(f"\nsource extent: {src_w:.2f} x {src_h:.2f} px per tile")
    print(f"tile size:     {out_w} x {out_h}"
          f"{' (native, no resampling)' if args.native else ''}")
    sx, sy = out_w / src_w, out_h / src_h
    if abs(sx / sy - 1) > 1e-4:
        print(f"resample:      x{sx:.4f} horizontal, x{sy:.4f} vertical  ->  "
              f"{abs(sx / sy - 1) * 100:.1f}% aspect distortion")
    print(f"solve residual: rms={np.sqrt((resid ** 2).mean()):.2f}px "
          f"max={np.abs(resid).max():.2f}px  (frame px, in the seam bands)")

    seams = seam_error(T, rel, out_w, out_h)
    e = np.array(list(seams.values()))
    measured = np.array([v for k, v in seams.items() if weight[k] == 1.0])
    worst = max(seams, key=seams.get)
    print(f"seam error:    median={np.median(e):.2f}px  mean={e.mean():.2f}px"
          f"  p99={np.percentile(e, 99):.2f}px  max={e.max():.2f}px "
          f"at {worst[0]}->{worst[1]}")
    print(f"               {100 * (e < 2).mean():.0f}% of seams under 2px, "
          f"{100 * (e < 5).mean():.0f}% under 5px")
    if len(measured) < len(e):
        print(f"               over measured pairs only: mean="
              f"{measured.mean():.2f}px  max={measured.max():.2f}px")

    # A tile cannot be clipped back into the frame the way a crop origin could —
    # moving one breaks its joint with all four neighbours — so a crop that
    # runs off the frame or reaches the watermark is a hard stop, not a nudge.
    corners = tile_corners(out_w, out_h)
    strays, wm, margin = [], [], []
    for f in nodes:
        pts = project(T[f], corners)
        over = max(-pts.min(), pts[:, 0].max() - fw, pts[:, 1].max() - fh)
        margin.append(-over)
        if over > 0:
            strays.append((over, f))
        if (pts[:, 1] > args.watermark_top).any():
            wm.append(f)
    if strays:
        strays.sort(reverse=True)
        raise SystemExit(
            f"{len(strays)} tile(s) would sample outside their frame — the grid "
            f"has drifted further than the overlap allows. Worst: " +
            ", ".join(f"{f[0]}_{f[1]} by {o:.0f}px" for o, f in strays[:5]))
    print(f"crop margin:   {min(margin):.0f}px at the tightest frame "
          f"(of {(fw - src_w) / 2:.0f}px nominal)")
    print(f"watermark:     "
          f"{'excluded from all tiles' if not wm else f'INSIDE {len(wm)} tile(s)'}")


def render(args, paths, nodes, T, out_w, out_h, origin, nx, ny):
    """Resample every tile, and assemble the preview and mosaic if asked."""
    args.out.mkdir(parents=True, exist_ok=True)
    ext = "png" if args.format == "png" else "jpeg"
    save_kw = {} if args.format == "png" else {"quality": args.quality,
                                               "subsampling": 0}
    thumb = None
    if args.preview:
        tw = max(1, round(args.preview_width / nx))
        thumb = (tw, max(1, round(tw * out_h / out_w)))
    preview = Image.new("RGB", (thumb[0] * nx, thumb[1] * ny)) if thumb else None

    mosaic = None
    if args.mosaic:
        mpx = out_w * nx * out_h * ny / 1e6
        if mpx > args.mosaic_budget:
            raise SystemExit(
                f"the mosaic would be {out_w * nx} x {out_h * ny} = "
                f"{mpx:.0f} Mpx, about {mpx * 3 / 1024:.0f} GB in memory, over "
                f"the {args.mosaic_budget:.0f} Mpx budget. Use --preview, raise "
                f"--mosaic-budget, or --region a part of the grid.")
        mosaic = Image.new("RGB", (out_w * nx, out_h * ny))

    print(f"\nrendering {len(nodes)} tiles on {args.jobs} workers...", flush=True)
    t0, done, last = time.time(), 0, 0.0
    jobs = [(f, T[f].tolist()) for f in nodes]
    with ProcessPoolExecutor(
            args.jobs, initializer=_init_render,
            initargs=(paths, args.out, ext, save_kw, out_w, out_h,
                      max(1, args.supersample), thumb, args.resume)) as ex:
        for f, raw in ex.map(_render_tile, jobs, chunksize=4):
            done += 1
            gx, gy = f[0] - origin[0], f[1] - origin[1]
            if raw is not None:
                preview.paste(Image.frombytes("RGB", thumb, raw),
                              (gx * thumb[0], gy * thumb[1]))
            if mosaic:
                # Read back rather than ship the tile through the pool: at 8 MB
                # of raw pixels a tile costs more to pickle than to re-decode.
                mosaic.paste(Image.open(args.out / f"{f[0]}_{f[1]}.{ext}"),
                             (gx * out_w, gy * out_h))
            now = time.time()
            if not args.quiet and (now - last > 5 or done == len(nodes)):
                rate = done / max(now - t0, 1e-9)
                print(f"  {done}/{len(nodes)} tiles  {rate:.1f}/s  "
                      f"eta {(len(nodes) - done) / max(rate, 1e-9) / 60:.1f} min",
                      flush=True)
                last = now
    print(f"wrote {len(nodes)} tiles to {args.out}/  ({out_w}x{out_h} each)")

    for im, path, q in ((mosaic, args.mosaic, args.quality),
                        (preview, args.preview, 90)):
        if im:
            path.parent.mkdir(parents=True, exist_ok=True)
            im.save(path, quality=q)
            print(f"wrote {path} ({im.width} x {im.height})")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--frames", type=Path, default=Path("frames"))
    p.add_argument("--out", type=Path, default=Path("tiles"))
    p.add_argument("--region", type=int, nargs=4, metavar=("X0", "Y0", "NX", "NY"),
                   help="restrict the whole pipeline to a sub-rectangle of the "
                        "grid; tiles keep their global names, so this is the "
                        "way to try settings on a corner of a large capture")
    p.add_argument("--watermark-top", type=int, default=3300,
                   help="rows below this are excluded from matching (default 3300)")
    p.add_argument("--window", type=int, default=384, metavar="W",
                   help="edge of each correlation window in px (default 384)")
    p.add_argument("--samples", type=int, default=7, metavar="N",
                   help="fit each pair's affine from an N x N grid of windows "
                        "spanning the overlap (default 7)")
    p.add_argument("--jobs", "-j", type=int, default=min(8, os.cpu_count() or 1),
                   metavar="J", help="worker processes for measuring and "
                        "rendering (default min(8, cpus)); each needs roughly "
                        "200 MB")
    p.add_argument("--max-rms", type=float, default=8.0, metavar="PX",
                   help="a pair whose affine fits worse than this is replaced "
                        "by the local median (default 8)")
    p.add_argument("--max-dev", type=float, default=80.0, metavar="PX",
                   help="...as is one whose step differs from the median of "
                        "its row by more than this (default 80)")
    p.add_argument("--no-retry", action="store_true",
                   help="do not re-measure rejected pairs from the local "
                        "median; substitute it directly, as older builds did")
    p.add_argument("--prior", type=float, default=1.0, metavar="W",
                   help="how hard each frame's linear correction is pulled "
                        "toward the shared projection (default 1.0), in pixels "
                        "of tile-corner drift per pixel of seam; 0 lets the "
                        "grid breathe out of its frames")
    p.add_argument("--nominal-weight", type=float, default=0.05, metavar="W",
                   help="weight given to a substituted pair in the solve "
                        "(default 0.05), so a guess cannot outvote a "
                        "measurement")
    p.add_argument("--supersample", type=int, default=2, metavar="S",
                   help="resample tiles at S times the output resolution and "
                        "box-reduce (default 2); the sampler has no prefilter "
                        "of its own, so S=1 aliases")
    p.add_argument("--tile-size", type=int, default=None, metavar="S",
                   help="output square tile size in px; default is the smaller "
                        "measured step rounded to --multiple")
    p.add_argument("--multiple", type=int, default=8, metavar="M",
                   help="round the auto tile size to a multiple of M (default 8)")
    p.add_argument("--native", action="store_true",
                   help="keep the native non-square tile size (no resampling, "
                        "preserves the imagery's true aspect ratio)")
    p.add_argument("--cache", type=Path, default=None,
                   help="read/write measured pair affines here; a partial "
                        "cache is resumed rather than discarded")
    p.add_argument("--measure-only", action="store_true",
                   help="measure and cache, then stop before solving")
    p.add_argument("--resume", action="store_true",
                   help="skip tiles that already exist in --out")
    p.add_argument("--format", choices=("png", "jpeg"), default="png")
    p.add_argument("--quality", type=int, default=95)
    p.add_argument("--mosaic", type=Path, default=None,
                   help="also write the assembled mosaic here; refused above "
                        "--mosaic-budget, use --preview instead")
    p.add_argument("--mosaic-budget", type=float, default=1000.0, metavar="MPX",
                   help="largest mosaic to attempt, in megapixels (default "
                        "1000, about 3 GB resident)")
    p.add_argument("--preview", type=Path, default=None,
                   help="write a downscaled mosaic preview here; assembled "
                        "from per-tile thumbnails, so it costs no extra memory")
    p.add_argument("--preview-width", type=int, default=4000)
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    selftest()
    selftest_solve()

    paths, (gnx, gny), nodes = discover(args.frames, args.region)
    xs = sorted({x for x, _ in nodes})
    ys = sorted({y for _, y in nodes})
    nx, ny = len(xs), len(ys)
    fw, fh = frame_size(paths, nodes)
    print(f"{args.frames}/: {gnx} x {gny} grid, frames {fw} x {fh}")
    if args.region:
        print(f"region:        x {xs[0]}..{xs[-1]}, y {ys[0]}..{ys[-1]}  "
              f"({nx} x {ny} = {len(nodes)} frames)")
    else:
        print(f"stitching:     {len(nodes)} frames")

    cfg = Measurement(args.watermark_top, args.window, args.samples)
    meta = cfg.cache_meta(fw, fh)
    have = measure_pairs(args, paths, nodes, cfg, meta)
    if args.measure_only:
        print(f"measured {len(have)} pairs; stopping before the solve "
              f"(--measure-only)")
        return

    rel, weight = screen_pairs(args, paths, have, cfg, meta)
    src_w, src_h, out_w, out_h = choose_tile_size(args, rel, fw, fh)

    # The squaring rides along in the projection: asking for square tiles out
    # of a non-square step is a scale in the mosaic-to-frame map, not a
    # separate pass over the pixels, so it costs nothing beyond the one
    # resampling the perspective correction already required.
    print("\nfitting the shared projection...")
    Phi, _ = fit_projection(rel, out_w, out_h, fw, fh, not args.quiet)

    print(f"\nsolving {6 * len(nodes)} unknowns from {18 * len(rel)} "
          f"constraints...", flush=True)
    t0 = time.time()
    T, resid, (istop, itn) = solve_frames(rel, nodes, out_w, out_h, weight,
                                          Phi, args.prior, fw, fh)
    print(f"  lsqr stopped on {istop} after {itn} iterations "
          f"({time.time() - t0:.1f}s)")

    report(args, T, rel, weight, resid, nodes, src_w, src_h, out_w, out_h, fw, fh)
    render(args, paths, nodes, T, out_w, out_h, (xs[0], ys[0]), nx, ny)


if __name__ == "__main__":
    sys.exit(main())
