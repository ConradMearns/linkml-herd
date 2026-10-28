// Infinite canvas: pan, zoom, drag tables, and save the page back out with the layout embedded.
// `edgePath` and `num` mirror render.py so redrawn edges match linkml-herd's output.
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const dataEl = $('herd-data');
  const data = JSON.parse(dataEl.textContent);
  const G = data.geometry;
  const viewport = $('viewport');
  const sizer = $('sizer');
  const world = $('world');
  const svg = $('edges');
  const stateEl = $('save-state');
  const tables = new Map([...world.querySelectorAll('.table')].map((t) => [t.dataset.name, t]));
  const edges = [...svg.querySelectorAll('.edge')];
  const MIN_ZOOM = 0.1, MAX_ZOOM = 2, DOT_GAP = 20;
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  let pinned = Object.keys(data.layout.tables || {}).length > 0;
  let dirty = false;
  let view = { x: 0, y: 0, k: 1 };
  let viewTouched = false; // until the user pans/zooms, keep the content fitted on resize

  // --- geometry / routing (mirrors render.py) ------------------------------
  const num = (v) => String(Math.floor(v * 10 + 0.5) / 10);

  function box(name) {
    const t = tables.get(name);
    const x = parseFloat(t.style.left);
    return { x, y: parseFloat(t.style.top), right: x + G.tableW };
  }

  function rowY(b, index) {
    if (index < 0) return b.y + G.border + G.headerH / 2;
    return b.y + G.border + G.headerH + index * G.rowH + G.rowH / 2;
  }

  function slotIndex(name, slot) {
    const rows = [...tables.get(name).querySelectorAll('.row[data-slot]')];
    return rows.findIndex((r) => r.dataset.slot === slot);
  }

  function edgePath(src, sy, dst, ty, waypoints) {
    if (!waypoints.length && dst.x < src.right && src.x < dst.right) {
      const sx = src.right, tx = dst.right, bulge = G.loop + Math.abs(ty - sy) / 6;
      const d = `M${num(sx)},${num(sy)} C${num(sx + bulge)},${num(sy)} ${num(tx + bulge)},${num(ty)} ${num(tx)},${num(ty)}`;
      return [d, [Math.max(sx, tx) + bulge * 0.75, (sy + ty) / 2]];
    }
    const forward = dst.x > src.x;
    const [sx, tx] = forward ? [src.right, dst.x] : [src.x, dst.right];
    const anchors = [[sx, sy], ...waypoints, [tx, ty]];
    let d = `M${num(sx)},${num(sy)}`;
    for (let j = 1; j < anchors.length; j++) {
      const [ax, ay] = anchors[j - 1], [bx, by] = anchors[j];
      if (j % 2) {
        const mx = (ax + bx) / 2;
        d += ` C${num(mx)},${num(ay)} ${num(mx)},${num(by)} ${num(bx)},${num(by)}`;
      } else {
        d += ` L${num(bx)},${num(by)}`;
      }
    }
    const [ax, ay] = anchors[0], [bx, by] = anchors[1];
    return [d, [(ax + bx) / 2, (ay + by) / 2]];
  }

  function waypointsOf(edge) {
    return edge.dataset.waypoints ? JSON.parse(edge.dataset.waypoints) : [];
  }

  function redraw(edge) {
    const { from, fromSlot, to, toSlot } = edge.dataset;
    const src = box(from), dst = box(to);
    const sy = rowY(src, slotIndex(from, fromSlot));
    const ty = rowY(dst, toSlot ? slotIndex(to, toSlot) : -1);
    const [d, [lx, ly]] = edgePath(src, sy, dst, ty, waypointsOf(edge));
    edge.querySelector('path').setAttribute('d', d);
    const label = edge.querySelector('text');
    label.setAttribute('x', num(lx));
    label.setAttribute('y', num(ly));
  }

  /** Extent of everything drawn, in canvas coordinates. */
  function contentBounds() {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const t of tables.values()) {
      const x = parseFloat(t.style.left), y = parseFloat(t.style.top);
      x0 = Math.min(x0, x), y0 = Math.min(y0, y);
      x1 = Math.max(x1, x + G.tableW), y1 = Math.max(y1, y + t.offsetHeight);
    }
    const b = svg.getBBox();
    if (b.width || b.height) {
      x0 = Math.min(x0, b.x), y0 = Math.min(y0, b.y);
      x1 = Math.max(x1, b.x + b.width), y1 = Math.max(y1, b.y + b.height);
    }
    return Number.isFinite(x0) ? { x0, y0, x1, y1 } : { x0: 0, y0: 0, x1: 0, y1: 0 };
  }

  // --- view: pan / zoom ------------------------------------------------------
  function applyView(touched = true) {
    viewTouched ||= touched;
    world.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.k})`;
    let gap = DOT_GAP * view.k;
    while (gap < 10) gap *= 2; // thin out the dot grid when zoomed far out
    viewport.style.backgroundSize = `${gap}px ${gap}px`;
    viewport.style.backgroundPosition = `${view.x}px ${view.y}px`;
    $('zoom-level').textContent = `${Math.round(view.k * 100)}%`;
  }

  /** Zoom by `factor`, keeping the viewport point (cx, cy) fixed on screen. */
  function zoomAt(cx, cy, factor) {
    const k = clamp(view.k * factor, MIN_ZOOM, MAX_ZOOM);
    const f = k / view.k;
    view = { x: cx - (cx - view.x) * f, y: cy - (cy - view.y) * f, k };
    applyView();
  }

  function zoomCenter(factor) {
    zoomAt(viewport.clientWidth / 2, viewport.clientHeight / 2, factor);
  }

  function fitView() {
    const b = contentBounds();
    const pad = G.pad;
    const w = Math.max(1, b.x1 - b.x0), h = Math.max(1, b.y1 - b.y0);
    const vw = viewport.clientWidth, vh = viewport.clientHeight;
    const k = clamp(Math.min((vw - 2 * pad) / w, (vh - 2 * pad) / h, 1), MIN_ZOOM, MAX_ZOOM);
    view = { k, x: (vw - w * k) / 2 - b.x0 * k, y: (vh - h * k) / 2 - b.y0 * k };
    applyView(false);
  }

  function centerOn(name) {
    const t = tables.get(name);
    if (!t) return;
    const b = box(name);
    view.x = viewport.clientWidth / 2 - (b.x + G.tableW / 2) * view.k;
    view.y = viewport.clientHeight / 2 - (b.y + t.offsetHeight / 2) * view.k;
    applyView();
    t.classList.add('flash');
    setTimeout(() => t.classList.remove('flash'), 1500);
  }

  // Drag the background to pan; two fingers pinch-zoom.
  const pointers = new Map();
  viewport.addEventListener('pointerdown', (e) => {
    if (e.target.closest('.table, .controls, .legend')) return;
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    viewport.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    viewport.classList.add('panning');
  });
  viewport.addEventListener('pointermove', (e) => {
    const p = pointers.get(e.pointerId);
    if (!p) return;
    const dx = e.clientX - p.x, dy = e.clientY - p.y;
    if (pointers.size === 1) {
      view.x += dx;
      view.y += dy;
      applyView();
    } else if (pointers.size === 2) {
      const other = [...pointers].find(([id]) => id !== e.pointerId)[1];
      const before = Math.hypot(p.x - other.x, p.y - other.y);
      const after = Math.hypot(e.clientX - other.x, e.clientY - other.y);
      const r = viewport.getBoundingClientRect();
      view.x += dx / 2;
      view.y += dy / 2;
      zoomAt((e.clientX + other.x) / 2 - r.left, (e.clientY + other.y) / 2 - r.top, before ? after / before : 1);
    }
    p.x = e.clientX;
    p.y = e.clientY;
  });
  const release = (e) => {
    pointers.delete(e.pointerId);
    if (!pointers.size) viewport.classList.remove('panning');
  };
  viewport.addEventListener('pointerup', release);
  viewport.addEventListener('pointercancel', release);

  // Wheel / trackpad pinch zooms around the cursor (like Svelte Flow).
  viewport.addEventListener(
    'wheel',
    (e) => {
      if (e.target.closest('.controls, .legend')) return;
      e.preventDefault();
      const r = viewport.getBoundingClientRect();
      const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? r.height : 1;
      const speed = e.ctrlKey ? 0.01 : 0.002; // pinch gestures arrive as ctrl+wheel
      zoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY * unit * speed));
    },
    { passive: false }
  );

  // The page itself never scrolls; anchor jumps would scroll the hidden overflow.
  viewport.addEventListener('scroll', () => viewport.scrollTo(0, 0));

  // --- dragging tables -----------------------------------------------------------
  function markDirty() {
    dirty = true;
    setState('', 'unsaved changes');
  }

  for (const [name, t] of tables) {
    t.addEventListener('pointerdown', (ev) => {
      if (ev.button !== 0) return;
      ev.preventDefault();
      ev.stopPropagation();
      t.setPointerCapture(ev.pointerId);
      t.classList.add('dragging');
      const start = { x: ev.clientX, y: ev.clientY, left: box(name).x, top: box(name).y };
      const attached = edges.filter((e) => e.dataset.from === name || e.dataset.to === name);
      let moved = false;

      const move = (e) => {
        const left = Math.round(start.left + (e.clientX - start.x) / view.k);
        const top = Math.round(start.top + (e.clientY - start.y) / view.k);
        if (!moved && left === start.left && top === start.top) return;
        moved = true;
        t.style.left = `${left}px`;
        t.style.top = `${top}px`;
        for (const edge of attached) {
          delete edge.dataset.waypoints; // lanes belonged to the old position
          redraw(edge);
        }
      };
      const end = () => {
        t.removeEventListener('pointermove', move);
        t.classList.remove('dragging');
        if (!moved) return;
        pinned = true;
        markDirty();
      };
      t.addEventListener('pointermove', move);
      t.addEventListener('pointerup', end, { once: true });
      t.addEventListener('pointercancel', end, { once: true });
    });
  }

  // --- reset / save ------------------------------------------------------------
  function setState(cls, text) {
    stateEl.className = `save-state ${cls}`;
    stateEl.textContent = text;
  }

  function resetLayout() {
    for (const [name, pos] of Object.entries(data.auto.tables)) {
      const t = tables.get(name);
      if (!t) continue;
      t.style.left = `${num(pos.x)}px`;
      t.style.top = `${num(pos.y)}px`;
    }
    for (const edge of edges) {
      const pts = data.auto.edges[edge.dataset.id];
      if (pts) edge.dataset.waypoints = JSON.stringify(pts);
      else delete edge.dataset.waypoints;
      redraw(edge);
    }
    pinned = false;
    fitView();
    markDirty();
  }

  function currentLayout() {
    if (!pinned) return {};
    const layout = { tables: {}, edges: {} };
    for (const name of tables.keys()) {
      const b = box(name);
      layout.tables[name] = { x: b.x, y: b.y };
    }
    for (const edge of edges) {
      const pts = waypointsOf(edge);
      if (pts.length) layout.edges[edge.dataset.id] = pts;
    }
    return layout;
  }

  /** The page as linkml-herd would write it for the current layout (scrollable, no live view state). */
  function snapshot() {
    data.layout = currentLayout();
    dataEl.textContent = JSON.stringify(data).replaceAll('</', '<\\/');
    const doc = document.documentElement.cloneNode(true);
    const q = (id) => doc.querySelector(`#${id}`);
    const b = contentBounds();
    q('viewport').className = 'viewport';
    q('viewport').removeAttribute('style');
    q('sizer').setAttribute('style', `width:${num(b.x1 - b.x0 + 2 * G.pad)}px;height:${num(b.y1 - b.y0 + 2 * G.pad)}px`);
    q('world').setAttribute('style', `transform:translate(${num(G.pad - b.x0)}px,${num(G.pad - b.y0)}px)`);
    // The file keeps the theme linkml-herd baked in; switching in the menu is the viewer's own choice.
    if (bakedTheme === 'auto') doc.removeAttribute('data-theme');
    else doc.setAttribute('data-theme', bakedTheme);
    q('inverse')?.toggleAttribute('checked', !!data.showInverse);
    q('actions').hidden = true;
    q('controls').hidden = true;
    q('save-state').className = 'save-state';
    q('save-state').textContent = '';
    for (const el of doc.querySelectorAll('.dragging, .flash')) el.classList.remove('dragging', 'flash');
    return '<!doctype html>\n' + doc.outerHTML + '\n';
  }

  // Browsers won't open a save dialog at a path (so not at this page's folder),
  // but Chromium can keep the file handle in IndexedDB: after the first save,
  // later saves of this page write straight back to the same file.
  const handles = {
    key: location.href.split('#')[0],
    open() {
      return new Promise((resolve, reject) => {
        const req = indexedDB.open('linkml-herd', 1);
        req.onupgradeneeded = () => req.result.createObjectStore('handles');
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      });
    },
    async get() {
      try {
        const db = await this.open();
        return await new Promise((resolve) => {
          const req = db.transaction('handles').objectStore('handles').get(this.key);
          req.onsuccess = () => resolve(req.result ?? null);
          req.onerror = () => resolve(null);
        });
      } catch {
        return null;
      }
    },
    async set(handle) {
      try {
        const db = await this.open();
        db.transaction('handles', 'readwrite').objectStore('handles').put(handle, this.key);
      } catch {
        // Not fatal: the next save just asks for the file again.
      }
    }
  };
  // Some browsers never answer IndexedDB requests on file:// pages; don't let Save hang on that.
  const lookup = Promise.race([handles.get(), new Promise((resolve) => setTimeout(resolve, 1000, null))]);
  let fileHandle = window.showSaveFilePicker ? lookup : Promise.resolve(null);

  /** Remember the picker's folder per directory, so sibling diagrams open there too. */
  function pickerId() {
    const dir = decodeURIComponent(location.pathname).replace(/[^/]*$/, '');
    let h = 0;
    for (const c of dir) h = (Math.imul(h, 31) + c.codePointAt(0)) | 0;
    return `herd-${(h >>> 0).toString(36)}`;
  }

  async function writeTo(handle, html) {
    if ((await handle.requestPermission({ mode: 'readwrite' })) !== 'granted') return false;
    const w = await handle.createWritable();
    await w.write(html);
    await w.close();
    return true;
  }

  function download(text, name, type) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type }));
    a.download = name;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  async function save() {
    const html = snapshot();
    const name = data.file || 'diagram.html';
    try {
      if (window.showSaveFilePicker) {
        let handle = await fileHandle;
        let written = false;
        try {
          written = !!handle && (await writeTo(handle, html));
        } catch (err) {
          if (err.name !== 'NotFoundError' && err.name !== 'NotAllowedError') throw err;
        }
        if (!written) {
          handle = await window.showSaveFilePicker({
            suggestedName: name,
            ...(handle ? { startIn: handle } : { id: pickerId() }),
            types: [{ description: 'HTML', accept: { 'text/html': ['.html'] } }]
          });
          await writeTo(handle, html);
          handles.set(handle);
        }
        fileHandle = Promise.resolve(handle);
        setState('saved', `saved ✓ ${handle.name}`);
      } else {
        download(html, name, 'text/html');
        setState('saved', `downloaded ${name} — replace the original with it`);
      }
      dirty = false;
    } catch (err) {
      if (err.name === 'AbortError') return setState('', dirty ? 'unsaved changes' : '');
      setState('error', `save failed: ${err.message}`);
    }
  }

  // --- SVG export -------------------------------------------------------------------
  // linkml-herd draws the diagram as a plain SVG image into <template id="herd-svg">, coloured with
  // var(--token). Export moves it to the current layout and fills in the theme on screen.
  function svgSnapshot() {
    const img = $('herd-svg').content.firstElementChild.cloneNode(true);
    const b = contentBounds();
    const [x, y, w, h] = [b.x0 - G.pad, b.y0 - G.pad, b.x1 - b.x0 + 2 * G.pad, b.y1 - b.y0 + 2 * G.pad].map(num);
    img.setAttribute('viewBox', `${x} ${y} ${w} ${h}`);
    img.setAttribute('width', w);
    img.setAttribute('height', h);
    const bg = img.querySelector('[data-bg]');
    for (const [k, v] of Object.entries({ x, y, width: w, height: h })) bg.setAttribute(k, v);
    if (!data.showInverse) for (const g of img.querySelectorAll('g[data-inverse]')) g.remove();
    for (const g of img.querySelectorAll('g[data-name]')) {
      const t = tables.get(g.dataset.name);
      g.setAttribute('transform', `translate(${num(parseFloat(t.style.left))},${num(parseFloat(t.style.top))})`);
    }
    const drawn = new Map([...img.querySelectorAll('g[data-id]')].map((g) => [g.dataset.id, g]));
    for (const edge of edges) {
      const g = drawn.get(edge.dataset.id);
      if (!g) continue;
      g.querySelector('path').setAttribute('d', edge.querySelector('path').getAttribute('d'));
      for (const k of ['x', 'y']) g.querySelector('text').setAttribute(k, edge.querySelector('text').getAttribute(k));
    }
    const style = getComputedStyle(document.documentElement);
    const text = new XMLSerializer().serializeToString(img);
    return '<?xml version="1.0" encoding="UTF-8"?>\n' +
      text.replace(/var\(--([\w-]+)\)/g, (_, t) => style.getPropertyValue(`--${t}`).trim()) + '\n';
  }

  async function exportSvg() {
    const image = svgSnapshot();
    const name = (data.file || 'diagram.html').replace(/\.html?$/i, '') + '.svg';
    try {
      if (window.showSaveFilePicker) {
        const handle = await window.showSaveFilePicker({
          suggestedName: name,
          id: pickerId(),
          types: [{ description: 'SVG image', accept: { 'image/svg+xml': ['.svg'] } }]
        });
        await writeTo(handle, image);
        setState('saved', `exported ✓ ${handle.name}`);
      } else {
        download(image, name, 'image/svg+xml');
        setState('saved', `downloaded ${name}`);
      }
    } catch (err) {
      if (err.name === 'AbortError') return setState('', dirty ? 'unsaved changes' : '');
      setState('error', `export failed: ${err.message}`);
    }
  }

  // --- theme --------------------------------------------------------------------------
  // `linkml-herd --theme` bakes one in; picking another here is remembered for this page (and
  // forgotten when linkml-herd bakes in a different one). The head script applies it on load.
  const bakedTheme = data.theme || 'auto';
  const themeKey = `herd-theme:${location.pathname}:${bakedTheme}`;
  function setTheme(theme) {
    const root = document.documentElement;
    if (theme === 'auto') delete root.dataset.theme;
    else root.dataset.theme = theme;
    try {
      if (theme === bakedTheme) localStorage.removeItem(themeKey);
      else localStorage.setItem(themeKey, theme);
    } catch {
      // Storage blocked: the choice still applies for this visit.
    }
  }

  // --- inverse relationships ---------------------------------------------------------
  // Off by default; "Save canvas" keeps the choice in the page, and linkml-herd reads it back.
  function showInverse(on) {
    data.showInverse = on;
    document.documentElement.classList.toggle('show-inverse', on);
    markDirty();
  }

  // --- wire up ---------------------------------------------------------------------
  sizer.removeAttribute('style');
  viewport.classList.add('live');
  $('actions').hidden = false;
  $('controls').hidden = false;
  $('save').addEventListener('click', save);
  $('reset').addEventListener('click', resetLayout);
  $('export').addEventListener('click', exportSvg);
  $('theme').value = document.documentElement.dataset.theme || 'auto';
  $('theme').addEventListener('change', (e) => setTheme(e.target.value));
  if ($('inverse')) {
    $('inverse').checked = !!data.showInverse; // browsers may restore a stale form state on reload
    $('inverse').addEventListener('change', (e) => showInverse(e.target.checked));
  }
  $('zoom-in').addEventListener('click', () => zoomCenter(1.2));
  $('zoom-out').addEventListener('click', () => zoomCenter(1 / 1.2));
  $('fit').addEventListener('click', fitView);
  for (const a of document.querySelectorAll('.problems a[data-table]')) {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      a.closest('details').open = false;
      centerOn(a.dataset.table);
    });
  }
  window.addEventListener('beforeunload', (e) => {
    if (dirty) e.preventDefault();
  });
  new ResizeObserver(() => viewTouched || fitView()).observe(viewport);
  fitView();
})();
