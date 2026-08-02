/* Claude Code Monitor — web UI ("Kart" design language).
   Vanilla JS, no dependencies; charts are hand-built SVG. */
'use strict';

// ── utils ─────────────────────────────────────────────────────────────
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const usd = v => {
  v = +v || 0;
  if (v >= 1000) return '$' + v.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (v >= 10) return '$' + v.toFixed(2);
  if (v >= 0.01) return '$' + v.toFixed(3);
  return v > 0 ? '$' + v.toFixed(5) : '$0';
};
const usdAxis = v => {
  v = +v || 0;
  if (v === 0) return '$0';
  if (v >= 1000 && (v / 1000) % 1 === 0) return '$' + (v / 1000) + 'k';
  return '$' + v.toLocaleString(undefined, { maximumFractionDigits: 0 });
};
const tok = v => {
  v = +v || 0;
  if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
  if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M';
  if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K';
  return String(Math.round(v));
};
const dur = s => {
  s = Math.round(+s || 0);
  if (!s) return '—';
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm ' + String(s % 60).padStart(2, '0') + 's';
  const h = Math.floor(s / 3600);
  if (h < 48) return h + 'h ' + String(Math.floor((s % 3600) / 60)).padStart(2, '0') + 'm';
  return Math.floor(h / 24) + 'd ' + (h % 24) + 'h';
};
const ago = iso => {
  if (!iso) return '—';
  const d = (Date.now() - new Date(iso).getTime()) / 1000;
  return d < 0 ? 'now' : dur(d) + ' ago';
};
const dt = iso => iso ? new Date(iso).toLocaleString(undefined,
  { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
const pct = v => (100 * (+v || 0)).toFixed(1) + '%';
const debounce = (fn, ms) => {
  let h; return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); };
};

// Validated categorical palette (slots defined in style.css per theme).
const SERIES = ['var(--s1)', 'var(--s2)', 'var(--s3)', 'var(--s4)', 'var(--s5)'];
// Project avatar hues, assigned by name hash so identity is stable.
const HUES = ['#7b5cfa', '#0ea5a0', '#e8930c', '#d6456f', '#38bdf8', '#16a34a',
              '#8f74ff', '#f5b40a'];
const hueFor = name => {
  let h = 0;
  for (const c of String(name)) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return HUES[h % HUES.length];
};

// ── tooltip (hover + keyboard focus; Esc dismisses) ───────────────────
const tipEl = $('#tip');
function hideTip() { tipEl.classList.remove('on'); }
addEventListener('keydown', e => { if (e.key === 'Escape') hideTip(); });

function bindTip(el, text) {
  el.addEventListener('focus', () => {
    const r = el.getBoundingClientRect();
    tipEl.textContent = typeof text === 'function' ? text() : text;
    tipEl.classList.add('on');
    tipEl.style.left = Math.min(r.left, innerWidth - 260) + 'px';
    tipEl.style.top = Math.max(8, r.top - tipEl.getBoundingClientRect().height - 8) + 'px';
  });
  el.addEventListener('blur', hideTip);
  el.addEventListener('mousemove', e => {
    tipEl.textContent = typeof text === 'function' ? text() : text;
    tipEl.classList.add('on');
    const r = tipEl.getBoundingClientRect();
    let x = e.clientX + 13, y = e.clientY - r.height - 10;
    if (x + r.width > innerWidth - 8) x = e.clientX - r.width - 13;
    if (y < 8) y = e.clientY + 16;
    tipEl.style.left = x + 'px';
    tipEl.style.top = y + 'px';
  });
  el.addEventListener('mouseleave', hideTip);
}
function hydrateTips(root) {
  $$('[data-tip]', root).forEach(el => {
    const t = el.getAttribute('data-tip');
    if (t) bindTip(el, t);
  });
}

// ── state & api (short-TTL cache + per-navigation abort) ──────────────
const state = { days: 30, summary: null, live: null, signal: null };
const apiCache = new Map();
const API_TTL = 8000;
function invalidateApi() { apiCache.clear(); }

function apiUrl(path, params) {
  const u = new URL(path, location.origin);
  const p = Object.assign({}, params || {});
  if (state.days) p.days = state.days;
  Object.entries(p).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') u.searchParams.set(k, v);
  });
  return u;
}
async function api(path, params, opts = {}) {
  const key = apiUrl(path, params).toString();
  if (!opts.fresh) {
    const hit = apiCache.get(key);
    if (hit && Date.now() - hit.at < API_TTL) return hit.data;
  }
  const r = await fetch(key, { signal: state.signal });
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  const data = await r.json();
  apiCache.set(key, { at: Date.now(), data });
  // Evict once past a sane size — every distinct URL otherwise lives in the
  // map (and holds its whole response) for the life of the tab.
  if (apiCache.size > 64) {
    const cutoff = Date.now() - API_TTL;
    for (const [k, v] of apiCache) if (v.at < cutoff) apiCache.delete(k);
  }
  return data;
}

