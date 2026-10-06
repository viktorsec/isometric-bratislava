#!/usr/bin/env python3
"""Incrementally build the sparse RGBA redraw pyramid; originals are untouched."""
import argparse
import fcntl
from collections import OrderedDict
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
CELL = 1024
STRIDE = 896
FADE = 96
SCHEMA = 1
NAME = re.compile(r'_c(\d+)_r(\d+)_')


def fingerprint(p):
    s = p.stat()
    return f'{s.st_mtime_ns}-{s.st_size}'


def scan(inbox):
    cells = {}
    if inbox.exists():
        for p in sorted(inbox.iterdir()):
            m = NAME.search(p.name)
            if p.is_file() and not p.name.startswith('.') and m:
                c, r = map(int, m.groups())
                key = f'{c},{r}'
                if key in cells:
                    raise ValueError(f'multiple files claim cell {key}')
                with Image.open(p) as im:
                    if im.width != im.height:
                        raise ValueError(f'{p.name}: redraw must be square')
                    size = im.width
                cells[key] = dict(name=p.name, v=fingerprint(p), size=size)
    return cells


def read_geometry(path):
    raw = path.read_text()
    info = json.loads(raw[raw.index('{'):raw.rindex('}') + 1])
    return info['width'], info['height'], info['tileSize'], info['maxLevel']


def origin(c, r, width, height):
    return max(0, min(c * STRIDE, width - CELL)), max(0, min(r * STRIDE, height - CELL))


