#!/usr/bin/env python3
"""Collect frames into a single flat, grid-named folder.

This script reads each folder's `.esp` project file instead and recovers the actual
camera latitude and longitude keyframes, then names every frame by its true grid
position:

    <out>/<x>_<y>.jpeg      x = column, 0 = west  -> increasing east
                            y = row,    0 = north -> increasing south

which matches image orientation: the camera faces north, so north renders at frame
top and east renders to the right.

Dry run by default. Pass --apply to actually move the files.
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

FRAME_RE = re.compile(r"_(\d+)\.(\w+)$")


def _collect(node, out):
    """Walk the esp tree gathering {type: node} for anything with a value/keyframes."""
    if isinstance(node, dict):
        t = node.get("type")
        if t and ("keyframes" in node or isinstance(node.get("value"), dict)):
            out.setdefault(t, node)
        for v in node.values():
            _collect(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect(v, out)


def _values(node):
    """Keyframe values if animated, else the single static value."""
    if node.get("keyframes"):
        return [k["value"] for k in node["keyframes"]]
    return [node["value"]["relative"]]


def read_project(esp_path):
    """Return (latitude, [longitudes]) in degrees."""
    data = json.loads(esp_path.read_text())
    attrs = {}
    _collect(data["scenes"], attrs)
    try:
        lat = _values(attrs["latitude"])[0] * 180 - 90
        lons = [v * 360 - 180 for v in _values(attrs["longitude"])]
    except KeyError as exc:
        raise SystemExit(f"{esp_path}: missing camera attribute {exc}")
    return lat, lons


def discover(root, pattern):
    """Find render folders, each as (dir, latitude, longitudes, [frame files])."""
    found = []
    for d in sorted(root.glob(pattern)):
        if not d.is_dir():
            continue
        esps = list(d.glob("*.esp"))
        footage = d / "footage"
        if not esps or not footage.is_dir():
            continue
        lat, lons = read_project(esps[0])
        frames = {}
        for f in footage.iterdir():
            m = FRAME_RE.search(f.name)
            if m:
                frames[int(m.group(1))] = f
        if frames:
            found.append((d, lat, lons, frames))
    return found


def build_plan(found, out_dir):
    """Map every frame to its grid slot. Returns (plan, rows) or exits on a problem."""
    if not found:
        raise SystemExit("No render folders found. Run this from the repo root.")

    # Rows: sort by latitude descending so y=0 is the northernmost row.
    lats = sorted({round(lat, 6) for _, lat, _, _ in found}, reverse=True)
    if len(lats) != len(found):
        raise SystemExit(
            f"{len(found)} folders but only {len(lats)} distinct latitudes — "
            "two renders share a row. Resolve before moving."
        )

    widths = {len(fr) for _, _, _, fr in found}
    if len(widths) != 1:
        raise SystemExit(f"Rows have differing frame counts: {sorted(widths)}")
    width = widths.pop()

    plan, rows = [], []
    for d, lat, lons, frames in found:
        y = lats.index(round(lat, 6))
        # Frame index runs in keyframe order; flip if the sweep ran east -> west.
        reverse = len(lons) > 1 and lons[0] > lons[-1]
        idx = sorted(frames)
        if idx != list(range(len(idx))):
            raise SystemExit(f"{d}: frame indices are not contiguous from 0: {idx}")
        for i in idx:
            x = (width - 1 - i) if reverse else i
            plan.append((frames[i], out_dir / f"{x}_{y}{frames[i].suffix}"))
        rows.append((y, d, lat, lons[0], lons[-1], reverse))

    dests = [dst for _, dst in plan]
    if len(set(dests)) != len(dests):
        raise SystemExit("Two source frames map to the same destination name.")

    return plan, sorted(rows), width, len(lats)


def check_regularity(rows, tol=1e-5):
    """Warnings about a grid that isn't the regular lattice the stitcher assumes.

    Distinct latitudes are not enough: a row entered as 47.9481 instead of
    47.9841, or a longitude sweep typed one digit short, still yields a valid-
    looking plan but silently misplaces a whole row of frames. Cheap to check
    here, expensive to notice after the stitch.
    """
    warn = []
    lats = [r[2] for r in rows]
    gaps = [round(a - b, 10) for a, b in zip(lats, lats[1:])]
    if gaps:
        want = sorted(gaps)[len(gaps) // 2]
        odd = [(rows[i][0], rows[i + 1][0], g)
               for i, g in enumerate(gaps) if abs(g - want) > tol]
        if odd:
            warn.append(f"latitude spacing is not uniform (most gaps {want:.4f}):")
            warn += [f"    rows {a}->{b} differ by {g:.4f}" for a, b, g in odd]

    for col, name in ((3, "start"), (4, "end")):
        vals = sorted(r[col] for r in rows)
        want = vals[len(vals) // 2]
        odd = [(r[0], r[col]) for r in rows if abs(r[col] - want) > tol]
        if odd:
            warn.append(f"longitude {name} differs between rows (most are {want:.4f}):")
            warn += [f"    row {y}: {v:.4f}" for y, v in odd]
    return warn


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=Path("."),
                    help="Repo root containing the render folders (default: .)")
    ap.add_argument("--pattern", default="bratislava2*",
                    help="Glob for render folders (default: bratislava2*)")
    ap.add_argument("--out", type=Path, default=None,
                    help="Destination folder (default: <root>/frames)")
    ap.add_argument("--copy", action="store_true",
                    help="Copy instead of move, leaving the originals in place")
    ap.add_argument("--apply", action="store_true",
                    help="Actually perform the operation (default is a dry run)")
    args = ap.parse_args()

    root = args.root.resolve()
    out_dir = (args.out or root / "frames").resolve()

    found = discover(root, args.pattern)
    plan, rows, width, height = build_plan(found, out_dir)

    print(f"{height} rows x {width} columns = {len(plan)} frames -> {out_dir}\n")
    print(f"{'y':>2}  {'latitude':>9}  {'longitude sweep':>22}  folder")
    for y, d, lat, lon0, lon1, reverse in rows:
        arrow = "<-" if reverse else "->"
        print(f"{y:>2}  {lat:>9.4f}  {lon0:>9.4f} {arrow} {lon1:<9.4f}  {d.name}")

    warnings = check_regularity(rows)
    if warnings:
        print("\n!! grid is not a regular lattice:")
        for w in warnings:
            print(f"  {w}")
        print("  The stitcher assumes uniform spacing — check these before applying.")
    else:
        print(f"\ngrid is a regular lattice: {height} rows evenly spaced, "
              "all sharing one longitude sweep")

    collisions = [dst for _, dst in plan if dst.exists()]
    if collisions:
        raise SystemExit(
            f"\n{len(collisions)} destination file(s) already exist, e.g. "
            f"{collisions[0].name}. Remove {out_dir} or pick another --out."
        )

    verb = "copy" if args.copy else "move"
    if not args.apply:
        print(f"\nDry run — nothing written. Re-run with --apply to {verb}.")
        print("Sample:", ", ".join(f"{s.parent.parent.name}/{s.name} -> {d.name}"
                                   for s, d in plan[:2]))
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    op = shutil.copy2 if args.copy else shutil.move
    for src, dst in plan:
        op(str(src), str(dst))
    print(f"\n{verb.capitalize()}d {len(plan)} frames into {out_dir}")

    if not args.copy:
        print("Source folders left in place (the .esp files are worth keeping).")


if __name__ == "__main__":
    sys.exit(main())