// ── chart primitives ──────────────────────────────────────────────────
const SVGNS = 'http://www.w3.org/2000/svg';
let gradSeq = 0;
function svg(tag, attrs, parent) {
  const e = document.createElementNS(SVGNS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
function niceScale(max, targetTicks = 4) {
  if (!(max > 0)) return { max: 1, ticks: [0, 1] };
  const raw = max / targetTicks;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
  const top = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = 0; v <= top + 1e-9; v += step) ticks.push(v);
  return { max: top, ticks };
}
function smoothPath(pts, t = 7) {
  if (!pts.length) return '';
  // A single point still needs a moveto — callers append "L…Z" to close the
  // area fill, and a path that starts with L is silently invalid SVG.
  if (pts.length === 1) return `M${pts[0][0].toFixed(1)} ${pts[0][1].toFixed(1)}`;
  let d = `M${pts[0][0].toFixed(1)} ${pts[0][1].toFixed(1)}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[Math.max(i - 1, 0)], p1 = pts[i], p2 = pts[i + 1];
    const p3 = pts[Math.min(i + 2, pts.length - 1)];
    const c1 = [p1[0] + (p2[0] - p0[0]) / t, p1[1] + (p2[1] - p0[1]) / t];
    const c2 = [p2[0] - (p3[0] - p1[0]) / t, p2[1] - (p3[1] - p1[1]) / t];
    d += `C${c1[0].toFixed(1)} ${c1[1].toFixed(1)} ${c2[0].toFixed(1)} ` +
         `${c2[1].toFixed(1)} ${p2[0].toFixed(1)} ${p2[1].toFixed(1)}`;
  }
  return d;
}

/** Mini sparkline; returns an <svg> string. */
function sparkSVG(series, w, h, color) {
  if (!series.length) return '';
  const mx = Math.max(...series) || 1;
  const pts = series.map((v, i) =>
    [i * w / (series.length - 1 || 1), 3 + (1 - v / mx) * (h - 6)]);
  const line = smoothPath(pts);
  const gid = 'spk' + (++gradSeq);
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true">
<defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="${color}" stop-opacity=".3"/>
<stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>
<path d="${line}L${w} ${h}L0 ${h}Z" fill="url(#${gid})"/>
<path d="${line}" fill="none" stroke="${color}" stroke-width="2"
 stroke-linecap="round"/></svg>`;
}

/** The signature chart: cumulative spend with a value flag on the biggest
    day and a crosshair. rows: [{date:'YYYY-MM-DD', cost, sessions}] */
function waveChart(host, rows, opts = {}) {
  host.innerHTML = '';
  if (!rows.length) { host.innerHTML = '<div class="empty">No data</div>'; return; }
  // Real pixel width, like every other chart. A fixed 1000-unit viewBox with
  // preserveAspectRatio:none stretched X ~3× on a wide screen while Y stayed
  // 1:1 — visibly distorting the value flag and every text label. The resize
  // handler re-renders, so the width tracks the card.
  const W = host.clientWidth || 1000, H = opts.height || 248;
  const padL = 40, padB = 24, padT = 30;
  const cum = [];
  let run = 0;
  for (const r of rows) { run += r.cost; cum.push(run); }
  const sc = niceScale(run || 1);
  const X = i => padL + i * (W - padL - 8) / (rows.length - 1 || 1);
  const Y = v => padT + (1 - v / sc.max) * (H - padT - padB);
  const pts = cum.map((v, i) => [X(i), Y(v)]);
  const line = smoothPath(pts);

  const s = svg('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}`, height: H }, host);
  const gid = 'wf' + (++gradSeq);
  const defs = svg('defs', {}, s);
  const grad = svg('linearGradient', { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
  svg('stop', { offset: 0, 'stop-color': '#7b5cfa', 'stop-opacity': .34 }, grad);
  svg('stop', { offset: .75, 'stop-color': '#7b5cfa', 'stop-opacity': .04 }, grad);
  svg('stop', { offset: 1, 'stop-color': '#7b5cfa', 'stop-opacity': 0 }, grad);

  for (const v of sc.ticks) {
    const y = Y(v);
    svg('line', { class: 'grid-line', x1: padL, x2: W - 8, y1: y, y2: y,
                  'stroke-dasharray': v === 0 ? '' : '3 5' }, s);
    const t = svg('text', { class: 'axis-t', x: 2, y: y + 3 }, s);
    t.textContent = usdAxis(v);
  }
  svg('path', { d: `${line}L${X(rows.length - 1)} ${Y(0)}L${X(0)} ${Y(0)}Z`,
                fill: `url(#${gid})` }, s);
  svg('path', { class: 'ln', d: line, stroke: 'var(--vio)' }, s);

  // x labels: ~7 across, last right-anchored so it never clips.
  const every = Math.max(1, Math.ceil(rows.length / 7));
  rows.forEach((r, i) => {
    if (i % every !== 0 && i !== rows.length - 1) return;
    const last = i === rows.length - 1;
    const t = svg('text', {
      class: 'axis-t', x: last ? W - 8 : X(i), y: H - 6,
      'text-anchor': last ? 'end' : i === 0 ? 'start' : 'middle',
    }, s);
    t.textContent = r.date.slice(5).replace('-', '/');
  });

  // flag on the biggest single day
  let bi = 0;
  rows.forEach((r, i) => { if (r.cost > rows[bi].cost) bi = i; });
  if (rows[bi].cost > 0) {
    const mx = X(bi), my = Y(cum[bi]);
    svg('line', { x1: mx, y1: my, x2: mx, y2: Y(0), stroke: 'var(--line2)',
                  'stroke-dasharray': '4 4' }, s);
    svg('circle', { cx: mx, cy: my, r: 5.5, fill: 'var(--card)',
                    stroke: 'var(--vio)', 'stroke-width': 3 }, s);
    // Sized for unstretched pixels: "2026-07-30 · biggest day" in 9.5px mono
    // is ~137px, so 138 clipped it once the X-stretch was removed.
    const fw = 156, fx = Math.max(padL, Math.min(mx - fw / 2, W - fw - 10));
    const fy = Math.max(2, my - 50);
    const g = svg('g', { transform: `translate(${fx},${fy})` }, s);
    svg('rect', { width: fw, height: 38, rx: 10, style: 'fill:var(--tip-bg)' }, g);
    const t1 = svg('text', { x: fw / 2, y: 16, 'text-anchor': 'middle',
      'font-size': 12, 'font-weight': 700, style: 'fill:var(--tip-ink)',
      'font-family': 'Liberation Sans,system-ui' }, g);
    t1.textContent = `+${usd(rows[bi].cost)}`;
    const t2 = svg('text', { x: fw / 2, y: 30, 'text-anchor': 'middle',
      'font-size': 9.5, style: 'fill:var(--tip-ink);opacity:.6',
      'font-family': 'ui-monospace,monospace' }, g);
    t2.textContent = `${rows[bi].date} · biggest day`;
  }
  svg('circle', { cx: pts[pts.length - 1][0], cy: pts[pts.length - 1][1], r: 5,
                  fill: 'var(--vio)' }, s);

  // crosshair
  const hit = svg('rect', { x: padL, y: 0, width: W - padL, height: H,
                            fill: 'transparent' }, s);
  const cross = svg('line', { class: 'grid-line', y1: padT, y2: Y(0), opacity: 0 }, s);
  const mark = svg('circle', { r: 4, fill: 'var(--vio)', opacity: 0 }, s);
  hit.addEventListener('mousemove', e => {
    const bb = s.getBoundingClientRect();
    const px = (e.clientX - bb.left) * (W / bb.width);
    let best = 0, bd = Infinity;
    pts.forEach((p, i) => { const d = Math.abs(p[0] - px); if (d < bd) { bd = d; best = i; } });
    cross.setAttribute('x1', pts[best][0]); cross.setAttribute('x2', pts[best][0]);
    cross.setAttribute('opacity', 1);
    mark.setAttribute('cx', pts[best][0]); mark.setAttribute('cy', pts[best][1]);
    mark.setAttribute('opacity', 1);
    tipEl.textContent =
      `${rows[best].date}\nthat day  ${usd(rows[best].cost)}\ncumulative ${usd(cum[best])}`;
    tipEl.classList.add('on');
    tipEl.style.left = Math.min(e.clientX + 13, innerWidth - 220) + 'px';
    tipEl.style.top = (e.clientY - 58) + 'px';
  });
  hit.addEventListener('mouseleave', () => {
    cross.setAttribute('opacity', 0); mark.setAttribute('opacity', 0); hideTip();
  });
}

/** Time-series area+line with crosshair (session detail). points: [{t,v}] */
function lineChart(host, points, opts = {}) {
  const W = host.clientWidth || 700, H = opts.height || 170;
  const pad = { t: 8, r: 8, b: 20, l: 46 };
  host.innerHTML = '';
  if (!points.length) { host.innerHTML = '<div class="empty">No data</div>'; return; }
  const s = svg('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}`, height: H }, host);
  const xs = points.map(p => p.t), ys = points.map(p => p.v);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const sc = niceScale(Math.max(...ys, opts.minMax || 0));
  const X = t => pad.l + (x1 === x0 ? 0 : (t - x0) / (x1 - x0)) * (W - pad.l - pad.r);
  const Y = v => H - pad.b - (v / sc.max) * (H - pad.t - pad.b);
  for (const v of sc.ticks) {
    const y = Y(v);
    svg('line', { class: 'grid-line', x1: pad.l, x2: W - pad.r, y1: y, y2: y }, s);
    const t = svg('text', { class: 'axis-t', x: 4, y: y + 3 }, s);
    t.textContent = opts.fmt ? opts.fmt(v) : tok(v);
  }
  if (x1 > x0) {
    const fmtT = ts => new Date(ts * 1000).toLocaleString(undefined,
      { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    [[x0, 'start'], [(x0 + x1) / 2, 'middle'], [x1, 'end']].forEach(([tv, pos]) => {
      const t = svg('text', { class: 'axis-t', x: X(tv), y: H - 5,
        'text-anchor': pos }, s);
      t.textContent = fmtT(tv);
    });
  }
  const color = opts.color || 'var(--vio)';
  const d = points.map((p, i) =>
    `${i ? 'L' : 'M'}${X(p.t).toFixed(1)},${Y(p.v).toFixed(1)}`).join('');
  const gid = 'lc' + (++gradSeq);
  const defs = svg('defs', {}, s);
  const grad = svg('linearGradient', { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
  svg('stop', { offset: 0, 'stop-color': opts.hex || '#7b5cfa', 'stop-opacity': .22 }, grad);
  svg('stop', { offset: 1, 'stop-color': opts.hex || '#7b5cfa', 'stop-opacity': 0 }, grad);
  svg('path', { d: `${d}L${X(x1)},${H - pad.b}L${X(x0)},${H - pad.b}Z`,
                fill: `url(#${gid})` }, s);
  svg('path', { class: 'ln', d, stroke: color }, s);

  const hit = svg('rect', { x: pad.l, y: pad.t, width: W - pad.l - pad.r,
    height: H - pad.t - pad.b, fill: 'transparent' }, s);
  const cross = svg('line', { class: 'grid-line', y1: pad.t, y2: H - pad.b,
    opacity: 0 }, s);
  const mark = svg('circle', { r: 3.5, fill: color, opacity: 0 }, s);
  hit.addEventListener('mousemove', e => {
    const bb = s.getBoundingClientRect();
    const px = (e.clientX - bb.left) * (W / bb.width);
    let best = points[0], bd = Infinity;
    for (const p of points) { const dd = Math.abs(X(p.t) - px); if (dd < bd) { bd = dd; best = p; } }
    cross.setAttribute('x1', X(best.t)); cross.setAttribute('x2', X(best.t));
    cross.setAttribute('opacity', 1);
    mark.setAttribute('cx', X(best.t)); mark.setAttribute('cy', Y(best.v));
    mark.setAttribute('opacity', 1);
    tipEl.textContent = opts.label ? opts.label(best)
      : `${new Date(best.t * 1000).toLocaleString()}\n${tok(best.v)}`;
    tipEl.classList.add('on');
    tipEl.style.left = Math.min(e.clientX + 13, innerWidth - 220) + 'px';
    tipEl.style.top = (e.clientY - 46) + 'px';
  });
  hit.addEventListener('mouseleave', () => {
    cross.setAttribute('opacity', 0); mark.setAttribute('opacity', 0); hideTip();
  });
}

/** Column chart (daily spend on Cost, agent-cost histogram). */
function colChart(host, rows, opts = {}) {
  const W = host.clientWidth || 700, H = opts.height || 170;
  const pad = { t: 8, r: 6, b: 20, l: 46 };
  host.innerHTML = '';
  if (!rows.length) { host.innerHTML = '<div class="empty">No data</div>'; return; }
  const s = svg('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}`, height: H }, host);
  const sc = niceScale(Math.max(...rows.map(r => r.v)));
  const iw = (W - pad.l - pad.r) / rows.length;
  const bw = Math.max(2, iw - 3);
  for (const v of sc.ticks) {
    const y = H - pad.b - (v / sc.max) * (H - pad.t - pad.b);
    svg('line', { class: 'grid-line', x1: pad.l, x2: W - pad.r, y1: y, y2: y,
                  'stroke-dasharray': v === 0 ? '' : '3 5' }, s);
    const t = svg('text', { class: 'axis-t', x: 4, y: y + 3 }, s);
    t.textContent = opts.fmt ? opts.fmt(v) : usdAxis(v);
  }
  rows.forEach((r, i) => {
    const h = Math.max(r.v > 0 ? 2 : 0, (r.v / sc.max) * (H - pad.t - pad.b));
    const rect = svg('rect', {
      class: 'col', x: pad.l + i * iw + (iw - bw) / 2, y: H - pad.b - h,
      width: bw, height: h, rx: Math.min(4, bw / 2.5),
      fill: opts.color || 'var(--vio)',
    }, s);
    bindTip(rect, () => r.tip);
  });
  // label density follows available width so narrow cards don't collide
  const maxLabels = Math.max(2, Math.min(6, Math.floor((W - pad.l) / 90)));
  const every = Math.max(1, Math.ceil(rows.length / maxLabels));
  rows.forEach((r, i) => {
    if (i % every !== 0 && i !== rows.length - 1) return;
    if (i !== rows.length - 1 && (rows.length - 1 - i) * iw < 46) return;
    const last = i === rows.length - 1;
    const t = svg('text', {
      class: 'axis-t', x: last ? W - pad.r : pad.l + i * iw + iw / 2, y: H - 5,
      'text-anchor': last ? 'end' : i === 0 ? 'start' : 'middle',
    }, s);
    t.textContent = r.label || '';
  });
}

// ── list/bar helpers ──────────────────────────────────────────────────
function foldTail(items, keep, label = 'others') {
  if (items.length <= keep + 1) return items;
  const head = items.slice(0, keep);
  const tail = items.slice(keep);
  const sum = tail.reduce((a, b) => a + b.value, 0);
  head.push({
    label: `${tail.length} ${label}`, value: sum,
    text: items[0].fmt ? items[0].fmt(sum) : String(sum),
    muted: true, tip: tail.slice(0, 12).map(t => t.label).join('\n'),
  });
  return head;
}
function barList(items) {
  if (!items.length) return '<div class="empty">No data</div>';
  const max = Math.max(...items.map(i => i.value)) || 1;
  return '<div class="bars">' + items.map(i => {
    const w = Math.max(0.4, (i.value / max) * 100);
    const bare = w < 4 ? ' bare' : '';
    const inner = `<span class="lab" title="${esc(i.label)}">${esc(i.label)}</span>
      <div class="track${bare}" data-tip="${esc(i.tip || '')}">
        <div class="fill" style="width:${w.toFixed(2)}%;background:${
          i.muted ? 'var(--line2)' : (i.color || 'var(--vio)')}"></div></div>
      <span class="val">${esc(i.text)}<i>${esc(i.sub || '')}</i></span>`;
    if (i.href) return `<a class="brow" href="${esc(i.href)}">${inner}</a>`;
    if (i.act) return `<button type="button" class="brow" data-act="${esc(i.act)}"
      title="Show what ${esc(i.label)} actually ran">${inner}</button>`;
    return `<div class="brow">${inner}</div>`;
  }).join('') + '</div>';
}

/* Modal + tools drill-down: what a tool actually executed, in a popup. */
function closeModal() {
  const m = $('#modal');
  if (m) {
    removeEventListener('keydown', m._esc);
    if (m._prev && m._prev.focus) m._prev.focus();
    m.remove();
  }
}
function openModal(title) {
  closeModal();
  const m = document.createElement('div');
  m.id = 'modal';
  m._prev = document.activeElement;
  m.innerHTML = `<div class="mback"></div>
    <div class="mpanel" role="dialog" aria-modal="true" aria-label="${esc(title)}">
      <div class="mhead"><b>${esc(title)}</b><span class="meta" id="mMeta"></span>
        <button type="button" class="btn" id="mClose" aria-label="Close">✕</button></div>
      <div class="mbody" id="mBody"><div class="skel">${
        '<div class="skel-row"></div>'.repeat(7)}</div></div>
    </div>`;
  document.body.appendChild(m);
  $('#mClose').focus();
  $('#mClose').addEventListener('click', closeModal);
  m.querySelector('.mback').addEventListener('click', closeModal);
  m._esc = e => { if (e.key === 'Escape') closeModal(); };
  addEventListener('keydown', m._esc);
  return m;
}
async function openToolCalls(tool, sid) {
  const m = openModal(tool);
  let d;
  try {
    d = await api(sid ? `/api/sessions/${sid}/tool-calls` : '/api/tool-calls',
                  { tool });
  } catch (e) {
    if ($('#modal') === m) $('#mBody').innerHTML =
      `<div class="empty"><b>Couldn't load calls</b>${esc(e.message)}</div>`;
    return;
  }
  if ($('#modal') !== m) return;   // closed while loading
  $('#mMeta').textContent = `${d.total.toLocaleString()} call${
    d.total === 1 ? '' : 's'}` +
    (d.truncated ? ` · showing newest ${d.calls.length}` : '') +
    (d.sessions_searched != null && d.sessions_searched < d.sessions_total
      ? ` · searched ${d.sessions_searched} of ${d.sessions_total} sessions, newest first`
      : '');
  $('#mBody').innerHTML = d.calls.map(c => `<div class="tcall">
      <div class="tct"><span>${dt(c.ts)}</span>
        ${c.project ? `<span class="tag vio">${esc(c.project)}</span>` : ''}
        ${c.agent ? `<span class="tag" title="ran inside a subagent">${
          esc(c.agent.slice(0, 38))}</span>` : ''}
        ${c.error ? '<span class="tag bad">error</span>' : ''}</div>
      ${c.sub ? `<div class="tcs">${esc(c.sub)}</div>` : ''}
      <code>${esc(c.text) || '<span class="mut">(no arguments)</span>'}</code>
    </div>`).join('') ||
    '<div class="empty"><b>No calls found</b>Nothing recorded in the searched range.</div>';
}
function wireToolRows(root, sid) {
  $$('.brow[data-act]', root).forEach(b =>
    b.addEventListener('click', () => openToolCalls(b.dataset.act, sid)));
}
function legend(pairs) {
  return '<div class="legend">' + pairs.map(([n, c]) =>
    `<span class="lg"><span class="sw" style="background:${c}"></span>${esc(n)}</span>`
  ).join('') + '</div>';
}
function heatmap(cells) {
  const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const grid = {};
  let max = 0;
  cells.forEach(c => { grid[c.day + ':' + c.hour] = c.cost; max = Math.max(max, c.cost); });
  let html = '<div class="hm"><span></span>';
  for (let h = 0; h < 24; h++) html += `<span class="hl">${h % 3 === 0 ? h : ''}</span>`;
  html += '</div>';
  for (let d = 0; d < 7; d++) {
    html += `<div class="hm" style="margin-top:3px"><span class="rl">${DAYS[d]}</span>`;
    for (let h = 0; h < 24; h++) {
      const v = grid[d + ':' + h] || 0;
      const a = max ? Math.pow(v / max, 0.45) : 0;
      html += `<span class="cell" data-tip="${DAYS[d]} ${h}:00\n${esc(usd(v))}"
        style="${v ? `background:color-mix(in srgb,var(--vio) ${
        (a * 100).toFixed(0)}%,var(--panel2))` : ''}"></span>`;
    }
    html += '</div>';
  }
  const steps = [0, .25, .5, .75, 1].map(a =>
    `<i style="background:${a ? `color-mix(in srgb,var(--vio) ${
      (Math.pow(a, .45) * 100).toFixed(0)}%,var(--panel2))` : 'var(--panel2)'}"></i>`).join('');
  html += `<div class="hm-key"><span>$0</span><span class="ramp">${steps}</span>` +
          `<span>${esc(usd(max))}</span><span style="margin-left:auto">per ` +
          `hour-of-week, summed over the window</span></div>`;
  return html;
}

// ── status helpers ────────────────────────────────────────────────────
function sessStatus(s) {
  if (s.live) return { cls: 'live', label: 'Live' };
  const age = s.ended ? (Date.now() - new Date(s.ended).getTime()) / 1000 : Infinity;
  return age < 86400 ? { cls: 'idle', label: 'Idle' } : { cls: 'done', label: 'Done' };
}
const stPill = st => `<span class="st ${st.cls}"><i></i>${st.label}</span>`;
const AGENT_ST = { running: ['run', 'Running'], done: ['ok', 'Done'],
                   stopped: ['done', 'Stopped'] };
const agentPill = state2 => {
  const [cls, label] = AGENT_ST[state2] || ['done', state2];
  return `<span class="st ${cls}"><i></i>${label}</span>`;
};
/** Agents cell: "2▶ /34" when some are working right now, plain total when
    idle. The running count is what you actually scan the column for. */
const agentsCell = s => {
  const total = s.agents || 0;
  const run2 = s.agents_running || 0;
  if (!total) return '·';
  if (!run2) return String(total);
  return `<span class="agr" title="${run2} running now · ${total} total">${
    run2}▶</span><span class="mut">/${total}</span>`;
};

const avatar = name => {
  // A project can legitimately have no name (transcript with no cwd); indexing
  // [0] on '' would throw and take the whole view down with it.
  const s = String(name ?? '').trim();
  return `<span class="av" style="background:${hueFor(s)}22;color:${hueFor(s)}">${
    esc(s ? s[0].toUpperCase() : '?')}</span>`;
};

// ── table (rows are real links; headers optionally sortable) ──────────
function table(cols, rows, opts = {}) {
  const sortable = !!opts.onSort;
  const head = cols.map(c => {
    const cls = [c.n ? 'n' : '', sortable && c.sort ? 'sortable' : '',
                 opts.sort && opts.sort === c.sort ? 'sorted' : ''].filter(Boolean).join(' ');
    return `<th scope="col" class="${cls}"${
      sortable && c.sort ? ` data-sort="${c.sort}" tabindex="0" role="button"` : ''}>${
      esc(c.h)}</th>`;
  }).join('');
  if (!rows.length) {
    return `<div class="tw"><table><thead><tr>${head}</tr></thead></table>
      <div class="empty">${opts.empty ||
        '<b>Nothing here yet</b>No sessions match the current window or search.'}</div></div>`;
  }
  const linkCol = cols.find(c => c.link) || cols.find(c => c.grow) || cols[0];
  const body = rows.map(r => {
    const cells = cols.map(c => {
      let v = r[c.key] ?? '';
      // Escape centrally: _href carries ids taken from transcript filenames,
      // so it is not ours to trust even though it is normally a UUID.
      if (r._href && c === linkCol)
        v = `<a class="rowlink" href="${esc(r._href)}">${v}</a>`;
      return `<td class="${c.n ? 'n' : ''} ${c.grow ? 'grow' : ''} ${
        c.cls || ''}">${v}</td>`;
    }).join('');
    return `<tr>${cells}</tr>`;
  }).join('');
  return `<div class="tw"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}
function wireTable(root, onSort) {
  $$('tbody tr', root).forEach(tr => {
    const a = tr.querySelector('a.rowlink');
    if (!a) return;
    tr.addEventListener('click', ev => {
      if (ev.target.closest('a')) return;
      if ((getSelection() || '').toString()) return;
      location.hash = a.getAttribute('href');
    });
  });
  if (onSort) $$('thead th[data-sort]', root).forEach(th => {
    const go = () => onSort(th.dataset.sort);
    th.addEventListener('click', go);
    th.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
    });
  });
}

// ── window pills ──────────────────────────────────────────────────────
const WINDOWS = [[1, '24h'], [7, '7d'], [30, '30d'], [90, '90d'], [0, 'All']];
const winLabel = () => {
  const w = WINDOWS.find(x => x[0] === state.days);
  return w && w[0] ? w[1] : 'all time';
};
function windowPicker() {
  return `<div class="pills" role="group" aria-label="Time window">` +
    WINDOWS.map(([d, l]) =>
      `<button data-d="${d}" class="${d === state.days ? 'on' : ''}">${l}</button>`
    ).join('') + `</div>`;
}
function wireWindow(root) {
  $$('.pills button[data-d]', root || document).forEach(b =>
    b.addEventListener('click', () => {
      state.days = +b.dataset.d;
      localStorage.setItem('cm.days', state.days);
      // Instant feedback on the pills, then swap the content in place — a
      // full route() blanks the page to a skeleton and resets scroll for
      // what is just a data change.
      $$('.pills button[data-d]').forEach(x =>
        x.classList.toggle('on', +x.dataset.d === state.days));
      const y = scrollY;
      invalidateApi();
      route(true).then(() => scrollTo(0, y));
    }));
}

// ── navigation feedback ───────────────────────────────────────────────
const prog = document.createElement('div');
prog.id = 'nprog';
document.body.appendChild(prog);
let progTimer;
function progStart() {
  clearTimeout(progTimer);
  prog.classList.add('on');
  prog.style.width = '0%';
  void prog.offsetWidth;
  prog.style.width = '65%';
}
function progDone() {
  prog.style.width = '100%';
  progTimer = setTimeout(() => {
    prog.classList.remove('on');
    prog.style.width = '0%';
  }, 220);
}
const SKELETONS = {
  tiles: n => `<div class="kpis skel">${'<div class="skel-box skel-tile"></div>'.repeat(n)}</div>`,
  card: () => '<section class="blk skel"><div class="skel-box skel-card"></div></section>',
  rows: n => `<div class="skel" style="margin-top:18px">${'<div class="skel-row"></div>'.repeat(n)}</div>`,
};
const PAGE = {
  overview:  { title: 'Dashboard', skel: () => SKELETONS.tiles(3) + SKELETONS.card() },
  sessions:  { title: 'Sessions',  skel: () => SKELETONS.rows(14) },
  session:   { title: 'Session',   skel: () => SKELETONS.tiles(6) + SKELETONS.card() },
  agents:    { title: 'Agents',    skel: () => SKELETONS.card() + SKELETONS.rows(10) },
  agent:     { title: 'Agent',     skel: () => SKELETONS.tiles(5) + SKELETONS.card() },
  workflows: { title: 'Workflows', skel: () => SKELETONS.card() + SKELETONS.card() },
  cost:      { title: 'Cost',      skel: () => SKELETONS.tiles(5) + SKELETONS.card() },
  tools:     { title: 'Tools',     skel: () => SKELETONS.card() },
};
function paintShell(name) {
  const meta = PAGE[name] || { title: '', skel: () => '' };
  $('#view').innerHTML =
    `<div class="hd"><div><h1>${esc(meta.title)}</h1>
      <p class="sub">loading…</p></div></div>`;
  return setTimeout(() => {
    const head = $('#view .hd');
    if (head) head.insertAdjacentHTML('afterend', meta.skel());
  }, 120);
}

// ── views ─────────────────────────────────────────────────────────────
const views = {};

const greeting = () => {
  const h = new Date().getHours();
  return h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening';
};
const todayChip = () => `<span class="datechip">📅 ${new Date().toLocaleDateString(
  undefined, { day: 'numeric', month: 'short', year: 'numeric' })}</span>`;

// Fan-out artwork for the overview card (decorative; stats carry the info).
const FAN_ART = `<svg viewBox="0 0 220 130" width="100%" height="100" aria-hidden="true">
<g fill="none" stroke="var(--vio)" stroke-width="3" stroke-linecap="round" opacity=".9">
<path d="M28 65h32"/><path d="M60 65c28 0 20-38 48-38"/>
<path d="M60 65c20 0 15 20 35 20"/><path d="M60 65h70"/>
<path d="M130 65c24 0 18-23 40-23" opacity=".5"/>
<path d="M95 85c18 0 13 21 33 21" opacity=".5"/></g>
<circle cx="24" cy="65" r="10" fill="var(--card)" stroke="var(--vio)" stroke-width="3"/>
<circle cx="110" cy="27" r="7" fill="var(--gold)"/>
<circle cx="132" cy="65" r="8" fill="var(--vio)"/>
<circle cx="97" cy="85" r="6" fill="#16a34a"/>
<circle cx="172" cy="42" r="6" fill="#d6456f"/>
<circle cx="130" cy="106" r="5.5" fill="#38bdf8"/>
<circle cx="110" cy="27" r="11.5" fill="none" stroke="var(--gold)" stroke-width="1.6" opacity=".4"/>
<circle cx="132" cy="65" r="12.5" fill="none" stroke="var(--vio)" stroke-width="1.6" opacity=".4"/>
</svg>`;

views.overview = async () => {
  const [d, live] = await Promise.all([
    api('/api/summary'),
    api('/api/live', {}, { fresh: true }),
  ]);
  state.summary = d;
  state.live = live;
  const t = d.totals, e = d.economics;

  // Warn only when something is actionable: a session close to the 1M window.
  const alerts = [];
  const pressured = live.live
    .filter(s => (s.current_context || 0) > 700000)
    .sort((a, b) => b.current_context - a.current_context);
  if (pressured.length) {
    const s0 = pressured[0];
    alerts.push(`<div class="alert amb">⚠ <span><b>${esc(s0.project)}</b> context is at
      <b>${(s0.current_context / 1e4 / 100).toFixed(1)}%</b> of the 1M window —
      compaction soon.</span></div>`);
  }

  const daily = d.daily || [];
  const dailyCost = daily.map(r => r.cost);
  const cumSeries = [];
  let run = 0;
  for (const v of dailyCost) { run += v; cumSeries.push(run); }
  // Window numbers come from the same per-day series the chart draws, so the
  // headline and the chart always agree. totals (t.*) counts whole sessions
  // active in the window — a straddling session's whole bill — which is a
  // different (bigger) number; it still backs the counts, not the spend.
  const windowSpend = dailyCost.reduce((a, b) => a + b, 0);
  const windowUncached = daily.reduce((a, r) => a + (r.uncached || 0), 0);
  const winSaved = Math.max(0, windowUncached - windowSpend);
  const winSavedPct = windowUncached ? 100 * winSaved / windowUncached : 0;

  const sessData = await api('/api/sessions', { sort: 'recent', limit: 8 });
  const rows = sessData.sessions.map(s => {
    const st = sessStatus(s);
    return {
      _href: `#/session/${s.id}`,
      av: avatar(s.project),
      sess: `<b>${esc(s.project)}</b><span class="tp">${esc(s.title)}</span>`,
      model: `<span class="mchip">${esc(s.model_label)}</span>`,
      tk: tok(s.tokens), ag: agentsCell(s),
      cost: usd(s.cost), st: stPill(st),
      when: `<span class="dur">${ago(s.ended)}</span>`,
    };
  });

  $('#view').innerHTML = `
    <div class="hd">
      <div><h1>${greeting()}, ${esc(d.user || 'there')} 👋</h1>
        <div class="sub">Here's what your agents are doing right now.</div></div>
      <div class="right">${windowPicker()}${todayChip()}</div>
    </div>

    ${alerts.length
      ? `<div style="display:flex;flex-direction:column;gap:8px;margin-bottom:14px">${
          alerts.join('')}</div>` : ''}
    <div class="sect">Dashboard overview</div>
    <div class="hero">
      <div class="illus">
        <div class="cap">One orchestrator, many hands</div>
        <div class="sub2">${t.agents.toLocaleString()} subagents fanned out in the
          last ${winLabel()} — peak parallelism ×${d.peak_parallelism || 1}.</div>
        ${FAN_ART}
      </div>

      <div class="kpis" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr));margin-bottom:0">
        <div class="kpi">
          <span class="ic" style="background:color-mix(in srgb,var(--vio) 12%,transparent);color:var(--vio-text)">$</span>
          <div class="k">Actual spend</div>
          <div class="v">${usd(windowSpend)}</div>
          <span class="delta up">↓ ${winSavedPct.toFixed(0)}% vs uncached list</span>
          <span class="spk">${sparkSVG(dailyCost, 86, 30, '#16a34a')}</span>
        </div>
        <div class="kpi">
          <span class="ic" style="background:var(--red-bg);color:var(--red)">⧗</span>
          <div class="k">Real cost / 1M output</div>
          <div class="v">${usd(e.effective_output_rate)}</div>
          <span class="delta bad">↑ ${e.multiple_of_list.toFixed(0)}× the list rate</span>
          <span class="spk">${sparkSVG(cumSeries, 86, 30, '#dc2626')}</span>
        </div>
        <div class="kpi">
          <span class="ic" style="background:var(--amber-bg);color:var(--amber)">⧉</span>
          <div class="k">Output of total tokens</div>
          <div class="v">${tok(e.tokens.output)} <small>/ ${tok(t.tokens)}</small></div>
          <span class="delta warn">${(100 * e.output_share).toFixed(2)}% is new work</span>
        </div>
      </div>

    </div>

    <section class="blk" style="display:grid;grid-template-columns:1fr minmax(300px,380px);gap:16px">
      <div class="card">
        <div class="ch"><h2>Sessions</h2><a class="meta" href="#/sessions">View all →</a></div>
        <div class="cb" style="padding:4px 0 0">
        ${table([
          { h: '', key: 'av' },
          { h: 'Project', key: 'sess', grow: 1, link: 1 },
          { h: 'Model', key: 'model' },
          { h: 'Tokens', key: 'tk', n: 1 },
          { h: 'Agents', key: 'ag', n: 1 },
          { h: 'Cost', key: 'cost', n: 1, cls: 'cost' },
          { h: 'Status', key: 'st' },
          { h: 'Last write', key: 'when', n: 1 },
        ], rows)}</div>
      </div>
      <div style="display:flex;flex-direction:column;gap:16px">
        <div class="card">
          <div class="ch"><h2>Projects</h2><a class="meta" href="#/cost">Cost →</a></div>
          <div class="cb">${barList(foldTail(d.projects.map(p => ({
            label: p.key, value: p.cost, text: usd(p.cost),
            sub: (100 * p.cost / (t.cost || 1)).toFixed(0) + '%',
            href: `#/sessions?project=${encodeURIComponent(p.key)}`,
            fmt: usd,
            tip: `${p.sessions} sessions · ${p.agents} agents\n${tok(p.tokens)} tokens`,
          })), 5))}
          <div style="border-top:1px solid var(--line);margin-top:12px;padding-top:12px;
            display:flex;justify-content:space-between;font-size:12.5px">
            <span class="mut">Cache saved in this window</span>
            <b style="color:var(--green)" class="num">${usd(winSaved)}
              (${winSavedPct.toFixed(0)}%)</b></div>
          </div>
        </div>
        <div class="card" style="flex:1">
          <div class="ch"><h2>Models</h2></div>
          <div class="cb">${barList(d.models.filter(m => m.cost > 0).slice(0, 4)
            .map((m, i) => ({
              label: m.label, value: m.cost, color: SERIES[i], text: usd(m.cost),
              sub: (100 * m.cost / (t.cost || 1)).toFixed(0) + '%',
              tip: `${m.api_calls.toLocaleString()} calls · ${tok(m.tokens)} tokens`,
            })))}</div>
        </div>
      </div>
    </section>

    <section class="blk" style="display:grid;
        grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px">
      <div class="card">
        <div class="ch"><h2>Spend per day</h2>
          <span class="meta">peak ${usd(Math.max(...dailyCost, 0))}/day</span></div>
        <div class="cb"><div id="dailyBars"></div></div>
      </div>
      <div class="card">
        <div class="ch"><h2>Spend, cumulative</h2>
          <span class="meta" style="color:var(--green)">${
            usd(windowSpend)} in the last ${winLabel()}</span></div>
        <div class="cb"><div id="wave"></div></div>
      </div>
    </section>

    <footer class="pagefoot"><span>Transcripts never leave this machine — the only
        network call is to Anthropic, for your plan limits.</span>
      <span>All figures are API list-price equivalents, not amounts billed.</span></footer>`;

  hydrateTips($('#view'));
  wireTable($('#view'));
  wireWindow($('#view'));
  colChart($('#dailyBars'), daily.map(r => ({
    v: r.cost, label: r.date.slice(5),
    tip: `${r.date}\n${usd(r.cost)} · ${r.sessions} sessions`,
  })), { height: 248 });
  waveChart($('#wave'), daily, { height: 248 });
};

views.sessions = async (params) => {
  const q = params.get('q') || '', project = params.get('project') || '';
  const sort = params.get('sort') || 'recent';
  const d = await api('/api/sessions', { q, project, sort });

  const rows = d.sessions.map(s => {
    const st = sessStatus(s);
    return {
      _href: `#/session/${s.id}`,
      id: `#${esc(s.short.slice(0, 6))}`,
      sess: `<b>${esc(s.project)}</b><span class="tp">${esc(s.title)}</span>`,
      model: `<span class="mchip">${esc(s.model_label)}</span>`,
      turns: s.turns, calls: s.api_calls.toLocaleString(),
      ag: agentsCell(s), tk: tok(s.tokens), cx: tok(s.peak_context),
      cost: usd(s.cost), st: stPill(st),
      when: `<span class="dur">${ago(s.ended)}</span>`,
    };
  });
  const setParam = (k, v) => {
    const p2 = new URLSearchParams(location.hash.split('?')[1] || '');
    v ? p2.set(k, v) : p2.delete(k);
    location.hash = '#/sessions?' + p2;
  };

  $('#view').innerHTML = `
    <div class="hd">
      <div><h1>Sessions</h1><p class="sub">${d.total} session${
        d.total === 1 ? '' : 's'} in the last ${winLabel()}${
        project ? ` · ${esc(project)}` : ''}${q ? ` · matching “${esc(q)}”` : ''}</p></div>
      <div class="right">
        ${project || q ? `<a class="btn" href="#/sessions">Clear filters ✕</a>` : ''}
        <input type="search" id="q" placeholder="Search titles, prompts, paths…"
               aria-label="Search sessions" value="${esc(q)}">
        ${windowPicker()}
      </div>
    </div>
    ${table([
      { h: 'ID', key: 'id' },
      { h: 'Project', key: 'sess', grow: 1, link: 1, sort: 'recent' },
      { h: 'Model', key: 'model' },
      { h: 'Turns', key: 'turns', n: 1, sort: 'turns' },
      { h: 'Calls', key: 'calls', n: 1 },
      { h: 'Agents', key: 'ag', n: 1, sort: 'agents' },
      { h: 'Tokens', key: 'tk', n: 1, sort: 'tokens' },
      { h: 'Peak context', key: 'cx', n: 1, sort: 'context' },
      { h: 'Cost', key: 'cost', n: 1, sort: 'cost', cls: 'cost' },
      { h: 'Status', key: 'st' },
      { h: 'Last write', key: 'when', n: 1, sort: 'recent' },
    ], rows, {
      sort, onSort: k => setParam('sort', k),
      empty: q || project
        ? '<b>No matches</b>Try a different search, or clear the filters.'
        : '<b>No sessions in this window</b>Widen the time window to see older work.',
    })}`;

  wireTable($('#view'), k => setParam('sort', k));
  wireWindow($('#view'));
  const qi = $('#q');
  qi.addEventListener('input', debounce(() => {
    const p2 = new URLSearchParams(location.hash.split('?')[1] || '');
    qi.value ? p2.set('q', qi.value) : p2.delete('q');
    history.replaceState(null, '', '#/sessions?' + p2);
    route();
  }, 260));
  if (q) { qi.focus(); qi.setSelectionRange(q.length, q.length); }
};

