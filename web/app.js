/* Zoom/pan viewer for the Bratislava mosaic.
 *
 * The pyramid built by scripts/pyramid.py is a set of levels, each half the
 * resolution of the one above, cut into square tiles. The viewer only ever
 * holds tiles that are (a) at roughly screen resolution and (b) on screen,
 * which is what keeps a 148,240 x 64,528 image — 9.6 gigapixels, ten levels,
 * 49,000 tiles — usable: the working set is a few dozen tiles regardless of
 * how large the mosaic grows.
 *
 * Three ideas do most of the work:
 *   - Coarse-to-fine painting. Every frame draws each level from 0 up to the
 *     target, so a blurry ancestor covers any tile that has not arrived yet
 *     and there are never holes or flashes.
 *   - Explicit unloading. Tiles are decoded to ImageBitmaps, which can be
 *     close()d to release their memory immediately rather than waiting for the
 *     GC to notice; least-recently-drawn tiles are dropped past a budget.
 *   - In-flight cancellation. Pan quickly and the tiles you flew past are
 *     aborted mid-request instead of competing with the ones you can see.
 */
(() => {
  'use strict';

  const INFO = window.MOSAIC_INFO;
  if (!INFO) {
    document.body.innerHTML =
      '<p style="padding:2rem">No tiles found. Run <code>python scripts/pyramid.py</code> first.</p>';
    return;
  }

  const T = INFO.tileSize;
  const TOP = INFO.maxLevel;
  const W = INFO.width;
  const H = INFO.height;
  const LEVELS = INFO.levels;
  const BASE = 'tiles/';

  // Layers are alternate renderings of one geometry — the raw photography, the
  // AI re-render, and the 8-bit reductions of it — so they share every level,
  // tile index and transform, and differ only in which directory a tile is
  // fetched from. A layer marked `pixel` is drawn without smoothing.
  const LAYERS = INFO.layers || [{ id: '', label: 'Raw' }];
  let layer = 0;

  // Tuning. A 512px tile costs ~1 MB decoded, so the cache ceiling is roughly
  // MAX_TILES megabytes of GPU/CPU memory.
  const MAX_TILES = 160;
  const MAX_INFLIGHT = 6;
  const MARGIN = 1;        // tiles fetched beyond the viewport edge

  // Levels 0..PINNED are fetched up front and never evicted, so there is
  // always *something* to draw under a tile that has not arrived. Derived from
  // the pyramid rather than fixed at 1: a deep pyramid puts many levels
  // between the overview and the target, and with only level 0 pinned the
  // fallback while panning at full zoom is a 290px-wide thumbnail of the whole
  // city. Pinning down to the last level that is still only a handful of tiles
  // costs 16 tiles here and keeps the fallback within a few levels of what is
  // wanted.
  const PINNED = (() => {
    let z = 0;
    while (z < TOP && LEVELS[z + 1].cols * LEVELS[z + 1].rows <= 24) z++;
    return z;
  })();
  const FRICTION = 0.92;
  const MIN_VELOCITY = 0.04;

  // --- export grid -------------------------------------------------------
  // Squares handed to a diffusion model. Each overlaps its neighbours by
  // EXPORT_OVERLAP on every side, so a re-render can be outpainted from the
  // strip it shares with the square already done; the grid therefore advances
  // by the stride, not by the square. The last column and row are pulled back
  // flush with the image edge instead of hanging over it, which only ever
  // gives them *more* overlap than the rest.

  const EXPORT_SIZE = 1024;
  const EXPORT_OVERLAP = 128;
  const STRIDE = EXPORT_SIZE - EXPORT_OVERLAP;

  const spanCount = (extent) =>
    extent <= EXPORT_SIZE ? 1 : Math.ceil((extent - EXPORT_SIZE) / STRIDE) + 1;

  const GRID_COLS = spanCount(W);
  const GRID_ROWS = spanCount(H);

  const cellX = (col) => Math.max(0, Math.min(col * STRIDE, W - EXPORT_SIZE));
  const cellY = (row) => Math.max(0, Math.min(row * STRIDE, H - EXPORT_SIZE));

  // The filename a cell leaves under, and comes back under. It carries the
  // cell's position twice — as grid coordinates and as pixel origin — so a
  // re-render can be placed again from its name alone.
  const cellName = (col, row, ext) => [
    LAYERS[layer].id || 'tile',
    'c' + String(col).padStart(3, '0'),
    'r' + String(row).padStart(3, '0'),
    'x' + String(cellX(col)).padStart(6, '0'),
    'y' + String(cellY(row)).padStart(6, '0'),
  ].join('_') + '.' + ext;

  const cellKey = (col, row) => col + ',' + row;

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  let gridOn = false;
  let hover = null;          // {col, row} under the cursor, or null
  let exporting = false;

  // --- canvas ------------------------------------------------------------

  const canvas = document.getElementById('view');
  const ctx = canvas.getContext('2d', { alpha: false });
  let cw = 0, ch = 0, dpr = 1;

  function resize() {
    // Capping DPR at 2 halves the fill cost on 3x phones for no visible loss.
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    cw = canvas.clientWidth;
    ch = canvas.clientHeight;
    canvas.width = Math.round(cw * dpr);
    canvas.height = Math.round(ch * dpr);
    clampView();
    invalidate();
  }

  // --- view transform ----------------------------------------------------
  // screen = image * scale + t, in CSS pixels.

  let scale = 1, tx = 0, ty = 0;

  const fitScale = () => Math.min(cw / W, ch / H);

  // Zoom stops where one image pixel covers one device pixel: 50% on a 2x
  // screen. Past that the viewer is only enlarging pixels it already has, and
  // an 8-bit layer in particular has nothing further to show. `?zoom=N` lifts
  // the ceiling to N times that, for inspecting the tiles themselves.
  const ZOOM = (() => {
    const v = Number(new URLSearchParams(location.search).get('zoom'));
    return Number.isFinite(v) && v > 0 ? v : 1;
  })();
  const maxScale = () => Math.max(ZOOM / dpr, fitScale());

  function clampView() {
    scale = Math.min(Math.max(scale, fitScale()), maxScale());
    const iw = W * scale, ih = H * scale;
    tx = iw <= cw ? (cw - iw) / 2 : Math.min(0, Math.max(cw - iw, tx));
    ty = ih <= ch ? (ch - ih) / 2 : Math.min(0, Math.max(ch - ih, ty));
  }

  /** Zoom towards `target`, holding the image point under (ax, ay) still. */
  function zoomTo(target, ax, ay) {
    const s = Math.min(Math.max(target, fitScale()), maxScale());
    const k = s / scale;
    tx = ax - (ax - tx) * k;
    ty = ay - (ay - ty) * k;
    scale = s;
    clampView();
    invalidate();
  }

  /** Finest level that is still at or above screen resolution. */
  function pickLevel() {
    const device = scale * dpr;             // device px per full-res image px
    const z = TOP + Math.ceil(Math.log2(device) - 1e-6);
    return Math.max(0, Math.min(TOP, z));
  }

  /** Tile index range covering the viewport at `z`, grown by `margin`. */
  function tileRange(z, margin) {
    const ls = Math.pow(2, z - TOP);        // level px per full-res px
    const lv = LEVELS[z];
    const ix0 = Math.max(0, -tx / scale), ix1 = Math.min(W, (cw - tx) / scale);
    const iy0 = Math.max(0, -ty / scale), iy1 = Math.min(H, (ch - ty) / scale);
    if (ix1 <= ix0 || iy1 <= iy0) return null;
    return {
      x0: Math.max(0, Math.floor(ix0 * ls / T) - margin),
      x1: Math.min(lv.cols - 1, Math.floor((ix1 * ls - 0.5) / T) + margin),
      y0: Math.max(0, Math.floor(iy0 * ls / T) - margin),
      y1: Math.min(lv.rows - 1, Math.floor((iy1 * ls - 0.5) / T) + margin),
    };
  }

  // --- tile cache --------------------------------------------------------

  const tiles = new Map();
  let frame = 0;
  let inflight = 0;
  let queue = [];

  // Keyed by layer too, so both renderings can sit in the cache at once and a
  // toggle back does not have to re-fetch what was just on screen.
  const key = (l, z, x, y) => l + '/' + z + '/' + x + '/' + y;

  function tile(l, z, x, y) {
    const k = key(l, z, x, y);
    let t = tiles.get(k);
    if (!t) {
      t = { k, l, z, x, y, state: 'idle', img: null, abort: null };
      tiles.set(k, t);
    }
    return t;
  }

  function release(t) {
    if (t.abort) t.abort();
    if (t.img && t.img.close) t.img.close();   // free decoded pixels now
    t.img = null;
    t.abort = null;
    t.dead = true;
  }

  /** Drop least-recently-drawn tiles once past the budget. */
  function sweep() {
    if (tiles.size <= MAX_TILES) return;
    const victims = [];
    for (const t of tiles.values()) {
      if (t.z <= PINNED || t.last === frame) continue;  // pinned or in use
      victims.push(t);
    }
    victims.sort((a, b) => (a.last || 0) - (b.last || 0));
    let over = tiles.size - MAX_TILES;
    for (const t of victims) {
      if (over-- <= 0) break;
      release(t);
      tiles.delete(t.k);
    }
  }

  // --- loading -----------------------------------------------------------

  // fetch() cannot read file:// URLs, so fall back to <img> when the page is
  // opened straight off disk. ImageBitmap is preferred: it decodes off the
  // main thread and can be freed explicitly.
  const useFetch =
    location.protocol !== 'file:' && typeof createImageBitmap === 'function';

  function load(t) {
    t.state = 'loading';
    inflight++;
    const url = BASE + LAYERS[t.l].id + '/' + t.z + '/'
              + t.x + '_' + t.y + '.' + INFO.ext;

    const done = (img) => {
      inflight--;
      if (t.dead) {
        if (img && img.close) img.close();
      } else {
        t.img = img;
        t.state = img ? 'ready' : 'error';
      }
      pump();
      invalidate();
    };

    if (useFetch) {
      const ac = new AbortController();
      t.abort = () => ac.abort();
      fetch(url, { signal: ac.signal })
        .then((r) => { if (!r.ok) throw new Error(r.status); return r.blob(); })
        .then(createImageBitmap)
        .then(done, () => done(null));
    } else {
      const img = new Image();
      t.abort = () => { img.src = ''; };      // cancels an in-flight request
      img.onload = () => done(img);
      img.onerror = () => done(null);
      img.src = url;
    }
  }

  function pump() {
    while (inflight < MAX_INFLIGHT && queue.length) {
      const t = queue.pop();                 // queue is sorted worst-first
      if (t.state === 'idle') load(t);
    }
  }

  /** Queue every missing tile in `range` at level `z`, nearest-first. */
  function request(l, z, range, weight) {
    if (!range) return 0;
    const ls = Math.pow(2, z - TOP);
    const s = scale / ls;
    const mx = cw / 2, my = ch / 2;
    let missing = 0;
    for (let y = range.y0; y <= range.y1; y++) {
      for (let x = range.x0; x <= range.x1; x++) {
        const t = tile(l, z, x, y);
        if (t.state === 'ready') continue;
        t.last = frame;                      // wanted: not an eviction target
        if (t.state !== 'idle') { if (t.state === 'loading') missing++; continue; }
        const dx = tx + (x + 0.5) * T * s - mx;
        const dy = ty + (y + 0.5) * T * s - my;
        t.priority = weight * Math.hypot(dx, dy);
        queue.push(t);
        missing++;
      }
    }
    return missing;
  }

  // --- painting ----------------------------------------------------------

  function paintLevel(l, z, range) {
    if (!range) return;
    const ls = Math.pow(2, z - TOP);
    const s = scale / ls;
    const lv = LEVELS[z];
    for (let y = range.y0; y <= range.y1; y++) {
      for (let x = range.x0; x <= range.x1; x++) {
        const t = tiles.get(key(l, z, x, y));
        if (!t || t.state !== 'ready') continue;
        t.last = frame;
        const lx = x * T, ly = y * T;
        const tw = Math.min(T, lv.w - lx), th = Math.min(T, lv.h - ly);
        // Round both edges with the same expression so neighbouring tiles
        // land on identical boundaries — no hairline seams, no overlap.
        const x0 = Math.round(tx + lx * s), y0 = Math.round(ty + ly * s);
        const x1 = Math.round(tx + (lx + tw) * s);
        const y1 = Math.round(ty + (ly + th) * s);
        ctx.drawImage(t.img, x0, y0, x1 - x0, y1 - y0);
      }
    }
  }

  /** Stride lines, plus the full 1024 square of whichever cell is hovered. */
  function drawGrid() {
    if (!gridOn) return;

    const step = STRIDE * scale;
    const x0 = clamp(tx, 0, cw), x1 = clamp(tx + W * scale, 0, cw);
    const y0 = clamp(ty, 0, ch), y1 = clamp(ty + H * scale, 0, ch);
    if (x1 <= x0 || y1 <= y0) return;

    ctx.save();
    ctx.lineWidth = 1;
    ctx.strokeStyle = 'rgba(96, 200, 255, 0.85)';

    // Only the lines on screen: at fit scale the whole grid is 155 x 68.
    const c0 = clamp(Math.floor((x0 - tx) / scale / STRIDE), 0, GRID_COLS - 1);
    const c1 = clamp(Math.ceil((x1 - tx) / scale / STRIDE), 0, GRID_COLS - 1);
    const r0 = clamp(Math.floor((y0 - ty) / scale / STRIDE), 0, GRID_ROWS - 1);
    const r1 = clamp(Math.ceil((y1 - ty) / scale / STRIDE), 0, GRID_ROWS - 1);

    ctx.beginPath();
    for (let c = c0; c <= c1; c++) {
      const x = Math.round(tx + cellX(c) * scale) + 0.5;
      ctx.moveTo(x, y0); ctx.lineTo(x, y1);
    }
    for (let r = r0; r <= r1; r++) {
      const y = Math.round(ty + cellY(r) * scale) + 0.5;
      ctx.moveTo(x0, y); ctx.lineTo(x1, y);
    }
    ctx.stroke();

    if (step >= 90) {
      ctx.fillStyle = 'rgba(140, 215, 255, 1)';
      ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, monospace';
      ctx.textBaseline = 'top';
      // Light type on aerial photography is otherwise lost over pale roofs.
      ctx.shadowColor = 'rgba(0, 0, 0, 0.8)';
      ctx.shadowBlur = 3;
      for (let r = r0; r <= r1; r++) {
        for (let c = c0; c <= c1; c++) {
          ctx.fillText(c + ',' + r,
                       tx + cellX(c) * scale + 5, ty + cellY(r) * scale + 4);
        }
      }
      ctx.shadowBlur = 0;
    }

    if (hover) {
      const hx = tx + cellX(hover.col) * scale;
      const hy = ty + cellY(hover.row) * scale;
      const s = EXPORT_SIZE * scale;
      const o = EXPORT_OVERLAP * scale;
      // The fill stays faint: it covers the imagery being judged.
      ctx.fillStyle = 'rgba(96, 200, 255, 0.14)';
      ctx.fillRect(hx, hy, s, s);
      ctx.lineWidth = 2;
      ctx.strokeStyle = 'rgba(160, 225, 255, 1)';
      ctx.strokeRect(hx + 1, hy + 1, s - 2, s - 2);
      // Inner rect: what no neighbour also covers. White against the blue
      // border, so the two edges stay tellable apart.
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.9)';
      ctx.strokeRect(hx + o, hy + o, s - 2 * o, s - 2 * o);
    }
    ctx.restore();
  }

  function render() {
    frame++;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = '#0d0f12';
    ctx.fillRect(0, 0, cw, ch);
    // Zoom stops at 1:1, so this only ever minifies — which wants smoothing,
    // being honest downsampling rather than blur. Past 1:1 (only reachable
    // with ?zoom=) an 8-bit layer has to go nearest neighbour instead, or the
    // browser interpolates its pixels back into gradients.
    ctx.imageSmoothingEnabled = !(LAYERS[layer].pixel && scale * dpr > 1);
    ctx.imageSmoothingQuality = moving() ? 'low' : 'high';

    const z = pickLevel();
    const target = tileRange(z, MARGIN);

    // Coarse first: ancestors fill anything the target level is missing. Only
    // ever from the active layer — showing the other one's pixels would leave
    // the user unsure which rendering they are looking at.
    for (let lz = 0; lz < z; lz++) paintLevel(layer, lz, tileRange(lz, 0));
    paintLevel(layer, z, target);

    // Rebuild the queue every frame so tiles scrolled off are simply dropped.
    queue.length = 0;
    let missing = request(layer, z, target, 1);
    if (z > 0 && missing) request(layer, z - 1, tileRange(z - 1, 0), 0.25);
    queue.sort((a, b) => b.priority - a.priority);
    pump();

    drawRedrawn();
    drawGrid();
    placeExportButton();

    sweep();
    spinner.hidden = inflight === 0;
    // Fitting 148,240 px into a window lands near 0.9%, which rounds to a
    // useless "1%" and then sits there through the first few zoom steps.
    const pct = scale * 100;
    zoomLabel.textContent =
      (pct >= 10 ? Math.round(pct) : pct.toFixed(pct >= 1 ? 1 : 2)) + '%';
  }

  // --- frame loop --------------------------------------------------------
  // One rAF drives inertia, zoom tweens and repainting.

  let rafId = 0;
  let vx = 0, vy = 0;
  let zoomAnim = null;
  let dragging = false;
  let wheelUntil = 0;

  const moving = () =>
    dragging || zoomAnim || vx || vy || performance.now() < wheelUntil;

  function invalidate() {
    if (!rafId) rafId = requestAnimationFrame(tick);
  }

  function tick() {
    rafId = 0;
    let more = false;

    if (zoomAnim) {
      const p = Math.min(1, (performance.now() - zoomAnim.t0) / zoomAnim.dur);
      const e = 1 - Math.pow(1 - p, 3);
      zoomTo(Math.exp(zoomAnim.from + (zoomAnim.to - zoomAnim.from) * e),
             zoomAnim.ax, zoomAnim.ay);
      if (p >= 1) zoomAnim = null; else more = true;
    }

    if (vx || vy) {
      if (Math.abs(vx) < MIN_VELOCITY && Math.abs(vy) < MIN_VELOCITY) {
        vx = vy = 0;
      } else {
        const wantX = tx + vx, wantY = ty + vy;
        tx = wantX; ty = wantY;
        vx *= FRICTION; vy *= FRICTION;
        clampView();
        if (tx !== wantX) vx = 0;              // stop dead against an edge
        if (ty !== wantY) vy = 0;
        more = more || vx !== 0 || vy !== 0;
      }
    }

    render();
    if (more) invalidate();
    else scheduleHash();
  }

  function animateZoom(target, ax, ay) {
    const s = Math.min(Math.max(target, fitScale()), maxScale());
    zoomAnim = {
      from: Math.log(scale), to: Math.log(s),
      ax, ay, t0: performance.now(), dur: 220,
    };
    invalidate();
  }

  // --- pointer input -----------------------------------------------------

  const pointers = new Map();
  let pinchDist = 0, pinchX = 0, pinchY = 0;
  let lastX = 0, lastY = 0, lastT = 0;

  const centroid = () => {
    let x = 0, y = 0;
    for (const p of pointers.values()) { x += p.x; y += p.y; }
    return { x: x / pointers.size, y: y / pointers.size };
  };

  const spread = () => {
    const [a, b] = [...pointers.values()];
    return Math.hypot(a.x - b.x, a.y - b.y);
  };

  canvas.addEventListener('pointerdown', (e) => {
    try { canvas.setPointerCapture(e.pointerId); } catch (_) { /* not capturable */ }
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    vx = vy = 0;
    zoomAnim = null;
    dragging = true;
    canvas.classList.add('dragging');
    const c = centroid();
    lastX = c.x; lastY = c.y; lastT = performance.now();
    if (pointers.size === 2) { pinchDist = spread(); pinchX = c.x; pinchY = c.y; }
  });

  canvas.addEventListener('pointermove', (e) => {
    const p = pointers.get(e.pointerId);
    if (!p) return;
    p.x = e.clientX; p.y = e.clientY;

    const c = centroid();
    const dx = c.x - lastX, dy = c.y - lastY;
    tx += dx; ty += dy;

    if (pointers.size === 2) {
      const d = spread();
      if (pinchDist > 0 && d > 0) zoomTo(scale * (d / pinchDist), c.x, c.y);
      pinchDist = d;
    }

    const now = performance.now();
    const dt = now - lastT;
    if (dt > 0) {
      // Blend towards the instantaneous velocity: smooths out jittery samples
      // without lagging behind a fast flick.
      const k = Math.min(1, dt / 40);
      vx += (dx * (16 / dt) - vx) * k;
      vy += (dy * (16 / dt) - vy) * k;
    }
    lastX = c.x; lastY = c.y; lastT = now;

    clampView();
    invalidate();
  });

  function endPointer(e) {
    if (!pointers.delete(e.pointerId)) return;
    if (pointers.size) {
      const c = centroid();
      lastX = c.x; lastY = c.y;
      pinchDist = pointers.size === 2 ? spread() : 0;
      return;
    }
    dragging = false;
    canvas.classList.remove('dragging');
    // Only coast if the pointer was still moving when it lifted.
    if (performance.now() - lastT > 80) vx = vy = 0;
    // The pan moved the grid under a stationary cursor; without this the
    // button comes back on the cell that used to be there.
    if (gridOn) setHover(cellAt(lastX, lastY));
    invalidate();
  }

  canvas.addEventListener('pointerup', endPointer);
  canvas.addEventListener('pointercancel', endPointer);

  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    let d = e.deltaY;
    if (e.deltaMode === 1) d *= 16;
    else if (e.deltaMode === 2) d *= ch;
    zoomAnim = null;
    wheelUntil = performance.now() + 150;
    zoomTo(scale * Math.exp(-d * 0.0022), e.clientX, e.clientY);
    if (gridOn) setHover(cellAt(e.clientX, e.clientY));
  }, { passive: false });

  canvas.addEventListener('dblclick', (e) => {
    e.preventDefault();
    animateZoom(scale * 2, e.clientX, e.clientY);
  });

  window.addEventListener('keydown', (e) => {
    const step = e.shiftKey ? 400 : 120;
    switch (e.key) {
      case 'ArrowLeft':  tx += step; break;
      case 'ArrowRight': tx -= step; break;
      case 'ArrowUp':    ty += step; break;
      case 'ArrowDown':  ty -= step; break;
      case '+': case '=': animateZoom(scale * 2, cw / 2, ch / 2); return;
      case '-': case '_': animateZoom(scale / 2, cw / 2, ch / 2); return;
      case '0': animateZoom(fitScale(), cw / 2, ch / 2); return;
      case '1': animateZoom(1 / dpr, cw / 2, ch / 2); return;   // 1:1 pixels
      case 't': case 'T': setLayer(layer + 1); return;
      case 'g': case 'G': setGrid(!gridOn); return;
      case 'r': case 'R': setRedrawn(!redrawnOn); return;
      case 'p': case 'P': setSidebar(sidebar.hidden); return;
      case 'Escape': if (!sidebar.hidden) setSidebar(false); return;
      default: return;
    }
    e.preventDefault();
    clampView();
    invalidate();
  });

  document.getElementById('zoom-in')
    .addEventListener('click', () => animateZoom(scale * 2, cw / 2, ch / 2));
  document.getElementById('zoom-out')
    .addEventListener('click', () => animateZoom(scale / 2, cw / 2, ch / 2));
  document.getElementById('zoom-fit')
    .addEventListener('click', () => animateZoom(fitScale(), cw / 2, ch / 2));

  // --- layer switcher ----------------------------------------------------

  const layerBox = document.getElementById('layers');
  const layerBtns = LAYERS.map((l, i) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = l.label;
    b.title = 'Show ' + l.label + ' (T cycles)';
    b.addEventListener('click', () => setLayer(i));
    layerBox.appendChild(b);
    return b;
  });

  function setLayer(next) {
    layer = ((next % LAYERS.length) + LAYERS.length) % LAYERS.length;
    layerBtns.forEach((b, i) => b.setAttribute('aria-pressed', i === layer));
    scheduleHash();
    invalidate();
  }

  // A single layer means nothing to switch between.
  if (LAYERS.length < 2) layerBox.hidden = true;

  // --- export grid UI ----------------------------------------------------

  const gridBtn = document.getElementById('grid-toggle');
  const exportBtn = document.getElementById('export-cell');

  // Below this a 1024 square is too small to aim at, and the button would
  // cover the cell it belongs to. Stated as a zoom level rather than a cell
  // size on screen so it matches the figure in the HUD.
  const HOVER_MIN_SCALE = 0.15;

  function setGrid(on) {
    gridOn = on;
    gridBtn.setAttribute('aria-pressed', String(on));
    if (!on) hover = null;
    invalidate();
  }

  gridBtn.addEventListener('click', () => setGrid(!gridOn));

  /** Cell under a screen point, or null. */
  function cellAt(sx, sy) {
    const ix = (sx - tx) / scale, iy = (sy - ty) / scale;
    if (ix < 0 || iy < 0 || ix >= W || iy >= H) return null;
    return {
      col: clamp(Math.floor(ix / STRIDE), 0, GRID_COLS - 1),
      row: clamp(Math.floor(iy / STRIDE), 0, GRID_ROWS - 1),
    };
  }

  function setHover(next) {
    if (!!next === !!hover &&
        (!next || (next.col === hover.col && next.row === hover.row))) return;
    hover = next;
    invalidate();
  }

  function placeExportButton() {
    const show = gridOn && hover && !dragging && scale >= HOVER_MIN_SCALE;
    if (!show) { exportBtn.hidden = true; return; }
    const cx = tx + (cellX(hover.col) + EXPORT_SIZE / 2) * scale;
    const cy = ty + (cellY(hover.row) + EXPORT_SIZE / 2) * scale;
    exportBtn.style.left = cx + 'px';
    exportBtn.style.top = cy + 'px';
    if (!exporting) exportBtn.textContent = '⬇ ' + hover.col + ',' + hover.row;
    exportBtn.hidden = false;
  }

  canvas.addEventListener('pointermove', (e) => {
    if (gridOn && !dragging) setHover(cellAt(e.clientX, e.clientY));
  });

  // Moving onto the button is still inside its own cell, so the hover must
  // survive the canvas losing the pointer to it.
  canvas.addEventListener('pointerleave', (e) => {
    if (e.relatedTarget !== exportBtn) setHover(null);
  });

  // --- exporting a cell --------------------------------------------------

  function fetchImage(url) {
    if (useFetch) {
      return fetch(url)
        .then((r) => (r.ok ? r.blob() : null))
        .then((b) => (b ? createImageBitmap(b) : null))
        .catch(() => null);
    }
    return new Promise((res) => {
      const img = new Image();
      img.onload = () => res(img);
      img.onerror = () => res(null);
      img.src = url;
    });
  }

  /** Compose the full-resolution 1024 square at (col, row) and download it. */
  async function exportCell(col, row) {
    exporting = true;
    exportBtn.disabled = true;
    exportBtn.textContent = 'Rendering…';

    const ox = cellX(col), oy = cellY(row);
    const out = document.createElement('canvas');
    out.width = out.height = EXPORT_SIZE;
    const g = out.getContext('2d');
    g.imageSmoothingEnabled = false;
    g.fillStyle = '#000';
    g.fillRect(0, 0, EXPORT_SIZE, EXPORT_SIZE);

    // Straight from the level-TOP tiles at 1:1 — never from the view canvas,
    // which holds whatever level the current zoom happens to be showing.
    const lv = LEVELS[TOP];
    const tx0 = Math.floor(ox / T), tx1 = Math.floor((ox + EXPORT_SIZE - 1) / T);
    const ty0 = Math.floor(oy / T), ty1 = Math.floor((oy + EXPORT_SIZE - 1) / T);
    const id = LAYERS[layer].id;

    const jobs = [];
    for (let y = ty0; y <= ty1; y++) {
      for (let x = tx0; x <= tx1; x++) {
        if (x >= lv.cols || y >= lv.rows) continue;
        const url = BASE + id + '/' + TOP + '/' + x + '_' + y + '.' + INFO.ext;
        jobs.push(fetchImage(url).then((img) => ({ img, x, y })));
      }
    }

    let missing = 0;
    for (const { img, x, y } of await Promise.all(jobs)) {
      if (!img) { missing++; continue; }
      g.drawImage(img, x * T - ox, y * T - oy);
      if (img.close) img.close();
    }

    const name = cellName(col, row, 'png');

    try {
      const blob = await new Promise((res, rej) => {
        try { out.toBlob((b) => (b ? res(b) : rej(new Error('encode failed'))),
                         'image/png'); }
        catch (err) { rej(err); }
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = name;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      exportBtn.textContent = missing ? 'Saved (' + missing + ' gaps)' : 'Saved';
    } catch (err) {
      // Canvas reads are blocked for file:// images in most browsers.
      exportBtn.textContent = 'Failed — serve over http';
      console.error(err);
    }

    setTimeout(() => {
      exporting = false;
      exportBtn.disabled = false;
      invalidate();
    }, 900);
  }

  exportBtn.addEventListener('click', () => {
    if (!exporting && hover) exportCell(hover.col, hover.row);
  });

  // --- redrawn cells -----------------------------------------------------
  // The way back in: a 1024 square dropped onto its cell is PUT to
  // scripts/serve.py, which files it in redrawn-cells/ under the very name it
  // was exported with. They are drawn as their own layer over whichever
  // rendering is showing, rather than as one of the pyramid layers, because
  // they arrive one at a time and there is no pyramid of them — this is the
  // work in progress, not a finished rendering. Once enough cells are in,
  // reassemble.py turns them into tiles and pyramid.py makes them a real layer.
  //
  // Cells overlap by EXPORT_OVERLAP, so a later one paints over the edge of
  // its neighbour. Row-major order makes that deterministic: right over left,
  // below over above, the same order a re-render outpaints in.

  const REDRAWN = 'redrawn-cells/';
  const MAX_REDRAWN_IMGS = 48;        // ~4 MB each decoded at full size

  let redrawnOn = false;
  let redrawnIndex = null;            // "col,row" -> {name, v}; null = no server
  const redrawnImgs = new Map();
  let dropCell = null;                // cell a drag is currently over

  const redrawnBtn = document.getElementById('redrawn-toggle');
  const note = document.getElementById('import-note');
  const dialog = document.getElementById('replace-dialog');
  const dialogText = document.getElementById('replace-text');

  let noteTimer = 0;
  function say(text, hold) {
    clearTimeout(noteTimer);
    if (!text) { note.hidden = true; return; }
    note.textContent = text;
    note.hidden = false;
    if (hold) noteTimer = setTimeout(() => { note.hidden = true; }, hold);
  }

  /** Ask the server what has been redrawn. A static server just 404s. */
  function loadRedrawnIndex() {
    if (!useFetch) return Promise.resolve();
    return fetch(REDRAWN + 'index.json')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))))
      .then((data) => {
        redrawnIndex = new Map();
        for (const [k, name] of Object.entries(data.cells || {})) {
          redrawnIndex.set(k, { name, v: 0 });
        }
        redrawnBtn.hidden = false;
        if (redrawnIndex.size) setRedrawn(true);
        invalidate();
      })
      .catch(() => {
        // Served statically, or off disk: the overlay has nothing to read and
        // a drop has nowhere to go. Both stay hidden rather than failing later.
        redrawnIndex = null;
      });
  }

  function setRedrawn(on) {
    redrawnOn = on && !!redrawnIndex;
    redrawnBtn.setAttribute('aria-pressed', String(redrawnOn));
    invalidate();
  }

  redrawnBtn.addEventListener('click', () => setRedrawn(!redrawnOn));

  function redrawnImage(col, row, entry) {
    const k = cellKey(col, row);
    let e = redrawnImgs.get(k);
    if (e && e.name === entry.name && e.v === entry.v) return e;
    if (e) { if (e.img && e.img.close) e.img.close(); redrawnImgs.delete(k); }
    e = { name: entry.name, v: entry.v, img: null, last: frame };
    redrawnImgs.set(k, e);
    // `v` busts the cache after a replace; without it the browser would keep
    // showing the cell that was just overwritten.
    const url = REDRAWN + encodeURIComponent(entry.name)
              + (entry.v ? '?v=' + entry.v : '');
    fetchImage(url).then((img) => {
      if (redrawnImgs.get(k) !== e) {          // superseded while loading
        if (img && img.close) img.close();
        return;
      }
      e.img = img;
      invalidate();
    });
    return e;
  }

  /** Cell index range covering the viewport, grown by one on each side. */
  function cellRange() {
    const ix0 = Math.max(0, -tx / scale), ix1 = Math.min(W, (cw - tx) / scale);
    const iy0 = Math.max(0, -ty / scale), iy1 = Math.min(H, (ch - ty) / scale);
    if (ix1 <= ix0 || iy1 <= iy0) return null;
    return {
      c0: clamp(Math.ceil((ix0 - EXPORT_SIZE) / STRIDE) - 1, 0, GRID_COLS - 1),
      c1: clamp(Math.floor(ix1 / STRIDE) + 1, 0, GRID_COLS - 1),
      r0: clamp(Math.ceil((iy0 - EXPORT_SIZE) / STRIDE) - 1, 0, GRID_ROWS - 1),
      r1: clamp(Math.floor(iy1 / STRIDE) + 1, 0, GRID_ROWS - 1),
    };
  }

  function drawRedrawn() {
    if (!redrawnOn || !redrawnIndex || !redrawnIndex.size) return;
    const r = cellRange();
    if (!r) return;

    for (let row = r.r0; row <= r.r1; row++) {
      for (let col = r.c0; col <= r.c1; col++) {
        const entry = redrawnIndex.get(cellKey(col, row));
        if (!entry) continue;
        const e = redrawnImage(col, row, entry);
        e.last = frame;
        if (!e.img) continue;
        // Rounded the same way tiles are, so two adjacent cells cannot leave
        // a hairline of the layer underneath between them.
        const x0 = Math.round(tx + cellX(col) * scale);
        const y0 = Math.round(ty + cellY(row) * scale);
        const x1 = Math.round(tx + (cellX(col) + EXPORT_SIZE) * scale);
        const y1 = Math.round(ty + (cellY(row) + EXPORT_SIZE) * scale);
        ctx.drawImage(e.img, x0, y0, x1 - x0, y1 - y0);
      }
    }

    if (redrawnImgs.size > MAX_REDRAWN_IMGS) {
      const victims = [...redrawnImgs.entries()]
        .filter(([, e]) => e.last !== frame)
        .sort((a, b) => (a[1].last || 0) - (b[1].last || 0));
      let over = redrawnImgs.size - MAX_REDRAWN_IMGS;
      for (const [k, e] of victims) {
        if (over-- <= 0) break;
        if (e.img && e.img.close) e.img.close();
        redrawnImgs.delete(k);
      }
    }
  }

  // --- importing a cell --------------------------------------------------

  const canImport = () => gridOn && !!redrawnIndex;

  const EXT = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp' };

  function extOf(file) {
    if (EXT[file.type]) return EXT[file.type];
    const m = /\.(png|jpe?g|webp)$/i.exec(file.name || '');
    return m ? m[1].toLowerCase().replace('jpeg', 'jpg') : null;
  }

  /** Measure the drop, so an obviously wrong file is caught before it lands. */
  function measure(file) {
    if (!useFetch) return Promise.resolve(null);
    return createImageBitmap(file).then((b) => {
      const size = { w: b.width, h: b.height };
      if (b.close) b.close();
      return size;
    }, () => null);
  }

  function askReplace(col, row, name) {
    dialogText.textContent =
      'Cell ' + col + ',' + row + ' already holds ' + name
      + '. Replace it with the file you dropped?';
    if (!dialog.showModal) {                  // no <dialog>: fall back
      return Promise.resolve(window.confirm(dialogText.textContent));
    }
    dialog.showModal();
    return new Promise((res) => {
      dialog.addEventListener(
        'close', () => res(dialog.returnValue === 'replace'), { once: true });
    });
  }

  async function importCell(col, row, file) {
    const ext = extOf(file);
    if (!ext) { say('Not an image — drop a PNG of the cell.', 4000); return; }

    const size = await measure(file);
    if (size && size.w !== size.h) {
      say('Cell images must be square — that one is '
          + size.w + '×' + size.h + '.', 5000);
      return;
    }

    const k = cellKey(col, row);
    const existing = redrawnIndex.get(k);
    if (existing && !(await askReplace(col, row, existing.name))) {
      say('Kept ' + existing.name, 2500);
      return;
    }

    const name = cellName(col, row, ext);
    say('Saving ' + name + '…');
    try {
      const r = await fetch(
        REDRAWN + encodeURIComponent(name) + (existing ? '?replace=1' : ''),
        { method: 'PUT', body: file });
      if (!r.ok) {
        const detail = await r.json().catch(() => ({}));
        throw new Error(detail.error || r.status);
      }
      const data = await r.json();
      redrawnIndex.set(k, {
        name: data.name,
        v: existing ? (existing.v || 0) + 1 : 0,
      });
      setRedrawn(true);                    // no point saving it invisibly
      say((existing ? 'Replaced ' : 'Saved ') + data.name
          + (size && size.w !== EXPORT_SIZE
             ? ' (' + size.w + 'px, not ' + EXPORT_SIZE + ')' : ''), 4000);
    } catch (err) {
      say('Could not save: ' + err.message, 6000);
      console.error(err);
    }
  }

  // Only files, and only over the canvas: a drag that started as a text
  // selection elsewhere in the UI should not arm the grid.
  const hasFile = (e) =>
    e.dataTransfer && [...e.dataTransfer.types].includes('Files');

  window.addEventListener('dragover', (e) => {
    if (!hasFile(e)) return;
    e.preventDefault();
    if (!canImport()) {
      e.dataTransfer.dropEffect = 'none';
      say(redrawnIndex ? 'Turn the grid on (G) to import a cell.'
                       : 'Importing needs ./scripts/serve.py, not a static server.');
      return;
    }
    e.dataTransfer.dropEffect = 'copy';
    dropCell = cellAt(e.clientX, e.clientY);
    setHover(dropCell);
    say(dropCell ? 'Drop into cell ' + dropCell.col + ',' + dropCell.row
                 : 'Outside the mosaic');
  });

  window.addEventListener('dragleave', (e) => {
    // Fires for every child element the drag crosses; only the one that leaves
    // the window means the drag is really gone.
    if (e.relatedTarget) return;
    dropCell = null;
    say(null);
  });

  window.addEventListener('drop', (e) => {
    if (!hasFile(e)) return;
    e.preventDefault();
    const cell = cellAt(e.clientX, e.clientY);
    dropCell = null;
    if (!canImport()) {
      say(redrawnIndex ? 'Turn the grid on (G) to import a cell.'
                       : 'Importing needs ./scripts/serve.py, not a static server.',
          5000);
      return;
    }
    if (!cell) { say('Dropped outside the mosaic.', 3000); return; }
    const file = e.dataTransfer.files[0];
    if (!file) { say('Nothing to import.', 3000); return; }
    if (e.dataTransfer.files.length > 1) {
      // One drop is one cell; guessing which of five files it meant would be
      // worse than saying so.
      say('Drop one file at a time — a drop names a single cell.', 5000);
      return;
    }
    importCell(cell.col, cell.row, file);
  });

  // --- prompt builder ----------------------------------------------------
  // The sidebar reads the project's own prompt.json — the file at the repo
  // root, routed here by serve.py — so it cannot drift from the text actually
  // being sent. Fetched rather than inlined: it is edited far more often than
  // this viewer is, and by hand.

  const sidebar = document.getElementById('sidebar');
  const promptBtn = document.getElementById('prompt-toggle');
  const addonBox = document.getElementById('prompt-addons');
  const preview = document.getElementById('prompt-preview');
  const copyBtn = document.getElementById('prompt-copy');

  let PROMPT = null;
  const chosen = new Set();

  // Addons go in as further bullets of the base prompt's Style list, at the
  // slot marker — a model follows one list of rules more reliably than a list
  // plus a postscript contradicting it. With nothing ticked the marker's whole
  // line goes, so the base is byte-for-byte the prompt in the file.
  function composePrompt() {
    if (!PROMPT) return '';
    const slot = PROMPT.slot || '{{addons}}';
    const bullet = PROMPT.bullet || '- ';
    const picked = (PROMPT.addons || []).filter((a) => chosen.has(a.id));
    const lines = picked.map((a) => bullet + a.text).join('\n');
    if (!PROMPT.base.includes(slot)) {
      return lines ? PROMPT.base + '\n' + lines : PROMPT.base;
    }
    return lines ? PROMPT.base.replace(slot, lines)
                 : PROMPT.base.replace(slot + '\n', '');
  }

  function refreshPrompt() {
    preview.textContent = composePrompt();
  }

  function buildAddons() {
    for (const a of PROMPT.addons || []) {
      const label = document.createElement('label');
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.value = a.id;
      box.addEventListener('change', () => {
        if (box.checked) chosen.add(a.id); else chosen.delete(a.id);
        refreshPrompt();
      });
      const text = document.createElement('span');
      text.textContent = a.label;
      if (a.description) {
        const hint = document.createElement('span');
        hint.className = 'hint';
        hint.textContent = a.description;
        text.appendChild(hint);
      }
      label.append(box, text);
      addonBox.appendChild(label);
    }
    refreshPrompt();
  }

  function loadPrompt() {
    if (PROMPT) return Promise.resolve();
    return fetch('prompt.json')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))))
      .then((data) => { PROMPT = data; buildAddons(); })
      .catch(() => {
        // Either the page is off disk, where fetch() cannot read file:// URLs,
        // or it is on a plain static server, which knows nothing of a file
        // above web/. Both are answered by the one command.
        preview.textContent =
          'Could not load prompt.json — serve this page with ./scripts/serve.py.';
      });
  }

  function setSidebar(open) {
    sidebar.hidden = !open;
    promptBtn.setAttribute('aria-pressed', String(open));
    if (open) loadPrompt();
  }

  promptBtn.addEventListener('click', () => setSidebar(sidebar.hidden));
  document.getElementById('sidebar-close')
    .addEventListener('click', () => setSidebar(false));

  copyBtn.addEventListener('click', async () => {
    const text = composePrompt();
    if (!text) return;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        // http://<lan-ip> is not a secure context, and that is how this viewer
        // usually gets opened from another machine.
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand('copy');
        ta.remove();
        if (!ok) throw new Error('execCommand refused');
      }
      copyBtn.textContent = 'Copied';
    } catch (err) {
      copyBtn.textContent = 'Copy failed — select the text';
      console.error(err);
    }
    setTimeout(() => { copyBtn.textContent = 'Copy prompt'; }, 1400);
  });

  // --- shareable position in the URL -------------------------------------

  let hashTimer = 0;
  function scheduleHash() {
    clearTimeout(hashTimer);
    hashTimer = setTimeout(() => {
      const cx = Math.round((cw / 2 - tx) / scale);
      const cy = Math.round((ch / 2 - ty) / scale);
      const h = `#${cx},${cy},${scale.toPrecision(4)},${LAYERS[layer].id}`;
      if (h !== location.hash) history.replaceState(null, '', h);
    }, 400);
  }

  function applyHash() {
    const m = location.hash.match(
      /^#(-?[\d.]+),(-?[\d.]+),([\d.eE+-]+)(?:,([\w-]*))?$/);
    if (!m) return false;
    const [cx, cy, s] = m.slice(1, 4).map(Number);
    if (!isFinite(cx) || !isFinite(cy) || !isFinite(s) || s <= 0) return false;
    if (m[4] !== undefined) {
      const i = LAYERS.findIndex((v) => v.id === m[4]);
      if (i >= 0) layer = i;
    }
    // Clamp before placing, not after: a hash asking for more zoom than the
    // viewer allows would otherwise position the centre for a scale it is not
    // going to use, and land somewhere else entirely.
    scale = Math.min(Math.max(s, fitScale()), maxScale());
    tx = cw / 2 - cx * scale;
    ty = ch / 2 - cy * scale;
    clampView();
    return true;
  }

  // --- start -------------------------------------------------------------

  const spinner = document.getElementById('spinner');
  const zoomLabel = document.getElementById('zoom-level');

  window.addEventListener('resize', resize);
  // setLayer, not invalidate: a hash carrying a different layer has to move the
  // pressed state too, or the switcher disagrees with what is on screen.
  window.addEventListener('hashchange', () => { if (applyHash()) setLayer(layer); });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) invalidate();
  });

  resize();
  if (!applyHash()) {
    scale = fitScale();
    clampView();
  }
  setLayer(layer);
  loadRedrawnIndex();

  // Pull the pinned overview levels up front, for every layer: 5 small tiles
  // each, which guarantee something is on screen however fast the user moves
  // and make switching layers show up instantly rather than on the network.
  for (let l = 0; l < LAYERS.length; l++) {
    for (let z = 0; z <= PINNED; z++) {
      for (let y = 0; y < LEVELS[z].rows; y++) {
        for (let x = 0; x < LEVELS[z].cols; x++) load(tile(l, z, x, y));
      }
    }
  }
  invalidate();
})();
