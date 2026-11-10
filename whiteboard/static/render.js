/* AI Whiteboard renderer.
 *
 * compile(element, env) turns a board element into a "drawing": a list of
 * ops (marker strokes, fills, handwriting, emoji, images) plus its bbox.
 * drawDrawing(ctx, drawing, p) paints it, only partly when p < 1, which is
 * how the marker-drawing animation works. Every element gets its own seeded
 * random generator, so its hand-drawn wobble is identical on every render.
 */
(function () {
  'use strict';
  const WB = (window.WB = window.WB || {});
  const TAU = Math.PI * 2;

  // ------------------------------------------------------------------ colours
  const MARKER = {
    black: '#25282c', ink: '#25282c', blue: '#1e5fc2', red: '#d6392c', green: '#239a4b',
    orange: '#ec7a18', purple: '#7b3fb6', brown: '#8a5a2e', pink: '#de4b8c', teal: '#128f8b',
    gray: '#6c727a', grey: '#6c727a', yellow: '#e8b10a', navy: '#1f3a6e', white: '#ffffff',
    lightblue: '#5aa9f0', gold: '#e0a800', cyan: '#1aa6c4', magenta: '#c43aa6', lime: '#6fb51f',
  };
  const PAPER = {
    yellow: '#fff1a1', pink: '#ffd2e1', blue: '#cfe5ff', green: '#d5f3c9', orange: '#ffdab0',
    purple: '#e6d8ff', white: '#fbfbf7', gray: '#e9eaec', grey: '#e9eaec',
  };
  const PAPER_ACCENT = {
    yellow: 'orange', pink: 'red', blue: 'blue', green: 'green', orange: 'brown', purple: 'purple',
    white: 'blue', gray: 'black', grey: 'black',
  };
  const PALETTE = ['blue', 'orange', 'green', 'red', 'purple', 'teal', 'pink', 'brown', 'yellow', 'gray'];
  const PAPER_INK = '#2a2b2e';

  const _cc = document.createElement('canvas').getContext('2d');
  const _rgbCache = new Map();
  function toRGB(css) {
    if (_rgbCache.has(css)) return _rgbCache.get(css);
    _cc.fillStyle = '#000';
    _cc.fillStyle = String(css);
    const v = _cc.fillStyle;
    let out;
    if (v[0] === '#') {
      out = [parseInt(v.slice(1, 3), 16), parseInt(v.slice(3, 5), 16), parseInt(v.slice(5, 7), 16), 1];
    } else {
      const m = v.match(/[\d.]+/g) || [0, 0, 0, 1];
      out = [+m[0], +m[1], +m[2], m[3] != null ? +m[3] : 1];
    }
    _rgbCache.set(css, out);
    return out;
  }
  function rgba(css, a) {
    const c = toRGB(css);
    return `rgba(${c[0]},${c[1]},${c[2]},${a})`;
  }
  function mix(c1, c2, t) {
    const a = toRGB(c1), b = toRGB(c2);
    const h = (i) => Math.round(a[i] + (b[i] - a[i]) * t).toString(16).padStart(2, '0');
    return '#' + h(0) + h(1) + h(2);
  }

  const THEMES = {
    whiteboard: { surface: '#fbfbf9', dark: false },
    grid: { surface: '#fbfbf9', dark: false },
    dots: { surface: '#fbfbf9', dark: false },
    chalkboard: { surface: '#2e4a3d', dark: true },
    blueprint: { surface: '#1f4e8c', dark: true },
  };
  function makeTheme(name) {
    const t = THEMES[name] || THEMES.whiteboard;
    return { name: name || 'whiteboard', dark: t.dark, surface: t.surface, ink: t.dark ? '#f2efe4' : MARKER.black };
  }
  function color(name, theme, fallback) {
    if (name == null || name === '' || name === true) name = fallback;
    if (name == null) return theme.ink;
    const key = String(name).toLowerCase().trim();
    if (key === 'black' || key === 'ink') return theme.ink;
    let c = MARKER[key] || String(name);
    if (theme.dark && key !== 'white') c = mix(c, '#ffffff', 0.38);
    return c;
  }

  // ------------------------------------------------------------------ random
  function hashStr(s) {
    let h = 2166136261 >>> 0;
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  }
  function rng(seed) {
    let a = seed >>> 0;
    return function () {
      a = (a + 0x6d2b79f5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  const J = (r, a) => (r() * 2 - 1) * a;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const num = (v, d) => (v == null || v === '' || isNaN(+v) ? d : +v);

  // ------------------------------------------------------------------ geometry
  function quadPts(a, c, b, n) {
    const out = [];
    for (let i = 0; i <= n; i++) {
      const t = i / n, u = 1 - t;
      out.push([u * u * a[0] + 2 * u * t * c[0] + t * t * b[0], u * u * a[1] + 2 * u * t * c[1] + t * t * b[1]]);
    }
    return out;
  }
  function cubicPts(a, c1, c2, b, n) {
    const out = [];
    for (let i = 0; i <= n; i++) {
      const t = i / n, u = 1 - t;
      out.push([
        u * u * u * a[0] + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t * t * t * b[0],
        u * u * u * a[1] + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t * t * t * b[1],
      ]);
    }
    return out;
  }
  function ellipsePts(cx, cy, rx, ry, n, a0) {
    n = n || Math.max(24, Math.ceil((rx + ry) * 0.35));
    a0 = a0 || 0;
    const out = [];
    for (let i = 0; i < n; i++) {
      const a = a0 + (i / n) * TAU;
      out.push([cx + Math.cos(a) * rx, cy + Math.sin(a) * ry]);
    }
    return out;
  }
  function roundRectPts(x, y, w, h, rad) {
    rad = Math.max(0, Math.min(rad || 0, w / 2, h / 2));
    if (rad < 1) return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]];
    const out = [];
    const corner = (cx, cy, a0) => {
      for (let i = 0; i <= 6; i++) {
        const a = a0 + (i / 6) * (Math.PI / 2);
        out.push([cx + Math.cos(a) * rad, cy + Math.sin(a) * rad]);
      }
    };
    corner(x + w - rad, y + rad, -Math.PI / 2);
    corner(x + w - rad, y + h - rad, 0);
    corner(x + rad, y + h - rad, Math.PI / 2);
    corner(x + rad, y + rad, Math.PI);
    return out;
  }
  function starPts(cx, cy, rx, ry, points, inner) {
    const out = [];
    const n = points || 5;
    for (let i = 0; i < n * 2; i++) {
      const a = -Math.PI / 2 + (i * Math.PI) / n;
      const k = i % 2 ? inner || 0.45 : 1;
      out.push([cx + Math.cos(a) * rx * k, cy + Math.sin(a) * ry * k]);
    }
    return out;
  }
  function cloudPts(r, x, y, w, h) {
    const cx = x + w / 2, cy = y + h / 2, rx = w / 2, ry = h / 2;
    const n = Math.max(7, Math.round((Math.PI * (rx + ry)) / 75));
    const out = [];
    const a0 = -Math.PI / 2 + J(r, 0.3);
    for (let i = 0; i < n; i++) {
      const a1 = a0 + (i / n) * TAU, a2 = a0 + ((i + 1) / n) * TAU, am = (a1 + a2) / 2;
      const p1 = [cx + Math.cos(a1) * rx * 0.84, cy + Math.sin(a1) * ry * 0.8];
      const p2 = [cx + Math.cos(a2) * rx * 0.84, cy + Math.sin(a2) * ry * 0.8];
      const k = 1.16 + J(r, 0.05);
      const c = [cx + Math.cos(am) * rx * k, cy + Math.sin(am) * ry * k];
      const seg = quadPts(p1, c, p2, 8);
      if (i) seg.shift();
      out.push(...seg);
    }
    return out;
  }
  function polyBBox(pts) {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const p of pts) {
      if (p[0] < x0) x0 = p[0];
      if (p[1] < y0) y0 = p[1];
      if (p[0] > x1) x1 = p[0];
      if (p[1] > y1) y1 = p[1];
    }
    return [x0, y0, x1 - x0, y1 - y0];
  }
  function unionBox(boxes) {
    const bs = boxes.filter(Boolean);
    if (!bs.length) return [0, 0, 0, 0];
    const x0 = Math.min(...bs.map((b) => b[0])), y0 = Math.min(...bs.map((b) => b[1]));
    const x1 = Math.max(...bs.map((b) => b[0] + b[2])), y1 = Math.max(...bs.map((b) => b[1] + b[3]));
    return [x0, y0, x1 - x0, y1 - y0];
  }
  function shift(pts, dx, dy) {
    return pts.map((p) => [p[0] + dx, p[1] + dy]);
  }
  function pathLength(pts) {
    let s = 0;
    for (let i = 1; i < pts.length; i++) s += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
    return s;
  }
  function pointAt(pts, frac) {
    const total = pathLength(pts) * frac;
    let s = 0;
    for (let i = 1; i < pts.length; i++) {
      const d = Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
      if (s + d >= total) {
        const t = d ? (total - s) / d : 0;
        return [pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * t, pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * t, i];
      }
      s += d;
    }
    const last = pts[pts.length - 1];
    return [last[0], last[1], pts.length - 1];
  }
  function spline(pts, step) {
    if (pts.length < 3) return pts.slice();
    step = step || 6;
    const out = [pts[0]];
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[Math.max(0, i - 1)], p1 = pts[i], p2 = pts[i + 1], p3 = pts[Math.min(pts.length - 1, i + 2)];
      const n = Math.max(2, Math.ceil(Math.hypot(p2[0] - p1[0], p2[1] - p1[1]) / step));
      for (let s = 1; s <= n; s++) {
        const t = s / n, t2 = t * t, t3 = t2 * t;
        out.push([
          0.5 * (2 * p1[0] + (-p0[0] + p2[0]) * t + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3),
          0.5 * (2 * p1[1] + (-p0[1] + p2[1]) * t + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3),
        ]);
      }
    }
    return out;
  }
  // Point on a box edge, heading from its centre towards (tx, ty)
  function boxEdgePoint(b, tx, ty, gap) {
    const cx = b[0] + b[2] / 2, cy = b[1] + b[3] / 2;
    const hw = b[2] / 2 + gap, hh = b[3] / 2 + gap;
    const dx = tx - cx, dy = ty - cy;
    if (!dx && !dy) return [cx, cy];
    const k = Math.min(dx ? hw / Math.abs(dx) : Infinity, dy ? hh / Math.abs(dy) : Infinity);
    return [cx + dx * k, cy + dy * k];
  }

  // ------------------------------------------------------------------ hand-drawn strokes
  // A line drawn by hand: a slight bow plus a gentle tremor.
  function roughLine(r, x1, y1, x2, y2, R) {
    R = R == null ? 1 : R;
    const len = Math.hypot(x2 - x1, y2 - y1);
    if (len < 1 || R === 0) return [[x1, y1], [x2, y2]];
    const nx = -(y2 - y1) / len, ny = (x2 - x1) / len;
    const bow = J(r, Math.min(len * 0.012, 4)) * R;
    const n = Math.max(2, Math.ceil(len / 8));
    const p1 = r() * TAU, p2 = r() * TAU, amp = 0.45 * R;
    const out = [];
    for (let i = 0; i <= n; i++) {
      const t = i / n;
      const along = t * len;
      const b = 4 * t * (1 - t) * bow + amp * (Math.sin(along / 23 + p1) * 0.65 + Math.sin(along / 9 + p2) * 0.35) * Math.min(1, 4 * t * (1 - t) * 4);
      out.push([x1 + (x2 - x1) * t + nx * b, y1 + (y2 - y1) * t + ny * b]);
    }
    return out;
  }
  // A polygon drawn in one pen motion; closed ones overshoot the start a little.
  function roughPoly(r, pts, closed, R) {
    R = R == null ? 1 : R;
    const v = pts.map((p) => [p[0] + J(r, 1.3 * R), p[1] + J(r, 1.3 * R)]);
    const out = [];
    const n = closed ? v.length : v.length - 1;
    for (let i = 0; i < n; i++) {
      const a = v[i], b = v[(i + 1) % v.length];
      const seg = roughLine(r, a[0], a[1], b[0], b[1], R);
      if (i) seg.shift();
      out.push(...seg);
    }
    if (closed && R > 0 && v.length > 1) {
      const a = v[0], b = v[1];
      const len = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
      const k = Math.min(9, len * 0.15) / len;
      out.push([a[0] + (b[0] - a[0]) * k + J(r, 1), a[1] + (b[1] - a[1]) * k + J(r, 1)]);
    }
    return out;
  }
  // A hand-drawn ellipse: starts anywhere, drifts slightly and overlaps its start.
  function roughEllipse(r, cx, cy, rx, ry, R) {
    R = R == null ? 1 : R;
    const a0 = r() * TAU;
    const over = R > 0 ? (0.16 + r() * 0.3) * Math.min(1, R) : 0;
    const n = Math.max(20, Math.ceil((Math.PI * (rx + ry)) / 6));
    const drift = J(r, 0.035) * R, wob = 0.012 * R;
    const p1 = r() * TAU, p2 = r() * TAU;
    const out = [];
    for (let i = 0; i <= n; i++) {
      const t = i / n, a = a0 + (TAU + over) * t;
      const k = 1 + drift * (t - 0.5) + wob * Math.sin(a * 2 + p1) + wob * 0.6 * Math.sin(a * 3 + p2);
      out.push([cx + Math.cos(a) * rx * k, cy + Math.sin(a) * ry * k]);
    }
    return out;
  }
  // Gentle wobble along an existing (dense) outline.
  function shake(r, pts, amp, closed) {
    if (!amp) return closed ? pts.concat([pts[0]]) : pts.slice();
    const p1 = r() * TAU, p2 = r() * TAU, p3 = r() * TAU;
    let s = 0;
    const out = pts.map((p, i) => {
      if (i) s += Math.hypot(p[0] - pts[i - 1][0], p[1] - pts[i - 1][1]);
      return [p[0] + amp * Math.sin(s / 19 + p1) + amp * 0.5 * Math.sin(s / 7 + p3), p[1] + amp * Math.sin(s / 17 + p2)];
    });
    if (closed && out.length > 1) {
      out.push([out[0][0] + J(r, amp), out[0][1] + J(r, amp)]);
      out.push([out[1][0] + J(r, amp), out[1][1] + J(r, amp)]);
    }
    return out;
  }
  // Arrow head as one "V" pen motion at the end of a path.
  function arrowHead(r, pts, size, atStart, R) {
    const tip = atStart ? pts[0] : pts[pts.length - 1];
    let back = tip;
    const seq = atStart ? pts : pts.slice().reverse();
    let acc = 0;
    for (let i = 1; i < seq.length; i++) {
      acc += Math.hypot(seq[i][0] - seq[i - 1][0], seq[i][1] - seq[i - 1][1]);
      back = seq[i];
      if (acc >= size * 1.2) break;
    }
    const ang = Math.atan2(tip[1] - back[1], tip[0] - back[0]);
    const spread = 0.48 + J(r, 0.05) * (R == null ? 1 : R);
    const a = [tip[0] - Math.cos(ang - spread) * size, tip[1] - Math.sin(ang - spread) * size];
    const b = [tip[0] - Math.cos(ang + spread) * size * (1 + J(r, 0.08)), tip[1] - Math.sin(ang + spread) * size * (1 + J(r, 0.08))];
    return { pts: [a, tip, b], ang };
  }
  // Diagonal scribble lines that fill a polygon (clipped to it while drawn).
  function hachure(c, poly, col, o) {
    const b = polyBBox(poly);
    const gap = o.gap || 9;
    const ang = (o.angle != null ? o.angle : -41) * (Math.PI / 180);
    const cx = b[0] + b[2] / 2, cy = b[1] + b[3] / 2;
    const diag = Math.hypot(b[2], b[3]) / 2 + gap;
    const dx = Math.cos(ang), dy = Math.sin(ang), nx = -dy, ny = dx;
    const ops = [];
    let flip = false;
    for (let off = -diag; off <= diag; off += gap) {
      let ax = cx + nx * off - dx * diag, ay = cy + ny * off - dy * diag;
      let bx = cx + nx * off + dx * diag, by = cy + ny * off + dy * diag;
      if (flip) [ax, ay, bx, by] = [bx, by, ax, ay];
      flip = !flip;
      ops.push(S(roughLine(c.r, ax, ay, bx, by, c.R * 0.5), col, o.width || 2, { alpha: 0.75, clip: poly }));
    }
    return ops;
  }

  // ------------------------------------------------------------------ ops
  function S(pts, col, w, o) {
    o = o || {};
    return { k: 'stroke', pts, color: col, w: w || 3, alpha: o.alpha != null ? o.alpha : 0.93, dash: o.dash, clip: o.clip, comp: o.comp, cap: o.cap };
  }
  function F(polys, fill, o) {
    o = o || {};
    if (polys.length && typeof polys[0][0] === 'number') polys = [polys];
    return { k: 'fill', polys, fill, alpha: o.alpha != null ? o.alpha : 1, shadow: o.shadow, comp: o.comp, rule: o.rule || 'nonzero', fast: o.fast, clip: o.clip };
  }

  const EMOJI_FONT = '"Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji", sans-serif';
  const FONT = {
    hand: 'Kalam, "Segoe Print", "Ink Free", "Comic Sans MS", cursive',
    marker: '"Permanent Marker", Kalam, "Segoe Print", cursive',
    neat: '"Patrick Hand", Kalam, "Segoe Print", cursive',
    script: 'Caveat, "Segoe Script", "Ink Free", cursive',
    mono: '"Cascadia Mono", "Cascadia Code", Consolas, "Courier New", monospace',
    sans: 'system-ui, "Segoe UI", Roboto, sans-serif',
  };
  function font(size, family, bold) {
    return `${bold ? 700 : 400} ${Math.round(size * 10) / 10}px ${FONT[family] || FONT.hand}`;
  }
  const M = document.createElement('canvas').getContext('2d');
  function measure(text, f) {
    M.font = f;
    return M.measureText(text).width;
  }

  function polyPath(ctx, pts, closed) {
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
    if (closed) ctx.closePath();
  }
  function prepStroke(op) {
    if (op.cum) return;
    const c = [0];
    for (let i = 1; i < op.pts.length; i++) {
      c.push(c[i - 1] + Math.hypot(op.pts[i][0] - op.pts[i - 1][0], op.pts[i][1] - op.pts[i - 1][1]));
    }
    op.cum = c;
    op.len = c[c.length - 1];
  }
  function scaleOf(ctx) {
    const m = ctx.getTransform();
    return Math.hypot(m.a, m.b) || 1;
  }
  function drawStroke(ctx, op, p) {
    const pts = op.pts;
    if (!pts || !pts.length || p <= 0) return null;
    prepStroke(op);
    ctx.save();
    if (op.clip) {
      ctx.beginPath();
      polyPath(ctx, op.clip, true);
      ctx.clip();
    }
    if (op.comp) ctx.globalCompositeOperation = op.comp;
    ctx.globalAlpha *= op.alpha;
    ctx.strokeStyle = op.color;
    ctx.fillStyle = op.color;
    ctx.lineWidth = op.w;
    ctx.lineCap = op.cap || 'round';
    ctx.lineJoin = 'round';
    if (op.dash) ctx.setLineDash(op.dash);
    let tip = pts[0];
    if (op.len < 0.5) {
      ctx.beginPath();
      ctx.arc(pts[0][0], pts[0][1], op.w / 2, 0, TAU);
      ctx.fill();
    } else {
      const target = p >= 1 ? op.len : op.len * p;
      ctx.beginPath();
      ctx.moveTo(pts[0][0], pts[0][1]);
      for (let i = 1; i < pts.length; i++) {
        if (op.cum[i] <= target) {
          ctx.lineTo(pts[i][0], pts[i][1]);
          tip = pts[i];
        } else {
          const seg = op.cum[i] - op.cum[i - 1];
          const t = seg > 0 ? (target - op.cum[i - 1]) / seg : 0;
          tip = [pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * t, pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * t];
          ctx.lineTo(tip[0], tip[1]);
          break;
        }
      }
      ctx.stroke();
    }
    ctx.restore();
    return tip;
  }
  function drawFill(ctx, op, p) {
    if (p <= 0) return null;
    ctx.save();
    if (op.clip) {
      ctx.beginPath();
      polyPath(ctx, op.clip, true);
      ctx.clip();
    }
    if (op.comp) ctx.globalCompositeOperation = op.comp;
    ctx.globalAlpha *= op.alpha * Math.min(1, p);
    if (op.shadow) {
      const s = scaleOf(ctx);
      ctx.shadowColor = op.shadow.color;
      ctx.shadowBlur = op.shadow.blur * s;
      ctx.shadowOffsetX = op.shadow.x * s;
      ctx.shadowOffsetY = op.shadow.y * s;
    }
    ctx.fillStyle = typeof op.fill === 'function' ? op.fill(ctx) : op.fill;
    ctx.beginPath();
    for (const poly of op.polys) if (poly.length > 1) polyPath(ctx, poly, true);
    ctx.fill(op.rule);
    ctx.restore();
    return null;
  }
  function drawText(ctx, op, p) {
    const show = p >= 1 ? op.n : Math.floor(op.n * p);
    if (show <= 0) return null;
    ctx.save();
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    ctx.globalAlpha *= op.alpha != null ? op.alpha : 0.96;
    let left = show, tip = null;
    for (const s of op.segs) {
      if (left <= 0) break;
      const str = left >= s.chars.length ? s.t : s.chars.slice(0, left).join('');
      left -= s.chars.length;
      ctx.font = s.font;
      ctx.fillStyle = s.color;
      ctx.fillText(str, s.x, s.y);
      if (p < 1) tip = [s.x + ctx.measureText(str).width, s.y + s.size * 0.3];
    }
    ctx.restore();
    return tip;
  }
  function easeOutBack(t) {
    const c1 = 1.70158, c3 = c1 + 1;
    return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2);
  }
  function drawEmoji(ctx, op, p) {
    if (p <= 0) return null;
    const s = p >= 1 ? 1 : Math.max(0.01, easeOutBack(p));
    ctx.save();
    ctx.translate(op.x, op.y);
    ctx.scale(s, s);
    ctx.font = `${op.size}px ${EMOJI_FONT}`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(op.t, 0, op.size * 0.04);
    ctx.restore();
    return null;
  }

  // Images load asynchronously; WB.onAsset is called when one arrives.
  const IMAGES = new Map();
  function getImage(src) {
    let rec = IMAGES.get(src);
    if (rec) return rec;
    rec = { img: new Image(), ok: false, failed: false, tainted: false };
    IMAGES.set(src, rec);
    const load = (cors) => {
      const img = new Image();
      if (cors && !src.startsWith('data:')) img.crossOrigin = 'anonymous';
      img.onload = () => {
        rec.img = img;
        rec.ok = true;
        rec.tainted = !cors && !src.startsWith('data:');
        if (WB.onAsset) WB.onAsset(src);
      };
      img.onerror = () => {
        if (cors && !src.startsWith('data:')) return load(false);
        rec.failed = true;
        if (WB.onAsset) WB.onAsset(src);
      };
      img.src = src;
    };
    load(true);
    return rec;
  }
  function drawImage(ctx, op, p, opts) {
    if (p <= 0) return null;
    const rec = getImage(op.src);
    ctx.save();
    ctx.globalAlpha *= Math.min(1, p);
    if (rec.ok && !(rec.tainted && opts && opts.export)) {
      ctx.drawImage(rec.img, op.x, op.y, op.w, op.h);
    } else {
      ctx.fillStyle = 'rgba(120,130,140,0.12)';
      ctx.fillRect(op.x, op.y, op.w, op.h);
      ctx.fillStyle = 'rgba(80,90,100,0.7)';
      ctx.font = font(18, 'hand');
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(rec.failed ? 'image failed to load' : rec.tainted ? '(image)' : 'loading image...', op.x + op.w / 2, op.y + op.h / 2);
    }
    ctx.restore();
    return null;
  }

  function drawOp(ctx, op, p, opts) {
    switch (op.k) {
      case 'stroke': return drawStroke(ctx, op, p);
      case 'fill': return drawFill(ctx, op, p);
      case 'text': return drawText(ctx, op, p);
      case 'emoji': return drawEmoji(ctx, op, p);
      case 'img': return drawImage(ctx, op, p, opts);
      case 'fx': return op.draw(ctx, p);
      default: return null;
    }
  }
  function opCost(op) {
    switch (op.k) {
      case 'stroke': prepStroke(op); return Math.max(35, op.len / 1.6);
      case 'fill': return op.fast ? 50 : 150;
      case 'text': return 40 + op.n * 24;
      case 'emoji': return 260;
      case 'img': return 380;
      default: return op.cost || 120;
    }
  }

  // ------------------------------------------------------------------ text layout
  // **bold** inside strings; a list of {text, color, bold} segments for colour.
  function parseRich(text) {
    const out = [];
    const push = (s, base) => {
      const parts = String(s).split(/(\*\*[^*]+?\*\*)/);
      for (const part of parts) {
        if (!part) continue;
        if (part.length > 4 && part.startsWith('**') && part.endsWith('**')) out.push(Object.assign({}, base, { t: part.slice(2, -2), bold: true }));
        else out.push(Object.assign({}, base, { t: part }));
      }
    };
    if (Array.isArray(text)) {
      for (const seg of text) {
        if (seg && typeof seg === 'object') push(seg.text == null ? '' : seg.text, { color: seg.color, bold: seg.bold });
        else push(seg == null ? '' : seg, {});
      }
    } else push(text == null ? '' : typeof text === 'object' ? JSON.stringify(text) : text, {});
    return out;
  }
  function layoutText(text, o) {
    const size = o.size || 28;
    const family = o.family || 'hand';
    const lh = size * (o.lineHeight || 1.25);
    const maxW = o.maxWidth > 0 ? o.maxWidth : 0;
    const lines = [[]];
    let lw = 0;
    const W = (t, bold) => measure(t, font(size, family, bold));
    const trimEnd = (line) => {
      while (line.length && /^[ \t]+$/.test(line[line.length - 1].t)) lw -= line.pop().w;
    };
    const newLine = () => {
      trimEnd(lines[lines.length - 1]);
      lines.push([]);
      lw = 0;
    };
    for (const run of parseRich(text)) {
      const bold = !!(run.bold || o.bold);
      for (let tok of run.t.split(/(\n|[ \t]+)/)) {
        if (!tok) continue;
        if (tok === '\n') {
          newLine();
          continue;
        }
        const space = /^[ \t]+$/.test(tok);
        if (space && lw === 0 && lines.length > 1) continue;
        let w = W(tok, bold);
        if (!space && maxW && lw > 0 && lw + w > maxW) newLine();
        if (!space && maxW && w > maxW) {
          let chunk = '';
          for (const ch of Array.from(tok)) {
            if (lw + W(chunk + ch, bold) > maxW && chunk) {
              lines[lines.length - 1].push({ t: chunk, w: W(chunk, bold), bold, color: run.color });
              newLine();
              chunk = ch;
            } else chunk += ch;
          }
          tok = chunk;
          w = W(tok, bold);
        }
        lines[lines.length - 1].push({ t: tok, w, bold, color: run.color });
        lw += w;
      }
    }
    trimEnd(lines[lines.length - 1]);
    const out = lines.map((toks) => {
      const runs = [];
      for (const t of toks) {
        const last = runs[runs.length - 1];
        if (last && last.bold === t.bold && last.color === t.color) {
          last.t += t.t;
          last.w += t.w;
        } else runs.push(Object.assign({}, t));
      }
      return { runs, width: runs.reduce((s, r) => s + r.w, 0) };
    });
    return { lines: out, width: Math.max(0, ...out.map((l) => l.width)), height: out.length * lh, lh, size, family };
  }
  // A text op from a layout. align: left | center | right (x is that edge/centre)
  function textOp(c, lay, x, y, align, col, o) {
    const segs = [];
    lay.lines.forEach((line, i) => {
      let lx = align === 'center' ? x - line.width / 2 : align === 'right' ? x - line.width : x;
      const ly = y + i * lay.lh + lay.lh / 2;
      for (const run of line.runs) {
        segs.push({ t: run.t, chars: Array.from(run.t), x: lx, y: ly, font: font(lay.size, lay.family, run.bold), color: run.color ? c.C(run.color) : col, size: lay.size });
        lx += run.w;
      }
    });
    return { k: 'text', segs, n: segs.reduce((s, g) => s + g.chars.length, 0), alpha: o && o.alpha };
  }
  function lineLefts(lay, x, align) {
    return lay.lines.map((l) => (align === 'center' ? x - l.width / 2 : align === 'right' ? x - l.width : x));
  }

  // ------------------------------------------------------------------ element compilers
  const COMPILERS = {};

  COMPILERS.text = function (el, c) {
    const size = num(el.size, 28);
    const lay = layoutText(el.text, { size, family: el.font, bold: el.bold, maxWidth: num(el.width, num(el.w, 0)), lineHeight: el.line_height });
    const align = el.align || 'left';
    const x = num(el.x, 0), y = num(el.y, 0);
    const col = c.C(el.color, 'black');
    const ops = [];
    const lefts = lineLefts(lay, x, align);
    if (el.highlight) {
      const hc = c.C(el.highlight === true ? 'yellow' : el.highlight);
      lay.lines.forEach((l, i) => {
        if (!l.width) return;
        const cy = y + i * lay.lh + lay.lh / 2 + size * 0.06;
        ops.push(S(roughLine(c.r, lefts[i] - size * 0.15, cy, lefts[i] + l.width + size * 0.15, cy + J(c.r, 2), c.R * 0.5), hc, size * 0.95, highlighterStyle(c)));
      });
    }
    ops.push(textOp(c, lay, x, y, align, col));
    if (el.underline) {
      const uc = el.underline === true ? col : c.C(el.underline);
      lay.lines.forEach((l, i) => {
        if (!l.width) return;
        const uy = y + i * lay.lh + lay.lh / 2 + size * 0.55;
        ops.push(S(roughLine(c.r, lefts[i] - 2, uy, lefts[i] + l.width + 4, uy + J(c.r, 2), c.R), uc, Math.max(2, size * 0.075)));
      });
    }
    const left = Math.min(...lefts, x);
    return { ops, bbox: [align === 'left' ? x : Math.min(...lefts), y, lay.width, lay.height + (el.underline ? size * 0.2 : 0)], _left: left };
  };
  function highlighterStyle(c) {
    return c.theme.dark ? { alpha: 0.28, cap: 'butt' } : { alpha: 0.42, comp: 'multiply', cap: 'butt' };
  }

  COMPILERS.title = function (el, c) {
    const size = num(el.size, 64);
    const align = el.align || 'center';
    const x = num(el.x, align === 'center' ? c.env.board.width / 2 : 40);
    const y = num(el.y, 24);
    const lay = layoutText(el.text, { size, family: el.font, bold: el.bold !== false, maxWidth: num(el.width, 0) });
    const col = c.C(el.color, 'blue');
    const ops = [textOp(c, lay, x, y, align, col)];
    const left = align === 'center' ? x - lay.width / 2 : align === 'right' ? x - lay.width : x;
    let h = lay.height;
    if (el.underline !== false) {
      const uc = c.C(el.underline === true || el.underline == null ? 'green' : el.underline);
      const x0 = left - size * 0.12, x1 = left + lay.width + size * 0.3;
      const uy = y + lay.height + size * 0.02;
      const pts = quadPts([x0, uy + size * 0.06], [(x0 + x1) / 2, uy + size * 0.2], [x1, uy - size * 0.14], 30);
      ops.push(S(shake(c.r, pts, 0.6 * c.R), uc, Math.max(3, size * 0.085)));
      h += size * 0.25;
    }
    return { ops, bbox: [left - size * 0.12, y, lay.width + size * 0.42, h] };
  };

  // ---- shapes
  function shapeGeometry(kind, el, c) {
    let x = num(el.x, 0), y = num(el.y, 0), w = num(el.w, kind === 'circle' ? 140 : 200), h = num(el.h, kind === 'circle' ? 140 : 120);
    if (kind === 'circle' && el.r != null) {
      const cx = num(el.cx, num(el.x, 0)), cy = num(el.cy, num(el.y, 0));
      w = h = el.r * 2;
      x = cx - el.r;
      y = cy - el.r;
    }
    const g = { x, y, w, h, inner: [x + w * 0.08, y + h * 0.1, w * 0.84, h * 0.8] };
    const cx = x + w / 2, cy = y + h / 2;
    switch (kind) {
      case 'rect': {
        const rad = num(el.radius, 0);
        g.poly = roundRectPts(x, y, w, h, rad);
        g.outline = rad >= 1 ? shake(c.r, g.poly, 0.7 * c.R, true) : roughPoly(c.r, g.poly, true, c.R);
        break;
      }
      case 'ellipse':
      case 'circle':
        g.poly = ellipsePts(cx, cy, w / 2, h / 2, 48);
        g.outline = roughEllipse(c.r, cx, cy, w / 2, h / 2, c.R);
        g.inner = [x + w * 0.16, y + h * 0.16, w * 0.68, h * 0.68];
        break;
      case 'diamond':
        g.poly = [[cx, y], [x + w, cy], [cx, y + h], [x, cy]];
        g.outline = roughPoly(c.r, g.poly, true, c.R);
        g.inner = [x + w * 0.24, y + h * 0.26, w * 0.52, h * 0.48];
        break;
      case 'triangle':
        g.poly = [[cx, y], [x + w, y + h], [x, y + h]];
        g.outline = roughPoly(c.r, g.poly, true, c.R);
        g.inner = [x + w * 0.26, y + h * 0.45, w * 0.48, h * 0.48];
        break;
      case 'hexagon':
        g.poly = [[x + w * 0.25, y], [x + w * 0.75, y], [x + w, cy], [x + w * 0.75, y + h], [x + w * 0.25, y + h], [x, cy]];
        g.outline = roughPoly(c.r, g.poly, true, c.R);
        g.inner = [x + w * 0.16, y + h * 0.12, w * 0.68, h * 0.76];
        break;
      case 'star': {
        g.poly = starPts(cx, y + h * 0.53, w / 2, h * 0.53, num(el.points, 5), 0.45);
        g.outline = roughPoly(c.r, g.poly, true, c.R);
        g.inner = [x + w * 0.3, y + h * 0.35, w * 0.4, h * 0.36];
        break;
      }
      case 'parallelogram': {
        const sk = Math.min(w * 0.22, h * 0.6);
        g.poly = [[x + sk, y], [x + w, y], [x + w - sk, y + h], [x, y + h]];
        g.outline = roughPoly(c.r, g.poly, true, c.R);
        g.inner = [x + sk * 0.8, y + h * 0.1, w - sk * 1.6, h * 0.8];
        break;
      }
      case 'cloud':
        g.poly = cloudPts(c.r, x, y, w, h);
        g.outline = shake(c.r, g.poly, 0.7 * c.R, true);
        g.inner = [x + w * 0.17, y + h * 0.2, w * 0.66, h * 0.6];
        break;
      case 'cylinder': {
        const ry = Math.min(h * 0.16, w * 0.3);
        const arc = (cy0, a0, a1) => {
          const out = [];
          for (let i = 0; i <= 24; i++) {
            const a = a0 + ((a1 - a0) * i) / 24;
            out.push([cx + (Math.cos(a) * w) / 2, cy0 + Math.sin(a) * ry]);
          }
          return out;
        };
        const front = arc(y + h - ry, Math.PI, 0);
        g.poly = [[x, y + ry], ...front, ...arc(y + ry, 0, -Math.PI)];
        g.outline = null;
        g.extra = [
          roughEllipse(c.r, cx, y + ry, w / 2, ry, c.R * 0.8),
          roughLine(c.r, x, y + ry, x, y + h - ry, c.R),
          shake(c.r, front, 0.5 * c.R),
          roughLine(c.r, x + w, y + h - ry, x + w, y + ry, c.R),
        ];
        g.inner = [x + w * 0.1, y + ry * 2.2, w * 0.8, h - ry * 3.2];
        break;
      }
      default:
        g.poly = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]];
        g.outline = roughPoly(c.r, g.poly, true, c.R);
    }
    return g;
  }
  function fillOps(el, poly, c, col) {
    if (!el.fill) return [];
    const tint = el.fill === true || el.fill === 'tint';
    const fc = tint ? col : c.C(el.fill);
    const style = el.fill_style || 'soft';
    if (style === 'hachure') return hachure(c, poly, fc, { width: Math.max(1.5, num(el.width, 3) * 0.6), gap: num(el.hachure_gap, 9) });
    if (style === 'solid') return [F(poly, fc, { alpha: num(el.fill_alpha, 0.95) })];
    const off = 1.6 * c.R;
    return [F(shift(poly, J(c.r, off), J(c.r, off)), fc, { alpha: num(el.fill_alpha, tint ? 0.13 : 0.3) })];
  }
  function labelOps(el, c, box, defaultSize) {
    if (el.label == null || el.label === '') return [];
    const size = num(el.label_size, num(el.size, defaultSize || 24));
    const f = font(size, el.font, el.bold);
    const widest = Math.max(0, ...String(Array.isArray(el.label) ? el.label.map((s) => (s && s.text) || s).join('') : el.label).split(/\s+/).map((wd) => measure(wd, f)));
    const lay = layoutText(el.label, { size, family: el.font, bold: el.bold, maxWidth: Math.max(30, box[2], widest + 2) });
    const col = el.label_color ? c.C(el.label_color) : el.text_color ? c.C(el.text_color) : c.ink;
    return [textOp(c, lay, box[0] + box[2] / 2, box[1] + box[3] / 2 - lay.height / 2, 'center', col)];
  }
  function compileShape(el, c) {
    const kind = el.type;
    const g = shapeGeometry(kind, el, c);
    const col = c.C(el.color, 'black');
    const w = num(el.width, 3);
    const o = { dash: el.dash ? [w * 3.2, w * 2.6] : null };
    const ops = fillOps(el, g.poly, c, col);
    if (w > 0) {
      if (g.outline) ops.push(S(g.outline, col, w, o));
      if (g.extra) for (const pts of g.extra) ops.push(S(pts, col, w, o));
    }
    ops.push(...labelOps(el, c, g.inner));
    return { ops, bbox: [g.x - w / 2, g.y - w / 2, g.w + w, g.h + w], centered: kind === 'circle' && el.r != null };
  }
  for (const k of ['rect', 'ellipse', 'circle', 'diamond', 'triangle', 'hexagon', 'star', 'cloud', 'cylinder', 'parallelogram']) COMPILERS[k] = compileShape;

  // ---- buttons the user can click (wait_for_choice)
  COMPILERS.button = function (el, c) {
    const size = num(el.size, 28);
    const col = c.C(el.color, 'blue');
    const pad = size * 1.1;
    const lay = layoutText(el.text, { size, family: el.font, bold: true, maxWidth: el.w ? Math.max(30, num(el.w, 0) - pad * 2) : 520 });
    const x = num(el.x, 0), y = num(el.y, 0);
    const w = num(el.w, lay.width + pad * 2), h = num(el.h, lay.height + size * 0.9);
    const lw = el.chosen ? 4 : 3;
    const poly = roundRectPts(x, y, w, h, Math.min(h / 2, size * 0.7));
    const ops = [F(shift(poly, 3, 5), col, { alpha: el.chosen ? 0.18 : 0.3 })];
    if (el.chosen) ops.push(F(poly, col, { alpha: 0.95 }));
    else ops.push(F(poly, c.theme.surface, { alpha: 1 }), F(poly, col, { alpha: 0.1 }));
    ops.push(S(roughPoly(c.r, poly, true, c.R * 0.5), col, lw));
    const cy = y + h / 2;
    if (el.chosen) {
      const tx = x + size * 0.36;
      ops.push(S(roughPoly(c.r, [[tx, cy], [tx + size * 0.18, cy + size * 0.2], [tx + size * 0.48, cy - size * 0.26]], false, c.R * 0.4), '#ffffff', Math.max(3, size * 0.13), { alpha: 1 }));
    }
    ops.push(textOp(c, lay, x + w / 2 + (el.chosen ? size * 0.2 : 0), cy - lay.height / 2, 'center', el.chosen ? '#ffffff' : col));
    return { ops, bbox: [x - lw / 2, y - lw / 2, w + lw + 3, h + lw + 5] };
  };

  // ---- lines & arrows
  function headOps(c, pts, which, col, w, size, filled) {
    const ops = [];
    const ends = which === 'both' ? [true, false] : which === 'start' ? [true] : which === 'none' || which === false ? [] : [false];
    for (const atStart of ends) {
      const h = arrowHead(c.r, pts, size, atStart, c.R);
      if (filled) {
        ops.push(F([h.pts[0], h.pts[1], h.pts[2]], col, { alpha: 0.95, fast: true }));
      }
      ops.push(S(h.pts, col, w));
    }
    return ops;
  }
  COMPILERS.line = function (el, c) {
    let pts = (el.points || []).map((p) => [+p[0], +p[1]]);
    if (!pts.length) return { ops: [], bbox: [0, 0, 0, 0] };
    const col = c.C(el.color, 'black');
    const w = num(el.width, 4);
    let path;
    if (el.smooth) path = shake(c.r, spline(pts, 5), 0.3 * c.R);
    else if (pts.length === 1) path = pts;
    else path = roughPoly(c.r, pts, !!el.closed, c.R);
    const ops = [];
    if (el.fill && el.closed) ops.push(...fillOps(el, pts, c, col));
    ops.push(S(path, col, w, { dash: el.dash ? [w * 3, w * 2.5] : null, alpha: el.author === 'user' ? 0.95 : 0.93 }));
    const head = el.arrow === true ? 'end' : el.arrow;
    if (head && head !== 'none' && path.length > 1) ops.push(...headOps(c, path, head, col, w, Math.max(14, w * 3.6), el.head_style === 'filled'));
    const b = polyBBox(path);
    return { ops, bbox: [b[0] - w, b[1] - w, b[2] + 2 * w, b[3] + 2 * w] };
  };

  function endRef(v) {
    if (typeof v === 'string') return { id: v };
    if (v && !Array.isArray(v) && typeof v === 'object' && v.id) return v;
    return null;
  }
  function sidePoint(b, side, gap) {
    const cx = b[0] + b[2] / 2, cy = b[1] + b[3] / 2;
    switch (side) {
      case 'top': return [cx, b[1] - gap];
      case 'bottom': return [cx, b[1] + b[3] + gap];
      case 'left': return [b[0] - gap, cy];
      case 'right': return [b[0] + b[2] + gap, cy];
      default: return [cx, cy];
    }
  }
  COMPILERS.arrow = function (el, c) {
    const gap = num(el.gap, 8);
    const ra = endRef(el.from), rb = endRef(el.to);
    const ba = ra ? c.env.bboxOf(ra.id) : null, bb = rb ? c.env.bboxOf(rb.id) : null;
    if ((ra && !ba) || (rb && !bb)) return { ops: [], bbox: [0, 0, 0, 0], dynamic: true, missing: true };
    const centre = (b) => [b[0] + b[2] / 2, b[1] + b[3] / 2];
    const ca = ba ? centre(ba) : [+el.from[0], +el.from[1]];
    const cb = bb ? centre(bb) : [+el.to[0], +el.to[1]];
    let A = ba ? (ra.side ? sidePoint(ba, ra.side, gap) : boxEdgePoint(ba, cb[0], cb[1], gap)) : ca;
    let B = bb ? (rb.side ? sidePoint(bb, rb.side, gap) : boxEdgePoint(bb, ca[0], ca[1], gap)) : cb;
    const col = c.C(el.color, 'black');
    const w = num(el.width, 3.5);
    const route = el.route || (el.curve ? 'curve' : 'straight');
    const len = Math.hypot(B[0] - A[0], B[1] - A[1]) || 1;
    const ops = [];
    let path;
    if (el.style === 'block') {
      const t = num(el.thickness, 34);
      const ang = Math.atan2(B[1] - A[1], B[0] - A[0]);
      const hl = Math.min(len * 0.45, t * 1.4);
      const loc = (u, v) => [A[0] + Math.cos(ang) * u - Math.sin(ang) * v, A[1] + Math.sin(ang) * u + Math.cos(ang) * v];
      const poly = [loc(0, -t / 2), loc(len - hl, -t / 2), loc(len - hl, -t), loc(len, 0), loc(len - hl, t), loc(len - hl, t / 2), loc(0, t / 2)];
      ops.push(F(shift(poly, J(c.r, 2), J(c.r, 2)), el.fill ? c.C(el.fill) : col, { alpha: el.fill ? 0.35 : 0.14 }));
      ops.push(S(roughPoly(c.r, poly, true, c.R), col, w));
      const b = polyBBox(poly);
      return { ops: ops.concat(arrowLabel(el, c, poly.concat([A, B]), b)), bbox: b, dynamic: !!(ra || rb) };
    }
    if (route === 'elbow') {
      const sa = ra && ra.side, sb = rb && rb.side;
      const horiz = sa ? sa === 'left' || sa === 'right' : Math.abs(cb[0] - ca[0]) >= Math.abs(cb[1] - ca[1]);
      if (ba && !sa) A = horiz ? sidePoint(ba, cb[0] > ca[0] ? 'right' : 'left', gap) : sidePoint(ba, cb[1] > ca[1] ? 'bottom' : 'top', gap);
      if (bb && !sb) B = horiz ? sidePoint(bb, cb[0] > ca[0] ? 'left' : 'right', gap) : sidePoint(bb, cb[1] > ca[1] ? 'top' : 'bottom', gap);
      const pts = horiz
        ? [A, [(A[0] + B[0]) / 2, A[1]], [(A[0] + B[0]) / 2, B[1]], B]
        : [A, [A[0], (A[1] + B[1]) / 2], [B[0], (A[1] + B[1]) / 2], B];
      path = roughPoly(c.r, pts, false, c.R);
    } else if (route === 'curve') {
      const k = num(el.curve, 0.25);
      const nx = -(B[1] - A[1]) / len, ny = (B[0] - A[0]) / len;
      const ctrl = [(A[0] + B[0]) / 2 + nx * k * len, (A[1] + B[1]) / 2 + ny * k * len];
      path = shake(c.r, quadPts(A, ctrl, B, Math.max(12, Math.ceil(len / 10))), 0.5 * c.R);
    } else {
      path = roughLine(c.r, A[0], A[1], B[0], B[1], c.R);
    }
    ops.push(S(path, col, w, { dash: el.dash ? [w * 3, w * 2.5] : null }));
    ops.push(...headOps(c, path, el.head || 'end', col, w, Math.max(14, w * 4), el.head_style === 'filled'));
    const b = polyBBox(path);
    const bbox = [b[0] - 12, b[1] - 12, b[2] + 24, b[3] + 24];
    return { ops: ops.concat(arrowLabel(el, c, path, bbox)), bbox, dynamic: !!(ra || rb) };
  };
  function arrowLabel(el, c, path, bbox) {
    if (el.label == null || el.label === '') return [];
    const size = num(el.label_size, num(el.size, 22));
    const lay = layoutText(el.label, { size, family: el.font, maxWidth: 260 });
    const m = pointAt(path, 0.5);
    const pad = size * 0.25;
    const box = [m[0] - lay.width / 2 - pad, m[1] - lay.height / 2 - pad * 0.5, lay.width + pad * 2, lay.height + pad];
    const ops = [F(roundRectPts(box[0], box[1], box[2], box[3], 6), c.theme.surface, { alpha: 0.92, fast: true })];
    ops.push(textOp(c, lay, m[0], m[1] - lay.height / 2, 'center', el.label_color ? c.C(el.label_color) : c.C(el.color, 'black')));
    const u = unionBox([bbox, box]);
    bbox[0] = u[0]; bbox[1] = u[1]; bbox[2] = u[2]; bbox[3] = u[3];
    return ops;
  }

  COMPILERS.highlight = function (el, c) {
    const x = num(el.x, 0), y = num(el.y, 0), w = num(el.w, 300), h = num(el.h, 40);
    const col = c.C(el.color, 'yellow');
    const passes = Math.max(1, Math.round(h / 46));
    const lw = (h / passes) * 1.08;
    const ops = [];
    for (let i = 0; i < passes; i++) {
      const cy = y + (i + 0.5) * (h / passes);
      const ltr = i % 2 === 0;
      const xa = ltr ? x : x + w, xb = ltr ? x + w : x;
      ops.push(S(roughLine(c.r, xa, cy + J(c.r, 2), xb, cy + J(c.r, 2), c.R * 0.4), col, lw, highlighterStyle(c)));
    }
    return { ops, bbox: [x, y, w, h] };
  };

  COMPILERS.emoji = function (el, c) {
    const size = num(el.size, 64);
    const x = num(el.x, 0), y = num(el.y, 0);
    const t = String(el.text || el.emoji || el.char || '');
    return { ops: [{ k: 'emoji', t, x: x + size * 0.575, y: y + size * 0.575, size }], bbox: [x, y, size * 1.15, size * 1.15] };
  };

  COMPILERS.image = function (el, c) {
    const x = num(el.x, 0), y = num(el.y, 0);
    const rec = getImage(String(el.src));
    let w = num(el.w, 0), h = num(el.h, 0);
    const nat = rec.ok ? [rec.img.naturalWidth || 320, rec.img.naturalHeight || 220] : null;
    if (!w && !h) w = nat ? Math.min(nat[0], 420) : 320;
    if (w && !h) h = nat ? (w * nat[1]) / nat[0] : w * 0.68;
    if (h && !w) w = nat ? (h * nat[0]) / nat[1] : h * 1.47;
    const frame = el.frame || 'tape';
    const ops = [];
    let bbox = [x, y, w, h];
    if (frame === 'polaroid') {
      const p = Math.max(10, w * 0.05);
      const poly = [[x - p, y - p], [x + w + p, y - p], [x + w + p, y + h + p * 3.6], [x - p, y + h + p * 3.6]];
      ops.push(F(poly, '#ffffff', { shadow: { color: 'rgba(0,0,0,0.22)', blur: 10, x: 2, y: 5 } }));
      bbox = [x - p, y - p, w + 2 * p, h + p * 4.6];
      if (el.caption) {
        const lay = layoutText(el.caption, { size: Math.max(16, p * 1.5), family: el.font, maxWidth: w });
        ops.push({ k: 'img', src: String(el.src), x, y, w, h });
        ops.push(textOp(c, lay, x + w / 2, y + h + p * 0.5, 'center', PAPER_INK));
        return { ops, bbox, defaultRot: J(c.r, 2.5) };
      }
    } else if (frame !== 'none') {
      ops.push(F([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], 'rgba(0,0,0,0.0)', { shadow: { color: 'rgba(0,0,0,0.18)', blur: 8, x: 1, y: 3 }, fast: true }));
    }
    ops.push({ k: 'img', src: String(el.src), x, y, w, h });
    if (frame === 'tape') {
      for (const [tx, ang] of [[x + w * 0.12, -0.6], [x + w * 0.88, 0.6]]) ops.push(tapeOp(c, tx, y, Math.min(70, w * 0.3), ang));
    }
    return { ops, bbox, defaultRot: frame === 'polaroid' ? J(c.r, 2.5) : 0 };
  };
  function tapeOp(c, cx, cy, len, ang) {
    const th = len * 0.32;
    const a = ang + J(c.r, 0.12);
    const ca = Math.cos(a), sa = Math.sin(a);
    const pt = (u, v) => [cx + ca * u - sa * v, cy + sa * u + ca * v];
    const poly = [pt(-len / 2, -th / 2), pt(len / 2, -th / 2 + J(c.r, 1)), pt(len / 2 + J(c.r, 2), th / 2), pt(-len / 2, th / 2 + J(c.r, 1))];
    return F(poly, 'rgba(236,233,220,0.78)', { shadow: { color: 'rgba(0,0,0,0.12)', blur: 2, x: 0, y: 1 }, fast: true });
  }

  COMPILERS.path = function (el, c) {
    const subs = WB.parseSvgPath(String(el.d || ''));
    let s = num(el.scale, 1);
    const all = [].concat(...subs.map((sp) => sp.pts));
    let ox = num(el.x, 0), oy = num(el.y, 0);
    if (all.length && (el.w || el.h) && el.scale == null) {
      const b = polyBBox(all);
      s = Math.min(el.w ? el.w / (b[2] || 1) : Infinity, el.h ? el.h / (b[3] || 1) : Infinity);
      ox -= b[0] * s;
      oy -= b[1] * s;
    }
    const col = c.C(el.color, 'black');
    const w = num(el.width, 3);
    const ops = [];
    const tr = subs.map((sp) => ({ pts: sp.pts.map((p) => [ox + p[0] * s, oy + p[1] * s]), closed: sp.closed }));
    if (el.fill) {
      const closed = tr.filter((sp) => sp.closed || tr.length === 1).map((sp) => sp.pts);
      if (closed.length) ops.push(F(closed, c.C(el.fill), { alpha: num(el.fill_alpha, 0.85), rule: 'evenodd' }));
    }
    if (w > 0 && el.stroke !== false) {
      for (const sp of tr) {
        if (sp.pts.length < 2) continue;
        const pts = shake(c.r, sp.pts, 0.5 * c.R, false);
        if (sp.closed) pts.push(pts[0]);
        ops.push(S(pts, col, w, { dash: el.dash ? [w * 3, w * 2.5] : null }));
      }
    }
    const b = tr.length ? polyBBox([].concat(...tr.map((sp) => sp.pts))) : [ox, oy, 0, 0];
    return { ops, bbox: [b[0] - w / 2, b[1] - w / 2, b[2] + w, b[3] + w] };
  };

  // ---- panels, sticky notes, lists
  function bulletOps(c, kind, n, x, cy, size, col) {
    const ops = [];
    const r = c.r;
    switch (kind) {
      case 'none':
        break;
      case 'number': {
        const lay = layoutText(`${n}.`, { size, bold: true });
        ops.push(textOp(c, lay, x, cy - lay.lh / 2, 'left', col));
        break;
      }
      case 'check':
        ops.push(S(roughPoly(r, [[x + size * 0.05, cy - size * 0.02], [x + size * 0.3, cy + size * 0.25], [x + size * 0.72, cy - size * 0.35]], false, c.R * 0.5), col, Math.max(2.5, size * 0.11)));
        break;
      case 'box':
      case 'box_done': {
        const s = size * 0.6;
        ops.push(S(roughPoly(r, [[x, cy - s / 2], [x + s, cy - s / 2], [x + s, cy + s / 2], [x, cy + s / 2]], true, c.R * 0.6), col, Math.max(2, size * 0.075)));
        if (kind === 'box_done') ops.push(S(roughPoly(r, [[x + s * 0.12, cy - s * 0.02], [x + s * 0.4, cy + s * 0.32], [x + s * 1.05, cy - s * 0.62]], false, c.R * 0.4), c.C('green'), Math.max(2.5, size * 0.11)));
        break;
      }
      case 'arrow': {
        const pts = roughLine(r, x, cy, x + size * 0.62, cy, c.R * 0.4);
        ops.push(S(pts, col, Math.max(2, size * 0.08)));
        ops.push(...headOps(c, pts, 'end', col, Math.max(2, size * 0.08), size * 0.26));
        break;
      }
      case 'star':
        ops.push(F(starPts(x + size * 0.3, cy, size * 0.3, size * 0.3, 5, 0.45), col, { fast: true }));
        break;
      case 'dash':
        ops.push(S(roughLine(r, x + size * 0.05, cy, x + size * 0.5, cy, c.R * 0.4), col, Math.max(2, size * 0.09)));
        break;
      default: // dot
        ops.push(F(ellipsePts(x + size * 0.25, cy + size * 0.02, size * 0.13, size * 0.13, 14), col, { fast: true }));
    }
    return ops;
  }
  function itemsLayout(c, items, o, x0, y, width, depth, ops) {
    const size = o.size;
    let n = 0;
    for (const raw of items || []) {
      const it = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : { text: raw };
      n += 1;
      let kind = it.bullet || (depth > 0 && o.bullets === 'number' ? 'dot' : o.bullets);
      if (it.done && kind === 'box') kind = 'box_done';
      else if (it.done) kind = 'check';
      const indent = depth * size * 1.05;
      const bw = kind === 'none' ? 0 : kind === 'number' ? measure(`${n}.`, font(size, o.family, true)) + size * 0.35 : size * (kind === 'dot' || kind === 'star' || kind === 'dash' ? 0.85 : 1.08);
      const lay = layoutText(it.text, { size, family: o.family, bold: it.bold, maxWidth: Math.max(40, width - indent - bw) });
      const lx = x0 + indent + bw;
      const bcol = it.done ? c.C('green') : it.bullet_color ? c.C(it.bullet_color) : o.accent;
      ops.push(...bulletOps(c, kind, n, x0 + indent, y + lay.lh / 2, size, bcol));
      ops.push(textOp(c, lay, lx, y, 'left', it.color ? c.C(it.color) : o.ink));
      if (it.strike) {
        lay.lines.forEach((l, i) => {
          const sy = y + i * lay.lh + lay.lh / 2 + size * 0.05;
          ops.push(S(roughLine(c.r, lx - 3, sy, lx + l.width + 3, sy + J(c.r, 2), c.R), o.ink, Math.max(2, size * 0.07)));
        });
      }
      y += lay.height + size * 0.16;
      if (Array.isArray(it.items) && it.items.length) y = itemsLayout(c, it.items, o, x0, y, width, depth + 1, ops);
    }
    return y;
  }
  function compilePanel(el, c, sticky) {
    const style = sticky ? 'sticky' : el.style || 'box';
    const isSticky = style === 'sticky';
    const size = num(el.size, 26);
    const w = num(el.w, isSticky ? 240 : 360);
    const x = num(el.x, 0), y = num(el.y, 0);
    const paperName = isSticky ? String(el.paper || (sticky ? el.color : null) || 'yellow').toLowerCase() : null;
    const accentName = isSticky ? el.title_color || el.accent || (sticky ? null : el.color) || PAPER_ACCENT[paperName] || 'orange' : el.color || 'blue';
    const accent = isSticky ? color(accentName, makeTheme('whiteboard')) : c.C(accentName);
    const ink = el.text_color ? c.C(el.text_color) : isSticky || style === 'card' && !c.theme.dark ? PAPER_INK : c.ink;
    const pad = num(el.padding, size * 0.75);
    const inner = w - pad * 2;
    const content = [];
    let cy = y + pad * 0.85;
    let headerH = 0;
    if (el.title != null && el.title !== '') {
      const tsize = num(el.title_size, size * 1.18);
      const lay = layoutText(el.title, { size: tsize, family: el.title_font || el.font, bold: true, maxWidth: inner });
      const align = el.title_align || (style === 'cloud' ? 'center' : 'left');
      const tx = align === 'center' ? x + w / 2 : x + pad;
      if (style === 'card') {
        cy = y + pad * 0.5;
        content.push(textOp(c, lay, tx, cy, align, '#ffffff'));
        headerH = lay.height + pad;
        cy = y + headerH + pad * 0.7;
      } else {
        content.push(textOp(c, lay, tx, cy, align, accent));
        if (el.title_underline !== false) {
          const ux = align === 'center' ? tx - lay.width / 2 : tx;
          const uy = cy + lay.height + tsize * 0.02;
          content.push(S(roughLine(c.r, ux - 3, uy + J(c.r, 1.5), ux + lay.lines[lay.lines.length - 1].width + 6, uy + J(c.r, 2), c.R * 0.8), accent, Math.max(2, tsize * 0.075)));
        }
        cy += lay.height + size * 0.42;
      }
    }
    const family = el.font;
    if (el.text != null && el.text !== '') {
      const lay = layoutText(el.text, { size, family, maxWidth: inner });
      const align = el.text_align || (style === 'cloud' ? 'center' : 'left');
      content.push(textOp(c, lay, align === 'center' ? x + w / 2 : x + pad, cy, align, ink));
      cy += lay.height + size * 0.3;
    }
    const items = Array.isArray(el.items) ? el.items : null;
    if (items && items.length) {
      cy = itemsLayout(c, items, { size, family, bullets: el.bullets || (isSticky && !el.bullets ? 'dot' : 'dot'), accent: el.bullet_color ? c.C(el.bullet_color) : accent, ink }, x + pad, cy, inner, 0, content);
    }
    let h = Math.max(num(el.h, 0), cy - y + pad * 0.65, isSticky ? w * 0.72 : 0);
    const frame = [];
    let bbox = [x, y, w, h];
    if (isSticky) {
      const paper = PAPER[paperName] || c.C(paperName);
      const curl = Math.min(14, w * 0.05);
      const poly = [[x, y], [x + w, y], [x + w + J(c.r, 1), y + h - curl], [x + w - curl * 0.6, y + h], [x, y + h + J(c.r, 1)]];
      frame.push(F(poly, paper, { shadow: { color: 'rgba(40,30,0,0.28)', blur: 12, x: 2, y: 6 } }));
      frame.push(F(poly, (ctx) => {
        const g = ctx.createLinearGradient(x, y, x, y + h);
        g.addColorStop(0, 'rgba(255,255,255,0.18)');
        g.addColorStop(0.75, 'rgba(255,255,255,0)');
        g.addColorStop(1, 'rgba(120,90,0,0.08)');
        return g;
      }, { fast: true }));
      if (el.tape !== false) frame.push(tapeOp(c, x + w / 2 + J(c.r, w * 0.08), y + 2, Math.min(90, w * 0.38), J(c.r, 0.08)));
      bbox = [x - 2, y - 14, w + 6, h + 18];
    } else if (style === 'box' || style === 'dashed') {
      const col = accent;
      const rad = num(el.radius, 6);
      const poly = roundRectPts(x, y, w, h, rad);
      const fillCol = el.fill ? c.C(el.fill) : col;
      frame.push(F(shift(poly, J(c.r, 1.5), J(c.r, 1.5)), fillCol, { alpha: num(el.fill_alpha, el.fill ? 0.25 : 0.075) }));
      const bw = num(el.width, 3);
      if (bw > 0) frame.push(S(rad >= 2 ? shake(c.r, poly, 0.7 * c.R, true) : roughPoly(c.r, poly, true, c.R), col, bw, { dash: style === 'dashed' || el.dash ? [bw * 3.2, bw * 2.6] : null }));
    } else if (style === 'card') {
      const rad = num(el.radius, 14);
      const poly = roundRectPts(x, y, w, h, rad);
      frame.push(F(poly, c.theme.dark ? '#fbfbf7' : '#ffffff', { alpha: 0.97, shadow: { color: 'rgba(0,0,0,0.16)', blur: 10, x: 0, y: 4 } }));
      if (headerH) {
        const band = [];
        for (let i = 0; i <= 6; i++) {
          const a = Math.PI + (i / 6) * (Math.PI / 2);
          band.push([x + rad + Math.cos(a) * rad, y + rad + Math.sin(a) * rad]);
        }
        for (let i = 0; i <= 6; i++) {
          const a = -Math.PI / 2 + (i / 6) * (Math.PI / 2);
          band.push([x + w - rad + Math.cos(a) * rad, y + rad + Math.sin(a) * rad]);
        }
        band.push([x + w, y + headerH], [x, y + headerH]);
        frame.push(F(band, accent, { alpha: 0.95 }));
      }
      frame.push(S(shake(c.r, poly, 0.6 * c.R, true), accent, num(el.width, 3)));
    } else if (style === 'cloud') {
      const mx = w * 0.07, my = h * 0.12;
      const poly = cloudPts(c.r, x - mx, y - my, w + mx * 2, h + my * 2);
      frame.push(F(shift(poly, J(c.r, 1.5), J(c.r, 1.5)), el.fill ? c.C(el.fill) : accent, { alpha: num(el.fill_alpha, el.fill ? 0.25 : 0.08) }));
      frame.push(S(shake(c.r, poly, 0.7 * c.R, true), accent, num(el.width, 3)));
      bbox = [x - mx, y - my, w + mx * 2, h + my * 2];
    }
    return { ops: frame.concat(content), bbox, defaultRot: isSticky ? J(c.r, 2.2) : 0 };
  }
  COMPILERS.panel = (el, c) => compilePanel(el, c, false);
  COMPILERS.sticky = (el, c) => compilePanel(el, c, true);

  // ---- speech bubbles
  COMPILERS.bubble = function (el, c) {
    const size = num(el.size, 28);
    const kind = el.kind || 'speech';
    const lay = layoutText(el.text, { size, family: el.font, bold: el.bold, maxWidth: el.w ? el.w * 0.7 : 360 });
    const bw = num(el.w, Math.max(lay.width / 0.7 + size * 0.6, size * 4));
    const bh = num(el.h, Math.max(lay.height / 0.64 + size * 0.5, size * 2.4));
    const x = num(el.x, 0), y = num(el.y, 0);
    const cx = x + bw / 2, cy = y + bh / 2, rx = bw / 2, ry = bh / 2;
    let tail = Array.isArray(el.tail) ? [+el.tail[0], +el.tail[1]] : null;
    let dynamic = false;
    if (typeof el.tail === 'string') {
      dynamic = true;
      const b = c.env.bboxOf(el.tail);
      if (b) tail = boxEdgePoint(b, cx, cy, 10);
    }
    if (!tail) tail = [x + bw * 0.22, y + bh + size * 1.5];
    const col = c.C(el.color, 'black');
    const fill = el.fill ? c.C(el.fill) : c.theme.surface;
    const lw = num(el.width, 3);
    const ops = [];
    const at = Math.atan2((tail[1] - cy) / ry, (tail[0] - cx) / rx);
    let poly, extra = [];
    if (kind === 'thought') {
      poly = cloudPts(c.r, x, y, bw, bh);
      const edge = [cx + Math.cos(at) * rx * 0.95, cy + Math.sin(at) * ry * 0.95];
      for (let i = 0; i < 3; i++) {
        const t = 0.3 + i * 0.3;
        const px = edge[0] + (tail[0] - edge[0]) * t, py = edge[1] + (tail[1] - edge[1]) * t;
        const rr = size * (0.42 - i * 0.11);
        extra.push(ellipsePts(px, py, rr, rr * 0.85, 16));
      }
    } else {
      const n = kind === 'shout' ? 26 : 72;
      const pts = [];
      let inserted = false;
      for (let i = 0; i < n; i++) {
        const a = at + 0.24 + (i / n) * (TAU - 0.48);
        let k = 1;
        if (kind === 'shout') k = i % 2 ? 0.8 + J(c.r, 0.05) : 1.05 + J(c.r, 0.07);
        pts.push([cx + Math.cos(a) * rx * k, cy + Math.sin(a) * ry * k]);
      }
      pts.push(tail);
      inserted = true;
      void inserted;
      poly = pts;
    }
    ops.push(F(poly, fill, { alpha: 0.97, fast: true }));
    ops.push(S(kind === 'shout' ? roughPoly(c.r, poly, true, c.R * 0.6) : shake(c.r, poly, 0.7 * c.R, true), col, lw));
    for (const e of extra) {
      ops.push(F(e, fill, { alpha: 0.97, fast: true }));
      ops.push(S(shake(c.r, e, 0.4, true), col, lw * 0.8));
    }
    ops.push(textOp(c, lay, cx, cy - lay.height / 2, 'center', el.text_color ? c.C(el.text_color) : c.ink));
    const bbox = unionBox([[x - lw, y - lw, bw + 2 * lw, bh + 2 * lw], [tail[0] - 4, tail[1] - 4, 8, 8]]);
    return { ops, bbox, dynamic };
  };

  // ---- ribbon banner
  COMPILERS.banner = function (el, c) {
    const size = num(el.size, 40);
    const lay = layoutText(el.text, { size, family: el.font, bold: el.bold !== false });
    const tw = size * 1.1;
    const bandW = num(el.w, lay.width + size * 2);
    const bandH = size * 1.6;
    const arch = num(el.arch, bandH * 0.1);
    const x0 = num(el.x, 0) + tw * 0.7, y0 = num(el.y, 0) + arch;
    const drop = bandH * 0.35;
    const col = c.C(el.color, 'blue');
    const light = c.theme.dark ? rgba(col, 0.3) : mix(col, '#ffffff', 0.86);
    const mid = c.theme.dark ? rgba(col, 0.45) : mix(col, '#ffffff', 0.68);
    const dark = c.theme.dark ? rgba(col, 0.6) : mix(col, '#ffffff', 0.45);
    const lw = num(el.width, 3);
    const yAt = (u) => y0 - arch * 4 * u * (1 - u); // arched band: u in [0,1]
    const tailL = [[x0 + tw * 0.35, y0 + drop], [x0 - tw * 0.7, y0 + drop], [x0 - tw * 0.32, y0 + drop + bandH / 2], [x0 - tw * 0.7, y0 + drop + bandH], [x0 + tw * 0.35, y0 + drop + bandH]];
    const xr = x0 + bandW;
    const tailR = tailL.map((p) => [xr + (x0 - p[0]), p[1]]);
    const foldL = [[x0, y0 + bandH], [x0 + tw * 0.35, y0 + drop + bandH], [x0 + tw * 0.35, y0 + bandH * 0.9]];
    const foldR = foldL.map((p) => [xr + (x0 - p[0]), p[1]]);
    const top = [], bot = [];
    for (let i = 0; i <= 24; i++) {
      const u = i / 24;
      top.push([x0 + bandW * u, yAt(u)]);
      bot.push([x0 + bandW * u, yAt(u) + bandH]);
    }
    const band = top.concat(bot.reverse());
    const ops = [
      F(tailL, mid), F(tailR, mid),
      S(roughPoly(c.r, tailL, true, c.R * 0.7), col, lw), S(roughPoly(c.r, tailR, true, c.R * 0.7), col, lw),
      F(foldL, dark), F(foldR, dark),
      F(band, light),
      S(shake(c.r, band, 0.6 * c.R, true), col, lw),
    ];
    const tcol = el.text_color ? c.C(el.text_color) : c.theme.dark ? c.ink : mix(col, '#000000', 0.35);
    ops.push(textOp(c, lay, x0 + bandW / 2, y0 - arch * 0.9 + bandH / 2 - lay.height / 2, 'center', tcol));
    return { ops, bbox: [x0 - tw * 0.7, y0 - arch, bandW + tw * 1.4, bandH + drop + arch] };
  };

  // ---- table
  const NUMERIC = /^[-+]?[$€£¥]?\s?\d[\d,.]*\s?(%|[kKmMbB])?$/;
  COMPILERS.table = function (el, c) {
    const rows = (el.rows || []).map((r) => (Array.isArray(r) ? r : [r]));
    const size = num(el.size, 22);
    const ncols = Math.max(1, ...rows.map((r) => r.length));
    const padX = size * 0.6, padY = size * 0.38;
    const header = el.header !== false && rows.length > 1;
    const maxCol = num(el.max_col_width, 340);
    const cw = Array.isArray(el.col_widths) ? el.col_widths : [];
    const lays = rows.map((row, ri) => Array.from({ length: ncols }, (_, ci) =>
      layoutText(row[ci] == null ? '' : String(row[ci]), { size, family: el.font, bold: header && ri === 0, maxWidth: cw[ci] ? cw[ci] - 2 * padX : maxCol })));
    const colW = Array.from({ length: ncols }, (_, ci) => cw[ci] || Math.max(size * 2, ...lays.map((r) => r[ci].width)) + 2 * padX);
    const rowH = lays.map((r) => Math.max(...r.map((l) => l.height)) + 2 * padY);
    const x = num(el.x, 0), y = num(el.y, 0);
    const W = colW.reduce((a, b) => a + b, 0), H = rowH.reduce((a, b) => a + b, 0);
    const col = c.C(el.color, 'black');
    const hcol = c.C(el.header_color, 'blue');
    const ops = [];
    if (header) ops.push(F([[x, y], [x + W, y], [x + W, y + rowH[0]], [x, y + rowH[0]]], hcol, { alpha: 0.14 }));
    const lw = num(el.width, 2.5);
    ops.push(S(roughPoly(c.r, [[x, y], [x + W, y], [x + W, y + H], [x, y + H]], true, c.R), col, lw));
    let yy = y;
    for (let ri = 0; ri < rows.length - 1; ri++) {
      yy += rowH[ri];
      ops.push(S(roughLine(c.r, x, yy, x + W, yy, c.R), col, ri === 0 && header ? lw * 1.1 : lw * 0.75));
    }
    let xx = x;
    for (let ci = 0; ci < ncols - 1; ci++) {
      xx += colW[ci];
      ops.push(S(roughLine(c.r, xx, y, xx, y + H, c.R), col, lw * 0.75));
    }
    yy = y;
    rows.forEach((row, ri) => {
      let cx = x;
      for (let ci = 0; ci < ncols; ci++) {
        const lay = lays[ri][ci];
        const txt = row[ci] == null ? '' : String(row[ci]);
        const right = el.align === 'right' || (el.align == null && !(header && ri === 0) && NUMERIC.test(txt.trim()));
        const center = el.align === 'center';
        const tx = center ? cx + colW[ci] / 2 : right ? cx + colW[ci] - padX : cx + padX;
        ops.push(textOp(c, lay, tx, yy + padY, center ? 'center' : right ? 'right' : 'left', header && ri === 0 ? hcol : c.ink));
        cx += colW[ci];
      }
      yy += rowH[ri];
    });
    return { ops, bbox: [x - lw, y - lw, W + 2 * lw, H + 2 * lw] };
  };

  // ---- charts
  function niceStep(range, ticks) {
    const raw = range / Math.max(1, ticks);
    const mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
    const n = raw / mag;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * mag;
  }
  function fmtNum(v, unit) {
    const a = Math.abs(v);
    let s = a >= 1e9 ? (v / 1e9).toFixed(1).replace(/\.0$/, '') + 'B' : a >= 1e6 ? (v / 1e6).toFixed(1).replace(/\.0$/, '') + 'M' : a >= 1e4 ? (v / 1e3).toFixed(1).replace(/\.0$/, '') + 'k' : String(Math.round(v * 100) / 100);
    if (unit) s = unit === '$' || unit === '€' || unit === '£' ? unit + s : s + unit;
    return s;
  }
  function chartData(el) {
    let labels = [], series = [];
    if (Array.isArray(el.series) && el.series.length) {
      labels = (el.labels || []).map(String);
      series = el.series.map((s, i) => ({ name: s.name || '', values: (s.values || s.data || []).map((v) => +v || 0), color: s.color || PALETTE[i % PALETTE.length] }));
    } else {
      const data = el.data || el.values || [];
      if (data.length && typeof data[0] === 'object') {
        labels = data.map((d) => String(d.label == null ? '' : d.label));
        series = [{ name: '', values: data.map((d) => +d.value || 0), color: el.color || 'blue', each: data.map((d) => d.color) }];
      } else {
        labels = (el.labels || []).map(String);
        series = [{ name: '', values: data.map((v) => +v || 0), color: el.color || 'blue' }];
      }
    }
    const n = Math.max(labels.length, ...series.map((s) => s.values.length), 0);
    while (labels.length < n) labels.push('');
    return { labels, series, n };
  }
  COMPILERS.chart = function (el, c) {
    const x = num(el.x, 0), y = num(el.y, 0), w = num(el.w, 480), h = num(el.h, 320);
    const kind = el.kind || el.chart || 'bar';
    const size = num(el.size, 18);
    const { labels, series, n } = chartData(el);
    const ops = [];
    let top = y;
    if (el.title) {
      const lay = layoutText(el.title, { size: size * 1.3, family: el.font, bold: true, maxWidth: w });
      ops.push(textOp(c, lay, x + w / 2, y, 'center', c.C(el.title_color, 'black')));
      top += lay.height + size * 0.4;
    }
    const bottomLimit = y + h;
    if (kind === 'pie' || kind === 'donut') return pieChart(el, c, ops, labels, series[0] || { values: [] }, x, top, w, bottomLimit - top, size);

    const legend = series.length > 1;
    const all = [].concat(...series.map((s) => s.values));
    let maxV = el.max != null ? +el.max : Math.max(0, ...all);
    let minV = el.min != null ? +el.min : Math.min(0, ...all);
    if (maxV === minV) maxV = minV + 1;
    const step = niceStep(maxV - minV, 4);
    maxV = Math.ceil(maxV / step - 1e-9) * step;
    minV = Math.floor(minV / step + 1e-9) * step;
    const ticks = [];
    for (let v = minV; v <= maxV + step * 0.01; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
    const tickFont = font(size * 0.9, el.font);
    const tickUnit = /^[$€£¥]$/.test(el.unit || '') ? el.unit : '';
    const yLabW = Math.max(...ticks.map((t) => measure(fmtNum(t, tickUnit), tickFont)));
    const left = x + yLabW + size * 0.8;
    const right = x + w - size * 0.4;
    const labelRows = labels.some((l) => l) ? 2 : 0;
    const bottom = bottomLimit - (labelRows ? size * 1.25 * labelRows : size * 0.4) - (legend ? size * 1.7 : 0);
    const plotTop = top + size * 0.9;
    const Y = (v) => bottom - ((v - minV) / (maxV - minV)) * (bottom - plotTop);
    const ink = c.ink;
    // grid & ticks
    for (const t of ticks) {
      const ty = Y(t);
      if (t !== minV) ops.push(S(roughLine(c.r, left, ty, right, ty, c.R * 0.4), c.C('gray'), 1.4, { alpha: 0.4, dash: [5, 6] }));
      ops.push(textOp(c, layoutText(fmtNum(t, tickUnit), { size: size * 0.9, family: el.font }), left - size * 0.45, ty - size * 0.56, 'right', c.C('gray')));
    }
    ops.unshift(S(roughLine(c.r, left, plotTop - size * 0.4, left, bottom, c.R), ink, 3));
    ops.splice(1, 0, S(roughLine(c.r, left, Y(Math.max(0, minV)), right + size * 0.2, Y(Math.max(0, minV)), c.R), ink, 3));
    const gw = (right - left) / Math.max(1, n);
    const base = Y(Math.max(minV, Math.min(0, maxV)));
    const showVals = el.show_values !== false;
    if (kind === 'line' || kind === 'area') {
      series.forEach((s, si) => {
        const col = c.C(s.color);
        const pts = s.values.map((v, i) => [left + gw * (i + 0.5), Y(v)]);
        if (kind === 'area' && pts.length > 1) ops.push(F(pts.concat([[pts[pts.length - 1][0], base], [pts[0][0], base]]), col, { alpha: 0.18 }));
        if (pts.length > 1) ops.push(S(el.smooth ? shake(c.r, spline(pts, 6), 0.4) : roughPoly(c.r, pts, false, c.R), col, 3.5));
        pts.forEach((p, i) => {
          ops.push(F(ellipsePts(p[0], p[1], size * 0.24, size * 0.24, 14), col, { fast: true }));
          if (showVals && series.length === 1) ops.push(textOp(c, layoutText(fmtNum(s.values[i], el.unit), { size: size * 0.85, family: el.font }), p[0], p[1] - size * 1.5, 'center', col));
        });
        void si;
      });
    } else {
      const k = series.length;
      const bw = (gw * 0.7) / k;
      series.forEach((s, si) => {
        s.values.forEach((v, i) => {
          const bx = left + gw * i + gw * 0.15 + si * bw;
          const by = Y(v);
          const col = c.C((s.each && s.each[i]) || (k === 1 && el.colors !== false && el.multicolor ? PALETTE[i % PALETTE.length] : s.color));
          const top2 = Math.min(by, base), hgt = Math.abs(base - by);
          const rect = [[bx + bw * 0.06, base], [bx + bw * 0.06, base - (base - by)], [bx + bw * 0.94, base - (base - by)], [bx + bw * 0.94, base]];
          ops.push(F(shift(rect, J(c.r, 1.5), 0), col, { alpha: 0.32 }));
          ops.push(S(roughPoly(c.r, rect, false, c.R * 0.8), col, 3));
          if (showVals) ops.push(textOp(c, layoutText(fmtNum(v, el.unit), { size: size * 0.85, family: el.font, bold: true }), bx + bw / 2, (v >= 0 ? top2 : top2 + hgt) - (v >= 0 ? size * 1.25 : -size * 0.15), 'center', col));
        });
      });
    }
    labels.forEach((l, i) => {
      if (!l) return;
      const lay = layoutText(l, { size: size * 0.9, family: el.font, maxWidth: gw * 0.96 });
      const shown = lay.lines.length > 2 ? Object.assign({}, lay, { lines: lay.lines.slice(0, 2) }) : lay;
      ops.push(textOp(c, shown, left + gw * (i + 0.5), bottom + size * 0.3, 'center', ink));
    });
    if (legend) legendOps(c, ops, series.map((s) => [s.name, s.color]), left, bottomLimit - size * 1.3, size, el.font);
    return { ops, bbox: [x, y, w, h] };
  };
  function legendOps(c, ops, entries, x, y, size, family) {
    let lx = x;
    for (const [name, col] of entries) {
      const cc = c.C(col);
      ops.push(F([[lx, y + size * 0.15], [lx + size * 0.8, y + size * 0.15], [lx + size * 0.8, y + size * 0.85], [lx, y + size * 0.85]], cc, { alpha: 0.5, fast: true }));
      const lay = layoutText(name, { size: size * 0.9, family });
      ops.push(textOp(c, lay, lx + size * 1.1, y + size * 0.5 - lay.lh / 2, 'left', c.ink));
      lx += size * 1.1 + lay.width + size * 1.2;
    }
  }
  function pieChart(el, c, ops, labels, s, x, top, w, avail, size) {
    const vals = s.values.map((v) => Math.max(0, v));
    const total = vals.reduce((a, b) => a + b, 0) || 1;
    const R = Math.max(30, Math.min(w * 0.26, avail / 2 - size * 0.9));
    const cx = x + w / 2, cy = top + avail / 2;
    let a = -Math.PI / 2;
    const boundaries = [];
    vals.forEach((v, i) => {
      const sweep = (v / total) * TAU;
      const col = c.C((s.each && s.each[i]) || PALETTE[i % PALETTE.length]);
      const pts = [[cx, cy]];
      const steps = Math.max(2, Math.ceil(sweep / 0.08));
      for (let k = 0; k <= steps; k++) pts.push([cx + Math.cos(a + (sweep * k) / steps) * R, cy + Math.sin(a + (sweep * k) / steps) * R]);
      ops.push(F(pts, col, { alpha: 0.55 }));
      boundaries.push(a);
      const am = a + sweep / 2;
      const pct = Math.round((v / total) * 100);
      const txt = (labels[i] ? labels[i] + ' ' : '') + (el.show_values === false ? '' : el.unit ? fmtNum(v, el.unit) : pct + '%');
      const right = Math.cos(am) >= 0;
      const lx = cx + Math.cos(am) * (R + size * 0.9), ly = cy + Math.sin(am) * (R + size * 0.9);
      const room = Math.max(90, w / 2 - Math.abs(Math.cos(am)) * (R + size * 0.9) - 6);
      const lay = layoutText(txt.trim(), { size: size * 0.95, family: el.font, maxWidth: room });
      ops.push(textOp(c, lay, lx, ly - lay.height / 2, right ? 'left' : 'right', col === c.ink ? c.ink : mix(col, c.theme.dark ? '#ffffff' : '#000000', 0.25)));
      a += sweep;
    });
    if (el.kind === 'donut') {
      ops.push(F(ellipsePts(cx, cy, R * 0.52, R * 0.52, 48), c.theme.surface, { fast: true }));
      ops.push(S(roughEllipse(c.r, cx, cy, R * 0.52, R * 0.52, c.R), c.ink, 2.5));
    }
    ops.push(S(roughEllipse(c.r, cx, cy, R, R, c.R), c.ink, 3));
    if (vals.length > 1) for (const b of boundaries) ops.push(S(roughLine(c.r, cx + Math.cos(b) * (el.kind === 'donut' ? R * 0.52 : 0), cy + Math.sin(b) * (el.kind === 'donut' ? R * 0.52 : 0), cx + Math.cos(b) * R, cy + Math.sin(b) * R, c.R * 0.6), c.ink, 2.5));
    return { ops, bbox: [x, num(el.y, 0), w, num(el.h, 320)] };
  }

  // ---- code block
  const KEYWORDS = new Set(('def class return if else elif for while import from as function const let var new async await try except catch finally with yield in not and or is true false null None True False this self public private protected static void int float double string bool boolean fn mut struct impl use pub match enum interface type extends implements package go func defer chan select case switch break continue default lambda print echo do end then local nil undefined of export throw raise pass global').split(' '));
  const CODE_COLORS = { kw: '#8e3fbf', str: '#2f8f3a', num: '#d2691e', com: '#8b9096', fn: '#1e5fc2' };
  const CODE_COLORS_DARK = { kw: '#c792ea', str: '#a5e075', num: '#f9a65a', com: '#7f8b99', fn: '#82aaff' };
  function codeTokens(line, lang) {
    const hashComments = /^(py|python|rb|ruby|sh|bash|shell|yaml|yml|r|perl|toml|ps1|powershell)$/i.test(lang || '') || (!lang && /^\s*#/.test(line));
    const re = hashComments
      ? /(#.*$)|("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(\b\d+(?:\.\d+)?\b)|([A-Za-z_]\w*)|(\s+)|(.)/g
      : /(\/\/.*$|--\s.*$)|("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_]\w*)|(\s+)|(.)/g;
    const out = [];
    let m;
    while ((m = re.exec(line))) {
      if (m[1]) out.push([m[1], 'com']);
      else if (m[2]) out.push([m[2], 'str']);
      else if (m[3]) out.push([m[3], 'num']);
      else if (m[4]) out.push([m[4], KEYWORDS.has(m[4]) ? 'kw' : line[re.lastIndex] === '(' ? 'fn' : null]);
      else out.push([m[5] || m[6], null]);
    }
    return out;
  }
  COMPILERS.code = function (el, c) {
    const size = num(el.size, 18);
    const x = num(el.x, 0), y = num(el.y, 0);
    const dark = el.theme === 'dark';
    const lines = String(el.text != null ? el.text : el.code || '').replace(/\t/g, '    ').split('\n');
    const f = font(size, 'mono');
    const cw = measure('M', f);
    const pad = size * 0.9;
    const titleH = el.title ? size * 1.9 : 0;
    const lh = size * 1.4;
    const w = num(el.w, Math.max(220, Math.max(...lines.map((l) => l.length)) * cw + pad * 2));
    const h = titleH + lines.length * lh + pad * 1.3;
    const ops = [];
    const poly = roundRectPts(x, y, w, h, 12);
    ops.push(F(poly, dark ? '#1f2430' : '#f3f4f6', { alpha: 0.97, shadow: { color: 'rgba(0,0,0,0.12)', blur: 6, x: 0, y: 2 }, fast: true }));
    ops.push(S(shake(c.r, poly, 0.5 * c.R, true), dark ? '#3b4252' : c.C('gray'), 2.2, { alpha: 0.8 }));
    const colors = dark ? CODE_COLORS_DARK : CODE_COLORS;
    const base = dark ? '#e6e9ef' : '#2b2f36';
    if (el.title) {
      ['#ff5f57', '#febc2e', '#28c840'].forEach((dc, i) => ops.push(F(ellipsePts(x + pad * 0.8 + i * size * 0.9, y + titleH / 2, size * 0.27, size * 0.27, 12), dc, { fast: true })));
      const lay = layoutText(el.title, { size: size * 0.95, family: 'neat' });
      ops.push(textOp(c, lay, x + pad * 0.8 + size * 2.8, y + titleH / 2 - lay.lh / 2, 'left', dark ? '#aab2c0' : '#5b616b'));
      ops.push(S(roughLine(c.r, x + 4, y + titleH, x + w - 4, y + titleH, c.R * 0.4), dark ? '#3b4252' : '#cfd3d9', 1.5));
    }
    const maxChars = Math.floor((w - pad * 2) / cw);
    const segs = [];
    lines.forEach((line, i) => {
      const ly = y + titleH + pad * 0.65 + i * lh + lh / 2;
      let col = 0;
      for (const [tok, kind] of codeTokens(line, el.language)) {
        if (col >= maxChars) break;
        const t = tok.slice(0, maxChars - col);
        if (t.trim()) segs.push({ t, chars: Array.from(t), x: x + pad + col * cw, y: ly, font: f, color: kind ? colors[kind] : base, size });
        col += tok.length;
      }
    });
    ops.push({ k: 'text', segs, n: segs.reduce((s, g) => s + g.chars.length, 0), alpha: 1 });
    return { ops, bbox: [x, y, w, h] };
  };

  // ---- frame (labelled section)
  COMPILERS.frame = function (el, c) {
    const x = num(el.x, 0), y = num(el.y, 0), w = num(el.w, 400), h = num(el.h, 300);
    const col = c.C(el.color, 'gray');
    const lw = num(el.width, 2.5);
    const poly = roundRectPts(x, y, w, h, num(el.radius, 16));
    const ops = [];
    if (el.fill) ops.push(F(poly, c.C(el.fill), { alpha: num(el.fill_alpha, 0.1) }));
    ops.push(S(shake(c.r, poly, 0.6 * c.R, true), col, lw, { dash: el.dash === false ? null : [lw * 4, lw * 3] }));
    if (el.title) {
      const size = num(el.size, 24);
      const lay = layoutText(el.title, { size, family: el.font, bold: true });
      const tx = x + 22, ty = y - lay.height / 2;
      ops.push(F(roundRectPts(tx - 8, ty, lay.width + 16, lay.height, 6), c.theme.surface, { fast: true }));
      ops.push(textOp(c, lay, tx, ty, 'left', col));
    }
    return { ops, bbox: [x - lw, y - lw - (el.title ? num(el.size, 24) * 0.65 : 0), w + 2 * lw, h + 2 * lw + (el.title ? num(el.size, 24) * 0.65 : 0)] };
  };

  // ---- timeline
  COMPILERS.timeline = function (el, c) {
    const x = num(el.x, 0), y = num(el.y, 0), w = num(el.w, 1000);
    const size = num(el.size, 22);
    const items = (el.items || []).map((it) => (it && typeof it === 'object' ? it : { label: String(it) }));
    const n = Math.max(1, items.length);
    const sp = w / n;
    const col = c.C(el.color, 'blue');
    const labelLays = items.map((it) => layoutText(it.label || it.date || '', { size, family: el.font, bold: true, maxWidth: sp * 0.92 }));
    const textLays = items.map((it) => layoutText(it.text || '', { size: size * 0.9, family: el.font, maxWidth: sp * 0.92 }));
    const lh = Math.max(size * 1.25, ...labelLays.map((l) => l.height));
    const lineY = y + lh + size * 1.0;
    const ops = [];
    const main = roughLine(c.r, x, lineY, x + w, lineY, c.R);
    ops.push(S(main, col, 5));
    ops.push(...headOps(c, main, 'end', col, 5, 20));
    let maxText = 0;
    items.forEach((it, i) => {
      const px = x + sp * (i + 0.5);
      const ic = it.color ? c.C(it.color) : col;
      ops.push(F(ellipsePts(px, lineY, size * 0.42, size * 0.42, 16), ic, { fast: true }));
      ops.push(S(roughEllipse(c.r, px, lineY, size * 0.42, size * 0.42, c.R * 0.6), c.theme.dark ? c.ink : mix(ic, '#000000', 0.25), 2));
      ops.push(textOp(c, labelLays[i], px, lineY - size * 0.9 - labelLays[i].height, 'center', ic));
      if (textLays[i].lines.length && (it.text || '')) {
        ops.push(textOp(c, textLays[i], px, lineY + size * 0.9, 'center', c.ink));
        maxText = Math.max(maxText, textLays[i].height);
      }
    });
    return { ops, bbox: [x, y, w + 10, lineY - y + size * 0.9 + maxText + size * 0.3] };
  };

  // ---- mind map
  COMPILERS.mindmap = function (el, c) {
    const W = c.env.board.width, H = c.env.board.height;
    const cx = num(el.cx, num(el.x, W / 2)), cy = num(el.cy, num(el.y, H / 2));
    const R1 = num(el.radius, 260);
    const size = num(el.size, 24);
    const branches = (el.branches || []).map((b) => (b && typeof b === 'object' ? b : { text: String(b) }));
    const ops = [];
    const boxes = [];
    const accent = c.C(el.color, 'black');
    const clay = layoutText(el.center || el.text || el.title || '', { size: size * 1.25, family: el.font, bold: true, maxWidth: 230 });
    const crx = clay.width / 2 + size * 1.1, cry = clay.height / 2 + size * 0.85;
    ops.push(F(ellipsePts(cx, cy, crx, cry, 40), accent, { alpha: 0.08 }));
    ops.push(S(roughEllipse(c.r, cx, cy, crx, cry, c.R), accent, 3.5));
    ops.push(textOp(c, clay, cx, cy - clay.height / 2, 'center', accent));
    boxes.push([cx - crx, cy - cry, crx * 2, cry * 2]);
    const n = Math.max(1, branches.length);
    const a0 = num(el.start_angle, n === 2 ? 0 : -90 + 180 / n) * (Math.PI / 180);
    branches.forEach((b, i) => {
      const a = a0 + (i / n) * TAU;
      const bc = c.C(b.color || PALETTE[i % PALETTE.length]);
      const bx = cx + Math.cos(a) * R1 * 1.25, by = cy + Math.sin(a) * R1 * 0.78;
      const lay = layoutText(b.text || '', { size, family: el.font, bold: true, maxWidth: 200 });
      const pw = lay.width + size * 1.1, ph = lay.height + size * 0.7;
      const box = [bx - pw / 2, by - ph / 2, pw, ph];
      const start = [cx + Math.cos(a) * crx, cy + Math.sin(a) * cry];
      const end = boxEdgePoint(box, cx, cy, 4);
      const mid = [(start[0] + end[0]) / 2 - Math.sin(a) * 18, (start[1] + end[1]) / 2 + Math.cos(a) * 18];
      ops.push(S(shake(c.r, quadPts(start, mid, end, 18), 0.5 * c.R), bc, 5));
      const poly = roundRectPts(box[0], box[1], pw, ph, 12);
      ops.push(F(poly, bc, { alpha: 0.12 }));
      ops.push(S(shake(c.r, poly, 0.6 * c.R, true), bc, 3));
      ops.push(textOp(c, lay, bx, by - lay.height / 2, 'center', bc));
      boxes.push(box);
      const kids = (b.children || b.items || []).map((k) => (k && typeof k === 'object' ? k : { text: String(k) }));
      const m = kids.length;
      if (!m) return;
      const ks = size * 0.85;
      const klays = kids.map((k) => layoutText(k.text || '', { size: ks, family: el.font, maxWidth: 190 }));
      const cosA = Math.cos(a), sinA = Math.sin(a);
      const reach = num(el.child_radius, size * 3.2);
      const kcol = (k) => (k.color ? c.C(k.color) : c.ink);
      if (Math.abs(cosA) >= 0.3) {
        // a column of children beside the branch, each sitting on an underline
        const right = cosA > 0;
        const step = Math.max(ks * 1.9, ...klays.map((l) => l.height + ks * 0.75));
        const total = step * (m - 1);
        const baseY = by + sinA * total * 0.3;
        const s0 = [right ? box[0] + pw + 2 : box[0] - 2, by];
        kids.forEach((k, j) => {
          const kl = klays[j];
          const ky = baseY + j * step - total / 2 + kl.height / 2;
          const kx = right ? box[0] + pw + reach : box[0] - reach;
          const midX = s0[0] + (kx - s0[0]) * 0.55;
          ops.push(S(shake(c.r, cubicPts(s0, [midX, s0[1]], [midX, ky], [kx, ky], 16), 0.4 * c.R), bc, 2.5));
          ops.push(textOp(c, kl, right ? kx + 6 : kx - 6, ky - kl.height - 1, right ? 'left' : 'right', kcol(k)));
          const ux0 = right ? kx : kx - kl.width - 12, ux1 = right ? kx + kl.width + 12 : kx;
          ops.push(S(roughLine(c.r, ux0, ky, ux1, ky, c.R * 0.6), bc, 2.5));
          boxes.push([ux0, ky - kl.height - 2, ux1 - ux0, kl.height + 6]);
        });
      } else {
        // a row of children above or below the branch
        const down = sinA > 0;
        const widths = klays.map((l) => l.width + ks * 1.4);
        const total = widths.reduce((acc, v) => acc + v, 0);
        let kx0 = bx - total / 2 + cosA * total * 0.4;
        const s0 = [bx, down ? box[1] + ph + 2 : box[1] - 2];
        kids.forEach((k, j) => {
          const kl = klays[j];
          const kx = kx0 + widths[j] / 2;
          kx0 += widths[j];
          const ky = s0[1] + (down ? 1 : -1) * reach * 0.7;
          const midY = s0[1] + (ky - s0[1]) * 0.55;
          ops.push(S(shake(c.r, cubicPts(s0, [s0[0], midY], [kx, midY], [kx, ky], 16), 0.4 * c.R), bc, 2.5));
          ops.push(F(ellipsePts(kx, ky, 4, 4, 10), bc, { fast: true }));
          const ty = down ? ky + 6 : ky - 6 - kl.height;
          ops.push(textOp(c, kl, kx, ty, 'center', kcol(k)));
          boxes.push([kx - kl.width / 2, ty, kl.width, kl.height]);
        });
      }
    });
    return { ops, bbox: unionBox(boxes), centered: true };
  };

  // ---- flow (steps joined by arrows)
  COMPILERS.flow = function (el, c) {
    const steps = (el.steps || el.items || []).map((s) => (s && typeof s === 'object' ? s : { text: String(s) }));
    const size = num(el.size, 24);
    const dir = el.direction === 'down' ? 'down' : 'right';
    const gap = num(el.gap, 60);
    const x = num(el.x, 0), y = num(el.y, 0);
    const lays = steps.map((s) => layoutText(s.text || '', { size, family: el.font, bold: s.bold, maxWidth: num(el.w, 0) ? el.w * 0.84 : 210 }));
    const bh = num(el.h, Math.max(size * 2.6, ...lays.map((l) => l.height + size * 1.3)));
    const uniform = num(el.w, Math.max(size * 5, ...lays.map((l) => l.width + size * 1.6)));
    const ops = [];
    const boxes = [];
    let px = x, py = y;
    steps.forEach((s, i) => {
      const shape = s.shape || el.shape || 'rect';
      const bw = dir === 'down' ? uniform : num(el.w, Math.max(size * 4.2, lays[i].width + size * 1.6)) * (shape === 'diamond' ? 1.45 : 1);
      const hh = shape === 'diamond' ? bh * 1.35 : bh;
      const sx = dir === 'down' ? x + (uniform - bw) / 2 : px;
      const sy = dir === 'down' ? py : y + (bh - hh) / 2;
      const scol = s.color || (el.colorful ? PALETTE[i % PALETTE.length] : el.color || 'blue');
      const sub = compileShape({ type: shape === 'rounded' ? 'rect' : shape, id: el.id + ':' + i, x: sx, y: sy, w: bw, h: hh, color: scol, fill: s.fill || scol, fill_alpha: 0.12, radius: shape === 'rect' || shape === 'rounded' ? 14 : 0, label: s.text || '', label_size: size, label_color: 'black', font: el.font, width: 3 }, c);
      const box = [sx, sy, bw, hh];
      if (boxes.length) {
        const prev = boxes[boxes.length - 1];
        const a = dir === 'down' ? [prev[0] + prev[2] / 2, prev[1] + prev[3] + 6] : [prev[0] + prev[2] + 6, prev[1] + prev[3] / 2];
        const b = dir === 'down' ? [box[0] + box[2] / 2, box[1] - 8] : [box[0] - 8, box[1] + box[3] / 2];
        const line = roughLine(c.r, a[0], a[1], b[0], b[1], c.R);
        ops.push(S(line, c.ink, 3.5));
        ops.push(...headOps(c, line, 'end', c.ink, 3.5, 15));
      }
      ops.push(...sub.ops);
      boxes.push(box);
      if (dir === 'down') py += hh + gap;
      else px += bw + gap;
    });
    return { ops, bbox: unionBox(boxes) };
  };

  // ---- icons (library in icons.js)
  const ICON_FILL = {
    yellow: '#ffd84d', red: '#f05a4a', blue: '#5aa7ee', lightblue: '#bfe1ff', green: '#5cc171', lightgreen: '#bdeeb0',
    orange: '#ff9b3d', purple: '#a77ce0', lightpurple: '#dccbff', pink: '#ff9fc3', brown: '#b47a48', tan: '#e9c497',
    gray: '#b8bdc5', lightgray: '#e3e6ea', darkgray: '#5b6169', white: '#ffffff', skin: '#f8c9a0', gold: '#f5c03c',
    teal: '#3ec0b3', navy: '#2c4a86', cream: '#fff6dc', black: '#2b2d31', cyan: '#56d4f5',
  };
  const PATH_CACHE = new Map();
  function cachedPath(d) {
    let v = PATH_CACHE.get(d);
    if (!v) {
      v = WB.parseSvgPath(d);
      PATH_CACHE.set(d, v);
    }
    return v;
  }
  COMPILERS.icon = function (el, c) {
    const lib = WB.ICONS || {};
    const raw = String(el.name || '').toLowerCase().trim().replace(/[\s-]+/g, '_');
    const name = lib[raw] ? raw : (WB.ICON_ALIASES || {})[raw] || raw;
    const size = num(el.size, 80);
    const x = num(el.x, 0), y = num(el.y, 0);
    const def = lib[name];
    if (!def) {
      if (/\p{Extended_Pictographic}/u.test(String(el.name))) return COMPILERS.emoji({ text: el.name, x, y, size: size / 1.15 }, c);
      const ops = [S(roughEllipse(c.r, x + size / 2, y + size / 2, size * 0.45, size * 0.45, c.R), c.C('gray'), 3, { dash: [8, 6] })];
      ops.push(textOp(c, layoutText('?', { size: size * 0.5, bold: true }), x + size / 2, y + size * 0.2, 'center', c.C('gray')));
      return { ops, bbox: [x, y, size, size] };
    }
    const parts = typeof def.parts === 'function' ? def.parts() : def.parts;
    const s = size / 100;
    const primary = el.color ? ICON_FILL[String(el.color).toLowerCase()] || c.C(el.color) : ICON_FILL[def.color] || def.color;
    const secondary = el.color2 ? ICON_FILL[String(el.color2).toLowerCase()] || c.C(el.color2) : ICON_FILL[def.color2] || def.color2 || ICON_FILL.yellow;
    const outline = el.outline ? c.C(el.outline) : c.theme.ink;
    const resolve = (v) => {
      if (!v) return null;
      if (v === 'c') return primary;
      if (v === 'c2') return secondary;
      if (v === 'ink') return outline;
      if (v === 'surface') return c.theme.surface;
      return ICON_FILL[v] || v;
    };
    const lw = Math.max(1.6, size / 32);
    const ops = [];
    const mis = Math.min(2.2, size / 45) * c.R;
    for (const part of parts) {
      const [d, fill, stroke, wmul, dash] = part;
      const subs = cachedPath(d).map((sp) => ({ pts: sp.pts.map((p) => [x + p[0] * s, y + p[1] * s]), closed: sp.closed }));
      const fc = resolve(fill);
      if (fc) {
        const dx = J(c.r, mis), dy = J(c.r, mis);
        ops.push(F(subs.filter((sp) => sp.pts.length > 2).map((sp) => shift(sp.pts, dx, dy)), fc, { rule: 'evenodd', alpha: 1 }));
      }
      const sc = stroke === undefined ? outline : resolve(stroke);
      if (sc) {
        for (const sp of subs) {
          if (sp.pts.length < 1) continue;
          const pts = shake(c.r, sp.pts, 0.35 * c.R * Math.max(0.6, s), false);
          if (sp.closed && pts.length > 2) pts.push([pts[0][0] + J(c.r, 0.6), pts[0][1] + J(c.r, 0.6)]);
          ops.push(S(pts, sc, lw * (wmul || 1), { dash: dash ? [lw * 2.5, lw * 2.2] : null }));
        }
      }
    }
    return { ops, bbox: [x, y, size, size] };
  };

  // ------------------------------------------------------------------ compile / draw
  const NATIVE_CENTER = new Set(['mindmap']);
  function compile(el, env) {
    const fn = COMPILERS[el.type];
    const r = rng(hashStr(String(el.id) + ':' + (el.seed || 0)));
    const R = el.rough != null ? clamp(+el.rough || 0, 0, 3) : 1;
    const c = { r, R, theme: env.theme, env, ink: env.theme.ink, C: (name, fb) => color(name, env.theme, fb) };
    let d;
    try {
      d = fn ? fn(el, c) : { ops: [], bbox: [num(el.x, 0), num(el.y, 0), 10, 10] };
    } catch (err) {
      console.warn('[whiteboard] could not draw', el, err);
      d = { ops: [], bbox: [num(el.x, 0), num(el.y, 0), 10, 10], error: String(err) };
    }
    d.ops = d.ops || [];
    d.costs = d.ops.map(opCost);
    d.total = d.costs.reduce((a, b) => a + b, 0);
    d.tx = 0;
    d.ty = 0;
    if (!d.centered && !NATIVE_CENTER.has(el.type) && (el.cx != null || el.cy != null)) {
      const b = d.bbox;
      if (el.cx != null) d.tx = +el.cx - (b[0] + b[2] / 2);
      if (el.cy != null) d.ty = +el.cy - (b[1] + b[3] / 2);
    }
    const rotDeg = el.rotate != null ? +el.rotate || 0 : d.defaultRot || 0;
    d.rot = (rotDeg * Math.PI) / 180;
    d.pivot = [d.bbox[0] + d.bbox[2] / 2, d.bbox[1] + d.bbox[3] / 2];
    d.alpha = el.opacity != null ? clamp(+el.opacity, 0, 1) : 1;
    d.box = worldBox(d);
    return d;
  }
  function transformPoint(d, p) {
    let x = p[0], y = p[1];
    if (d.rot) {
      const cs = Math.cos(d.rot), sn = Math.sin(d.rot);
      const dx = x - d.pivot[0], dy = y - d.pivot[1];
      x = d.pivot[0] + dx * cs - dy * sn;
      y = d.pivot[1] + dx * sn + dy * cs;
    }
    return [x + d.tx, y + d.ty];
  }
  function worldBox(d) {
    const b = d.bbox;
    if (!d.rot) return [b[0] + d.tx, b[1] + d.ty, b[2], b[3]];
    return polyBBox([[b[0], b[1]], [b[0] + b[2], b[1]], [b[0] + b[2], b[1] + b[3]], [b[0], b[1] + b[3]]].map((p) => transformPoint(d, p)));
  }
  function drawDrawing(ctx, d, p, opts) {
    if (!d || !d.ops.length) return null;
    ctx.save();
    if (d.tx || d.ty) ctx.translate(d.tx, d.ty);
    if (d.rot) {
      ctx.translate(d.pivot[0], d.pivot[1]);
      ctx.rotate(d.rot);
      ctx.translate(-d.pivot[0], -d.pivot[1]);
    }
    if (d.alpha < 1) ctx.globalAlpha *= d.alpha;
    let tip = null;
    if (p >= 1) {
      for (const op of d.ops) drawOp(ctx, op, 1, opts);
    } else {
      const budget = p * d.total;
      let acc = 0;
      for (let i = 0; i < d.ops.length; i++) {
        const cst = d.costs[i];
        if (acc + cst <= budget) {
          const t = drawOp(ctx, d.ops[i], 1, opts);
          if (t && d.ops[i].k !== 'fill') tip = t;
          acc += cst;
          continue;
        }
        const t = drawOp(ctx, d.ops[i], (budget - acc) / cst, opts);
        if (t) tip = t;
        else if (d.ops[i].k === 'stroke' && d.ops[i].pts.length) tip = d.ops[i].pts[0];
        break;
      }
    }
    ctx.restore();
    return tip ? transformPoint(d, tip) : null;
  }
  // Colour of whatever the pen is drawing at progress p (for the pen cursor)
  function colorAt(d, p) {
    const budget = p * d.total;
    let acc = 0;
    for (let i = 0; i < d.ops.length; i++) {
      acc += d.costs[i];
      if (acc >= budget) {
        const op = d.ops[i];
        if (op.k === 'stroke') return op.color;
        if (op.k === 'text') return op.segs.length ? op.segs[0].color : null;
        return null;
      }
    }
    return null;
  }

  // ------------------------------------------------------------------ backgrounds
  function drawBackground(ctx, name, w, h, scale) {
    const theme = makeTheme(name);
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = theme.surface;
    ctx.fillRect(0, 0, w, h);
    const r = rng(12345);
    if (name === 'chalkboard') {
      for (let i = 0; i < 14; i++) {
        const gx = r() * w, gy = r() * h, gr = (80 + r() * 260) * scale;
        const g = ctx.createRadialGradient(gx, gy, 0, gx, gy, gr);
        g.addColorStop(0, 'rgba(255,255,255,0.045)');
        g.addColorStop(1, 'rgba(255,255,255,0)');
        ctx.fillStyle = g;
        ctx.fillRect(gx - gr, gy - gr, gr * 2, gr * 2);
      }
      ctx.fillStyle = 'rgba(255,255,255,0.05)';
      for (let i = 0; i < 2600; i++) ctx.fillRect(r() * w, r() * h, 1.2, 1.2);
    } else if (name === 'blueprint') {
      gridLines(ctx, w, h, 40 * scale, 'rgba(255,255,255,0.10)', 1);
      gridLines(ctx, w, h, 200 * scale, 'rgba(255,255,255,0.22)', 1.4);
    } else {
      // whiteboard sheen and faint ghosts of old marker
      const g = ctx.createLinearGradient(0, 0, w, h);
      g.addColorStop(0, 'rgba(255,255,255,0.9)');
      g.addColorStop(0.45, 'rgba(244,246,248,0.35)');
      g.addColorStop(0.55, 'rgba(255,255,255,0.75)');
      g.addColorStop(1, 'rgba(232,236,240,0.55)');
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, w, h);
      ctx.strokeStyle = 'rgba(120,130,150,0.011)';
      ctx.lineCap = 'round';
      for (let i = 0; i < 6; i++) {
        ctx.lineWidth = (20 + r() * 50) * scale;
        ctx.beginPath();
        const sx = r() * w, sy = r() * h;
        ctx.moveTo(sx, sy);
        ctx.bezierCurveTo(sx + (r() - 0.5) * 400 * scale, sy + (r() - 0.5) * 200 * scale, sx + (r() - 0.5) * 500 * scale, sy + (r() - 0.5) * 200 * scale, sx + (r() - 0.5) * 600 * scale, sy + (r() - 0.5) * 240 * scale);
        ctx.stroke();
      }
      if (name === 'grid') {
        gridLines(ctx, w, h, 40 * scale, 'rgba(70,120,190,0.12)', 1);
        gridLines(ctx, w, h, 200 * scale, 'rgba(70,120,190,0.2)', 1.3);
      } else if (name === 'dots') {
        ctx.fillStyle = 'rgba(70,90,120,0.28)';
        const st = 32 * scale;
        for (let yy = st; yy < h; yy += st) for (let xx = st; xx < w; xx += st) {
          ctx.beginPath();
          ctx.arc(xx, yy, Math.max(1, 1.6 * scale), 0, TAU);
          ctx.fill();
        }
      }
    }
    ctx.restore();
  }
  function gridLines(ctx, w, h, step, col, lw) {
    ctx.strokeStyle = col;
    ctx.lineWidth = lw;
    ctx.beginPath();
    for (let x = step; x < w; x += step) {
      ctx.moveTo(Math.round(x) + 0.5, 0);
      ctx.lineTo(Math.round(x) + 0.5, h);
    }
    for (let y = step; y < h; y += step) {
      ctx.moveTo(0, Math.round(y) + 0.5);
      ctx.lineTo(w, Math.round(y) + 0.5);
    }
    ctx.stroke();
  }

  Object.assign(WB, {
    compile, drawDrawing, colorAt, drawBackground, makeTheme, color, rgba, mix, layoutText, font,
    roughLine, roughEllipse, shake, spline, polyBBox, unionBox, hashStr, rng, quadPts, cubicPts,
    MARKER, PAPER, PALETTE, ICON_FILL, COMPILERS, getImage, IMAGES,
  });
})();