views.session = async (params, sid) => {
  const s = await api(`/api/sessions/${sid}`);
  const e = s.economics;
  const st = sessStatus(s);
  const typeRows = [
    ['Cache read', 'cache_read', SERIES[0]],
    ['Cache write · 5m', 'cache_write_5m', SERIES[1]],
    ['Cache write · 1h', 'cache_write_1h', SERIES[2]],
    ['Output', 'output', SERIES[3]],
    ['Input, uncached', 'input', SERIES[4]],
  ];

  $('#view').innerHTML = `
    <div class="crumb"><a href="#/sessions">Sessions</a> / ${esc(s.short)}</div>
    <div class="hd">
      <div><h1>${esc(s.title)}</h1>
        <p class="sub">${esc(s.project)} · ${esc(s.branch || 'no branch')} ·
          ${esc(s.model_label)} · started ${dt(s.started)} · CLI ${
          esc(s.version || '?')}</p></div>
      <div class="right">${stPill(st)}${s.live ?
        `<span class="mchip">PID ${s.pid} · ${s.cpu.toFixed(0)}% CPU · ${
          (s.rss / 1048576).toFixed(0)} MB</span>` : ''}</div>
    </div>

    <div class="kpis">
      ${[[usd(s.cost), 'Total cost', `${usd(s.cost_main)} main + ${usd(s.cost_agents)} agents`],
         [tok(s.usage.total), 'Tokens', `${pct(s.usage.cache_hit_rate)} cached`],
         [tok(s.peak_context), 'Peak context', 'largest single call'],
         [s.turns, 'Your turns', `${s.api_calls.toLocaleString()} API calls`],
         [s.agents.length, 'Subagents',
          `${(r2 => r2 ? `${r2} running now · ` : '')(
            s.agents.filter(a => a.state === 'running').length)}${
            s.tool_errors} tool errors`],
         [`<span class="dur">${dur(s.active_s)}</span>`, 'Generating',
          `of <span class="dur">${dur(s.duration_s)}</span> elapsed`]]
        .map(([v, k, n]) => `<div class="kpi">
          <div class="k">${k}</div><div class="v">${v}</div>
          <div class="k" style="margin-top:6px;font-weight:400">${n}</div></div>`).join('')}
    </div>

    <section class="blk grid cols4">
      <div class="card"><div class="ch"><h2>Context growth</h2>
        <span class="meta">per call</span></div>
        <div class="cb"><div id="ctxChart"></div></div></div>
      ${(s.hourly || []).some(b => b.cost > 0 || b.out > 0) ? `
      <div class="card"><div class="ch"><h2>Output tok / hour</h2>
        <span class="meta">24h · incl. agents</span></div>
        <div class="cb"><div id="hrTok"></div></div></div>
      <div class="card"><div class="ch"><h2>Spend / hour</h2>
        <span class="meta">24h · incl. agents</span></div>
        <div class="cb"><div id="hrCost"></div></div></div>` : ''}
      <div class="card"><div class="ch"><h2>Cumulative spend</h2>
        <span class="meta">incl. agents</span></div>
        <div class="cb"><div id="costChart"></div></div></div>
    </section>

    <section class="blk grid cols3">
      <div class="card"><div class="ch"><h2>Cost composition</h2></div>
        <div class="cb">${barList(typeRows.slice()
          .sort((a, b) => e.cost[b[1]] - e.cost[a[1]])
          .filter(([, k]) => e.cost[k] > 0)
          .map(([lab, k, c]) => ({
            label: lab, value: e.cost[k], color: c, text: usd(e.cost[k]),
            sub: (100 * e.cost[k] / (e.total_cost || 1)).toFixed(1) + '%',
            tip: `${e.tokens[k].toLocaleString()} tokens`,
          })))}
        <dl class="kv" style="margin-top:16px">
          <dt>Uncached equivalent</dt><dd>${usd(s.uncached_cost)}</dd>
          <dt>Saved by caching</dt><dd style="color:var(--green)">${
            usd(s.uncached_cost - s.cost_main)}</dd>
          <dt>Real cost / 1M output</dt><dd>${usd(e.effective_output_rate)}</dd>
          <dt>Avg output rate</dt><dd>${s.output_tps.toFixed(0)} tok/s</dd>
        </dl></div></div>
      <div class="card"><div class="ch"><h2>Tools</h2>
        <span class="meta">${Object.values(s.tools).reduce((a, b) => a + b, 0)} calls
          · click one for detail</span></div>
        <div class="cb" id="toolsBody">${barList(foldTail(Object.entries(s.tools)
          .sort((a, b) => b[1] - a[1])
          .map(([n, c]) => ({ label: n, value: c, text: String(c), act: n })), 9))}</div></div>
      <div class="card">
        <div class="ch"><h2>Models used</h2>
          <span class="meta">${s.models.length}</span></div>
        <div class="cb">${barList(
          s.models.map((m, i) => ({ label: m.label, value: m.cost,
            color: SERIES[i % SERIES.length],
            text: usd(m.cost), sub: `${m.calls} calls`, tip: `${tok(m.tokens)} tokens` })))}
        </div></div>
    </section>

    ${s.agents.length ? `<section class="blk"><div class="card">
      <div class="ch"><h2>Subagents</h2><span class="meta">${usd(s.cost_agents)} total</span></div>
      <div class="cb" style="padding:4px 0 0">${table([
        { h: 'Topic', key: 'topic', grow: 1, link: 1 },
        { h: 'Type', key: 'type' }, { h: 'Model', key: 'model' },
        { h: 'Calls', key: 'calls', n: 1 }, { h: 'Tokens', key: 'tk', n: 1 },
        { h: 'Time', key: 'time', n: 1 }, { h: 'Cost', key: 'cost', n: 1, cls: 'cost' },
        { h: 'Status', key: 'st' }, { h: 'Started', key: 'when', n: 1 }],
        s.agents.map(a => ({
          _href: `#/agent/${s.id}/${a.id}`,
          topic: esc(a.topic),
          type: `<span class="mut">${esc(a.type)}</span>`,
          model: `<span class="mchip">${esc(a.model_label)}</span>`,
          calls: a.api_calls, tk: tok(a.tokens),
          time: `<span class="dur">${dur(a.duration_s)}</span>`,
          cost: usd(a.cost), st: agentPill(a.state),
          when: `<span class="dur">${ago(a.started)}</span>`,
        })))}</div></div></section>` : ''}

    <section class="blk grid cols2">
      <div class="card"><div class="ch"><h2>Your prompts</h2>
        <span class="meta">${s.prompts.length}</span></div>
        <div class="cb" style="max-height:460px;overflow:auto">
          ${s.prompts.length ? s.prompts.map(p =>
            `<div class="prompt"><span class="when">${dt(p.ts)}</span>${
              esc(p.text)}</div>`).join('')
            : '<div class="empty">No prompts recorded.</div>'}</div></div>
      <div class="card"><div class="ch"><h2>Files touched</h2>
        <span class="meta">${s.files.length}</span></div>
        <div class="cb" style="max-height:460px;overflow:auto">
          ${s.files.length ? barList(s.files.slice(0, 40).map(f => ({
            label: f.path.split('/').slice(-2).join('/'), value: f.edits,
            text: String(f.edits), tip: f.path })))
            : '<div class="empty">No file edits recorded.</div>'}</div></div>
    </section>`;

  hydrateTips($('#view'));
  wireTable($('#view'));
  const toolsHost = $('#toolsBody');
  if (toolsHost) wireToolRows(toolsHost, s.id);
  const tl = s.timeline;
  lineChart($('#ctxChart'), tl.map(p => ({ t: p.t, v: p.ctx })), {
    height: 150, color: '#e8930c', hex: '#e8930c',
    label: p => `${new Date(p.t * 1000).toLocaleString()}\ncontext ${tok(p.v)} tokens`,
  });
  // spend_curve folds agent costs in, so the line ends at the Total cost KPI
  // instead of at the main-thread subtotal.
  let run = 0;
  const curve = (s.spend_curve && s.spend_curve.length)
    ? s.spend_curve
    : tl.map(p => ({ t: p.t, v: (run += p.cost) }));
  lineChart($('#costChart'), curve, {
    height: 150, fmt: usdAxis,
    label: p => `${new Date(p.t * 1000).toLocaleString()}\n${usd(p.v)} spent`,
  });
  const hb = s.hourly || [];
  if (hb.some(b => b.cost > 0 || b.out > 0)) {
    const hl = t => new Date(t * 1000).toLocaleTimeString(undefined,
      { hour: 'numeric' });
    const hd = t => new Date(t * 1000).toLocaleString(undefined,
      { weekday: 'short', hour: '2-digit', minute: '2-digit' });
    colChart($('#hrCost'), hb.map(b => ({
      v: b.cost, label: hl(b.t),
      tip: `${hd(b.t)} – ${hl(b.t + 3600)}\n${usd(b.cost)} spent`,
    })), { height: 150 });
    colChart($('#hrTok'), hb.map(b => ({
      v: b.out, label: hl(b.t),
      tip: `${hd(b.t)} – ${hl(b.t + 3600)}\n${tok(b.out)} output tokens`,
    })), { height: 150, color: 'var(--gold)', fmt: tok });
  }
};

