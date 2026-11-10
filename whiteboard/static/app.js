/* AI Whiteboard viewer: live connection, drawing animation queue, mascot,
 * snapshots for the AI, and the user's own markers.
 *
 * Two copies of the board are kept: `model` is the latest truth from the
 * server (used for layout reports and snapshots), `view` is what is on
 * screen. Every server event updates the model at once and joins a queue
 * that replays it onto the view with a marker animation.
 */
(function () {
  'use strict';
  const WB = window.WB;
  const $ = (id) => document.getElementById(id);
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  const store = {
    get(k, d) {
      try {
        const v = localStorage.getItem('wb.' + k);
        return v == null ? d : JSON.parse(v);
      } catch (e) {
        return d;
      }
    },
    set(k, v) {
      try {
        localStorage.setItem('wb.' + k, JSON.stringify(v));
      } catch (e) { /* storage may be unavailable */ }
    },
  };

  const DEFAULT_SETTINGS = { width: 1600, height: 1000, background: 'whiteboard', mascot: true };
  const newBoard = () => ({ settings: Object.assign({}, DEFAULT_SETTINGS), els: new Map(), order: [] });
  let model = newBoard();
  let view = newBoard();

  const SPEEDS = [0.5, 1, 2, 4, 0]; // 0 = instant
  const urlSpeed = parseFloat(new URLSearchParams(location.search).get('speed'));
  let speed = SPEEDS.includes(urlSpeed) ? urlSpeed : store.get('speed', 1);
  if (!SPEEDS.includes(speed)) speed = 1;

  let clientId = null;
  let fontsReady = false;
  let fontGen = 0;
  const queue = [];
  let current = null;
  let lastAgent = null;
  let idleSince = performance.now();

  const surface = $('surface');
  const canvases = { bg: $('bgCanvas'), ink: $('inkCanvas'), live: $('liveCanvas'), user: $('userCanvas') };
  const ctx = {};
  for (const k in canvases) ctx[k] = canvases[k].getContext('2d');
  let scale = 1, dpr = 1;

  // ------------------------------------------------------------ drawings cache
  const cache = new WeakMap();
  function envFor(board) {
    const env = {
      theme: WB.makeTheme(board.settings.background),
      board: board.settings,
      visiting: new Set(),
      bboxOf: (id) => {
        const el = board.els.get(id);
        if (!el) return null;
        const d = drawingOf(el, env);
        return d && !d.missing ? d.box : null;
      },
    };
    return env;
  }
  function drawingOf(el, env) {
    const rec = cache.get(el);
    if (rec && rec.theme === env.theme.name && rec.gen === fontGen && !rec.d.dynamic) return rec.d;
    if (env.visiting.has(el.id)) return null;
    env.visiting.add(el.id);
    const d = WB.compile(el, env);
    env.visiting.delete(el.id);
    cache.set(el, { theme: env.theme.name, gen: fontGen, d });
    return d;
  }
  WB.onAsset = () => {
    fontGen += 1;
    renderInk();
    reportLayout([...model.els.keys()]);
  };

  // ------------------------------------------------------------ board helpers
  function setBoard(board, state) {
    board.settings = Object.assign({}, DEFAULT_SETTINGS, state.settings || {});
    board.els = new Map();
    board.order = [];
    for (const el of state.elements || []) {
      board.els.set(el.id, el);
      board.order.push(el.id);
    }
  }
  function copyBoard(src) {
    return { settings: Object.assign({}, src.settings), els: new Map(src.els), order: src.order.slice() };
  }
  function putEl(board, el) {
    if (board.els.has(el.id)) board.order = board.order.filter((i) => i !== el.id);
    board.els.set(el.id, el);
    board.order.push(el.id);
  }
  function replaceEl(board, el) {
    if (!board.els.has(el.id)) board.order.push(el.id);
    board.els.set(el.id, el);
  }
  function dropEl(board, id) {
    if (!board.els.delete(id)) return;
    board.order = board.order.filter((i) => i !== id);
  }

  // ------------------------------------------------------------ layout & rendering
  function resize() {
    const W = view.settings.width, H = view.settings.height;
    const wall = $('wall').getBoundingClientRect();
    if (wall.width < 40 || wall.height < 40) return; // not laid out yet (hidden tab or pane)
    const frameW = clamp(Math.round(Math.min(wall.width, wall.height) * 0.018), 8, 16);
    document.documentElement.style.setProperty('--frame', frameW + 'px');
    const trayH = $('tray').offsetHeight || 34;
    let w = wall.width - 32 - frameW * 2;
    let h = wall.height - 28 - trayH - frameW * 2;
    if (w / h > W / H) w = h * (W / H);
    else h = w * (H / W);
    w = Math.max(160, Math.floor(w));
    h = Math.max(100, Math.floor((w * H) / W));
    surface.style.width = w + 'px';
    surface.style.height = h + 'px';
    $('tray').style.width = Math.round((w + frameW * 2) * 0.92) + 'px';
    scale = w / W;
    dpr = Math.min(window.devicePixelRatio || 1, 3);
    surface.style.setProperty('--u', scale.toFixed(4));
    for (const k in canvases) {
      canvases[k].width = Math.round(w * dpr);
      canvases[k].height = Math.round(h * dpr);
    }
    $('fx').setAttribute('viewBox', `0 0 ${W} ${H}`);
    renderBg();
    renderInk();
  }
  function boardTransform(c) {
    c.setTransform(scale * dpr, 0, 0, scale * dpr, 0, 0);
  }
  function clear(c) {
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.clearRect(0, 0, c.canvas.width, c.canvas.height);
  }
  function renderBg() {
    WB.drawBackground(ctx.bg, view.settings.background, canvases.bg.width, canvases.bg.height, scale * dpr);
    const theme = WB.makeTheme(view.settings.background);
    surface.classList.toggle('dark', theme.dark);
    surface.style.background = theme.surface;
  }
  function renderInk() {
    const c = ctx.ink;
    clear(c);
    boardTransform(c);
    const env = envFor(view);
    const hide = current && current.hide;
    for (const id of view.order) {
      if (hide && hide.has(id)) continue;
      WB.drawDrawing(c, drawingOf(view.els.get(id), env), 1);
    }
    updateEmptyHint();
    updateMascotSpace();
  }
  function inkAppend(el) {
    // dynamic elements (arrows) waiting for this one need a full redraw
    for (const id of view.order) {
      const other = view.els.get(id);
      if (other && other.type === 'arrow' && refersTo(other, el.id)) return renderInk();
    }
    const c = ctx.ink;
    boardTransform(c);
    WB.drawDrawing(c, drawingOf(el, envFor(view)), 1);
    updateEmptyHint();
    updateMascotSpace();
  }
  function refersTo(el, id) {
    const ref = (v) => v === id || (v && typeof v === 'object' && !Array.isArray(v) && v.id === id);
    return ref(el.from) || ref(el.to) || el.tail === id;
  }
  function updateEmptyHint() {
    $('emptyHint').hidden = view.order.length > 0 || !!current || queue.length > 0;
  }

  // ------------------------------------------------------------ the animation queue
  // A paced script (one with pauses) always plays at the chosen speed; only a
  // huge unpaced backlog is sped up, so the board never falls minutes behind.
  function effSpeed() {
    if (speed === 0) return Infinity;
    let adds = 0;
    for (const it of queue) {
      if (it.kind === 'pause') return speed;
      if (it.kind === 'add') adds += 1;
    }
    return speed * Math.max(1, adds / 40);
  }
  function instant() {
    return speed === 0 || document.hidden;
  }
  function startItem(it, now) {
    switch (it.kind) {
      case 'add': {
        const el = it.el;
        const replaced = view.els.has(el.id);
        putEl(view, el);
        if (it.agent) setActivity(it.agent, describe(el));
        if (!it.anim || instant()) {
          if (replaced) renderInk();
          else inkAppend(el);
          return;
        }
        const d = drawingOf(el, envFor(view));
        if (!d || !d.ops.length) {
          renderInk();
          return;
        }
        current = { kind: 'draw', el, d, start: now, dur: clamp(d.total, 220, 2600) / effSpeed(), hide: new Set([el.id]) };
        if (replaced) renderInk();
        updateEmptyHint();
        return;
      }
      case 'update':
        for (const el of it.els) replaceEl(view, el);
        renderInk();
        return;
      case 'remove': {
        const ids = it.ids.filter((id) => view.els.has(id));
        if (!ids.length) return;
        if (!it.anim || instant()) {
          ids.forEach((id) => dropEl(view, id));
          renderInk();
          return;
        }
        const env = envFor(view);
        const boxes = ids.map((id) => drawingOf(view.els.get(id), env)).filter(Boolean).map((d) => d.box);
        const off = offscreenOf(ids, env);
        ids.forEach((id) => dropEl(view, id));
        renderInk();
        startWipe(now, off, WB.unionBox(boxes), 70);
        return;
      }
      case 'clear': {
        const env = envFor(view);
        const off = view.order.length && !instant() ? offscreenOf(view.order, env) : null;
        view.els = new Map();
        view.order = [];
        if (it.settings) applySettings(it.settings);
        else renderInk();
        if (off) startWipe(now, off, [0, 0, view.settings.width, view.settings.height], 150);
        return;
      }
      case 'settings':
        applySettings(it.settings);
        return;
      case 'sync': {
        const fresh = newBoard();
        setBoard(fresh, it.state);
        if (it.animate && !instant()) {
          view = newBoard();
          view.settings = fresh.settings;
          resize();
          queue.splice(0, 0, ...fresh.order.map((id) => ({ kind: 'add', el: fresh.els.get(id), anim: true })));
        } else {
          view = fresh;
          resize();
        }
        return;
      }
      case 'say':
        say(it.text, it.duration);
        return;
      case 'pause':
        if (!instant()) current = { kind: 'pause', start: now, dur: it.seconds * 1000 };
        return;
      case 'point':
        pointAt(it.ev);
        return;
      case 'replay':
        view = newBoard();
        view.settings = Object.assign({}, model.settings);
        renderInk();
        return;
    }
  }
  function offscreenOf(ids, env) {
    const cv = document.createElement('canvas');
    cv.width = canvases.ink.width;
    cv.height = canvases.ink.height;
    const c = cv.getContext('2d');
    boardTransform(c);
    for (const id of ids) {
      const el = view.els.get(id);
      if (el) WB.drawDrawing(c, drawingOf(el, env), 1);
    }
    return cv;
  }
  function startWipe(now, off, box, width) {
    const pad = width * 0.4;
    const x0 = box[0] - pad, x1 = box[0] + box[2] + pad;
    const step = width * 0.75;
    const pts = [];
    let y = box[1] + width * 0.3;
    let ltr = true;
    const yEnd = box[1] + box[3] + width * 0.2;
    do {
      pts.push([ltr ? x0 : x1, y + (Math.random() - 0.5) * 10]);
      pts.push([ltr ? x1 : x0, y + step * 0.5 + (Math.random() - 0.5) * 10]);
      y += step;
      ltr = !ltr;
    } while (y < yEnd);
    let len = 0;
    for (let i = 1; i < pts.length; i++) len += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
    current = { kind: 'wipe', off, pts, len, width, start: now, dur: clamp(len / 3.4, 380, 1700) / Math.min(effSpeed(), 6) };
  }
  function step(now) {
    const cur = current;
    const p = Math.min(1, (now - cur.start) / cur.dur);
    const c = ctx.live;
    clear(c);
    if (cur.kind === 'draw') {
      boardTransform(c);
      const tip = WB.drawDrawing(c, cur.d, p);
      if (tip) movePen(tip, WB.colorAt(cur.d, p));
      if (p >= 1) {
        clear(c);
        current = null;
        inkAppend(cur.el);
      }
    } else if (cur.kind === 'pause') {
      hidePen();
      if (p >= 1) current = null;
    } else if (cur.kind === 'wipe') {
      c.drawImage(cur.off, 0, 0);
      boardTransform(c);
      c.globalCompositeOperation = 'destination-out';
      c.lineCap = 'round';
      c.lineJoin = 'round';
      c.lineWidth = cur.width;
      const target = cur.len * p;
      let acc = 0, tip = cur.pts[0];
      c.beginPath();
      c.moveTo(tip[0], tip[1]);
      for (let i = 1; i < cur.pts.length; i++) {
        const a = cur.pts[i - 1], b = cur.pts[i];
        const d = Math.hypot(b[0] - a[0], b[1] - a[1]);
        if (acc + d >= target) {
          const t = d ? (target - acc) / d : 0;
          tip = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
          c.lineTo(tip[0], tip[1]);
          break;
        }
        acc += d;
        tip = b;
        c.lineTo(b[0], b[1]);
      }
      c.stroke();
      c.globalCompositeOperation = 'source-over';
      moveEraser(tip, cur.width);
      if (p >= 1) {
        clear(c);
        current = null;
        hideEraser();
        updateEmptyHint();
      }
    }
  }
  let lastFrame = performance.now();
  function frame(now) {
    requestAnimationFrame(frame);
    lastFrame = performance.now();
    if (!fontsReady) return;
    let guard = 0;
    while (!current && queue.length && guard++ < 500) startItem(queue.shift(), now);
    if (current) step(now);
    const busy = !!current || queue.length > 0;
    if (busy) idleSince = now;
    else if (now - idleSince > 900) {
      hidePen();
      setActivity(null);
    }
    $('mascot').classList.toggle('busy', busy);
  }

  // ------------------------------------------------------------ pen & eraser sprites
  const pen = $('pen');
  let penShownAt = 0;
  function toScreen(p) {
    return [p[0] * scale, p[1] * scale];
  }
  function movePen(tip, col) {
    const s = toScreen(tip);
    const size = pen.offsetWidth || 150 * scale;
    pen.hidden = false;
    pen.classList.remove('away');
    // the tip of the marker drawing sits at (4/120, 110/120) of the sprite
    pen.style.transform = `translate(${s[0] - size * 0.033}px, ${s[1] - size * 0.917}px)`;
    if (col) pen.style.setProperty('--pen', col);
    penShownAt = performance.now();
    lookAt(tip);
  }
  function hidePen() {
    if (!pen.hidden && performance.now() - penShownAt > 600) pen.classList.add('away');
  }
  const eraser = $('eraserSprite');
  function moveEraser(tip) {
    const s = toScreen(tip);
    eraser.hidden = false;
    eraser.style.transform = `translate(${s[0] - eraser.offsetWidth / 2}px, ${s[1] - eraser.offsetHeight * 0.75}px) rotate(-8deg)`;
    lookAt(tip);
  }
  function hideEraser() {
    eraser.hidden = true;
  }

  // ------------------------------------------------------------ mascot
  const mascot = $('mascot');
  const MASCOT_ZONE = [1400, 770, 200, 230];
  function lookAt(p) {
    const cx = view.settings.width - 100, cy = view.settings.height - 160;
    const dx = p[0] - cx, dy = p[1] - cy;
    const d = Math.hypot(dx, dy) || 1;
    const look = mascot.querySelector('.rb-look');
    if (look) look.style.transform = `translate(${((dx / d) * 6).toFixed(1)}px, ${((dy / d) * 4).toFixed(1)}px)`;
  }
  function updateMascotSpace() {
    const show = view.settings.mascot !== false;
    mascot.classList.toggle('hidden', !show);
    if (!show) return;
    const W = view.settings.width, H = view.settings.height;
    const zone = [W - (1600 - MASCOT_ZONE[0]), H - (1000 - MASCOT_ZONE[1]), MASCOT_ZONE[2], MASCOT_ZONE[3]];
    const env = envFor(view);
    let crowded = false;
    for (const id of view.order) {
      const d = drawingOf(view.els.get(id), env);
      if (!d) continue;
      const b = d.box;
      const ix = Math.min(b[0] + b[2], zone[0] + zone[2]) - Math.max(b[0], zone[0]);
      const iy = Math.min(b[1] + b[3], zone[1] + zone[3]) - Math.max(b[1], zone[1]);
      if (ix > 20 && iy > 20) {
        crowded = true;
        break;
      }
    }
    mascot.classList.toggle('ducked', crowded && !mascot.classList.contains('talking'));
  }
  let sayTimer = null, typeTimer = null;
  function say(text, duration) {
    const box = $('speech'), span = $('speechText');
    clearTimeout(sayTimer);
    clearInterval(typeTimer);
    box.hidden = false;
    box.style.animation = 'none';
    void box.offsetWidth;
    box.style.animation = '';
    mascot.classList.add('talking');
    mascot.classList.remove('ducked', 'hidden');
    const chars = Array.from(String(text));
    let i = 0;
    span.textContent = '';
    typeTimer = setInterval(() => {
      i += 1;
      span.textContent = chars.slice(0, i).join('');
      if (i >= chars.length) {
        clearInterval(typeTimer);
        setTimeout(() => mascot.classList.remove('talking'), 300);
      }
    }, instant() ? 0 : 28);
    const ms = duration ? duration * 1000 : clamp(2500 + chars.length * 70, 3500, 12000);
    sayTimer = setTimeout(() => {
      box.hidden = true;
      mascot.classList.remove('talking');
      updateMascotSpace();
    }, ms);
  }
  mascot.querySelector('.robot').addEventListener('click', () => {
    const lines = ['Hi! Ask your AI to draw something here.', "I'm the board's helper. Let's do this!", 'Grab a marker from the tray and draw with me!'];
    say(lines[Math.floor(Math.random() * lines.length)], 3.5);
  });

  // ------------------------------------------------------------ pointer (point_at)
  const SVGNS = 'http://www.w3.org/2000/svg';
  function pointAt(ev) {
    const fx = $('fx');
    let box = ev.bbox;
    if (ev.id && model.els.has(ev.id)) {
      const d = drawingOf(model.els.get(ev.id), envFor(model));
      if (d) box = d.box;
    }
    let cx, cy, rx, ry;
    if (box) {
      cx = box[0] + box[2] / 2;
      cy = box[1] + box[3] / 2;
      rx = box[2] / 2 + 28;
      ry = box[3] / 2 + 24;
    } else {
      cx = +ev.x;
      cy = +ev.y;
      rx = ry = 46;
    }
    const g = document.createElementNS(SVGNS, 'g');
    g.setAttribute('class', 'fx-fade');
    const r = WB.rng(Math.floor(Math.random() * 1e9));
    const pts = WB.roughEllipse(r, cx, cy, rx, ry, 1.2);
    const path = document.createElementNS(SVGNS, 'path');
    path.setAttribute('d', 'M' + pts.map((p) => p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' L'));
    path.setAttribute('class', 'fx-ring');
    path.setAttribute('stroke', '#e8402f');
    path.setAttribute('stroke-width', '6');
    path.setAttribute('opacity', '0.9');
    g.appendChild(path);
    if (ev.label) {
      const t = document.createElementNS(SVGNS, 'text');
      const above = cy - ry > 70;
      t.setAttribute('x', clamp(cx, 120, view.settings.width - 120));
      t.setAttribute('y', above ? cy - ry - 18 : cy + ry + 40);
      t.setAttribute('text-anchor', 'middle');
      t.setAttribute('class', 'fx-label');
      t.setAttribute('font-size', '30');
      t.setAttribute('fill', '#e8402f');
      t.setAttribute('paint-order', 'stroke');
      t.setAttribute('stroke', 'rgba(255,255,255,0.85)');
      t.setAttribute('stroke-width', '6');
      t.textContent = ev.label;
      g.appendChild(t);
    }
    fx.appendChild(g);
    const len = path.getTotalLength();
    path.style.strokeDasharray = len;
    path.style.strokeDashoffset = len;
    path.getBoundingClientRect();
    path.style.transition = `stroke-dashoffset ${instant() ? 0 : 0.7}s ease-out`;
    path.style.strokeDashoffset = 0;
    lookAt([cx, cy]);
    setTimeout(() => (g.style.opacity = '0'), 4200);
    setTimeout(() => g.remove(), 5000);
  }

  // ------------------------------------------------------------ settings
  function applySettings(s) {
    const old = view.settings;
    view.settings = Object.assign({}, DEFAULT_SETTINGS, s);
    $('bgSelect').value = view.settings.background;
    if (old.width !== view.settings.width || old.height !== view.settings.height) resize();
    else {
      renderBg();
      renderInk();
    }
  }

  // ------------------------------------------------------------ activity line
  function describe(el) {
    const label = el.title || el.text || el.label || el.name || el.center || '';
    const s = Array.isArray(label) ? label.map((x) => (typeof x === 'object' ? x.text : x)).join('') : String(label);
    const short = s.replace(/\*\*/g, '').replace(/\s+/g, ' ').trim();
    return el.type + (short ? ` “${short.length > 34 ? short.slice(0, 32) + '…' : short}”` : '');
  }
  function setActivity(agent, what) {
    const a = $('activity');
    if (!agent) {
      a.classList.remove('on');
      return;
    }
    lastAgent = agent;
    a.innerHTML = '';
    const b = document.createElement('b');
    b.textContent = agent;
    a.append('✏️ ', b, ` is drawing ${what}`);
    a.classList.add('on');
  }

  // ------------------------------------------------------------ server connection
  let es = null, retry = 0, build = null;
  function connect() {
    setStatus('connecting', retry ? 'Reconnecting…' : 'Connecting…');
    es = new EventSource('/api/events');
    es.onmessage = (m) => {
      let ev;
      try {
        ev = JSON.parse(m.data);
      } catch (e) {
        return;
      }
      onEvent(ev);
    };
    es.onerror = () => {
      setStatus('offline', 'Offline, retrying…');
      es.close();
      retry += 1;
      setTimeout(connect, Math.min(5000, 600 * retry));
    };
  }
  function setStatus(state, text) {
    $('status').dataset.state = state;
    $('statusText').textContent = text;
  }
  function onEvent(ev) {
    switch (ev.type) {
      case 'hello':
        if (build && ev.build && ev.build !== build) {
          location.reload(); // the board was upgraded while this page was open
          return;
        }
        build = ev.build || build;
        retry = 0;
        clientId = ev.client;
        setStatus('live', 'Live');
        setBoard(model, ev.state);
        queue.length = 0;
        current = null;
        clear(ctx.live);
        view = copyBoard(model);
        $('bgSelect').value = view.settings.background;
        resize();
        reportLayout([...model.els.keys()]);
        break;
      case 'add':
        for (const el of ev.elements) {
          putEl(model, el);
          queue.push({ kind: 'add', el, anim: ev.origin !== clientId, agent: ev.origin === clientId ? null : ev.agent || (el.author === 'user' ? 'Someone' : 'The AI') });
        }
        if (ev.origin === clientId) clear(ctx.user);
        reportLayout(ev.elements.map((e) => e.id));
        break;
      case 'update':
        for (const el of ev.elements) replaceEl(model, el);
        queue.push({ kind: 'update', els: ev.elements });
        reportLayout(ev.elements.map((e) => e.id));
        break;
      case 'remove':
        ev.ids.forEach((id) => dropEl(model, id));
        queue.push({ kind: 'remove', ids: ev.ids, anim: ev.origin !== clientId });
        break;
      case 'clear':
        model.els = new Map();
        model.order = [];
        if (ev.settings) model.settings = Object.assign({}, DEFAULT_SETTINGS, ev.settings);
        queue.push({ kind: 'clear', settings: ev.settings });
        break;
      case 'settings':
        model.settings = Object.assign({}, DEFAULT_SETTINGS, ev.settings);
        queue.push({ kind: 'settings', settings: ev.settings });
        reportLayout([...model.els.keys()]);
        break;
      case 'sync':
        setBoard(model, ev.state);
        queue.push({ kind: 'sync', state: ev.state, animate: ev.animate });
        reportLayout([...model.els.keys()]);
        break;
      case 'say':
        queue.push({ kind: 'say', text: ev.text, duration: ev.duration });
        break;
      case 'point':
        queue.push({ kind: 'point', ev });
        break;
      case 'pause':
        queue.push({ kind: 'pause', seconds: +ev.seconds || 0 });
        break;
      case 'snapshot':
        snapshot(ev);
        break;
    }
    updateEmptyHint();
  }
  function post(path, body) {
    return fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then((r) => r.json())
      .catch(() => null);
  }

  // Tell the server how big things really are (it only has estimates).
  const layoutPending = new Set();
  let layoutTimer = null;
  function reportLayout(ids) {
    ids.forEach((id) => layoutPending.add(id));
    if (!layoutTimer) layoutTimer = setTimeout(flushLayout, 0);
  }
  function flushLayout() {
    layoutTimer = null;
    if (!fontsReady) return;
    const env = envFor(model);
    const out = {};
    for (const [id, el] of model.els) {
      if (!layoutPending.has(id) && !(el.type === 'arrow' || typeof el.tail === 'string')) continue;
      const d = drawingOf(el, env);
      if (d && !d.missing) out[id] = d.box.map((v) => Math.round(v * 10) / 10);
    }
    layoutPending.clear();
    if (Object.keys(out).length) post('/api/layout', { bboxes: out });
  }

  // Render the latest board for the AI (the "look" tool).
  function renderBoard(width, opts) {
    const W = model.settings.width, H = model.settings.height;
    const s = width / W;
    const cv = document.createElement('canvas');
    cv.width = Math.round(W * s);
    cv.height = Math.round(H * s);
    const c = cv.getContext('2d');
    WB.drawBackground(c, model.settings.background, cv.width, cv.height, s);
    c.setTransform(s, 0, 0, s, 0, 0);
    const env = envFor(model);
    for (const id of model.order) WB.drawDrawing(c, drawingOf(model.els.get(id), env), 1, opts);
    return cv;
  }
  function snapshot(ev) {
    const go = () => {
      const cv = renderBoard(clamp(+ev.width || 1600, 200, 4000), { export: true });
      const type = ev.format === 'jpeg' ? 'image/jpeg' : 'image/png';
      post('/api/snapshot', { req: ev.req, data: cv.toDataURL(type, 0.9) });
    };
    // let a visible tab answer first when several viewers are open
    if (document.hidden) setTimeout(go, 400);
    else go();
  }

  // ------------------------------------------------------------ your own markers
  let tool = null;
  const trayButtons = document.querySelectorAll('#tray .marker, #tray .tool');
  function setTool(t) {
    tool = t;
    surface.dataset.tool = t ? t.kind : '';
    trayButtons.forEach((b) => {
      const on = t && ((t.kind === 'pen' && b.dataset.color === t.color) || (t.kind !== 'pen' && b.dataset.tool === t.kind));
      b.classList.toggle('active', !!on);
    });
    $('trayHint').textContent = t ? (t.kind === 'pen' ? 'Drawing: Esc to put the marker down' : t.kind === 'eraser' ? 'Erasing: drag over things' : 'Click to place a sticky note') : 'Pick up a marker to draw too';
  }
  trayButtons.forEach((b) =>
    b.addEventListener('click', () => {
      const next = b.dataset.color ? { kind: 'pen', color: b.dataset.color } : { kind: b.dataset.tool };
      const same = tool && tool.kind === next.kind && tool.color === next.color;
      setTool(same ? null : next);
    }),
  );
  function boardPoint(e) {
    const r = surface.getBoundingClientRect();
    return [((e.clientX - r.left) / r.width) * view.settings.width, ((e.clientY - r.top) / r.height) * view.settings.height];
  }
  let stroke = null;
  const erasing = new Set();
  surface.addEventListener('pointerdown', (e) => {
    if (!tool || e.button !== 0) return;
    e.preventDefault();
    const p = boardPoint(e);
    if (tool.kind === 'note') {
      const text = window.prompt('Sticky note text:');
      if (text && text.trim()) {
        post('/api/draw', { elements: [{ type: 'sticky', x: Math.round(p[0] - 120), y: Math.round(p[1] - 40), w: 240, text: text.trim(), color: 'yellow' }], author: 'user', origin: clientId, agent: 'You' });
      }
      setTool(null);
      return;
    }
    surface.setPointerCapture(e.pointerId);
    if (tool.kind === 'pen') {
      stroke = { pts: [p], color: tool.color };
      drawUserStroke();
    } else if (tool.kind === 'eraser') {
      erasing.clear();
      eraseAt(p);
    }
  });
  surface.addEventListener('pointermove', (e) => {
    if (!tool) return;
    const p = boardPoint(e);
    if (tool.kind === 'pen' && stroke) {
      const last = stroke.pts[stroke.pts.length - 1];
      if (Math.hypot(p[0] - last[0], p[1] - last[1]) > 1.5) {
        stroke.pts.push(p);
        drawUserStroke();
      }
    } else if (tool.kind === 'eraser' && e.buttons & 1) {
      eraseAt(p);
    }
  });
  const finish = () => {
    if (stroke) {
      const pts = simplify(stroke.pts, 0.9).map((p) => [Math.round(p[0] * 10) / 10, Math.round(p[1] * 10) / 10]);
      post('/api/draw', { elements: [{ type: 'line', points: pts.length > 1 ? pts : [pts[0], pts[0]], smooth: true, rough: 0, color: stroke.color, width: 4 }], author: 'user', origin: clientId, agent: 'You' });
      stroke = null;
    }
    if (erasing.size) {
      post('/api/erase', { ids: [...erasing], origin: clientId });
      erasing.clear();
      clear(ctx.user);
    }
  };
  surface.addEventListener('pointerup', finish);
  surface.addEventListener('pointercancel', finish);
  function drawUserStroke() {
    const c = ctx.user;
    clear(c);
    boardTransform(c);
    c.strokeStyle = WB.color(stroke.color, WB.makeTheme(view.settings.background));
    c.lineWidth = 4;
    c.lineCap = 'round';
    c.lineJoin = 'round';
    c.globalAlpha = 0.95;
    const pts = WB.spline(stroke.pts, 5);
    c.beginPath();
    c.moveTo(pts[0][0], pts[0][1]);
    for (const q of pts) c.lineTo(q[0], q[1]);
    if (pts.length === 1) c.lineTo(pts[0][0] + 0.1, pts[0][1]);
    c.stroke();
  }
  function eraseAt(p) {
    const env = envFor(view);
    for (let i = view.order.length - 1; i >= 0; i--) {
      const id = view.order[i];
      if (erasing.has(id)) continue;
      const d = drawingOf(view.els.get(id), env);
      if (d && hits(d, p)) {
        erasing.add(id);
        break;
      }
    }
    const c = ctx.user;
    clear(c);
    boardTransform(c);
    c.fillStyle = 'rgba(214,57,44,0.12)';
    c.strokeStyle = 'rgba(214,57,44,0.5)';
    c.lineWidth = 2;
    for (const id of erasing) {
      const d = drawingOf(view.els.get(id), env);
      if (!d) continue;
      c.fillRect(d.box[0], d.box[1], d.box[2], d.box[3]);
      c.strokeRect(d.box[0], d.box[1], d.box[2], d.box[3]);
    }
  }
  function hits(d, p) {
    const b = d.box;
    if (p[0] < b[0] - 8 || p[0] > b[0] + b[2] + 8 || p[1] < b[1] - 8 || p[1] > b[1] + b[3] + 8) return false;
    const strokeOnly = d.ops.every((op) => op.k === 'stroke');
    if (!strokeOnly || d.rot) return true;
    const q = [p[0] - d.tx, p[1] - d.ty];
    return d.ops.some((op) => {
      const reach = 12 + op.w;
      if (op.pts.length === 1) return Math.hypot(op.pts[0][0] - q[0], op.pts[0][1] - q[1]) < reach;
      for (let i = 1; i < op.pts.length; i++) if (segDist(q, op.pts[i - 1], op.pts[i]) < reach) return true;
      return false;
    });
  }
  function segDist(p, a, b) {
    const dx = b[0] - a[0], dy = b[1] - a[1];
    const len2 = dx * dx + dy * dy;
    const t = len2 ? clamp(((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2, 0, 1) : 0;
    return Math.hypot(p[0] - (a[0] + dx * t), p[1] - (a[1] + dy * t));
  }
  function simplify(pts, eps) {
    if (pts.length < 3) return pts;
    const keep = new Uint8Array(pts.length);
    keep[0] = keep[pts.length - 1] = 1;
    const stack = [[0, pts.length - 1]];
    while (stack.length) {
      const [a, b] = stack.pop();
      let best = -1, bi = -1;
      const [ax, ay] = pts[a], [bx, by] = pts[b];
      const len = Math.hypot(bx - ax, by - ay) || 1;
      for (let i = a + 1; i < b; i++) {
        const dist = Math.abs((by - ay) * pts[i][0] - (bx - ax) * pts[i][1] + bx * ay - by * ax) / len;
        if (dist > best) {
          best = dist;
          bi = i;
        }
      }
      if (best > eps) {
        keep[bi] = 1;
        stack.push([a, bi], [bi, b]);
      }
    }
    return pts.filter((_, i) => keep[i]);
  }

  // ------------------------------------------------------------ toolbar
  function setSpeed(s) {
    speed = s;
    store.set('speed', s);
    $('speedBtn').textContent = s === 0 ? '⚡' : s === 0.5 ? '½×' : s + '×';
    $('speedBtn').title = s === 0 ? 'Drawing speed: instant' : `Drawing speed: ${s}×`;
  }
  setSpeed(speed);
  $('speedBtn').addEventListener('click', () => setSpeed(SPEEDS[(SPEEDS.indexOf(speed) + 1) % SPEEDS.length]));
  function replay() {
    queue.length = 0;
    current = null;
    clear(ctx.live);
    queue.push({ kind: 'replay' });
    for (const id of model.order) queue.push({ kind: 'add', el: model.els.get(id), anim: true });
  }
  $('replayBtn').addEventListener('click', replay);
  $('undoBtn').addEventListener('click', () => post('/api/undo', {}));
  $('exportBtn').addEventListener('click', () => {
    const cv = renderBoard(model.settings.width * 2, { export: true });
    const a = document.createElement('a');
    a.download = 'whiteboard-' + new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-') + '.png';
    a.href = cv.toDataURL('image/png');
    a.click();
  });
  $('fullBtn').addEventListener('click', toggleFull);
  function toggleFull() {
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen().catch(() => {});
  }
  $('bgSelect').addEventListener('change', (e) => post('/api/settings', { background: e.target.value }));

  const dlg = $('helpDialog');
  $('helpBtn').addEventListener('click', async () => {
    let info = null;
    try {
      info = await fetch('/api/info').then((r) => r.json());
    } catch (e) { /* offline */ }
    const py = info ? info.python : 'python';
    const script = info ? info.mcp_command[1] : 'whiteboard/mcp_server.py';
    const url = info ? info.url : location.origin;
    const q = (s) => (/\s/.test(s) ? `"${s}"` : s);
    $('cmdClaude').textContent = `claude mcp add whiteboard -- ${q(py)} ${q(script)}`;
    $('cmdMcp').textContent = JSON.stringify({ mcpServers: { whiteboard: { command: py, args: [script] } } }, null, 2);
    $('cmdCurl').textContent = `curl -X POST ${url}/api/draw -H "Content-Type: application/json" -d '{"elements":[{"type":"title","text":"Hello from an AI!"}]}'`;
    dlg.showModal();
  });
  dlg.addEventListener('click', (e) => {
    if (e.target === dlg) dlg.close();
  });
  document.querySelectorAll('.copy').forEach((b) =>
    b.addEventListener('click', () => {
      const text = $(b.dataset.copy).textContent;
      navigator.clipboard.writeText(text).then(() => {
        b.textContent = 'Copied';
        setTimeout(() => (b.textContent = 'Copy'), 1400);
      }).catch(() => {});
    }),
  );

  if (new URLSearchParams(location.search).has('present')) document.body.classList.add('present');
  document.addEventListener('keydown', (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement && document.activeElement.tagName);
    if (typing || dlg.open) return;
    if (e.key === 'Escape') setTool(null);
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
      e.preventDefault();
      post('/api/undo', {});
    } else if (!e.ctrlKey && !e.metaKey && !e.altKey) {
      if (e.key === 'f') toggleFull();
      else if (e.key === 'r') replay();
      else if (e.key === 'h') document.body.classList.toggle('present');
      else if (/^[1-4]$/.test(e.key)) document.querySelectorAll('#tray .marker')[Number(e.key) - 1]?.click();
      else if (e.key === 'e') document.querySelector('#tray .eraser')?.click();
    }
  });
  // Catch up instantly when nobody can see the animation, so the board is
  // never behind when the viewer comes back.
  function catchUp() {
    if (current) {
      const cur = current;
      current = null;
      clear(ctx.live);
      if (cur.kind === 'draw') inkAppend(cur.el);
      hideEraser();
    }
    if (!fontsReady) return;
    const was = speed;
    speed = 0;
    let guard = 0;
    while (queue.length && guard++ < 5000) startItem(queue.shift(), performance.now());
    speed = was;
    current = null;
    updateEmptyHint();
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) catchUp();
  });
  // Some embedded or covered windows stop animation frames without reporting
  // themselves as hidden: if frames stall, catch up from a timer instead.
  setInterval(() => {
    if (performance.now() - lastFrame > 400 && (current || queue.length)) catchUp();
  }, 400);

  // ------------------------------------------------------------ start
  let resizeTimer = null;
  const queueResize = () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(resize, 60);
  };
  window.addEventListener('resize', queueResize);
  if (window.ResizeObserver) new ResizeObserver(queueResize).observe($('wall'));
  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }
  async function loadFonts() {
    const specs = ['400 30px Kalam', '700 30px Kalam', '300 30px Kalam', '400 30px "Patrick Hand"', '400 30px "Permanent Marker"', '500 30px Caveat', '700 30px Caveat'];
    try {
      await Promise.race([Promise.all(specs.map((s) => document.fonts.load(s))), sleep(3500)]);
    } catch (e) { /* fall back to system handwriting fonts */ }
    fontsReady = true;
    fontGen += 1;
    resize();
    reportLayout([...model.els.keys()]);
  }
  resize();
  connect();
  loadFonts();
  requestAnimationFrame(frame);

  WB.app = { model: () => model, view: () => view, replay, renderBoard, setSpeed };
})();