def half_rgba(im):
    """Local 2x2 reduction in premultiplied alpha, replicating odd edges."""
    a = np.asarray(im, dtype=np.uint32)
    if len(a) % 2:
        a = np.concatenate((a, a[-1:]), axis=0)
    if a.shape[1] % 2:
        a = np.concatenate((a, a[:, -1:]), axis=1)
    h, w = a.shape[0] // 2, a.shape[1] // 2
    alpha = a[..., 3].reshape(h, 2, w, 2).sum((1, 3))
    rgb = (a[..., :3] * a[..., 3:]).reshape(h, 2, w, 2, 3).sum((1, 3))
    out = np.zeros((h, w, 4), dtype=np.uint8)
    out[..., :3] = ((rgb + alpha[..., None] // 2) // np.maximum(1, alpha[..., None])).astype(np.uint8)
    out[..., 3] = ((alpha + 2) // 4).astype(np.uint8)
    return Image.fromarray(out)


def bayer():
    m = np.zeros((1, 1), dtype=float)
    while len(m) < 8:
        m = np.block([[4*m, 4*m+2], [4*m+3, 4*m+1]])
    return (m + .5) / 64


class Builder:
    def __init__(self, inbox, out, geometry):
        self.inbox, self.out, self.geometry = inbox, out, geometry
        self.cache = OrderedDict()

    def source(self, key, cells, density, faded):
        ck = (key, faded)
        if ck in self.cache:
            self.cache.move_to_end(ck)
            return self.cache[ck]
        entry = cells[key]
        with Image.open(self.inbox / entry['name']) as src:
            im = src.convert('RGBA').resize((CELL*density, CELL*density), Image.Resampling.NEAREST)
        if faded:
            c, r = map(int, key.split(','))
            x, y = origin(c, r, self.width, self.height)
            fw = min(FADE, origin(c-1, r, self.width, self.height)[0] + CELL - x) if c and f'{c-1},{r}' in cells else 0
            fh = min(FADE, origin(c, r-1, self.width, self.height)[1] + CELL - y) if r and f'{c},{r-1}' in cells else 0
            if fw or fh:
                yy, xx = np.indices((CELL, CELL))
                threshold = bayer()[yy % 8, xx % 8]
                keep = np.ones((CELL, CELL), dtype=bool)
                if fw:
                    keep &= (xx >= fw) | ((xx+.5)/fw > threshold)
                if fh:
                    keep &= (yy >= fh) | ((yy+.5)/fh > threshold)
                mask = Image.fromarray(keep.astype(np.uint8)*255).resize(im.size, Image.Resampling.NEAREST)
                a = np.asarray(im).copy()
                a[..., 3] = np.where(np.asarray(mask), a[..., 3], 0)
                im = Image.fromarray(a)
        self.cache[ck] = im
        while len(self.cache) > 8:
            self.cache.popitem(last=False)
        return im

    def build(self, force=False):
        self.cache.clear()
        cells = scan(self.inbox)
        self.width, self.height, tile, base_top = read_geometry(self.geometry)
        width, height = self.width, self.height
        cols = max(1, math.ceil((width-CELL)/STRIDE)+1)
        rows = max(1, math.ceil((height-CELL)/STRIDE)+1)
        for key in cells:
            c, r = map(int, key.split(','))
            if c >= cols or r >= rows:
                raise ValueError(f'cell {key} is outside the current mosaic')
        density = 2 ** max(0, math.ceil(math.log2(max([CELL] + [e['size'] for e in cells.values()]) / CELL)))
        top = base_top + int(math.log2(density))
        config = dict(schema=SCHEMA, width=width, height=height, tileSize=tile, maxLevel=top, density=density,
                      cellSize=CELL, stride=STRIDE, fade=FADE)
        self.out.mkdir(parents=True, exist_ok=True)
        manifest_path = self.out / 'manifest.json'
        try:
            old = json.loads(manifest_path.read_text())
        except (OSError, ValueError):
            old = {}
        reset = force or any(old.get(k) != v for k, v in config.items())
        previous = old.get('sources', {})
        changed = set(cells) | set(previous) if reset else {k for k in set(cells) | set(previous) if cells.get(k) != previous.get(k)}
        if not changed and old:
            return old
        # Arriving/deleted neighbours change the leading masks of the cell to
        # their right/below. Recompose those complete footprints as well.
        affected = set(changed)
        for key in changed:
            c, r = map(int, key.split(','))
            for other in (f'{c+1},{r}', f'{c},{r+1}'):
                if other in cells or other in previous:
                    affected.add(other)
        dirty = set()
        for key in affected:
            c, r = map(int, key.split(','))
            x, y = origin(c, r, width, height)
            for ty in range(y*density//tile, math.ceil(min(height, y+CELL)*density/tile)):
                for tx in range(x*density//tile, math.ceil(min(width, x+CELL)*density/tile)):
                    dirty.add((tx, ty))
        ordered = sorted(cells, key=lambda k: tuple(reversed(tuple(map(int, k.split(','))))))
        version = uuid.uuid4().hex
        manifest = dict(config, sources=cells, version=version, variants={})
        writes = 0
        for mode in ('plain', 'crossfade'):
            levels = [{} for _ in range(top+1)] if reset else [dict(v) for v in old['variants'][mode]]
            current_dirty = dirty
            for z in range(top, -1, -1):
                factor = 2**(top-z)
                lw, lh = math.ceil(width*density/factor), math.ceil(height*density/factor)
                for tx, ty in sorted(current_dirty):
                    x0, y0 = tx*tile, ty*tile
                    tw, th = min(tile, lw-x0), min(tile, lh-y0)
                    if tw <= 0 or th <= 0:
                        continue
                    im = Image.new('RGBA', (tw, th))
                    if z == top:
                        for key in ordered:
                            c, r = map(int, key.split(','))
                            ox, oy = origin(c, r, width, height)
                            ox, oy = ox*density, oy*density
                            cx0, cy0 = max(x0, ox), max(y0, oy)
                            cx1, cy1 = min(x0+tw, ox+CELL*density), min(y0+th, oy+CELL*density)
                            if cx1 > cx0 and cy1 > cy0:
                                source = self.source(key, cells, density, mode == 'crossfade')
                                im.alpha_composite(source.crop((cx0-ox, cy0-oy, cx1-ox, cy1-oy)), (cx0-x0, cy0-y0))
                    else:
                        big = Image.new('RGBA', (min(2*tile, math.ceil(width*density/(factor//2))-2*x0),
                                                   min(2*tile, math.ceil(height*density/(factor//2))-2*y0)))
                        for dy in (0, 1):
                            for dx in (0, 1):
                                path = levels[z+1].get(f'{2*tx+dx},{2*ty+dy}')
                                if path:
                                    with Image.open(self.out / path) as child:
                                        big.paste(child.convert('RGBA'), (dx*tile, dy*tile))
                        im = half_rgba(big)
                    key = f'{tx},{ty}'
                    if not im.getchannel('A').getbbox():
                        levels[z].pop(key, None)
                        continue
                    path = f'{mode}/{z}/{tx}_{ty}_{version}.webp'
                    target = self.out / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    im.save(target, 'WEBP', lossless=True, method=4, exact=True)
                    levels[z][key] = path
                    writes += 1
                current_dirty = {(x//2, y//2) for x, y in current_dirty}
            manifest['variants'][mode] = levels
        # Do not publish a snapshot assembled from source files changing mid-build.
        if scan(self.inbox) != cells:
            raise RuntimeError('redraw sources changed during build; retrying on the next scan')
        tmp = manifest_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(manifest, separators=(',', ':')))
        os.replace(tmp, manifest_path)
        # Keep old URLs available for viewers still displaying the previous snapshot.
        keep = {p for levels in manifest['variants'].values() for lv in levels for p in lv.values()}
        for p in self.out.glob('*/*/*.webp'):
            if str(p.relative_to(self.out)) not in keep and p.stat().st_mtime < time.time()-3600:
                p.unlink()
        print(f'redraw pyramid: {len(cells)} cells, {len(changed)} changed, {writes} tiles written', flush=True)
        return manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--inbox', type=Path, default=ROOT/'redrawn-cells')
    ap.add_argument('--out', type=Path, default=ROOT/'web/redrawn-pyramid')
    ap.add_argument('--geometry', type=Path, default=ROOT/'web/tiles/info.js')
    ap.add_argument('--force', action='store_true')
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / '.build.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        Builder(args.inbox, args.out, args.geometry).build(args.force)


if __name__ == '__main__':
    main()