views.agents = async (params) => {
  const q = params.get('q') || '', type = params.get('type') || '';
  const sort = params.get('sort') || 'cost';
  const d = await api('/api/agents', { q, type, sort });

  $('#view').innerHTML = `
    <div class="hd">
      <div><h1>Agents</h1><p class="sub">${d.total} subagent runs in the last ${
        winLabel()}</p></div>
      <div class="right">
        ${type ? `<a class="btn" href="#/agents">Clear ✕</a>` : ''}
        <input type="search" id="q" placeholder="Search agent topics…"
               aria-label="Search agents" value="${esc(q)}">
        <div class="pills" id="sortSeg" role="group" aria-label="Sort agents">
          ${['cost', 'recent', 'tokens', 'duration'].map(k =>
            `<button data-k="${k}" class="${k === sort ? 'on' : ''}">${k}</button>`).join('')}
        </div>
        ${windowPicker()}
      </div>
    </div>

    <section class="grid cols2">
      <div class="card"><div class="ch"><h2>Cost per run</h2>
        <span class="meta">log-spaced bins</span></div>
        <div class="cb"><div id="dist"></div>
        <dl class="kv" style="margin-top:12px">
          ${Object.entries(d.percentiles).map(([p, v]) =>
            `<dt>p${p}</dt><dd>${usd(v)}</dd>`).join('')}
        </dl></div></div>
      <div class="card"><div class="ch"><h2>By type</h2></div>
        <div class="cb">${barList(d.by_type.map((b, i) => ({
          label: b.key, value: b.cost, color: SERIES[i % SERIES.length],
          text: usd(b.cost), sub: `${b.agents}`,
          href: `#/agents?type=${encodeURIComponent(b.key)}`,
          tip: `${b.agents} runs · ${tok(b.tokens)} tokens\n${
            b.api_calls.toLocaleString()} calls`,
        })))}</div></div>
    </section>

    <section class="blk">${table([
      { h: 'Topic', key: 'topic', grow: 1, link: 1 },
      { h: 'Type', key: 'type' }, { h: 'Project', key: 'proj' },
      { h: 'Model', key: 'model' }, { h: 'Calls', key: 'calls', n: 1 },
      { h: 'Tokens', key: 'tk', n: 1 }, { h: 'Time', key: 'time', n: 1 },
      { h: 'Tok/s', key: 'tps', n: 1 }, { h: 'Cost', key: 'cost', n: 1, cls: 'cost' },
      { h: 'Status', key: 'st' }, { h: 'When', key: 'when', n: 1 }],
      d.agents.map(a => ({
        _href: `#/agent/${a.session_id}/${a.id}`,
        topic: esc(a.topic),
        type: `<span class="mut">${esc(a.type)}</span>`,
        proj: `<span class="mut">${esc(a.project)}</span>`,
        model: `<span class="mchip">${esc(a.model_label)}</span>`,
        calls: a.api_calls, tk: tok(a.tokens),
        time: `<span class="dur">${dur(a.duration_s)}</span>`,
        tps: a.output_tps.toFixed(0), cost: usd(a.cost),
        st: agentPill(a.state),
        when: `<span class="dur">${ago(a.started)}</span>`,
      })))}</section>`;

  hydrateTips($('#view'));
  wireTable($('#view'));
  wireWindow($('#view'));
  colChart($('#dist'), d.distribution.map(b => ({
    v: b.count, label: usd(b.lo),
    tip: `${usd(b.lo)} – ${usd(b.hi)}\n${b.count} agents`,
  })), { height: 138, fmt: v => Math.round(v) });
  $$('#sortSeg button').forEach(b => b.addEventListener('click', () => {
    const p = new URLSearchParams(location.hash.split('?')[1] || '');
    p.set('sort', b.dataset.k);
    location.hash = '#/agents?' + p;
  }));
  const qi = $('#q');
  qi.addEventListener('input', debounce(() => {
    const p = new URLSearchParams(location.hash.split('?')[1] || '');
    qi.value ? p.set('q', qi.value) : p.delete('q');
    history.replaceState(null, '', '#/agents?' + p);
    route();
  }, 260));
  if (q) { qi.focus(); qi.setSelectionRange(q.length, q.length); }
};

views.agent = async (params, sid, aid) => {
  const a = await api(`/api/agents/${sid}/${aid}`);
  $('#view').innerHTML = `
    <div class="crumb"><a href="#/agents">Agents</a> /
      <a href="#/session/${esc(sid)}">${esc(a.session_title || sid.slice(0, 8))}</a> /
      ${esc(a.id.slice(0, 10))}</div>
    <div class="hd">
      <div><h1>${esc(a.topic)}</h1>
        <p class="sub">${esc(a.type)} · ${esc(a.model_label)} · ${esc(a.project)}${
        a.workflow_id ? ` · workflow ${esc(a.workflow_id.replace('wf_', ''))}` : ''}</p></div>
      <div class="right">${agentPill(a.state)}</div>
    </div>
    ${a.owns ? `<p class="sub" style="margin:-8px 0 14px"><code>${esc(a.owns)}</code></p>` : ''}
    <div class="kpis">
      ${[[usd(a.cost), 'Cost', `${a.api_calls} API calls`],
         [tok(a.tokens), 'Tokens', `${pct(a.cache_hit_rate)} cached`],
         [tok(a.output_tokens), 'Output', `${a.output_tps.toFixed(0)} tok/s`],
         [`<span class="dur">${dur(a.duration_s)}</span>`, 'Duration',
          `started ${ago(a.started)} · ${dt(a.started)}`],
         [a.tool_errors, 'Tool errors', `${Object.values(a.tools)
           .reduce((x, y) => x + y, 0)} tool calls`]]
        .map(([v, k, n]) => `<div class="kpi"><div class="k">${k}</div>
          <div class="v">${v}</div>
          <div class="k" style="margin-top:6px;font-weight:400">${n}</div></div>`).join('')}
    </div>
    <section class="blk grid cols2">
      <div class="card"><div class="ch"><h2>Tools used</h2></div>
        <div class="cb">${Object.keys(a.tools).length ? barList(Object.entries(a.tools)
          .sort((x, y) => y[1] - x[1]).map(([n, c]) =>
            ({ label: n, value: c, text: String(c) })))
          : '<div class="empty">No tool calls.</div>'}</div></div>
      <div class="card"><div class="ch"><h2>Result</h2></div>
        <div class="cb">${a.result
          ? `<div class="prompt">${esc(a.result)}</div>`
          : '<div class="empty">No result recorded — the agent may not have completed.</div>'}</div></div>
    </section>
    <section class="blk"><div class="card">
      <div class="ch"><h2>Prompt</h2><span class="meta">what this agent was asked to do</span></div>
      <div class="cb"><div class="prompt" style="max-height:520px">${
        esc(a.prompt || '—')}</div></div>
    </div></section>`;
  hydrateTips($('#view'));
};

views.workflows = async () => {
  const d = await api('/api/workflows');
  const wfs = d.workflows;

  const trimCommon = labels => {
    if (labels.length < 2) return labels;
    let i = 0;
    const first = labels[0];
    while (i < first.length && labels.every(l => l[i] === first[i])) i++;
    while (i > 0 && !/\s/.test(first[i - 1])) i--;
    return i > 12 ? labels.map(l => l.slice(i) || l) : labels;
  };

  const cards = wfs.map(w => {
    // An agent that never wrote a timestamp has no start or end; clamp it to
    // the run's own bounds so its bar renders instead of computing to NaN.
    const starts = w.lanes.map(l => l.start).filter(v => v != null);
    const ends = w.lanes.map(l => l.end).filter(v => v != null);
    const t0 = starts.length ? Math.min(...starts) : 0;
    const t1 = Math.max(ends.length ? Math.max(...ends) : 0, t0);
    const span = Math.max(1, t1 - t0);
    const at = l => (l.start != null ? l.start : t0);
    const to = l => (l.end != null ? l.end : t1);
    const ordered = w.lanes.slice()
      .sort((a, b) => (to(b) - at(b)) - (to(a) - at(a)));
    const shortLabels = trimCommon(ordered.map(l => l.topic || ''));
    const lanes = ordered.map((l, li) => {
      const x = ((at(l) - t0) / span) * 100;
      const wd = Math.max(0.6, ((to(l) - at(l)) / span) * 100);
      const col = l.state === 'running' ? 'var(--gold)'
                : l.state === 'stopped' ? 'var(--line2)' : 'var(--vio)';
      const topic = l.topic || '(untitled)';
      return `<div class="glane">
        <span class="gl" title="${esc(topic)}">${esc(shortLabels[li].slice(0, 60))}</span>
        <div class="gtrack"><div class="gbar" style="left:${x.toFixed(2)}%;width:${
          wd.toFixed(2)}%;background:${col}"
          data-tip="${esc(topic.slice(0, 70))}\n${esc(usd(l.cost))} · ${
          esc(tok(l.tokens))} tokens\n${esc(dur(to(l) - at(l)))}"></div></div></div>`;
    }).join('');
    const ticks = [0, .25, .5, .75, 1].map(f =>
      `<span>${f === 0 ? '0' : esc(dur(span * f))}</span>`).join('');
    return `<div class="card" style="margin-bottom:14px">
      <div class="ch">
        <div><h2>${esc(w.topic.slice(0, 84))}</h2>
          <p class="sub num">${esc(w.short)} · ${esc(w.project)} · ${dt(w.started)}</p></div>
        <div style="text-align:right;white-space:nowrap">
          <div class="num" style="font-weight:800;font-size:1.05rem">${usd(w.cost)}</div>
          <div class="meta">${w.completed}/${w.agents} done · peak ×${
            w.peak_parallelism}</div></div>
      </div>
      <div class="cb">
        <div class="gantt">${lanes}</div>
        <div class="gaxis"><span></span><span class="ticks">${ticks}</span></div>
        <div class="meta" style="display:flex;justify-content:space-between;margin-top:9px">
          <span><span class="dur">${dur(w.duration_s)}</span> wall clock · ${
            w.agents} agents</span>
          <span>${tok(w.tokens)} tokens</span></div>
      </div></div>`;
  }).join('');

  $('#view').innerHTML = `
    <div class="hd"><div><h1>Workflows</h1>
      <p class="sub">${wfs.length} fan-out runs · each bar is one agent, positioned
        by when it ran</p></div>
      <div class="right">${windowPicker()}${legend([
        ['completed', 'var(--vio)'], ['running', 'var(--gold)'],
        ['stopped', 'var(--line2)']])}</div>
    </div>
    ${cards || '<div class="empty"><b>No workflow runs in this window</b>' +
      'Widen the time window to see older fan-outs.</div>'}`;
  hydrateTips($('#view'));
  wireWindow($('#view'));
};

