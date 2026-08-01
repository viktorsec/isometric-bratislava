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
