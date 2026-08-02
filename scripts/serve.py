#!/usr/bin/env python3
"""Serve the viewer, and take re-rendered cells back from it.

`python -m http.server` is enough to look at the pyramid, but the viewer's
import grid has to put a file on disk, and a static server has nowhere to put
it. This is the same static server, serving `web/`, plus:

    GET  /prompt.json               the prompt, from the repo root
    GET  /redrawn-cells/index.json  what has been redrawn already
    GET  /redrawn-cells/<name>      one redrawn cell, for the overlay layer
    PUT  /redrawn-cells/<name>      store a cell dropped onto the grid

Stdlib only, so it runs without the venv:

    ./scripts/serve.py

Both extra paths point outside `web/` on purpose. `prompt.json` is project
source — it is edited far more often than this viewer and read by more than it —
and redrawn cells are render output, alongside `tiles/` and `subtiles/`. Neither
belongs inside the site just to be reachable from it.

`redrawn-cells/` sits at the repo root rather than under `web/`, next to the
other render outputs — it is source material for `reassemble.py`, not part of
the site. Names are the export grid's own (`<layer>_c<col>_r<row>_x_y.png`), so
a cell exported from the viewer, re-rendered elsewhere and dropped back lands
under the name it left with; the `c`/`r` pair in the name is what identifies
the cell, and the server refuses a PUT onto a cell that already has a file
unless the viewer says `?replace=1` — which it only does once the user has
confirmed it.
"""

import argparse
import json
import os
import re
import sys
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit, parse_qs

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
INBOX = ROOT / "redrawn-cells"

PREFIX = "/redrawn-cells/"
INDEX = PREFIX + "index.json"

# Root files the viewer fetches by name, mapped so the page can keep asking for
# them at the top level whatever directory they actually live in.
ROUTES = {"/prompt.json": ROOT / "prompt.json"}

# A 1024 square is under a megabyte as PNG; the ceiling is only here so a
# runaway or mistaken upload cannot fill the disk.
MAX_BYTES = 64 << 20

# Anything that is one path component of safe characters. Checked before the
# name is joined to a directory, so `..` and separators never get that far.
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CELL_IN_NAME = re.compile(r"_c(\d+)_r(\d+)_")

TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".json": "application/json",
}


def cell_of(name):
    """The (col, row) a filename claims, or None."""
    m = CELL_IN_NAME.search(name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def index_cells():
    """Map "col,row" -> filename for everything currently in the inbox."""
    out = {}
    if not INBOX.is_dir():
        return out
    for p in sorted(INBOX.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        cell = cell_of(p.name)
        if cell:
            out["%d,%d" % cell] = p.name
    return out


class Handler(SimpleHTTPRequestHandler):
    # The viewer pulls dozens of tiles at once; a single-threaded server would
    # serialise them behind whichever one is slowest.
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEB), **kw)

    # --- helpers ---------------------------------------------------------

    def send_json(self, obj, status=HTTPStatus.OK):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def fail(self, status, message):
        self.send_json({"error": message}, status)

    def send_file(self, path):
        if not path.is_file():
            return self.fail(HTTPStatus.NOT_FOUND, "no such file")
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type",
                         TYPES.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def inbox_path(self):
        """The inbox file this request names, or None if the name is unsafe."""
        name = unquote(urlsplit(self.path).path[len(PREFIX):])
        if not SAFE_NAME.match(name) or not cell_of(name):
            return None
        return INBOX / name

    # --- routes ----------------------------------------------------------

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ROUTES:
            return self.send_file(ROUTES[path])
        if path == INDEX:
            return self.send_json({"cells": index_cells()})
        if path.startswith(PREFIX):
            p = self.inbox_path()
            if p is None:
                return self.fail(HTTPStatus.NOT_FOUND, "no such cell")
            return self.send_file(p)
        return super().do_GET()

    def do_HEAD(self):
        path = urlsplit(self.path).path
        if path in ROUTES or path.startswith(PREFIX):
            return self.fail(HTTPStatus.METHOD_NOT_ALLOWED, "HEAD not supported")
        return super().do_HEAD()

    def do_PUT(self):
        parts = urlsplit(self.path)
        if not parts.path.startswith(PREFIX):
            return self.fail(HTTPStatus.NOT_FOUND, "nothing to write there")

        p = self.inbox_path()
        if p is None:
            return self.fail(HTTPStatus.BAD_REQUEST,
                             "name must be an export cell name")

        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self.fail(HTTPStatus.LENGTH_REQUIRED, "Content-Length required")
        if length <= 0 or length > MAX_BYTES:
            return self.fail(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "bad length")

        replace = parse_qs(parts.query).get("replace", ["0"])[0] == "1"
        key = "%d,%d" % cell_of(p.name)
        existing = index_cells().get(key)
        if existing and not replace:
            return self.fail(HTTPStatus.CONFLICT, "cell already redrawn")

        data = self.rfile.read(length)
        if len(data) != length:
            return self.fail(HTTPStatus.BAD_REQUEST, "truncated body")

        INBOX.mkdir(exist_ok=True)
        # Write beside the target and rename, so a dropped connection can never
        # leave a half-written cell for the viewer to pick up.
        tmp = p.with_name("." + p.name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, p)

        # A re-import under a different extension would otherwise leave both
        # files claiming the same cell.
        replaced = None
        if existing and existing != p.name:
            (INBOX / existing).unlink(missing_ok=True)
            replaced = existing

        sys.stderr.write("saved %s -> redrawn-cells/%s\n" % (key, p.name))
        self.send_json({"name": p.name, "cell": key, "replaced": replaced},
                       HTTPStatus.CREATED)

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, HEAD, PUT, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def end_headers(self):
        # The pyramid is regenerated in place; a cached tile from the previous
        # run is worse than a re-fetch.
        if urlsplit(self.path).path.endswith((".js", ".css", ".html", ".json")):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-p", "--port", type=int, default=8000)
    ap.add_argument("--host", default="0.0.0.0",
                    help="default serves the LAN too")
    args = ap.parse_args()

    if not (WEB / "tiles" / "info.js").is_file():
        print("no pyramid in web/tiles — run scripts/pyramid.py first",
              file=sys.stderr)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("serving %s on http://localhost:%d" % (WEB, args.port))
    print("imports land in %s" % INBOX)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