// severity → color for plan-limit bars (bright variants; text uses AA inks)
const limitColor = l =>
  (l.severity !== 'normal' || l.percent >= 90) ? 'var(--red-bright)'
  : l.percent >= 70 ? 'var(--amber-bright)' : 'var(--green-bright)';

/** {rel, abs} for a limit's reset — "2h 09m" + the local clock time it lands.
    Sub-day resets show the time ("15:10"); longer ones add the weekday
    ("Fri 08:00"), since "in 4d 18h" alone makes you do calendar math. */
const resetInfo = l => {
  if (!l.resets_in || l.resets_in <= 0) return null;
  const at = l.resets_at ? new Date(l.resets_at) : null;
  let abs = '';
  if (at && !isNaN(at)) {
    abs = l.resets_in < 86400
      ? at.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
      : at.toLocaleString(undefined,
          { weekday: 'short', hour: '2-digit', minute: '2-digit' });
  }
  return { rel: dur(l.resets_in), abs };
};

function planCard(plan) {
  if (!plan || plan.available === false) return '';
  const rows = (plan.limits || []).map(l => {
    const r = resetInfo(l);
    return `
    <div class="plrow">
      <div class="plhead"><span>${esc(l.label)}</span>
        <span class="num mut">${l.percent}% used${
          r ? ` · resets in ${r.rel}${r.abs ? ` (${esc(r.abs)})` : ''}` : ''}</span></div>
      <div class="track" style="height:7px"><div class="fill" style="width:${
        Math.min(100, l.percent)}%;background:${limitColor(l)}"></div></div>
    </div>`;
  }).join('');
  const x = plan.extra;
  return `<div class="card">
    <div class="ch"><h2>Plan limits</h2>
      <span class="meta">${esc(plan.plan)} plan${
        plan.source === 'live' ? ' · live'
        : plan.age_s != null ? ` · as of ${dur(plan.age_s)} ago` : ''}</span></div>
    <div class="cb">
      ${rows || '<div class="empty">No utilization data cached yet — run /usage once in Claude Code.</div>'}
      ${x ? `<dl class="kv" style="margin-top:16px">
        <dt>Extra usage</dt><dd>${x.enabled ? 'enabled' : 'off'}${
          x.reason === 'out_of_credits' ? ' · out of credits' : ''}</dd>
        <dt>Credits used</dt><dd>${x.used.toFixed(2)} / ${
          x.limit.toFixed(2)} ${esc(x.currency)}</dd></dl>` : ''}
      <p class="mut" style="font-size:.74rem;margin:14px 0 0;line-height:1.55">
        ${plan.source === 'live'
          ? 'Fetched from Anthropic with the OAuth token Claude Code already stores.'
          : "Anthropic unreachable — showing Claude Code's cached copy."}</p>
    </div></div>`;
}

views.cost = async () => {
  const [d, plan] = await Promise.all([
    api('/api/summary'),
    api('/api/plan').catch(() => null),
  ]);
  state.summary = d;
  const t = d.totals, c = d.cache, e = d.economics;
  // Spend within the window, from the same daily series the chart draws —
  // whole-session totals disagree with it whenever a session straddles the
  // window edge.
  const wSpend = d.daily.reduce((a, r) => a + r.cost, 0);
  const wUncached = d.daily.reduce((a, r) => a + (r.uncached || 0), 0);
  const wSaved = Math.max(0, wUncached - wSpend);
  const wSavedPct = wUncached ? 100 * wSaved / wUncached : 0;
  const typeRows = [
    ['Cache read', 'cache_read', SERIES[0]],
    ['Cache write · 5m', 'cache_write_5m', SERIES[1]],
    ['Cache write · 1h', 'cache_write_1h', SERIES[2]],
    ['Output', 'output', SERIES[3]],
    ['Input, uncached', 'input', SERIES[4]],
  ];
  $('#view').innerHTML = `
    <div class="hd"><div><h1>Cost</h1>
      <p class="sub">Last ${winLabel()} · all figures are API list-price equivalents,
        not amounts billed.</p></div>
      <div class="right">${windowPicker()}</div></div>

    <div class="kpis">
      ${[[usd(wSpend), 'Actual', 'with prompt caching', 'up',
          `↓ ${wSavedPct.toFixed(0)}% vs uncached`],
         [usd(wUncached), 'Without caching', 'same tokens, full input rate',
          'bad', `${(wUncached / (wSpend || 1)).toFixed(1)}× more`],
         [usd(wSaved), 'Saved', 'by prompt caching', 'up',
          `${wSavedPct.toFixed(0)}% of counterfactual`],
         [usd(e.effective_output_rate), 'Real cost / 1M output', `list is ${
          usd(e.list_output_rate)}`, 'bad', `${e.multiple_of_list.toFixed(0)}× list`],
         [pct(c.hit_rate), 'Cache hit rate', `${tok(c.cache_read)} tokens read`,
          'up', 'reads bill at 0.1×']]
        .map(([v, k, n, dc, dl]) => `<div class="kpi"><div class="k">${k}</div>
          <div class="v">${v}</div><span class="delta ${dc}">${dl}</span>
          <div class="k" style="margin-top:6px;font-weight:400">${n}</div></div>`).join('')}
    </div>

    <section class="blk"${plan && plan.available !== false
      ? ' style="display:grid;grid-template-columns:1fr minmax(320px,400px);gap:16px"'
      : ''}>
      <div class="card">
        <div class="ch"><h2>Daily spend</h2><span class="meta">${usd(wSpend)}
          total</span></div>
        <div class="cb"><div id="dailyChart"></div></div></div>
      ${planCard(plan)}
    </section>

    <section class="blk grid cols2">
      <div class="card"><div class="ch"><h2>Where the money goes</h2>
        <span class="meta">${usd(e.total_cost)}</span></div>
        <div class="cb">${barList(typeRows.slice()
          .sort((a, b) => e.cost[b[1]] - e.cost[a[1]])
          .map(([lab, k, cc]) => ({
            label: lab, value: e.cost[k], color: cc, text: usd(e.cost[k]),
            sub: (100 * e.cost[k] / (e.total_cost || 1)).toFixed(1) + '%',
            tip: `${e.tokens[k].toLocaleString()} tokens`,
          })))}
        <p class="mut" style="font-size:.78rem;margin:14px 0 0;line-height:1.6">
          Output is ${(100 * e.output_share).toFixed(2)}% of all tokens — every output
          token is paid for by re-reading the transcript that produced it.</p></div></div>
      <div class="card"><div class="ch"><h2>When it is spent</h2>
        <span class="meta">by hour of week</span></div>
        <div class="cb" id="heat"></div></div>
    </section>

    <section class="blk"><div class="card">
      <div class="ch"><h2>By project</h2></div>
      <div class="cb">${barList(foldTail(d.projects.map(p => ({
        label: p.key, value: p.cost, text: usd(p.cost),
        sub: (100 * p.cost / (t.cost || 1)).toFixed(0) + '%',
        href: `#/sessions?project=${encodeURIComponent(p.key)}`, fmt: usd,
        tip: `${p.sessions} sessions · ${p.agents} agents\n${tok(p.tokens)} tokens · ${
          pct(p.cache_hit_rate)} cached`,
      })), 9))}</div></div></section>

    <section class="blk"><div class="card">
      <div class="ch"><h2>By model</h2><span class="meta">exact per-call attribution</span></div>
      <div class="cb" style="padding:4px 0 0">${table([
        { h: 'Model', key: 'm' }, { h: 'Rate in/out per MTok', key: 'rate' },
        { h: 'Calls', key: 'calls', n: 1 }, { h: 'Input', key: 'inp', n: 1 },
        { h: 'Output', key: 'out', n: 1 }, { h: 'Cache hit', key: 'hit', n: 1 },
        { h: 'Cost', key: 'cost', n: 1, cls: 'cost' }],
        d.models.map((m, i) => {
          const r = d.rates[m.key] || {};
          return {
            m: `<span class="dot" style="background:${SERIES[i % 5]}"></span>${
              esc(m.label)}`,
            rate: `<span class="mut num">$${r.input ?? '?'} / $${r.output ?? '?'}${
              r.legacy ? ' *' : ''}</span>`,
            calls: m.api_calls.toLocaleString(), inp: tok(m.input_tokens),
            out: tok(m.output_tokens), hit: pct(m.cache_hit_rate),
            cost: usd(m.cost),
          };
        }))}
      </div>
      <p class="mut" style="font-size:.78rem;padding:10px 20px 16px;margin:0">
        * retired model — rate is a historical estimate.</p></div></section>`;
  hydrateTips($('#view'));
  wireTable($('#view'));
  wireWindow($('#view'));
  colChart($('#dailyChart'), d.daily.map(r => ({
    v: r.cost, label: r.date.slice(5),
    tip: `${r.date}\n${usd(r.cost)} · ${r.sessions} sessions`,
  })), { height: 190 });
  $('#heat').innerHTML = heatmap(d.heatmap);
  hydrateTips($('#heat'));
};

views.tools = async () => {
  const d = state.summary = await api('/api/summary');
  const total = d.tools.reduce((a, b) => a + b.total, 0) || 1;
  const max = Math.max(...d.tools.map(x => x.total), 1);
  $('#view').innerHTML = `
    <div class="hd"><div><h1>Tools</h1>
      <p class="sub">${total.toLocaleString()} tool calls in the last ${winLabel()}
        · main thread vs subagents · click a tool to see what it ran</p></div>
      <div class="right">${windowPicker()}${legend([
        ['main thread', 'var(--vio)'], ['subagents', 'var(--gold)']])}</div>
    </div>
    <div class="card"><div class="cb">
      <div class="bars">${d.tools.map(t2 => {
        const w = (t2.total / max) * 100;
        const mainPct = t2.total ? (t2.main / t2.total) * 100 : 0;
        return `<button type="button" class="brow" data-act="${esc(t2.tool)}"
          title="Show what ${esc(t2.tool)} actually ran">
          <span class="lab">${esc(t2.tool)}</span>
          <div class="track" data-tip="${esc(t2.tool)}\nmain ${t2.main} · agents ${t2.agent}">
            <div class="fill" style="width:${w.toFixed(2)}%;background:var(--gold);position:relative">
              <div style="position:absolute;inset:0;width:${mainPct.toFixed(2)}%;
                background:var(--vio);border-radius:99px 0 0 99px"></div></div></div>
          <span class="val">${t2.total.toLocaleString()}<i>${
            ((t2.total / total) * 100).toFixed(1)}%</i></span></button>`;
      }).join('')}</div>
    </div></div>`;
  hydrateTips($('#view'));
  wireWindow($('#view'));
  wireToolRows($('#view'), null);
};

// ── router (instant paint, cancellation, stale-response guard) ────────
let navToken = 0;
let navAbort = null;

async function route(silent) {
  // silent=true re-runs the current view for fresh data without wiping the
  // DOM first — no progress bar, no skeleton, content swaps when ready.
  silent = silent === true;
  const token = ++navToken;
  if (navAbort) navAbort.abort();
  navAbort = new AbortController();
  state.signal = navAbort.signal;

  const raw = location.hash.slice(1) || '/overview';
  const [path, qs] = raw.split('?');
  const params = new URLSearchParams(qs || '');
  const seg = path.split('/').filter(Boolean);
  const name = seg[0] || 'overview';

  const PARENT = { session: 'sessions', agent: 'agents' };
  const section = PARENT[name] || name;
  $$('.nav a').forEach(a => {
    const on = a.getAttribute('href') === '#/' + section;
    a.classList.toggle('active', on);
    if (on) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  document.title = `${(PAGE[name] || {}).title || 'Dashboard'} · Claude Code Monitor`;

  const fn = views[name];
  if (!fn) { location.hash = '#/overview'; return; }

  if (!silent) progStart();
  const skelTimer = silent ? null : paintShell(name);
  try {
    await fn(params, ...seg.slice(1));
  } catch (err) {
    if (err.name !== 'AbortError' && token === navToken) {
      if (silent) {
        console.warn('background refresh failed, keeping current view', err);
      } else {
        $('#view').innerHTML = `<div class="errbox"><b>Couldn't load this view</b>
          The dashboard reached the server but the request failed.
          <div><code>${esc(err.message)}</code></div>
          <button class="btn" onclick="location.reload()">Reload</button></div>`;
        console.error(err);
      }
    }
  } finally {
    if (skelTimer) clearTimeout(skelTimer);
    if (!silent && token === navToken) progDone();
  }
}

// ── live poll → sidebar telemetry ─────────────────────────────────────
const STREAM_KEEP = 60;
const stream = { pts: new Array(STREAM_KEEP).fill(0) };

function drawSideSpark() {
  const host = $('#sideSpark');
  if (!host) return;
  const w = host.clientWidth || 180, h = 26;
  const mx = Math.max(...stream.pts, 1);
  const pts = stream.pts.map((v, i) =>
    [i * w / (STREAM_KEEP - 1), 2 + (1 - v / mx) * (h - 5)]);
  const line = smoothPath(pts, 10);
  host.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <path d="${line}L${w} ${h}L0 ${h}Z" fill="rgba(34,197,94,.14)"/>
    <path d="${line}" fill="none" stroke="#22c55e" stroke-width="1.6"
      stroke-linecap="round"/></svg>`;
}

async function poll() {
  try {
    const d = await (await fetch('/api/live')).json();
    state.live = d;
    const n = d.live.length;
    const tps = d.tps_now != null ? d.tps_now
      : d.live.reduce((a, s) => a + (s.output_tps || 0), 0);
    stream.pts.push(n ? tps : 0);
    while (stream.pts.length > STREAM_KEEP) stream.pts.shift();

    $('#sideInfo').classList.toggle('on', n > 0);
    $('#sideLive').innerHTML = n
      ? `<i></i>${n} session${n > 1 ? 's' : ''} live`
      : '<i></i>No sessions running';
    $('#sideStats').innerHTML = n
      ? `${Math.round(tps).toLocaleString()} tok/s out · ${
          d.running_agents.length} agents<br>${usd(d.burn_rate_hourly)}/hr burn`
      : `${usd(d.spend_24h)} spent in 24h`;
    drawSideSpark();

    const badge = $('#liveBadge');
    badge.hidden = n === 0;
    badge.textContent = n;
    if (!state.plan) fetchPlan();
  } catch (e) { /* server restarting; retry next tick */ }
}

// ── plan & limits → sidebar ───────────────────────────────────────────
function paintPlan(p) {
  const box = $('#planBox');
  if (!p || p.available === false) { box.hidden = true; return; }
  const sev = l => (l.severity !== 'normal' || l.percent >= 90) ? '#f87171'
    : l.percent >= 70 ? '#fbbf24' : '#22c55e';
  $('#planName').textContent = p.plan + ' plan';
  $('#planAge').textContent = p.source === 'live' ? 'live'
    : p.age_s != null ? dur(p.age_s) + ' ago' : '';
  $('#planRows').innerHTML = (p.limits || []).map(l => {
    const r = resetInfo(l);
    return `
    <div class="pl" title="${esc(l.label)} — ${l.percent}% used${
      r ? `, resets in ${r.rel}${r.abs ? ` (${r.abs})` : ''}` : ''}">
      <span class="plk">${esc(l.label.replace(' · all models', ' all')
        .replace('Week · ', 'Wk '))}</span>
      <span class="plt"><i style="width:${Math.min(100, l.percent)}%;background:${
        sev(l)}"></i></span>
      <span class="plv">${l.percent}%</span>
      ${r ? `<span class="plr">↻ ${r.rel}${r.abs ? ` · ${esc(r.abs)}` : ''}</span>` : ''}
    </div>`;
  }).join('');
  box.hidden = false;
}
async function fetchPlan() {
  try {
    state.plan = await (await fetch('/api/plan')).json();
    paintPlan(state.plan);
  } catch (e) { /* server restarting; keep last */ }
}

// ── boot ──────────────────────────────────────────────────────────────
addEventListener('hashchange', route);

function syncThemeBtn() {
  const explicit = document.documentElement.getAttribute('data-theme');
  const dark = explicit
    ? explicit === 'dark'
    : matchMedia('(prefers-color-scheme: dark)').matches;
  $('#themeBtn').setAttribute('aria-checked', String(dark));
}
$('#themeBtn').addEventListener('click', () => {
  const cur = document.documentElement.getAttribute('data-theme');
  const dark = cur ? cur === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  const next = dark ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('cm.theme', next);
  syncThemeBtn();
  route();
});
$('#reloadBtn').addEventListener('click', async e => {
  const btn = e.currentTarget;
  btn.disabled = true;
  await fetch('/api/reindex', { method: 'POST' });
  invalidateApi();
  btn.disabled = false;
  route();
});

const savedDays = localStorage.getItem('cm.days');
if (savedDays !== null) state.days = +savedDays;
syncThemeBtn();

poll();
setInterval(poll, 3000);
fetchPlan();
setInterval(fetchPlan, 180000);
// Silent refresh for the pages that show live state — otherwise the running
// counts and status pills are a snapshot of whenever you navigated in.
const LIVE_PAGES = new Set(['overview', 'sessions', 'agents', 'workflows']);
setInterval(() => {
  const page = (location.hash.slice(1) || '/overview')
    .split('?')[0].split('/').filter(Boolean)[0] || 'overview';
  if (!LIVE_PAGES.has(page)) return;
  if (document.hidden) return;
  // Never re-render under someone's fingers: typing in search or selecting
  // text to copy would be wiped by the DOM swap.
  const ae = document.activeElement;
  if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA')) return;
  if ((getSelection() || '').toString()) return;
  const y = scrollY;
  invalidateApi();
  route(true).then(() => scrollTo(0, y));
}, 12000);
route();
addEventListener('resize', debounce(() => { route(true); drawSideSpark(); }, 250));
