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

// ── token kinds ───────────────────────────────────────────────────────
// The four buckets every token view draws, in fixed stacking/legend order
// with a fixed palette slot each — a kind keeps its colour on every page and
// in every mode, whichever kinds happen to be visible.
//
// This is not the API's own split. Claude Code caches the whole prompt, so
// the API's input_tokens is a ~100-token stub and everything new lands in
// "cache write" — together with context re-sent after the cache lapsed. The
// parser separates those two by comparing each call's context size with the
// previous call's, which is the split that actually explains a bill.
const KINDS = [
  { key: 'fresh', label: 'Fresh input', color: 'var(--s1)', hex: '#2a78d6',
    note: 'read for the first time — your prompts, tool results, its own earlier replies; full price' },
  { key: 'resent', label: 'Re-sent', color: 'var(--s2)', hex: '#eb6834',
    note: 'context the model had already read, written into the cache again at full price — the cache lapsed during an idle gap, or an earlier part of the prompt changed' },
  { key: 'cache_read', label: 'Cache read', color: 'var(--s3)', hex: '#1baf7a',
    note: 're-read from the prompt cache at 0.1× the input rate — the same context, every call' },
  { key: 'output', label: 'Output', color: 'var(--s4)', hex: '#eda100',
    note: 'what the model generated — the new work' },
];
const rereadX = x => x >= 10 ? `×${Math.round(x)}` : `×${(+x || 0).toFixed(1)}`;
const KIND = Object.fromEntries(KINDS.map(k => [k.key, k]));
const kindsOf = split => KINDS.map(k => ({ ...k, v: +(split && split[k.key]) || 0 }));
const splitTotal = split => KINDS.reduce((a, k) => a + (+(split && split[k.key]) || 0), 0);
const splitInput = split => splitTotal(split) - (+(split && split.output) || 0);
// One shared view mode for every stacked token chart: cache reads are
// re-billed on every call and dwarf everything else, so "hide cache reads"
// is what makes fresh input and output readable; "share" normalises to 100%.
const TOKMODES = [['all', 'All tokens'], ['nocache', 'Hide cache reads'],
                  ['share', 'Share']];
const modeKinds = () => state.tokmode === 'nocache'
  ? KINDS.filter(k => k.key !== 'cache_read') : KINDS;
const modeShare = () => state.tokmode === 'share';

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
const state = { days: 30, summary: null, live: null, signal: null,
                tokmode: localStorage.getItem('cm.tokmode') || 'all',
                flowModel: localStorage.getItem('cm.flowmodel') || '' };
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

/** Start / middle / end labels for a time axis. Inside a single day the
    middle and end labels drop the date, so three labels fit a narrow card. */
function timeAxis(s, X, x0, x1, y) {
  const sameDay = x1 - x0 < 86400;
  const full = ts => new Date(ts * 1000).toLocaleString(undefined,
    { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  const clock = ts => new Date(ts * 1000).toLocaleTimeString(undefined,
    { hour: '2-digit', minute: '2-digit' });
  [[x0, 'start', full], [(x0 + x1) / 2, 'middle', sameDay ? clock : full],
   [x1, 'end', sameDay ? clock : full]].forEach(([tv, pos, f]) => {
    const t = svg('text', { class: 'axis-t', x: X(tv), y, 'text-anchor': pos }, s);
    t.textContent = f(tv);
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
  if (x1 > x0) timeAxis(s, X, x0, x1, H - 5);
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

// ── remembered fold-away sections ─────────────────────────────────────
// Secondary detail a reader doesn't need on every visit, folded behind one
// bar so it stops pushing the important sections below the fold. The summary
// keeps the headline numbers visible, so folded still says something.
//
// The choice lives in localStorage, not in the DOM: the session page swaps
// its whole DOM every 12s, and remembering per-element would re-open the
// section on each live refresh. Sticking it also means the next session you
// open comes up the way you left the last one. Default is folded.
const foldIsOpen = key => localStorage.getItem('cm.fold.' + key) === '1';
function foldSection(key, title, meta, body) {
  return `<details class="fold" data-fold="${esc(key)}"${
    foldIsOpen(key) ? ' open' : ' data-stale="1"'}>
    <summary><span class="caret"></span>${esc(title)}
      <span class="meta">${meta}</span></summary>
    <div class="foldbody">${body}</div></details>`;
}
function wireFolds(root) {
  $$('details[data-fold]', root || document).forEach(el =>
    el.addEventListener('toggle', () => {
      localStorage.setItem('cm.fold.' + el.dataset.fold, el.open ? '1' : '0');
      // A chart drawn inside a closed fold measured a 0px host and fell back
      // to a default width; redraw once it can actually be seen. The stale
      // flag is set only at render time for closed folds, so this never
      // loops on the toggle a freshly-rendered open fold may fire.
      if (el.open && el.dataset.stale && el.querySelector('svg.chart')) {
        delete el.dataset.stale;
        const y = scrollY;
        route(true).then(() => scrollTo(0, y));
      }
    }));
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
async function openToolCalls(tool, sid, project) {
  const m = openModal(tool);
  let d;
  try {
    d = await api(sid ? `/api/sessions/${sid}/tool-calls` : '/api/tool-calls',
                  { tool, project });
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
function wireToolRows(root, sid, project) {
  $$('.brow[data-act]', root).forEach(b =>
    b.addEventListener('click', () => openToolCalls(b.dataset.act, sid, project)));
}
function legend(pairs) {
  return '<div class="legend">' + pairs.map(([n, c]) =>
    `<span class="lg"><span class="sw" style="background:${c}"></span>${esc(n)}</span>`
  ).join('') + '</div>';
}

// ── token composition ─────────────────────────────────────────────────
/** Inline composition bar: one segment per kind, 2px surface gaps. Every
    non-zero kind gets at least 2px so a 0.3% sliver of fresh input is still
    visible — the tooltip carries the exact numbers. */
function tokBar(split, opts = {}) {
  const ks = kindsOf(split).filter(k => k.v > 0);
  const total = ks.reduce((a, k) => a + k.v, 0);
  if (!total) return '';
  const tip = ks.map(k => `${k.label.padEnd(12)}${tok(k.v).padStart(8)}  ${
    (100 * k.v / total).toFixed(1).padStart(5)}%`).join('\n');
  return `<span class="tokbar${opts.cls ? ' ' + opts.cls : ''}" data-tip="${esc(tip)}">${
    ks.map(k => `<i style="flex:${k.v} 1 0%;background:${k.color}"></i>`).join('')
  }</span>`;
}
function tokModePicker() {
  return `<div class="tmode" role="group" aria-label="Token view">${
    TOKMODES.map(([k, l]) => `<button type="button" data-tm="${k}" class="${
      k === state.tokmode ? 'on' : ''}">${l}</button>`).join('')}</div>`;
}
function wireTokMode(root) {
  $$('.tmode button[data-tm]', root || document).forEach(b =>
    b.addEventListener('click', () => {
      state.tokmode = b.dataset.tm;
      localStorage.setItem('cm.tokmode', state.tokmode);
      $$('.tmode button[data-tm]').forEach(x =>
        x.classList.toggle('on', x.dataset.tm === state.tokmode));
      const y = scrollY;
      route(true).then(() => scrollTo(0, y));
    }));
}

/** Column outline with a 4px rounded cap and a square base. */
function capRect(x, y, w, h, r) {
  r = Math.max(0, Math.min(r, h / 2, w / 2));
  if (r < 0.5) return `M${x} ${y}h${w}v${h}h${-w}z`;
  return `M${x} ${y + r}a${r} ${r} 0 0 1 ${r} ${-r}h${w - 2 * r}a${r} ${r} 0 0 1 ${r} ${r}v${
    h - r}h${-w}z`;
}

/** Stacked columns by kind. rows: [{label, tip, split}]; opts.kinds picks
    the visible kinds (fixed order kept), opts.share normalises to 100%. */
function stackChart(host, rows, opts = {}) {
  const W = host.clientWidth || 700, H = opts.height || 190;
  const pad = { t: 10, r: 6, b: 22, l: 46 };
  const kinds = opts.kinds || KINDS, share = !!opts.share, GAP = 2;
  host.innerHTML = '';
  if (!rows.length) { host.innerHTML = '<div class="empty">No data</div>'; return; }
  const tot = r => kinds.reduce((a, k) => a + (+r.split[k.key] || 0), 0);
  const totals = rows.map(tot);
  const sc = share ? { max: 1, ticks: [0, .25, .5, .75, 1] } : niceScale(Math.max(...totals));
  const plotH = H - pad.t - pad.b;
  const Y = v => H - pad.b - (v / sc.max) * plotH;
  const s = svg('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}`, height: H }, host);
  for (const v of sc.ticks) {
    const y = Y(v);
    svg('line', { class: 'grid-line', x1: pad.l, x2: W - pad.r, y1: y, y2: y }, s);
    const t = svg('text', { class: 'axis-t', x: 4, y: y + 3 }, s);
    t.textContent = share ? Math.round(v * 100) + '%' : (opts.fmt || tok)(v);
  }
  const iw = (W - pad.l - pad.r) / rows.length;
  const bw = Math.min(24, Math.max(2, iw - GAP));
  rows.forEach((r, i) => {
    const total = totals[i];
    const x = pad.l + i * iw + (iw - bw) / 2;
    const g = svg('g', { class: 'stk' }, s);
    if (total > 0) {
      const scale = share ? 1 / total : 1;
      const segs = kinds.map(k => ({ k, v: (+r.split[k.key] || 0) * scale }))
        .filter(sg => sg.v > 0);
      let acc = 0;
      segs.forEach((sg, j) => {
        const y0 = Y(acc), y1 = Y(acc + sg.v);
        acc += sg.v;
        const top = j === segs.length - 1;
        // The surface gap comes off the top of each lower segment; the top
        // segment keeps its full height and the rounded cap.
        const h = Math.max(0.6, y0 - y1 - (top ? 0 : GAP));
        if (top) svg('path', { d: capRect(x, y1, bw, h, 4), fill: sg.k.color }, g);
        else svg('rect', { x, y: y1, width: bw, height: h, fill: sg.k.color }, g);
      });
    }
    const hit = svg('rect', { x: pad.l + i * iw, y: pad.t, width: iw, height: plotH,
      fill: 'transparent', tabindex: 0 }, s);
    hit.addEventListener('mouseenter', () => g.setAttribute('opacity', .78));
    hit.addEventListener('mouseleave', () => g.removeAttribute('opacity'));
    bindTip(hit, () => `${r.tip || r.label}\n` + kinds.map(k => {
      const v = +r.split[k.key] || 0;
      return `${k.label.padEnd(12)}${tok(v).padStart(8)}  ${
        (total ? 100 * v / total : 0).toFixed(1).padStart(5)}%`;
    }).join('\n') + `\n${'total'.padEnd(12)}${tok(total).padStart(8)}`);
  });
  const maxLabels = Math.max(2, Math.min(8, Math.floor((W - pad.l) / 80)));
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

/** Stacked area over time by kind — the session's tokens per call.
    points: [{t, split}]. Layers are separated by a hairline in the surface
    colour; a crosshair reads every kind at the nearest call. */
function stackArea(host, points, opts = {}) {
  const W = host.clientWidth || 700, H = opts.height || 170;
  const pad = { t: 8, r: 8, b: 20, l: 46 };
  const kinds = opts.kinds || KINDS, share = !!opts.share;
  host.innerHTML = '';
  if (points.length < 2) { host.innerHTML = '<div class="empty">Not enough calls yet</div>'; return; }
  const tot = p => kinds.reduce((a, k) => a + (+p.split[k.key] || 0), 0);
  const totals = points.map(tot);
  const xs = points.map(p => p.t);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const sc = share ? { max: 1, ticks: [0, .25, .5, .75, 1] } : niceScale(Math.max(...totals));
  const X = t => pad.l + (x1 === x0 ? 0 : (t - x0) / (x1 - x0)) * (W - pad.l - pad.r);
  const Y = v => H - pad.b - (v / sc.max) * (H - pad.t - pad.b);
  const s = svg('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}`, height: H }, host);
  for (const v of sc.ticks) {
    const y = Y(v);
    svg('line', { class: 'grid-line', x1: pad.l, x2: W - pad.r, y1: y, y2: y }, s);
    const t = svg('text', { class: 'axis-t', x: 4, y: y + 3 }, s);
    t.textContent = share ? Math.round(v * 100) + '%' : (opts.fmt || tok)(v);
  }
  if (x1 > x0) timeAxis(s, X, x0, x1, H - 5);
  const tops = {};
  let base = points.map(() => 0);
  kinds.forEach((k, ki) => {
    const top = points.map((p, i) => base[i] + (+p.split[k.key] || 0) *
      (share ? (totals[i] ? 1 / totals[i] : 0) : 1));
    tops[k.key] = top;
    const fwd = points.map((p, i) =>
      `${i ? 'L' : 'M'}${X(p.t).toFixed(1)},${Y(top[i]).toFixed(1)}`).join('');
    const back = points.map((p, i) =>
      `L${X(p.t).toFixed(1)},${Y(base[i]).toFixed(1)}`).reverse().join('');
    svg('path', { d: fwd + back + 'Z', fill: k.color, 'fill-opacity': .72 }, s);
    if (ki < kinds.length - 1)
      svg('path', { d: fwd, fill: 'none', stroke: 'var(--card)', 'stroke-width': 1.5 }, s);
    base = top;
  });
  const hit = svg('rect', { x: pad.l, y: pad.t, width: W - pad.l - pad.r,
    height: H - pad.t - pad.b, fill: 'transparent' }, s);
  const cross = svg('line', { class: 'grid-line', y1: pad.t, y2: H - pad.b, opacity: 0 }, s);
  const mark = svg('circle', { r: 4, fill: 'var(--ink)', stroke: 'var(--card)',
    'stroke-width': 2, opacity: 0 }, s);
  hit.addEventListener('mousemove', e => {
    const bb = s.getBoundingClientRect();
    const px = (e.clientX - bb.left) * (W / bb.width);
    let bi = 0, bd = Infinity;
    points.forEach((p, i) => { const dd = Math.abs(X(p.t) - px); if (dd < bd) { bd = dd; bi = i; } });
    const p = points[bi];
    cross.setAttribute('x1', X(p.t)); cross.setAttribute('x2', X(p.t));
    cross.setAttribute('opacity', 1);
    mark.setAttribute('cx', X(p.t)); mark.setAttribute('cy', Y(base[bi]));
    mark.setAttribute('opacity', 1);
    const total = totals[bi];
    tipEl.textContent = `${new Date(p.t * 1000).toLocaleString()}${
      opts.head ? ' · ' + opts.head(p) : ''}\n` + kinds.map(k => {
      const v = +p.split[k.key] || 0;
      return `${k.label.padEnd(12)}${tok(v).padStart(8)}  ${
        (total ? 100 * v / total : 0).toFixed(1).padStart(5)}%`;
    }).join('\n') + `\n${'total'.padEnd(12)}${tok(total).padStart(8)}`;
    tipEl.classList.add('on');
    tipEl.style.left = Math.min(e.clientX + 13, innerWidth - 260) + 'px';
    tipEl.style.top = Math.max(8, e.clientY - 20 - tipEl.getBoundingClientRect().height) + 'px';
  });
  hit.addEventListener('mouseleave', () => {
    cross.setAttribute('opacity', 0); mark.setAttribute('opacity', 0); hideTip();
  });
}

/** A stat tile for one token kind: the kind's colour as a dot, the count as
    the figure, one line of context underneath, optional sparkline. */
function kindTile(k, v, note, opts = {}) {
  return `<div class="kpi kind">
    <span class="ic" style="background:color-mix(in srgb,${k.color} 14%,transparent)"><i
      class="kdot" style="background:${k.color}"></i></span>
    <div class="k" title="${esc(k.note)}">${esc(k.label)}</div>
    <div class="v">${tok(v)}</div>
    ${note ? `<span class="delta ${opts.cls || 'info'}">${note}</span>` : ''}
    ${opts.spark && opts.spark.some(x => x > 0)
      ? `<span class="spk">${sparkSVG(opts.spark, 86, 30, k.hex)}</span>` : ''}
  </div>`;
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

/** One git series per chart. mode 'committed': violet area stepping up at
    commits, diamond marker per commit. mode 'wip': green line with gaps
    where the monitor wasn't sampling. Each gets its own scale — on one axis
    a 20K-line WIP flattens a 500-line committed step into invisibility. */
function gitChart(host, rows, opts = {}) {
  const W = host.clientWidth || 700, H = opts.height || 200;
  const pad = { t: 10, r: 8, b: 24, l: 52 };
  const wip = opts.mode === 'wip';
  const commits = opts.commits || [];
  host.innerHTML = '';
  if (!rows.length) { host.innerHTML = '<div class="empty">No data</div>'; return; }
  const val = r => wip ? r.wip : r.committed;
  const mx = Math.max(...rows.map(r => val(r) || 0), 10);
  const sc = niceScale(mx);
  const x0 = opts.start, x1 = opts.end;
  const s = svg('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}`, height: H }, host);
  const X = t => pad.l + (x1 === x0 ? 0 : (t - x0) / (x1 - x0)) * (W - pad.l - pad.r);
  const Y = v => H - pad.b - (v / sc.max) * (H - pad.t - pad.b);
  for (const v of sc.ticks) {
    const y = Y(v);
    svg('line', { class: 'grid-line', x1: pad.l, x2: W - pad.r, y1: y, y2: y,
                  'stroke-dasharray': v === 0 ? '' : '3 5' }, s);
    const t = svg('text', { class: 'axis-t', x: 4, y: y + 3 }, s);
    t.textContent = tok(v);
  }
  const fmtT = ts => new Date(ts * 1000).toLocaleString(undefined,
    { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  [[x0, 'start'], [(x0 + x1) / 2, 'middle'], [x1, 'end']].forEach(([tv, pos]) => {
    const t = svg('text', { class: 'axis-t', x: X(tv), y: H - 6,
      'text-anchor': pos }, s);
    t.textContent = fmtT(tv);
  });

  const color = wip ? '#16a34a' : 'var(--vio)';
  if (wip) {
    // gap-aware line: pen up wherever there is no sample
    let wd = '', pen = false;
    for (const r of rows) {
      if (r.wip == null) { pen = false; continue; }
      wd += `${pen ? 'L' : 'M'}${X(r.t).toFixed(1)},${Y(r.wip).toFixed(1)}`;
      pen = true;
    }
    if (wd) svg('path', { class: 'ln', d: wd, stroke: color }, s);
    else host.insertAdjacentHTML('beforeend',
      '<div class="empty">No samples yet — the WIP line starts when the monitor starts.</div>');
  } else {
    // Cumulative area: the line steps up at each commit.
    const cd = rows.map((r, i) =>
      `${i ? 'L' : 'M'}${X(r.t).toFixed(1)},${Y(r.committed).toFixed(1)}`).join('');
    const gid = 'gc' + (++gradSeq);
    const defs = svg('defs', {}, s);
    const grad = svg('linearGradient', { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
    svg('stop', { offset: 0, 'stop-color': '#7b5cfa', 'stop-opacity': .2 }, grad);
    svg('stop', { offset: 1, 'stop-color': '#7b5cfa', 'stop-opacity': 0 }, grad);
    svg('path', { d: `${cd}L${X(x1)},${H - pad.b}L${X(x0)},${H - pad.b}Z`,
                  fill: `url(#${gid})` }, s);
    svg('path', { class: 'ln', d: cd, stroke: color }, s);
    const yb = H - pad.b;
    for (const c of commits) {
      const x = X(c.t);
      const m = svg('path', {
        d: `M${x} ${yb - 5}L${x + 4} ${yb}L${x} ${yb + 5}L${x - 4} ${yb}Z`,
        fill: 'var(--vio)', stroke: 'var(--card)', 'stroke-width': 1,
        tabindex: 0,
      }, s);
      bindTip(m, `${c.repo} ${c.hash}\n${c.subject.slice(0, 70)}\n+${
        c.add.toLocaleString()} lines · ${fmtT(c.t)}`);
    }
  }

  // crosshair
  const hit = svg('rect', { x: pad.l, y: pad.t, width: W - pad.l - pad.r,
    height: H - pad.t - pad.b, fill: 'transparent' }, s);
  const cross = svg('line', { class: 'grid-line', y1: pad.t, y2: H - pad.b,
    opacity: 0 }, s);
  hit.addEventListener('mousemove', e => {
    const bb = s.getBoundingClientRect();
    const px = (e.clientX - bb.left) * (W / bb.width);
    let best = rows[0], bd = Infinity;
    for (const r of rows) {
      const dd = Math.abs(X(r.t) - px);
      if (dd < bd) { bd = dd; best = r; }
    }
    cross.setAttribute('x1', X(best.t)); cross.setAttribute('x2', X(best.t));
    cross.setAttribute('opacity', 1);
    const v = val(best);
    tipEl.textContent = `${fmtT(best.t)}\n${wip ? 'WIP' : 'committed'} ${
      v == null ? '—' : v.toLocaleString() + ' lines'}`;
    tipEl.classList.add('on');
    tipEl.style.left = Math.min(e.clientX + 13, innerWidth - 220) + 'px';
    tipEl.style.top = (e.clientY - 46) + 'px';
  });
  hit.addEventListener('mouseleave', () => {
    cross.setAttribute('opacity', 0); hideTip();
  });
}

/** Diverging columns: additions up (green), deletions down (red).
    rows: [{label, up, down, tip}] */
function divChart(host, rows, opts = {}) {
  const W = host.clientWidth || 700, H = opts.height || 190;
  const pad = { t: 14, r: 6, b: 20, l: 46 };
  host.innerHTML = '';
  if (!rows.length || !rows.some(r => r.up || r.down)) {
    host.innerHTML = '<div class="empty">No commits in this range</div>'; return;
  }
  const mUp = Math.max(...rows.map(r => r.up), 1);
  const mDn = Math.max(...rows.map(r => r.down), 1);
  const ih = H - pad.t - pad.b;
  const scale = ih / (mUp + mDn);
  const y0 = pad.t + mUp * scale;
  const s = svg('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}`, height: H }, host);
  svg('line', { class: 'grid-line', x1: pad.l, x2: W - pad.r, y1: y0, y2: y0 }, s);
  const tUp = svg('text', { class: 'axis-t', x: 4, y: pad.t + 3 }, s);
  tUp.textContent = '+' + tok(mUp);
  const tDn = svg('text', { class: 'axis-t', x: 4, y: H - pad.b + 3 }, s);
  tDn.textContent = '−' + tok(mDn);
  const iw = (W - pad.l - pad.r) / rows.length;
  const bw = Math.max(2, iw - 3);
  rows.forEach((r, i) => {
    const x = pad.l + i * iw + (iw - bw) / 2;
    if (r.up > 0) {
      const h = Math.max(1.5, r.up * scale);
      bindTip(svg('rect', { x, y: y0 - h, width: bw, height: h,
        rx: Math.min(3, bw / 3), fill: '#16a34a' }, s), () => r.tip);
    }
    if (r.down > 0) {
      const h = Math.max(1.5, r.down * scale);
      bindTip(svg('rect', { x, y: y0, width: bw, height: h,
        rx: Math.min(3, bw / 3), fill: '#dc2626' }, s), () => r.tip);
    }
  });
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

// ── status helpers ────────────────────────────────────────────────────
function sessStatus(s) {
  if (s.live) {
    const a = s.activity;
    if (a && a.state === 'waiting') return { cls: 'wait', label: 'Waiting' };
    if (a && a.stalled) return { cls: 'wait', label: 'Stalled?' };
    if (a) return { cls: 'live', label: 'Working' };
    return { cls: 'live', label: 'Live' };
  }
  const age = s.ended ? (Date.now() - new Date(s.ended).getTime()) / 1000 : Infinity;
  return age < 86400 ? { cls: 'idle', label: 'Idle' } : { cls: 'done', label: 'Done' };
}
/** Live-activity line: what a live session is doing right now, read from the
    transcript tail. Compact, for table cells. poll() re-renders these in
    place every 3s and a 1s ticker advances the elapsed counter between polls. */
function actHTML(s) {
  const a = s.activity;
  if (!s.live || !a) return '';
  const detail = a.detail ? ` <code title="${esc(a.sub || a.detail)}">${
    esc(a.detail.length > 60 ? a.detail.slice(0, 60) + '…' : a.detail)}</code>` : '';
  const since = a.since_s != null ? ` — ${tickSince(a.since_s)}` : '';
  const stalled = a.stalled ? ` <span class="astall" title="No transcript writes for 15+ minutes — probably waiting for a permission approval, or abandoned">stalled?</span>` : '';
  const agents = s.agents_running ? ` <span class="mut">· ${s.agents_running} agent${
    s.agents_running > 1 ? 's' : ''} running</span>` : '';
  return `<span class="actline ${esc(a.state)}" data-actsid="${esc(s.id)}"><i></i>${
    esc(a.label)}${detail}${since}${stalled}${agents}</span>`;
}

/** Elapsed counter that the 1s ticker keeps advancing between polls. */
const tickSince = sinceS => sinceS == null ? '' :
  `<span class="asince" data-since="${Math.round(Date.now() / 1000 - sinceS)}">${
    dur(Math.max(1, sinceS))}</span>`;

/** "Happening now" panel for the session page: exactly what the session is
    waiting on right now — each in-flight command with its own timer, the
    result it is chewing on, or the fact that it's your turn — plus the
    subagents working under it. poll() re-renders it in place every 3s. */
function nowPanel(s, agents = []) {
  const a = s.activity;
  if (!s.live || !a) return '';

  const toolRow = (t, done) => `<div class="np-row${done ? ' done' : ''}">
      <span class="np-badge">${esc(t.name || '?')}</span>
      <code>${esc(t.text || '(no arguments)')}</code>
      ${t.sub ? `<span class="np-desc" title="${esc(t.sub)}">${esc(t.sub)}</span>` : ''}
      ${done ? '<span class="np-ret">✓ returned</span>' : tickSince(t.since_s)}
    </div>`;

  const tools = a.tools || [];
  let title, rows = '';
  if (a.state === 'tool' && tools.length) {
    title = tools.length > 1
      ? `Waiting for <b>${tools.length} tool calls</b> to finish`
      : `Waiting for <b>${esc(tools[0].name || 'a tool')}</b> to finish`;
    rows = tools.map(t => toolRow(t, false)).join('');
  } else if (a.state === 'thinking' && a.tool_name) {
    title = `Thinking — <b>${esc(a.tool_name)}</b> just returned`;
    rows = toolRow({ name: a.tool_name, text: a.detail, sub: a.sub }, true);
  } else if (a.state === 'waiting') {
    title = a.label.startsWith('interrupted')
      ? 'Interrupted — waiting for you' : 'Waiting for your input';
  } else {
    // "writing a response", "processing tool results", … as a headline.
    title = esc(a.label.charAt(0).toUpperCase() + a.label.slice(1));
  }

  const ctx = a.prompt
    ? `<div class="np-ctx">working on <q>${esc(a.prompt)}</q></div>` : '';
  const SHOW = 6;
  const ag = agents.length ? `<div class="np-agents">
      <span class="np-agk"><i></i>${agents.length} agent${
        agents.length > 1 ? 's' : ''} running</span>
      ${agents.slice(0, SHOW).map(x => `<a class="np-agent"
        href="#/agent/${esc(s.id)}/${esc(x.id)}" title="${esc(x.topic || '')}">${
        esc((x.topic || x.type || x.id).slice(0, 44))} ${x.started ?
        tickSince((Date.now() - new Date(x.started).getTime()) / 1000) : ''}</a>`).join('')}
      ${agents.length > SHOW ? `<span class="mut">+${agents.length - SHOW} more</span>` : ''}
    </div>` : '';
  const stall = a.stalled ? `<div class="np-stall">⚠ Nothing written for ${
      dur(a.idle_s || 900)} — probably parked on a permission prompt in the
      terminal, or abandoned. Timers keep counting until the CLI writes again.</div>` : '';

  return `<div class="nowpanel ${esc(a.state)}" data-nowpanel="${esc(s.id)}">
    <div class="np-head"><span class="np-dot"><i></i></span>
      <span class="np-title">${title}</span>${tickSince(a.since_s)}
      <span class="np-meta">last write ${ago(s.ended)}</span></div>
    ${rows || ctx || ag ? `<div class="np-body">${rows}${ctx}${ag}</div>` : ''}
    ${stall}
  </div>`;
}
const stPill = st => `<span class="st ${st.cls}"><i></i>${st.label}</span>`;
const AGENT_ST = { running: ['run', 'Running'], done: ['ok', 'Done'],
                   stopped: ['done', 'Stopped'] };
const agentPill = state2 => {
  const [cls, label] = AGENT_ST[state2] || ['done', state2];
  return `<span class="st ${cls}"><i></i>${label}</span>`;
};
/** Agents cell: "• 2 /34" when some are working right now (pulsing dot, the
    same status language as the pills), plain total when idle. */
const agentsCell = s => {
  const total = s.agents || 0;
  const run2 = s.agents_running || 0;
  if (!total) return '·';
  if (!run2) return String(total);
  return `<span class="agr" title="${run2} running now · ${total} total"><i></i>${
    run2}</span> <span class="mut">/${total}</span>`;
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
  agent:     { title: 'Agent',     skel: () => SKELETONS.tiles(5) + SKELETONS.card() },
  workflows: { title: 'Workflows', skel: () => SKELETONS.card() + SKELETONS.card() },
  workflow:  { title: 'Workflow',  skel: () => SKELETONS.tiles(6) + SKELETONS.card() },
  git:       { title: 'Git',       skel: () => SKELETONS.tiles(4) + SKELETONS.card() },
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


/** Token-flow card: one API call drawn to a scope's shares — the whole
    window, or one model. Three input streams converge on the model, one
    output stream leaves it. Ribbon widths are linear in tokens (4px floor so
    a 1% stream stays visible — the labels carry the exact counts); particles
    travel along each ribbon, more of them on the busier streams, so it reads
    as flow. Under it: the insight numbers, what each stream is billed at and
    what it cost, and the window's spend at API list price.

    scope: { win, ins, money, cells, modelLabel, sub, picker } */
function flowCard(scope) {
  const { win, ins, money, cells } = scope;
  const W = 640, H = 184, x0 = 148, nodeX = 396, nodeW = 88, nodeH = 52, cy = 92;
  const inputs = [KIND.fresh, KIND.resent, KIND.cache_read];
  const inTotal = inputs.reduce((a, k) => a + (win[k.key] || 0), 0) || 1;
  const grand = inTotal + (win.output || 0);
  const width = v => Math.max(4, 34 * v / inTotal);
  const ys = [30, 92, 154];
  const still = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const uid = 'fp' + (++gradSeq);
  let n = 0;
  const ribbon = (d, w, k, share) => {
    const id = `${uid}-${++n}`;
    // Particle count follows the stream's share; every stream keeps a couple
    // so a 1% stream still visibly moves.
    const count = still ? 0 : Math.max(2, Math.min(9, Math.round(9 * share)));
    const dur = 3.2;
    const dots = Array.from({ length: count }, (_, i) => `<circle class="dot" r="${
      Math.min(3, 1.4 + w / 9).toFixed(1)}" fill="${k.color}">
      <animateMotion dur="${dur}s" repeatCount="indefinite" begin="${
        (-i * dur / count).toFixed(2)}s"><mpath href="#${id}"/></animateMotion></circle>`).join('');
    return `<path id="${id}" class="band" d="${d}" stroke="${k.color}"
      stroke-width="${w.toFixed(1)}"/>${dots}`;
  };
  // widest first so the thin streams stay on top
  const order = inputs.map((k, i) => ({ k, y: ys[i], v: win[k.key] || 0 }))
    .sort((a, b) => b.v - a.v);
  const bands = order.map(({ k, y, v }) => ribbon(
    `M${x0} ${y}C${x0 + 110} ${y},${nodeX - 100} ${cy},${nodeX} ${cy}`,
    width(v), k, v / inTotal)).join('');
  const outV = win.output || 0, outW = width(outV);
  const outBand = ribbon(`M${nodeX + nodeW} ${cy}H${W - 8}`, outW, KIND.output,
                         outV / inTotal);
  const labels = inputs.map((k, i) => {
    const v = win[k.key] || 0;
    return `<text class="fl" x="8" y="${ys[i] - 4}">${esc(k.label)}</text>
      <text class="fv" x="8" y="${ys[i] + 13}">${tok(v)}<tspan class="fs"> · ${
      (100 * v / inTotal).toFixed(1)}% of input</tspan></text>`;
  }).join('');
  const spent = (money && money.perKind) || {};
  const gid = uid + '-node';
  return `<div class="flow">
    <div class="fhead"><div><div class="cap">How your tokens flow</div>
      <div class="sub2">${scope.sub}</div></div>
      <div class="fsum"><b>${tok(grand)}</b><span>tokens</span></div></div>
    ${scope.picker || ''}
    <svg viewBox="0 0 ${W} ${H}" width="100%" role="img"
      aria-label="Input streams converging on the model and the output stream leaving it">
      <defs><linearGradient id="${gid}" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="var(--vio)" stop-opacity=".22"/>
        <stop offset="1" stop-color="var(--vio)" stop-opacity=".06"/></linearGradient></defs>
      <text class="fh" x="8" y="12">INPUT</text>
      <text class="fh" x="${W - 8}" y="12" text-anchor="end">OUTPUT</text>
      ${bands}${outBand}
      <rect x="${nodeX}" y="${cy - nodeH / 2}" width="${nodeW}" height="${nodeH}" rx="14"
        class="node" fill="url(#${gid})"/>
      <text class="nl" x="${nodeX + nodeW / 2}" y="${cy - 5}" text-anchor="middle">model</text>
      <text class="nv" x="${nodeX + nodeW / 2}" y="${cy + 12}" text-anchor="middle">${
        esc(scope.modelLabel)}</text>
      ${labels}
      <text class="fl" x="${W - 8}" y="${(cy - outW / 2 - 9).toFixed(1)}" text-anchor="end">${
        esc(KIND.output.label)}</text>
      <text class="fv" x="${W - 8}" y="${(cy + outW / 2 + 18).toFixed(1)}" text-anchor="end">${
        tok(outV)}<tspan class="fs"> · ${(100 * outV / (grand || 1)).toFixed(2)}% of all</tspan></text>
    </svg>
    <div class="insights">
      <div><b>${rereadX(win.fresh ? win.cache_read / win.fresh : 0)}</b>
        <span>each fresh token re-read, on average</span></div>
      <div><b>${pct((win.fresh + win.resent) ? win.resent / (win.fresh + win.resent) : 0)}</b>
        <span>of full-price input was re-sent, not new</span></div>
      <div><b>${(ins.cache_misses || 0).toLocaleString()}</b>
        <span>cache misses · ${pct(ins.calls_missed_share || 0)} of calls</span></div>
    </div>
    ${cells.length ? `<div class="rates" title="Rate: USD per million tokens. Spent: this scope's tokens at that rate, API list price.">${
      cells.map(({ k, price, note }) => `<div class="rc">
        <div class="rl"><span class="sw" style="background:${k.color}"></span>${esc(k.label)}</div>
        <b>${esc(price)}<small>/MTok</small></b><span class="rn">${esc(note)}</span>
        ${spent[k.key] != null ? `<span class="rm">≈ ${usd(spent[k.key])} spent</span>` : ''}
      </div>`).join('')}</div>` : ''}
    ${money ? `<div class="spend">
      <div><b>${usd(money.spend)}</b><span>at API list price — without a plan</span></div>
      <div><b>${usd(money.uncached)}</b><span>without prompt caching</span></div>
      <div><b class="good">${usd(money.saved)}</b><span>saved by caching · ${
        money.savedPct.toFixed(0)}%</span></div>
    </div>` : ''}
  </div>`;
}

/** Price per million tokens: whole dollars when whole, else cents. */
const rate$ = v => '$' + (Math.abs(v - Math.round(v)) < 0.005 ? Math.round(v) : v.toFixed(2));
/** The per-stream rate cells for one model's list prices. */
function rateCells(rate, mult) {
  const rIn = rate.input, rOut = rate.output;
  const w = `${rate$(rIn * mult.write_5m)}–${rate$(rIn * mult.write_1h)}`;
  return [
    { k: KIND.fresh, price: w, note: `cache write · ${mult.write_5m}–${mult.write_1h}× input` },
    { k: KIND.resent, price: w, note: 'written again · same rate' },
    { k: KIND.cache_read, price: rate$(rIn * mult.read), note: `${mult.read}× input rate` },
    { k: KIND.output, price: rate$(rOut), note: 'list output rate' },
  ];
}
/** Effective per-stream rates when several models are blended: spend ÷ tokens. */
function blendedCells(money, win) {
  return KINDS.map(k => ({
    k, price: rate$(win[k.key] ? money.perKind[k.key] / win[k.key] * 1e6 : 0),
    note: 'effective, blended',
  }));
}
/** Split the full-price input cost between fresh and re-sent by tokens —
    both are billed at the cache-write rate, so this is exact bar the 5m/1h
    tier mix — and return the per-stream spend. */
function perKindSpend(costByType, win, scale = 1) {
  const full = ((costByType.input || 0) + (costByType.cache_write_5m || 0) +
                (costByType.cache_write_1h || 0)) * scale;
  const fp = win.fresh + win.resent;
  return {
    fresh: fp ? full * win.fresh / fp : 0,
    resent: fp ? full * win.resent / fp : 0,
    cache_read: (costByType.cache_read || 0) * scale,
    output: (costByType.output || 0) * scale,
  };
}
function flowPicker(models) {
  return `<div class="tmode flowpick" role="group" aria-label="Model">${
    [{ key: '', label: 'All models' }, ...models].map(m => `<button type="button"
      data-fm="${esc(m.key)}" class="${m.key === state.flowModel ? 'on' : ''}">${
      esc(m.label)}</button>`).join('')}</div>`;
}
function wireFlowPick(root) {
  $$('.flowpick button[data-fm]', root || document).forEach(b =>
    b.addEventListener('click', () => {
      state.flowModel = b.dataset.fm;
      localStorage.setItem('cm.flowmodel', state.flowModel);
      const y = scrollY;
      route(true).then(() => scrollTo(0, y));
    }));
}

/** Compact stat row for the overview: dot · label + note · value · sparkline. */
function kindRow(k, v, note, opts = {}) {
  return `<div class="kpi krow">
    <span class="ic" style="background:color-mix(in srgb,${k.color} 14%,transparent)"><i
      class="kdot" style="background:${k.color}"></i></span>
    <div class="kt"><div class="k" title="${esc(k.note)}">${esc(k.label)}</div>
      ${note ? `<span class="delta ${opts.cls || 'info'}">${note}</span>` : ''}</div>
    <div class="v">${tok(v)}</div>
    ${opts.spark && opts.spark.some(x => x > 0)
      ? `<span class="spk">${sparkSVG(opts.spark, 78, 28, k.hex)}</span>` : ''}
  </div>`;
}

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
      <b>${(s0.current_context / 1e4).toFixed(1)}%</b> of the 1M window —
      compaction soon.</span></div>`);
  }

  const daily = d.daily || [];
  const dailyCost = daily.map(r => r.cost);
  // Window numbers come from the same per-day series the charts draw, so the
  // tiles, the ring and the columns always agree. totals (t.*) counts whole
  // sessions active in the window — a straddling session's whole bill —
  // which is a different (bigger) number; it still backs the counts.
  const windowSpend = dailyCost.reduce((a, b) => a + b, 0);
  const windowUncached = daily.reduce((a, r) => a + (r.uncached || 0), 0);
  const winSaved = Math.max(0, windowUncached - windowSpend);
  const winSavedPct = windowUncached ? 100 * winSaved / windowUncached : 0;
  const win = { fresh: 0, resent: 0, cache_read: 0, output: 0 };
  for (const r of daily) for (const k of KINDS) win[k.key] += r[k.key] || 0;
  const winTotal = splitTotal(win);
  const ins = d.insights || { cache_misses: 0, calls_missed_share: 0 };
  const series = key => daily.map(r => r[key] || 0);
  // What the sparkline beside each row is saying: the latest day against
  // the window's busiest one.
  const trend = xs => xs.length
    ? `today ${tok(xs[xs.length - 1])} · peak ${tok(Math.max(...xs))}/day` : '';
  const kinds = modeKinds(), share = modeShare();

  // The flow card's scope: the whole window (per-day series, so it agrees
  // with the chart and the spend fold) or one model (its own bucket — the
  // sessions active in the window — priced exactly at its own rates).
  const mult = d.cache_mult || { write_5m: 1.25, write_1h: 2, read: 0.1 };
  const modelsUsed = (d.models || []).filter(m => m.tokens > 0 && m.cost > 0)
    .sort((a, b) => b.tokens - a.tokens).slice(0, 6);
  if (state.flowModel && !modelsUsed.some(m => m.key === state.flowModel)) state.flowModel = '';
  const picked = modelsUsed.find(m => m.key === state.flowModel);
  let scope;
  if (picked) {
    const r = d.rates[picked.key] || { input: 0, output: 0, label: picked.label };
    const wm = picked.split;
    const savedM = Math.max(0, picked.uncached_cost - picked.cost);
    const moneyM = {
      spend: picked.cost, uncached: picked.uncached_cost, saved: savedM,
      savedPct: picked.uncached_cost ? 100 * savedM / picked.uncached_cost : 0,
      perKind: perKindSpend(picked.cost_by_type || {}, wm),
    };
    scope = {
      win: wm, money: moneyM, modelLabel: r.label,
      ins: { cache_misses: picked.cache_misses || 0,
             calls_missed_share: picked.api_calls ? (picked.cache_misses || 0) / picked.api_calls : 0 },
      cells: rateCells(r, mult),
      sub: `One ${esc(r.label)} call, drawn to its shares · sessions active in the last ${
        winLabel()} · ${esc(r.label)} list rates.`,
    };
  } else {
    const scale = e.total_cost ? windowSpend / e.total_cost : 0;
    const moneyW = {
      spend: windowSpend, uncached: windowUncached, saved: winSaved, savedPct: winSavedPct,
      perKind: perKindSpend(e.cost || {}, win, scale),
    };
    const one = modelsUsed.length === 1 ? d.rates[modelsUsed[0].key] : null;
    scope = {
      win, money: moneyW, ins,
      modelLabel: one ? one.label : `${modelsUsed.length} models`,
      cells: one ? rateCells(one, mult) : blendedCells(moneyW, win),
      sub: `One API call, drawn to this window's shares${one
        ? ` · ${esc(one.label)} list rates` : ' · rates blended across models'}.`,
    };
  }
  scope.picker = modelsUsed.length > 1 ? flowPicker(modelsUsed) : '';

  const sessData = await api('/api/sessions', { sort: 'recent', limit: 8 });
  const rows = sessData.sessions.map(s => {
    const st = sessStatus(s);
    return {
      _href: `#/session/${s.id}`,
      av: avatar(s.project),
      sess: `<b>${esc(s.project)}</b><span class="tp">${esc(s.title)}</span>${actHTML(s)}`,
      model: `<span class="mchip">${esc(s.model_label)}</span>`,
      tk: `${tok(s.tokens)}${tokBar(s.split)}`, out: tok(s.output_tokens),
      ag: agentsCell(s),
      cost: usd(s.cost), st: stPill(st),
      when: `<span class="dur">${ago(s.ended)}</span>`,
    };
  });

  const byTokens = list => list.slice().sort((a, b) => b.tokens - a.tokens);

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
        ${flowCard(scope)}
      </div>

      <div class="kpis krows">
        ${KINDS.map(k => kindRow(k, win[k.key], trend(series(k.key)),
          { cls: 'info', spark: series(k.key) })).join('')}
      </div>
    </div>

    <section class="blk">
      <div class="card">
        <div class="ch"><h2>Tokens per day</h2>
          <span class="meta">${tok(winTotal)} in the last ${winLabel()}</span>
          <div class="chacts">${tokModePicker()}</div></div>
        <div class="cb"><div id="tokDaily"></div>${
          legend(kinds.map(k => [k.label, k.color]))}</div>
      </div>
    </section>

    <section class="blk" style="display:grid;grid-template-columns:1fr minmax(300px,380px);gap:16px">
      <div class="card">
        <div class="ch"><h2>Sessions</h2><a class="meta" href="#/sessions">View all →</a></div>
        <div class="cb" style="padding:4px 0 0">
        ${table([
          { h: '', key: 'av' },
          { h: 'Project', key: 'sess', grow: 1, link: 1 },
          { h: 'Model', key: 'model' },
          { h: 'Tokens', key: 'tk', n: 1 },
          { h: 'Output', key: 'out', n: 1 },
          { h: 'Agents', key: 'ag', n: 1 },
          { h: 'Cost', key: 'cost', n: 1, cls: 'dim' },
          { h: 'Status', key: 'st' },
          { h: 'Last write', key: 'when', n: 1 },
        ], rows)}</div>
      </div>
      <div style="display:flex;flex-direction:column;gap:16px">
        <div class="card">
          <div class="ch"><h2>By project</h2><a class="meta" href="#/cost">Cost →</a></div>
          <div class="cb">${barList(foldTail(byTokens(d.projects).map(p => ({
            label: p.key, value: p.tokens, text: tok(p.tokens),
            sub: usd(p.cost),
            href: `#/sessions?project=${encodeURIComponent(p.key)}`,
            fmt: tok,
            tip: `${p.sessions} sessions · ${p.agents} agents\n${
              tok(p.split.fresh)} fresh · ${tok(p.split.resent)} re-sent · ${
              tok(p.split.cache_read)} cache read · ${tok(p.split.output)} output`,
          })), 5))}</div>
        </div>
        <div class="card" style="flex:1">
          <div class="ch"><h2>Models</h2></div>
          <div class="cb">${barList(byTokens(d.models.filter(m => m.tokens > 0)).slice(0, 4)
            .map((m, i) => ({
              label: m.label, value: m.tokens, color: SERIES[i], text: tok(m.tokens),
              sub: usd(m.cost),
              tip: `${m.api_calls.toLocaleString()} calls\n${
                tok(m.split.fresh)} fresh · ${tok(m.split.resent)} re-sent · ${
                tok(m.split.cache_read)} cache read · ${tok(m.split.output)} output`,
            })))}</div>
        </div>
      </div>
    </section>

    <section class="blk">${foldSection('ov.spend', 'Spend',
      `≈ ${usd(windowSpend)} in the last ${winLabel()} · ${usd(winSaved)} (${
        winSavedPct.toFixed(0)}%) saved by caching · ${
        usd(e.effective_output_rate)} real cost / 1M output`, `
      <div class="grid cols2">
        <div class="card">
          <div class="ch"><h2>Spend per day</h2>
            <span class="meta">peak ${usd(Math.max(...dailyCost, 0))}/day</span></div>
          <div class="cb"><div id="dailyBars"></div></div>
        </div>
        <div class="card">
          <div class="ch"><h2>Spend, cumulative</h2>
            <span class="meta">${usd(windowSpend)} in the last ${winLabel()}</span></div>
          <div class="cb"><div id="wave"></div></div>
        </div>
      </div>`)}</section>

    <footer class="pagefoot"><span>Transcripts never leave this machine — the only
        network call is to Anthropic, for your plan limits.</span>
      <span>Dollar figures are API list-price equivalents, not amounts billed.</span></footer>`;

  hydrateTips($('#view'));
  wireTable($('#view'));
  wireWindow($('#view'));
  wireFolds($('#view'));
  wireTokMode($('#view'));
  wireFlowPick($('#view'));
  stackChart($('#tokDaily'), daily.map(r => ({
    label: r.date.slice(5).replace('-', '/'), tip: r.date,
    split: { fresh: r.fresh, resent: r.resent,
             cache_read: r.cache_read, output: r.output },
  })), { height: 232, kinds, share });
  colChart($('#dailyBars'), daily.map(r => ({
    v: r.cost, label: r.date.slice(5),
    tip: `${r.date}\n${usd(r.cost)} · ${r.sessions} sessions`,
  })), { height: 248 });
  waveChart($('#wave'), daily, { height: 248 });
};

// Cost-composition rows (label, economics key, colour). Cache-write tiers
// split here because they price differently; both wear the cache-write hue's
// neighbourhood, and every other row keeps its token kind's colour.
const COST_ROWS = [
  ['Cache read', 'cache_read', KIND.cache_read.color],
  ['Cache write · 5m', 'cache_write_5m', KIND.resent.color],
  ['Cache write · 1h', 'cache_write_1h', 'var(--s5)'],
  ['Output', 'output', KIND.output.color],
  ['Input, uncached', 'input', KIND.fresh.color],
];

// ── crumbs (session-rooted; a project is just a label on a session) ───
const crumb = (...parts) => `<div class="crumb">${parts.join(' / ')}</div>`;
const sessionsLink = () => `<a href="#/sessions">Sessions</a>`;

// ── session-window attribution ────────────────────────────────────────────
// The plan-limit breakdown, condensed onto one line under the Sessions
// heading: project and model shares fold behind pills, and each pill reads
// as a summary even when folded. (limitColor/resetInfo live further down;
// they are only read at render time.)
function sessionWindowCard(plan) {
  if (!plan || !plan.session_window) return '';
  const sw = plan.session_window;
  const lim = (plan.limits || []).find(l => l.key === 'session');
  const top = sw.projects[0];
  const r = lim ? resetInfo(lim) : null;

  const limBox = lim
    ? `<span class="swbox" title="${esc(lim.label)} — ${lim.percent}% used${
        r ? `, resets in ${r.rel}${r.abs ? ` (${r.abs})` : ''}` : ''}">
         <span class="swlab">${esc(lim.label)}</span>
         <span class="swtrack"><i style="width:${Math.min(100, lim.percent)}%;
           background:${limitColor(lim)}"></i></span>
         <b>${lim.percent}%</b></span>`
    : '';
  const pill = (title, items, color, href) => {
    if (!items || !items.length) return '';
    const lead = items.slice(0, 2)
      .map(i => `${esc(i.label)} ${(i.share * 100).toFixed(0)}%`)
      .join(' · ');
    const rowsBar = barList(items.slice(0, 6).map(i => ({
      label: i.label, value: i.share, color,
      text: usd(i.cost), sub: (i.share * 100).toFixed(0) + '%',
      href: href ? href(i) : undefined,
      tip: `${tok(i.tokens)} tokens`,
    })));
    // data-fold + wireFolds keeps it open across the 12s silent refresh —
    // same persistence the session page's fold sections use.
    return `<details class="sww" data-fold="sw-${title}"${
      foldIsOpen('sw-' + title) ? ' open' : ''}>
      <summary>${title} · ${lead}</summary>
      <div class="swbody">${rowsBar}</div>
    </details>`;
  };

  return `<div class="swl">
  <span class="mut">Session window · ${usd(sw.cost)} burned</span>
  ${limBox}
  <span class="mut">${top ? `top ${esc(top.label)}` : 'no spend'}</span>
  ${pill('By project', sw.projects, 'var(--vio)',
    i => '#/sessions?project=' + encodeURIComponent(i.name))}
  ${pill('By model', sw.models, 'var(--gold)')}
  <span class="swreset">${r ? `resets in ${r.rel}${r.abs ? ` · ${esc(r.abs)}` : ''}` : ''}</span>
</div>`;
}

views.sessions = async (params) => {
  const q = params.get('q') || '', project = params.get('project') || '';
  const sort = params.get('sort') || 'recent';
  const [d, plan] = await Promise.all([
    api('/api/sessions', { q, project, sort }),
    planReq,
  ]);

  const rows = d.sessions.map(s => {
    const st = sessStatus(s);
    return {
      _href: `#/session/${s.id}`,
      sess: `<b>${esc(s.project)}</b><span class="tp"><span class="sid">#${
        esc(s.short.slice(0, 6))}</span> ${esc(s.title)}</span>${actHTML(s)}`,
      model: `<span class="mchip">${esc(s.model_label)}</span>`,
      turns: `<span title="${s.api_calls.toLocaleString()} API calls">${s.turns}</span>`,
      ag: agentsCell(s), tk: `${tok(s.tokens)}${tokBar(s.split)}`,
      out: tok(s.output_tokens), cx: tok(s.peak_context),
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
      <div><h1>Sessions${project ? ` — ${esc(project)}` : ''}</h1><p class="sub">${
        d.total} session${
        d.total === 1 ? '' : 's'} in the last ${winLabel()}${
        q ? ` · matching “${esc(q)}”` : ''}</p></div>
      <div class="right">
        ${project || q ? `<a class="btn" href="#/sessions">Clear filters ✕</a>` : ''}
        <input type="search" id="q" placeholder="Search titles, prompts, paths…"
               aria-label="Search sessions" value="${esc(q)}">
        ${windowPicker()}
      </div>
    </div>
    ${sessionWindowCard(plan)}
    ${table([
      { h: 'Project', key: 'sess', grow: 1, link: 1, sort: 'recent' },
      { h: 'Model', key: 'model' },
      { h: 'Turns', key: 'turns', n: 1, sort: 'turns' },
      { h: 'Agents', key: 'ag', n: 1, sort: 'agents' },
      { h: 'Tokens', key: 'tk', n: 1, sort: 'tokens' },
      { h: 'Output', key: 'out', n: 1 },
      { h: 'Peak ctx', key: 'cx', n: 1, sort: 'context' },
      { h: 'Cost', key: 'cost', n: 1, sort: 'cost', cls: 'dim' },
      { h: 'Status', key: 'st' },
      { h: 'Last write', key: 'when', n: 1, sort: 'recent' },
    ], rows, {
      sort, onSort: k => setParam('sort', k),
      empty: q || project
        ? '<b>No matches</b>Try a different search, or clear the filters.'
        : '<b>No sessions in this window</b>Widen the time window to see older work.',
    })}`;

  hydrateTips($('#view'));
  wireTable($('#view'), k => setParam('sort', k));
  wireWindow($('#view'));
  wireFolds($('#view'));
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
  // This session's workflow fan-outs, shown as progress chips on the
  // Subagents card. The endpoint is project-scoped, so filter to the session;
  // skip the request entirely when no agent came from a workflow.
  let sessWfs = [];
  if (s.agents.some(a => a.workflow_id)) {
    const wd = await api('/api/workflows', { project: s.project }).catch(() => null);
    if (wd) sessWfs = wd.workflows.filter(w => w.session_id === s.id);
  }
  const e = s.economics;
  const st = sessStatus(s);
  const typeRows = COST_ROWS;
  const toolCalls = Object.values(s.tools).reduce((a, b) => a + b, 0);
  const split = s.split;
  const sIns = s.insights || { reread_x: 0, resent_share: 0, cache_misses: 0 };
  const total = splitTotal(split), inp = splitInput(split);
  const running = s.agents.filter(a => a.state === 'running').length;
  const kinds = modeKinds(), share = modeShare();
  const hb = s.hourly || [];
  const hasHourly = hb.some(b => b.cost > 0 || b.out > 0);

  $('#view').innerHTML = `
    ${crumb(sessionsLink(), esc(s.short))}
    <div class="hd">
      <div><h1>${esc(s.title)}</h1>
        <p class="sub">${esc(s.project)} · ${esc(s.branch || 'no branch')} ·
          ${esc(s.model_label)} · started ${dt(s.started)} · CLI ${
          esc(s.version || '?')}</p>
        <p class="sub costline" title="API list-price equivalent — not an amount billed">≈ ${
          usd(s.cost)} list-price · ${usd(s.cost_main)} main + ${usd(s.cost_agents)} agents · ${
          usd(s.uncached_cost - s.cost_main)} saved by caching</p></div>
      <div class="right">${stPill(st)}${s.live ?
        `<span class="mchip">PID ${s.pid} · ${s.cpu.toFixed(0)}% CPU · ${
          (s.rss / 1048576).toFixed(0)} MB</span>` : ''}</div>
    </div>

    ${nowPanel(s, s.agents.filter(a => a.state === 'running'))}

    <div class="kpis">
      <div class="kpi">
        <div class="k">Tokens</div><div class="v">${tok(total)}</div>
        ${tokBar(split, { cls: 'wide' })}
        <div class="k sub2">${s.api_calls.toLocaleString()} API calls · incl. agents</div>
      </div>
      ${kindTile(KIND.fresh, split.fresh, `re-read ${rereadX(sIns.reread_x)} on average`)}
      ${kindTile(KIND.resent, split.resent,
        `${pct(sIns.resent_share)} of full-price input · ${sIns.cache_misses} misses`,
        { cls: split.resent > split.fresh * 0.5 ? 'bad' : 'warn' })}
      ${kindTile(KIND.cache_read, split.cache_read,
        `${pct(inp ? split.cache_read / inp : 0)} hit rate`, { cls: 'up' })}
      ${kindTile(KIND.output, split.output,
        `${(total ? 100 * split.output / total : 0).toFixed(2)}% of all`, { cls: 'warn' })}
    </div>
    <div class="kpis">
      ${[[tok(s.peak_context), 'Peak context', 'largest single call'],
         [s.turns, 'Your turns', `${s.api_calls.toLocaleString()} API calls`],
         [s.agents.length, 'Subagents',
          `${running ? `${running} running now · ` : ''}${s.tool_errors} tool errors`],
         [`<span class="dur">${dur(s.active_s)}</span>`, 'Generating',
          `of <span class="dur">${dur(s.duration_s)}</span> elapsed · ${
            s.output_tps.toFixed(0)} tok/s out`]]
        .map(([v, k, n]) => `<div class="kpi">
          <div class="k">${k}</div><div class="v">${v}</div>
          <div class="k sub2">${n}</div></div>`).join('')}
    </div>

    <section class="blk grid ${hasHourly ? 'cols3' : 'cols2'}">
      <div class="card"><div class="ch"><h2>Tokens per call</h2>
        <div class="chacts">${tokModePicker()}</div></div>
        <div class="cb"><div id="tokCalls"></div>${
          legend(kinds.map(k => [k.label, k.color]))}</div></div>
      ${hasHourly ? `
      <div class="card"><div class="ch"><h2>Tokens / hour</h2>
        <span class="meta">24h · incl. agents</span></div>
        <div class="cb"><div id="hrTok"></div>${
          legend(kinds.map(k => [k.label, k.color]))}</div></div>` : ''}
      <div class="card"><div class="ch"><h2>Context growth</h2>
        <span class="meta">per call</span></div>
        <div class="cb"><div id="ctxChart"></div></div></div>
    </section>

    <section class="blk">${foldSection('sess.detail', 'Cost, tools & models',
      `≈ ${usd(s.cost)} · ${usd(s.uncached_cost - s.cost_main)} saved by caching · ${
        toolCalls.toLocaleString()} tool call${toolCalls === 1 ? '' : 's'} · ${
        s.models.length} model${s.models.length === 1 ? '' : 's'}`, `
    <div class="grid cols4">
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
      <div class="card"><div class="ch"><h2>Cumulative spend</h2>
        <span class="meta">incl. agents</span></div>
        <div class="cb"><div id="costChart"></div></div></div>
      <div class="card"><div class="ch"><h2>Tools</h2>
        <span class="meta">${toolCalls} calls
          · click one for detail</span></div>
        <div class="cb" id="toolsBody">${barList(foldTail(Object.entries(s.tools)
          .sort((a, b) => b[1] - a[1])
          .map(([n, c]) => ({ label: n, value: c, text: String(c), act: n })), 9))}</div></div>
      <div class="card">
        <div class="ch"><h2>Models used</h2>
          <span class="meta">${s.models.length}</span></div>
        <div class="cb">${barList(
          s.models.map((m, i) => ({ label: m.label, value: m.tokens,
            color: SERIES[i % SERIES.length],
            text: tok(m.tokens), sub: `${m.calls} calls`,
            tip: `${tok(m.output)} output · ${usd(m.cost)}` })))}
        </div></div>
    </div>`)}</section>

    ${s.agents.length ? `<section class="blk"><div class="card">
      <div class="ch"><h2>Subagents</h2><span class="meta">${
        tok(s.agents.reduce((a, x) => a + x.tokens, 0))} tokens · ${usd(s.cost_agents)}</span></div>
      ${sessWfs.length ? `<div class="wfstrip">${sessWfs.map(w => `
        <a class="wfchip" href="#/workflow/${esc(w.session_id)}/${esc(w.id)}"
          title="${esc(w.topic || '')}">⑃ ${
          esc(w.name || (w.topic ? w.topic.slice(0, 34) : w.short))}
          <span class="num">${w.completed}/${w.agents}</span>${
          w.running ? `<span class="agr"><i></i>${w.running}</span>` : ''}
          <span class="num">${tok(w.tokens)}</span></a>`).join('')}</div>` : ''}
      <div class="cb" style="padding:4px 0 0">${table([
        { h: 'Topic', key: 'topic', grow: 1, link: 1 },
        { h: 'Type', key: 'type' }, { h: 'Model', key: 'model' },
        { h: 'Calls', key: 'calls', n: 1 }, { h: 'Tokens', key: 'tk', n: 1 },
        { h: 'Output', key: 'out', n: 1 },
        { h: 'Time', key: 'time', n: 1 }, { h: 'Cost', key: 'cost', n: 1, cls: 'dim' },
        { h: 'Status', key: 'st' }, { h: 'Started', key: 'when', n: 1 }],
        s.agents.map(a => ({
          _href: `#/agent/${s.id}/${a.id}`,
          topic: esc(a.topic),
          type: `<span class="mut">${esc(a.type)}</span>`,
          model: `<span class="mchip">${esc(a.model_label)}</span>`,
          calls: a.api_calls, tk: `${tok(a.tokens)}${tokBar(a.split)}`,
          out: tok(a.output_tokens),
          time: `<span class="dur">${dur(a.duration_s)}</span>`,
          cost: usd(a.cost), st: agentPill(a.state),
          when: `<span class="dur">${ago(a.started)}</span>`,
        })))}</div></div></section>` : ''}

    <section class="blk grid cols2">
      <div class="card"><div class="ch"><h2>Your prompts</h2>
        <div class="chacts">
          <span class="meta">${s.prompts.length}</span>
          ${s.prompts.length ? `<a class="btn xs" download
            href="/api/sessions/${esc(s.id)}/prompts?format=html"
            data-tip="Download every prompt as a standalone HTML page —
untruncated, searchable, printable">Export HTML</a>
          <a class="btn xs" download
            href="/api/sessions/${esc(s.id)}/prompts?format=json"
            data-tip="Download every prompt as structured JSON —
untruncated, with timestamps and per-prompt stats">JSON</a>` : ''}
        </div></div>
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
  wireFolds($('#view'));
  wireTokMode($('#view'));
  const toolsHost = $('#toolsBody');
  if (toolsHost) wireToolRows(toolsHost, s.id);
  const tl = s.timeline;
  stackArea($('#tokCalls'), tl.map(p => ({
    t: p.t, split: { fresh: p.fresh, resent: p.resent,
                     cache_read: p.cache_read, output: p.out },
  })), { height: 150, kinds, share });
  lineChart($('#ctxChart'), tl.map(p => ({ t: p.t, v: p.ctx })), {
    height: 150, color: '#e8930c', hex: '#e8930c',
    label: p => `${new Date(p.t * 1000).toLocaleString()}\ncontext ${tok(p.v)} tokens`,
  });
  // spend_curve folds agent costs in, so the line ends at the session's
  // total cost instead of at the main-thread subtotal.
  let run = 0;
  const curve = (s.spend_curve && s.spend_curve.length)
    ? s.spend_curve
    : tl.map(p => ({ t: p.t, v: (run += p.cost) }));
  lineChart($('#costChart'), curve, {
    height: 150, fmt: usdAxis,
    label: p => `${new Date(p.t * 1000).toLocaleString()}\n${usd(p.v)} spent`,
  });
  if (hasHourly) {
    const hl = t => new Date(t * 1000).toLocaleTimeString(undefined,
      { hour: 'numeric' });
    const hd = t => new Date(t * 1000).toLocaleString(undefined,
      { weekday: 'short', hour: '2-digit', minute: '2-digit' });
    stackChart($('#hrTok'), hb.map(b => ({
      label: hl(b.t), tip: `${hd(b.t)} – ${hl(b.t + 3600)}`,
      split: { fresh: b.fresh, resent: b.resent,
               cache_read: b.cache_read, output: b.out },
    })), { height: 150, kinds, share });
  }
};

views.agent = async (params, sid, aid) => {
  const a = await api(`/api/agents/${sid}/${aid}`);
  $('#view').innerHTML = `
    ${crumb(sessionsLink(),
      `<a href="#/session/${esc(sid)}">${esc(a.session_title || sid.slice(0, 8))}</a>`,
      esc(a.id.slice(0, 10)))}
    <div class="hd">
      <div><h1>${esc(a.topic)}</h1>
        <p class="sub">${esc(a.type)} · ${esc(a.model_label)} · ${esc(a.project)}${
        a.workflow_id ? ` · workflow ${esc(a.workflow_id.replace('wf_', ''))}` : ''}</p></div>
      <div class="right">${agentPill(a.state)}</div>
    </div>
    ${a.owns ? `<p class="sub" style="margin:-8px 0 14px"><code>${esc(a.owns)}</code></p>` : ''}
    <div class="kpis">
      <div class="kpi">
        <div class="k">Tokens</div><div class="v">${tok(a.tokens)}</div>
        ${tokBar(a.split, { cls: 'wide' })}
        <div class="k sub2">${a.api_calls} API calls · ≈ ${usd(a.cost)} list-price</div>
      </div>
      ${kindTile(KIND.fresh, a.split.fresh,
        `re-read ${rereadX((a.insights || {}).reread_x)} on average`)}
      ${kindTile(KIND.resent, a.split.resent,
        `${(a.insights || {}).cache_misses || 0} cache misses`, { cls: 'warn' })}
      ${kindTile(KIND.cache_read, a.split.cache_read, `${pct(a.cache_hit_rate)} hit rate`,
        { cls: 'up' })}
      ${kindTile(KIND.output, a.output_tokens, `${a.output_tps.toFixed(0)} tok/s`,
        { cls: 'warn' })}
      ${[[`<span class="dur">${dur(a.duration_s)}</span>`, 'Duration',
          `started ${ago(a.started)} · ${dt(a.started)}`],
         [a.tool_errors, 'Tool errors', `${Object.values(a.tools)
           .reduce((x, y) => x + y, 0)} tool calls`]]
        .map(([v, k, n]) => `<div class="kpi"><div class="k">${k}</div>
          <div class="v">${v}</div>
          <div class="k sub2">${n}</div></div>`).join('')}
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

views.workflows = async (params) => {
  const project = params.get('project') || '';
  const d = await api('/api/workflows', { project });
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
        <div><h2><a class="repolink" href="#/workflow/${esc(w.session_id)}/${
            esc(w.id)}">${esc(w.name || w.topic.slice(0, 84))}</a>
          ${w.running ? `<span class="st run" style="margin-left:8px"><i></i>${
            w.running} running</span>` : ''}</h2>
          <p class="sub num">${esc(w.short)} · ${esc(w.project)} · ${dt(w.started)}${
            w.name ? ` · ${esc(w.topic.slice(0, 60))}` : ''}</p></div>
        <div style="display:flex;align-items:center;gap:14px">
          <div style="text-align:right;white-space:nowrap">
            <div class="num" style="font-weight:800;font-size:1.05rem">${usd(w.cost)}</div>
            <div class="meta">${w.completed}/${w.agents} done · peak ×${
              w.peak_parallelism}</div></div>
          <a class="btn" href="#/workflow/${esc(w.session_id)}/${esc(w.id)}">Debug →</a>
        </div>
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
    <div class="hd"><div><h1>Workflows${project ? ` — ${esc(project)}` : ''}</h1>
      <p class="sub">${wfs.length} fan-out runs · each bar is one agent, positioned
        by when it ran</p></div>
      <div class="right">
        ${project ? `<a class="btn" href="#/workflows">All projects ✕</a>` : ''}
        ${windowPicker()}${legend([
        ['completed', 'var(--vio)'], ['running', 'var(--gold)'],
        ['stopped', 'var(--line2)']])}</div>
    </div>
    ${cards || '<div class="empty"><b>No workflow runs in this window</b>' +
      'Widen the time window to see older fan-outs.</div>'}`;
  hydrateTips($('#view'));
  wireWindow($('#view'));
};

// ── details persistence across silent re-renders ──────────────────────
// The 12s live refresh replaces the view's DOM wholesale, which used to slam
// shut any <details> the user was reading and reset its scroll. Views that
// re-render live tag their collapsibles with data-key and bracket the render
// with capture/restore.
function captureDetails(sameView) {
  const keep = {};
  if (sameView) {
    $$('#view details[data-key]').forEach(el => {
      keep[el.dataset.key] = {
        open: el.open,
        scroll: (el.querySelector('pre') || {}).scrollTop || 0,
      };
    });
  }
  return keep;
}
function restoreDetails(keep) {
  $$('#view details[data-key]').forEach(el => {
    const k = keep[el.dataset.key];
    if (!k) return;
    el.open = k.open;
    if (k.open) {
      const pre = el.querySelector('pre');
      if (pre) pre.scrollTop = k.scroll;
    }
  });
}

// ── workflow debugger ─────────────────────────────────────────────────

const WF_COLORS = { done: 'var(--vio)', running: 'var(--gold)',
                    stopped: 'var(--line2)' };

/** Gantt with a real time axis. HTML rows (clickable, ellipsizing labels)
    over a shared time scale; bars colored by state, selected row outlined. */
function wfGanttHTML(agents, t0, t1, sel) {
  const span = Math.max(1, t1 - t0);
  const fmtT = ts => new Date(ts * 1000).toLocaleTimeString(undefined,
    { hour: '2-digit', minute: '2-digit' });
  const ticks = [0, .25, .5, .75, 1].map(f =>
    `<span style="left:${(f * 100).toFixed(1)}%">${fmtT(t0 + span * f)}</span>`).join('');
  const rows = agents.map(a => {
    const s0 = a.started ? Date.parse(a.started) / 1000 : t0;
    const s1 = a.ended ? Date.parse(a.ended) / 1000 : t1;
    const x = ((s0 - t0) / span) * 100;
    const w = Math.max(0.5, ((s1 - s0) / span) * 100);
    const showDur = w > 9;
    return `<div class="wfg-row${a.id === sel ? ' sel' : ''}" data-aid="${esc(a.id)}"
        tabindex="0" role="button" title="${esc(a.topic)}">
      <span class="lbl"><i class="dot" style="background:${
        WF_COLORS[a.state]}"></i>${esc(a.topic)}</span>
      <div class="trk"><i class="bar${a.state === 'running' ? ' live' : ''}"
        style="left:${x.toFixed(2)}%;width:${w.toFixed(2)}%;background:${
        WF_COLORS[a.state]}">${showDur
          ? `<b>${dur(s1 - s0)}</b>` : ''}</i></div>
      <span class="cst num">${usd(a.cost)}</span>
    </div>`;
  }).join('');
  return `<div class="wfg">
    <div class="wfg-row axis"><span class="lbl"></span>
      <div class="trk">${ticks}</div><span class="cst"></span></div>
    ${rows}
  </div>`;
}

/** Agents-alive step area under the gantt — where the fan-out breathes. */
function concChart(host, agents, t0, t1) {
  const W = host.clientWidth || 700, H = 74;
  const pad = { l: 0, r: 0, t: 6, b: 4 };
  host.innerHTML = '';
  const ev = [];
  for (const a of agents) {
    if (!a.started) continue;
    ev.push([Date.parse(a.started) / 1000, 1]);
    ev.push([a.ended ? Date.parse(a.ended) / 1000 : t1, -1]);
  }
  if (!ev.length) return;
  ev.sort((x, y) => x[0] - y[0]);
  const pts = [[t0, 0]];
  let n = 0;
  for (const [t, dn] of ev) { pts.push([t, n]); n += dn; pts.push([t, n]); }
  pts.push([t1, n]);
  const peak = Math.max(...pts.map(p => p[1]), 1);
  const span = Math.max(1, t1 - t0);
  const X = t => pad.l + ((t - t0) / span) * (W - pad.l - pad.r);
  const Y = v => H - pad.b - (v / peak) * (H - pad.t - pad.b);
  const s = svg('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}`, height: H }, host);
  const d = pts.map((p, i) =>
    `${i ? 'L' : 'M'}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join('');
  svg('path', { d: `${d}L${X(t1)},${Y(0)}L${X(t0)},${Y(0)}Z`,
                fill: 'color-mix(in srgb,var(--vio) 14%,transparent)' }, s);
  svg('path', { class: 'ln', d, stroke: 'var(--vio)', 'stroke-width': 1.6 }, s);
}

function wfInspectorHTML(a, d) {
  if (!a) return '<div class="empty"><b>Pick an agent</b>Click a bar in the timeline.</div>';
  const toolsTotal = Object.values(a.tools).reduce((x, y) => x + y, 0);
  const looksJson = /^[[{]/.test((a.result_full || '').trim());
  return `
    <div class="insp-head">
      ${agentPill(a.state)}
      <h3 title="${esc(a.topic)}">${esc(a.topic)}</h3>
    </div>
    <div class="insp-meta">${esc(a.type)} · ${esc(a.model_label)} ·
      started ${ago(a.started)} · <span class="dur">${dur(a.duration_s)}</span>
      · ${a.output_tps.toFixed(0)} tok/s
      <a style="margin-left:auto" href="#/agent/${esc(d.session_id)}/${
        esc(a.id)}">full page →</a></div>
    <div class="insp-kpis">
      <span><b class="num">${usd(a.cost)}</b> cost</span>
      <span><b class="num">${tok(a.tokens)}</b> tokens</span>
      <span><b class="num">${a.api_calls}</b> calls</span>
      ${a.tool_errors
        ? `<button type="button" class="errlink" data-erragent="${esc(a.id)}">
            <b class="num bad">${a.tool_errors}</b> tool errors →</button>`
        : '<span><b class="num">0</b> tool errors</span>'}
    </div>
    ${toolsTotal ? `<div class="insp-tools">${barList(Object.entries(a.tools)
      .sort((x, y) => y[1] - x[1]).slice(0, 6)
      .map(([n, c]) => ({ label: n, value: c, text: String(c) })))}</div>` : ''}
    <details class="dfile" data-key="prompt" style="margin-top:10px">
      <summary>Prompt <span class="mut">· ${(a.prompt || '').length} chars</span></summary>
      <pre class="cmt-body" style="margin:0;border-radius:0;max-height:300px;overflow:auto">${
        esc(a.prompt || '—')}</pre>
    </details>
    <details class="dfile" data-key="result" style="margin-top:8px" ${
      a.result_full ? 'open' : ''}>
      <summary>Result${looksJson ? ' <span class="mut">· structured</span>' : ''}</summary>
      <pre class="cmt-body${looksJson ? ' json' : ''}"
        style="margin:0;border-radius:0;max-height:420px;overflow:auto">${
        esc(a.result_full || 'No result recorded — the agent may not have completed.')}</pre>
    </details>`;
}

async function openWfErrors(sid, wfid, agentId) {
  const m = openModal('Tool errors');
  let d;
  try {
    d = await api(`/api/workflows/${sid}/${wfid}/errors`);
  } catch (e) {
    if ($('#modal') === m) $('#mBody').innerHTML =
      `<div class="empty"><b>Couldn't load errors</b>${esc(e.message)}</div>`;
    return;
  }
  if ($('#modal') !== m) return;
  let list = d.errors;
  if (agentId) list = list.filter(e => e.agent_id === agentId);
  $('#mMeta').textContent = `${list.length} error${list.length === 1 ? '' : 's'}${
    agentId ? ' · this agent' : ` across the workflow`}`;
  $('#mBody').innerHTML = list.map(e => `<div class="tcall">
      <div class="tct"><span>${dt(e.ts)}</span>
        <span class="tag vio">${esc(e.tool)}</span>
        <span class="tag" title="${esc(e.agent)}">${esc(e.agent.slice(0, 44))}</span></div>
      ${e.input ? `<code>${esc(e.input)}</code>` : ''}
      <pre class="errpre">${esc(e.error || '(no error text recorded)')}</pre>
    </div>`).join('') ||
    '<div class="empty"><b>No errors found</b>Nothing recorded for this scope.</div>';
}

views.workflow = async (params, sid, wfid) => {
  const viewKey = `wf:${sid}/${wfid}`;
  const keep = captureDetails($('#view').dataset.viewkey === viewKey);
  const d = await api(`/api/workflows/${sid}/${wfid}`);
  const agents = d.agents;
  const sel = params.get('a')
    || (agents.find(a => a.state === 'running') || agents[0] || {}).id;
  const t0 = d.start_ts || 0;
  const t1 = d.counts.running
    ? Date.now() / 1000
    : (d.end_ts || t0 + 1);
  const c = d.counts;
  const speedup = d.duration_s > 0 ? d.agent_seconds / d.duration_s : 0;
  const statusPill = c.running
    ? `<span class="st run"><i></i>${c.running} running</span>`
    : c.done === c.total ? '<span class="st ok"><i></i>completed</span>'
    : `<span class="st done"><i></i>${c.done}/${c.total} done</span>`;

  $('#view').innerHTML = `
    ${crumb(sessionsLink(),
      `<a href="#/session/${esc(d.session_id)}">${esc(d.session_id.slice(0, 8))}</a>`,
      esc(d.short))}
    <div class="hd">
      <div><h1>${esc(d.name || d.topic || d.short)} ${statusPill}</h1>
        <p class="sub">${d.description ? `${esc(d.description)} · ` : ''}${
          esc(d.project)} · ${dt(d.started)}</p></div>
      <div class="right">
        <a class="btn" href="#/session/${esc(d.session_id)}">Session →</a>
      </div>
    </div>

    <div class="kpis">
      ${[[`${c.done}<small>/${c.total}</small>`, 'Agents done',
          `${c.running} running · ${c.stopped} stopped`],
         [`×${d.peak_parallelism}`, 'Peak parallelism',
          `${dur(d.agent_seconds)} of agent time`],
         [`<span class="dur">${dur(d.duration_s)}</span>`, 'Wall clock',
          speedup > 1 ? `${speedup.toFixed(1)}× faster than serial` : ''],
         [usd(d.cost), 'Cost', `${usd(d.cost / (c.total || 1))} avg / agent`],
         [tok(d.tokens), 'Tokens', `${tok(d.output_tokens)} output`],
         [`<span class="${d.tool_errors ? 'gdel' : ''}">${d.tool_errors}</span>`,
          'Tool errors', d.tool_errors ? 'click to inspect →' : 'across all agents',
          d.tool_errors ? 'wfErrTile' : '']]
        .map(([v, k, n, tid]) => `<div class="kpi${tid ? ' clickable' : ''}"${
          tid ? ` id="${tid}" role="button" tabindex="0"` : ''}><div class="k">${k}</div>
          <div class="v">${v}</div>
          <div class="k" style="margin-top:6px;font-weight:400">${n}</div></div>`).join('')}
    </div>

    ${d.phases.length ? `<div class="phaserow">${d.phases.map((p, i) =>
      `<span class="phasechip" title="${esc(p.detail)}"><b>${i + 1}</b> ${
        esc(p.title)}</span>`).join('<i class="phasearrow">→</i>')}</div>` : ''}

    <section class="blk wf-split">
      <div class="card">
        <div class="ch"><h2>Timeline</h2>
          <div class="right">${legend([['done', 'var(--vio)'],
            ['running', 'var(--gold)'], ['stopped', 'var(--line2)']])}</div></div>
        <div class="cb">
          <div class="wfg-row concrow">
            <span class="lbl">parallelism · peak ×${d.peak_parallelism}</span>
            <div class="conchost" id="wfconc"></div>
            <span class="cst"></span>
          </div>
          <div id="wfg">${wfGanttHTML(agents, t0, t1, sel)}</div>
        </div>
      </div>
      <div class="card">
        <div class="ch"><h2>Agent</h2><span class="meta">click a bar to inspect</span></div>
        <div class="cb insp" id="wfinsp"></div>
      </div>
    </section>

    ${d.script ? `<section class="blk"><div class="card">
      <div class="ch"><h2>Script</h2><span class="meta">${esc(d.name)}.js · ${
        (d.script.length / 1024).toFixed(1)}KB${d.when_to_use
        ? ` · ${esc(d.when_to_use.slice(0, 90))}` : ''}</span></div>
      <div class="cb"><details class="dfile" data-key="script">
        <summary>Show the workflow script</summary>
        <pre class="cmt-body" style="margin:0;border-radius:0;max-height:560px;overflow:auto">${
          esc(d.script)}</pre></details></div>
    </div></section>` : ''}`;

  hydrateTips($('#view'));
  concChart($('#wfconc'), agents, t0, t1);
  const wireErr = () => $$('#wfinsp .errlink').forEach(b =>
    b.addEventListener('click', () =>
      openWfErrors(sid, wfid, b.dataset.erragent)));
  const paint = aid => {
    $('#wfinsp').innerHTML =
      wfInspectorHTML(agents.find(x => x.id === aid), d);
    wireErr();
  };
  paint(sel);
  // Re-renders every 12s while live: hand back the collapsibles as the
  // reader left them, scroll position included.
  $('#view').dataset.viewkey = viewKey;
  restoreDetails(keep);
  const errTile = $('#wfErrTile');
  if (errTile) {
    const openErrs = () => openWfErrors(sid, wfid);
    errTile.addEventListener('click', openErrs);
    errTile.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openErrs(); }
    });
  }
  $$('.wfg-row[data-aid]').forEach(el => {
    const pick = () => {
      $$('.wfg-row.sel').forEach(x => x.classList.remove('sel'));
      el.classList.add('sel');
      const p = new URLSearchParams(location.hash.split('?')[1] || '');
      p.set('a', el.dataset.aid);
      history.replaceState(null, '', `#/workflow/${sid}/${wfid}?${p}`);
      paint(el.dataset.aid);
    };
    el.addEventListener('click', pick);
    el.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); }
    });
  });
};

// ── git view ──────────────────────────────────────────────────────────
// One time window for the whole section: repos included (session activity),
// chart span, and stats range all follow it. 'all' shows every repo but the
// charts still cap at the 30 days the commit-log cache reaches.
const GIT_RANGES = [['3600', '1h'], ['14400', '4h'], ['86400', '24h'],
                    ['604800', '7d'], ['2592000', '30d'], ['all', 'All']];
const GIT_DEFAULT_RANGE = '86400';
const gitRangeLabel = v =>
  (GIT_RANGES.find(([x]) => x === v) || [0, '24h'])[1];
const gitWindowText = v => v === 'all' ? 'all time' : `the last ${gitRangeLabel(v)}`;

const gitRangePills = rng =>
  `<div class="pills" id="gitRange" role="group" aria-label="Time window">
    ${GIT_RANGES.map(([v, l]) =>
      `<button data-r="${v}" class="${v === rng ? 'on' : ''}">${l}</button>`).join('')}
  </div>`;

function wireGitRange(rng) {
  $$('#gitRange button').forEach(b =>
    b.addEventListener('click', () =>
      gitSetP({ range: b.dataset.r === GIT_DEFAULT_RANGE ? null : b.dataset.r })));
}

function gitSetP(patch) {
  const p = new URLSearchParams(location.hash.split('?')[1] || '');
  for (const [k, v] of Object.entries(patch)) {
    v == null ? p.delete(k) : p.set(k, v);
  }
  history.replaceState(null, '', '#/git' + (p.toString() ? '?' + p : ''));
  route(true);
}

const gitTabs = view => `<div class="pills" id="gitTabs" role="group" aria-label="Git view">
  <button data-v="live" class="${view === 'live' ? 'on' : ''}">Live</button>
  <button data-v="stats" class="${view === 'stats' ? 'on' : ''}">Stats</button></div>`;

function wireGitTabs(view) {
  $$('#gitTabs button').forEach(b => b.addEventListener('click', () => {
    if (b.dataset.v === view) return;
    // the window is shared between tabs, so it survives the switch
    gitSetP({ view: b.dataset.v === 'live' ? null : b.dataset.v });
  }));
}

views.git = async (params, sub, rid) => {
  if (sub === 'repo' && rid) return gitRepoView(params, rid);
  const view = params.get('view') || 'live';
  const repo = params.get('repo') || 'all';
  if (view === 'stats') return gitStatsView(params, repo);

  const rng = params.get('range') || GIT_DEFAULT_RANGE;
  const [d, h] = await Promise.all([
    api('/api/git', { range: rng }),
    api('/api/git/history', { range: rng, repo }),
  ]);
  const t = d.totals;
  const scoped = repo !== 'all' ? d.repos.find(r => r.id === repo) : null;
  const lastWip = [...h.points].reverse().find(p => p.wip != null);

  const cards = d.repos.map(r => {
    const st = r.stats;
    const wip = st ? st.wip : 0;
    const seg = (v, c) => wip && v > 0
      ? `<i style="width:${(100 * v / wip).toFixed(1)}%;background:${c}"></i>` : '';
    const files = (st ? st.changed : []).slice(0, 6).map(f => `
      <div class="gfile"><span class="fp" title="${esc(f.file)}">${
        esc(f.file.split('/').slice(-2).join('/'))}</span>
        <span><b class="gadd">+${f.add.toLocaleString()}</b> <b class="gdel">−${
        f.del.toLocaleString()}</b></span></div>`).join('');
    const commits = (st ? st.commits : []).slice(0, 3).map(c => `
      <div class="gcommit"><code>${esc(c.hash)}</code>
        <span class="cs" title="${esc(c.subject)}">${esc(c.subject)}</span>
        <span class="dur">${ago(new Date(c.t * 1000).toISOString())}</span></div>`).join('');
    const upstream = st && st.ahead != null
      ? ` · <span class="num">↑${st.ahead}${st.behind ? ` ↓${st.behind}` : ''}</span>`
      : '';
    const lastCommit = st && st.commits.length
      ? ago(new Date(st.commits[0].t * 1000).toISOString()) : '—';
    return `<div class="card gcard${repo === r.id ? ' scoped' : ''}">
      <div class="ch">
        <div><h2><a class="repolink" href="#/git/repo/${esc(r.id)}"
            title="Explore ${esc(r.name)} — commits, tree, changes">${
            avatar(r.name)} ${esc(r.name)}</a>
          ${r.live ? `<span class="st live" style="margin-left:6px"><i></i>${
            r.live} live</span>` : ''}</h2>
          <p class="sub num">${st ? `${esc(st.branch)}${upstream} · ` : ''}last commit ${
            lastCommit}</p></div>
        <button type="button" class="btn scope" data-repo="${esc(r.id)}"
          title="Scope the chart to this repo">${repo === r.id ? 'Unscope ✕' : 'Chart'}</button>
      </div>
      <div class="cb">
        <div class="gwip">
          <span class="num" style="font-weight:800;font-size:1.05rem">${
            st ? wip.toLocaleString() : '—'}</span>
          <span class="mut">uncommitted lines</span>
          <span class="spk">${sparkSVG((r.spark || []).map(p => p[1]), 90, 24,
            '#16a34a')}</span>
        </div>
        ${st && wip ? `<div class="wipbar" title="staged ${st.staged_add} · unstaged ${
            st.unstaged_add} · untracked ${st.untracked_lines}">
          ${seg(st.staged_add, 'var(--vio)')}${seg(st.unstaged_add, 'var(--gold)')}${
            seg(st.untracked_lines, '#38bdf8')}</div>` :
          st ? '<div class="mut" style="font-size:12px">Working tree clean</div>' :
          '<div class="mut" style="font-size:12px">Not sampled yet…</div>'}
        ${files ? `<div class="gfiles">${files}</div>` : ''}
        ${commits ? `<div class="gcommits">${commits}</div>` : ''}
        <div class="gmeta">
          <span>${r.sessions} session${r.sessions === 1 ? '' : 's'} · ${
            usd(r.cost)} · ${st ? `${st.commits_today} commits today` : '…'}</span>
          <a href="#/sessions?project=${encodeURIComponent(r.project)}">sessions →</a>
        </div>
      </div>
    </div>`;
  }).join('');

  $('#view').innerHTML = `
    <div class="hd">
      <div><h1>Git</h1><p class="sub">${d.repos.length} repositor${
        d.repos.length === 1 ? 'y' : 'ies'} with sessions in ${
        gitWindowText(rng)} · sampled every ${d.interval.toFixed(0)}s</p></div>
      <div class="right">${gitRangePills(rng)}${gitTabs('live')}</div>
    </div>

    <div class="kpis">
      ${[[t.wip.toLocaleString(), 'Uncommitted lines',
          `${t.staged.toLocaleString()} staged · ${t.unstaged.toLocaleString()} unstaged · ${
            t.untracked.toLocaleString()} untracked`],
         [(h.total_committed || 0).toLocaleString(), 'Lines committed',
          `${h.commit_count || 0} commit${h.commit_count === 1 ? '' : 's'}${
            scoped ? ` · ${esc(scoped.name)}` : ''} in ${gitWindowText(rng)}`],
         [`${t.dirty}<small>/${d.repos.length}</small>`, 'Repos with WIP',
          'uncommitted changes right now'],
         [d.repos.reduce((a, r) => a + r.live, 0), 'Live sessions',
          'across these repos']]
        .map(([v, k, n]) => `<div class="kpi"><div class="k">${k}</div>
          <div class="v">${v}</div>
          <div class="k" style="margin-top:6px;font-weight:400">${n}</div></div>`).join('')}
    </div>

    <div class="sect">Code written${scoped ? ` — ${esc(scoped.name)}` : ''}</div>
    <section class="blk" style="display:grid;margin-top:0;
        grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px">
      <div class="card">
        <div class="ch"><h2>Committed</h2>
          <span class="meta">${(h.total_committed || 0).toLocaleString()} lines · ${
            h.commit_count || 0} commit${h.commit_count === 1 ? '' : 's'} in window
            · ◆ hover for detail</span></div>
        <div class="cb"><div id="gitCommitted"></div></div>
      </div>
      <div class="card">
        <div class="ch"><h2>Uncommitted WIP</h2>
          <span class="meta">${lastWip ? `${lastWip.wip.toLocaleString()} lines now`
            : 'sampling starts with the monitor'}</span></div>
        <div class="cb"><div id="gitWip"></div></div>
      </div>
    </section>

    <div class="gitgrid">${cards ||
      '<div class="empty"><b>No repositories found</b>No session ran inside a git repository.</div>'}</div>`;

  hydrateTips($('#view'));
  wireGitTabs('live');
  wireGitRange(rng);
  gitChart($('#gitCommitted'), h.points,
           { mode: 'committed', commits: h.commits,
             start: h.start, end: h.end, height: 200 });
  gitChart($('#gitWip'), h.points,
           { mode: 'wip', start: h.start, end: h.end, height: 200 });
  $$('.gcard .scope').forEach(b =>
    b.addEventListener('click', ev => {
      ev.stopPropagation();
      gitSetP({ repo: b.dataset.repo === repo ? null : b.dataset.repo });
    }));
  wireTable($('#view'));
};

async function gitStatsView(params, repo) {
  const rng = params.get('range') || GIT_DEFAULT_RANGE;
  const [d, st] = await Promise.all([
    api('/api/git', { range: rng }),
    api('/api/git/stats', { range: rng, repo }),
  ]);
  const scoped = repo !== 'all' ? d.repos.find(r => r.id === repo) : null;
  const t = st.totals;

  $('#view').innerHTML = `
    <div class="hd">
      <div><h1>Git</h1><p class="sub">Commit analytics${scoped
        ? ` — ${esc(scoped.name)}` : ` across ${d.repos.length} repos`} · ${
        gitWindowText(rng)}</p></div>
      <div class="right">
        ${scoped ? `<a class="btn" href="#/git?view=stats">All repos ✕</a>` : ''}
        ${gitRangePills(rng)}
        ${gitTabs('stats')}
      </div>
    </div>

    <div class="kpis">
      ${[[t.commits.toLocaleString(), 'Commits', `${t.days} active day${
            t.days === 1 ? '' : 's'}`],
         [`<span class="gadd">+${tok(t.add)}</span>`, 'Lines added', ''],
         [`<span class="gdel">−${tok(t.del)}</span>`, 'Lines removed', ''],
         [t.files.toLocaleString(), 'Files touched', 'in this range']]
        .map(([v, k, n]) => `<div class="kpi"><div class="k">${k}</div>
          <div class="v">${v}</div>
          <div class="k" style="margin-top:6px;font-weight:400">${n}</div></div>`).join('')}
    </div>

    <section class="blk" style="display:grid;
        grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px">
      <div class="card">
        <div class="ch"><h2>Commits per ${st.per_day ? 'day' : 'hour'}</h2></div>
        <div class="cb"><div id="stCommits"></div></div>
      </div>
      <div class="card">
        <div class="ch"><h2>Lines added / removed</h2>
          ${legend([['added', '#16a34a'], ['removed', '#dc2626']])}</div>
        <div class="cb"><div id="stLines"></div></div>
      </div>
    </section>

    <section class="blk grid cols3">
      <div class="card"><div class="ch"><h2>By author</h2></div>
        <div class="cb">${barList(st.authors.map(a => ({
          label: a.name, value: a.add + a.del,
          text: `+${tok(a.add)} −${tok(a.del)}`,
          sub: `${a.commits} commit${a.commits === 1 ? '' : 's'}`,
          tip: `${a.commits} commits · +${a.add.toLocaleString()} −${
            a.del.toLocaleString()} lines`,
        })))}</div></div>
      <div class="card"><div class="ch"><h2>Busiest files</h2></div>
        <div class="cb">${barList(st.files.map(f => ({
          label: f.file.split('/').slice(-2).join('/'), value: f.add + f.del,
          text: `+${tok(f.add)} −${tok(f.del)}`,
          sub: `${f.commits}×`, tip: f.file,
        })))}</div></div>
      <div class="card"><div class="ch"><h2>Commits by hour of day</h2></div>
        <div class="cb"><div id="stHours"></div></div></div>
    </section>`;

  hydrateTips($('#view'));
  wireGitTabs('stats');
  wireGitRange(rng);
  const bl = st.buckets;
  const blab = b => st.per_day
    ? new Date(b.t * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    : new Date(b.t * 1000).toLocaleTimeString(undefined, { hour: 'numeric' });
  colChart($('#stCommits'), bl.map(b => ({
    v: b.commits, label: blab(b),
    tip: `${blab(b)}\n${b.commits} commit${b.commits === 1 ? '' : 's'}`,
  })), { height: 190, fmt: v => Math.round(v) });
  divChart($('#stLines'), bl.map(b => ({
    label: blab(b), up: b.add, down: b.del,
    tip: `${blab(b)}\n+${b.add.toLocaleString()} −${b.del.toLocaleString()} lines`,
  })), { height: 190 });
  colChart($('#stHours'), st.hours.map((n, hr) => ({
    v: n, label: hr % 3 === 0 ? String(hr) : '',
    tip: `${hr}:00–${hr + 1}:00\n${n} commit${n === 1 ? '' : 's'}`,
  })), { height: 170, fmt: v => Math.round(v), color: 'var(--gold)' });
}

// ── git repo explorer ─────────────────────────────────────────────────

/** Lane layout for a commit DAG (newest first, like `git log --graph`).
    Each row gets: its lane, which lanes pass through, joins (child branches
    folding into this commit) and forks (merge parents fanning out below). */
function layoutGraph(commits) {
  const lanes = [];      // sha each lane expects next
  const rows = [];
  for (const c of commits) {
    let lane = lanes.indexOf(c.sha);
    const hadIncoming = lane !== -1;
    if (lane === -1) {
      lane = lanes.indexOf(null);
      if (lane === -1) { lane = lanes.length; lanes.push(null); }
    }
    // other lanes waiting for this same commit fold into it here
    const joins = [];
    lanes.forEach((h, i) => {
      if (h === c.sha && i !== lane) { joins.push(i); lanes[i] = null; }
    });
    const passThrough = lanes.map((h, i) => h != null && i !== lane);
    lanes[lane] = c.parents[0] || null;
    const forks = [];
    for (let pi = 1; pi < c.parents.length; pi++) {
      const p = c.parents[pi];
      let pl = lanes.indexOf(p);
      if (pl === -1) {
        pl = lanes.indexOf(null);
        if (pl === -1) { pl = lanes.length; lanes.push(null); }
        lanes[pl] = p;
      }
      forks.push(pl);
    }
    rows.push({
      c, lane, joins, forks, passThrough, hadIncoming,
      hasParent: c.parents.length > 0,
      width: lanes.length,
    });
    while (lanes.length && lanes[lanes.length - 1] == null) lanes.pop();
  }
  const maxLanes = Math.min(
    Math.max(...rows.map(r => Math.max(r.lane + 1, r.width)), 1), 12);
  return { rows, maxLanes };
}

const LANE_W = 14, ROW_H = 40;
const laneColor = i => HUES[i % HUES.length];

/** Per-row SVG cell: pass-through lines, the commit dot, join/fork curves. */
function graphCell(row, maxLanes) {
  const w = maxLanes * LANE_W, mid = ROW_H / 2;
  const x = i => i * LANE_W + LANE_W / 2;
  let p = '';
  row.passThrough.forEach((on, i) => {
    if (on && i < maxLanes) p += `<path d="M${x(i)} 0V${ROW_H}" stroke="${
      laneColor(i)}" stroke-width="2" fill="none" opacity=".55"/>`;
  });
  const lx = x(Math.min(row.lane, maxLanes - 1));
  if (row.hadIncoming)
    p += `<path d="M${lx} 0V${mid}" stroke="${laneColor(row.lane)}" stroke-width="2"/>`;
  if (row.hasParent)
    p += `<path d="M${lx} ${mid}V${ROW_H}" stroke="${laneColor(row.lane)}" stroke-width="2"/>`;
  for (const j of row.joins) {
    if (j >= maxLanes) continue;
    p += `<path d="M${x(j)} 0C${x(j)} ${mid} ${lx} ${mid * 0.6} ${lx} ${mid}"
      stroke="${laneColor(j)}" stroke-width="2" fill="none"/>`;
  }
  for (const f of row.forks) {
    if (f >= maxLanes) continue;
    p += `<path d="M${lx} ${mid}C${x(f)} ${mid * 1.4} ${x(f)} ${mid} ${x(f)} ${ROW_H}"
      stroke="${laneColor(f)}" stroke-width="2" fill="none"/>`;
  }
  const merge = row.c.parents.length > 1;
  p += `<circle cx="${lx}" cy="${mid}" r="${merge ? 3.5 : 4.5}"
    fill="${merge ? 'var(--card)' : laneColor(row.lane)}"
    stroke="${laneColor(row.lane)}" stroke-width="${merge ? 2 : 1.5}"/>`;
  return `<svg width="${w}" height="${ROW_H}" viewBox="0 0 ${w} ${ROW_H}"
    aria-hidden="true">${p}</svg>`;
}

const refChip = r => {
  const tag = r.startsWith('tag: ');
  const head = r.includes('HEAD');
  const name = r.replace('tag: ', '').replace('HEAD -> ', '');
  return `<span class="refchip${tag ? ' tag' : ''}${head ? ' head' : ''}">${
    tag ? '⌂ ' : ''}${esc(name)}</span>`;
};

function renderCommitDetail(host, c) {
  if (!c) {
    host.innerHTML = '<div class="empty"><b>Pick a commit</b>Click any row in the history to inspect it.</div>';
    return;
  }
  const peak = Math.max(...c.files.map(f => f.add + f.del), 1);
  const fileRows = c.files.map(f => `
    <div class="gfile"><span class="fp" title="${esc(f.file)}">${esc(f.file)}</span>
      <span class="fbar"><i style="width:${
        Math.max(3, 100 * (f.add + f.del) / peak).toFixed(0)}%"></i></span>
      <span><b class="gadd">+${f.add.toLocaleString()}</b> <b class="gdel">−${
        f.del.toLocaleString()}</b></span></div>`).join('');
  const diffs = (c.patch || []).map((s, i) => `
    <details class="dfile" data-key="diff:${esc(s.file)}"${
      (c.patch.length === 1 && !s.truncated) ? ' open' : ''}>
      <summary><code>${esc(s.file)}</code>${
        s.truncated ? '<span class="mut"> · truncated</span>' : ''}</summary>
      <pre class="diff">${s.text.split('\n').map(l => {
        const e = esc(l);
        if (l.startsWith('+')) return `<i class="da">${e}</i>`;
        if (l.startsWith('-')) return `<i class="dd">${e}</i>`;
        if (l.startsWith('@@')) return `<i class="dh">${e}</i>`;
        return `<i>${e}</i>`;
      }).join('\n')}</pre>
    </details>`).join('');
  const [subject, ...rest] = (c.message || '').split('\n');
  const body = rest.join('\n').trim();
  host.innerHTML = `
    <div class="cmt-head">
      <h3>${esc(subject)}</h3>
      <div class="cmt-meta">
        <code>${esc(c.short)}</code> · ${esc(c.author)} ·
        ${dt(new Date(c.t * 1000).toISOString())}
        <span style="margin-left:auto"><b class="gadd">+${c.add.toLocaleString()}</b>
        <b class="gdel">−${c.del.toLocaleString()}</b> ·
        ${c.files.length} file${c.files.length === 1 ? '' : 's'}</span>
      </div>
      ${body ? `<pre class="cmt-body">${esc(body)}</pre>` : ''}
    </div>
    <div class="gfiles" style="border:0;padding-top:0">${fileRows}</div>
    ${diffs ? `<div class="sect" style="margin:14px 0 6px">Changes</div>${diffs}` : ''}
    ${c.patch_truncated ? '<p class="mut" style="font-size:.75rem">Large commit — the patch is capped for display.</p>' : ''}`;
}

async function gitRepoView(params, rid) {
  const viewKey = `repo:${rid}`;
  let pendingRestore = captureDetails($('#view').dataset.viewkey === viewKey);
  const d = await api(`/api/git/repo/${rid}`);
  const sel = params.get('c') || (d.commits[0] && d.commits[0].sha) || '';
  const st = d.stats;
  const { rows, maxLanes } = layoutGraph(d.commits);
  const authors = new Set(d.commits.map(c => c.author));

  const hist = rows.map(row => {
    const c = row.c;
    return `<div class="grow-row${c.sha === sel ? ' sel' : ''}" data-sha="${esc(c.sha)}"
        tabindex="0" role="button">
      <span class="gcell">${graphCell(row, maxLanes)}</span>
      <span class="gmsg">
        ${(c.refs || []).map(refChip).join('')}
        <span class="gsub" title="${esc(c.subject)}">${esc(c.subject)}</span>
      </span>
      <span class="gchurn">${c.add != null
        ? `<b class="gadd">+${tok(c.add)}</b> <b class="gdel">−${tok(c.del)}</b>` : ''}</span>
      <span class="gwho" title="${esc(c.author)}">${esc(c.author.split(' ')[0])}</span>
      <span class="dur">${ago(new Date(c.t * 1000).toISOString())}</span>
    </div>`;
  }).join('');

  $('#view').innerHTML = `
    ${crumb(`<a href="#/git">Git</a>`, esc(d.name))}
    <div class="hd">
      <div><h1>${avatar(d.name)} ${esc(d.name)}
        ${d.live ? `<span class="st live" style="margin-left:8px"><i></i>${
          d.live} live</span>` : ''}</h1>
        <p class="sub num">${st ? `${esc(st.branch)} · ` : ''}${esc(d.path)}</p></div>
      <div class="right">
        <a class="btn" href="#/sessions?project=${encodeURIComponent(d.project)}">Sessions →</a>
      </div>
    </div>

    <div class="kpis">
      ${[[st ? st.wip.toLocaleString() : '—', 'Uncommitted lines',
          st && st.wip ? `${st.unstaged_add.toLocaleString()} unstaged · ${
            st.untracked_lines.toLocaleString()} untracked` : 'working tree clean'],
         [st ? st.commits_today : '—', 'Commits today',
          st ? `+${st.committed_add.toLocaleString()} lines` : ''],
         [d.commits.length, 'Commits shown', 'all branches, newest first'],
         [authors.size, `Author${authors.size === 1 ? '' : 's'}`, 'in this graph'],
         [`${d.sessions}`, 'Claude sessions', `${usd(d.cost)} spent here`]]
        .map(([v, k, n]) => `<div class="kpi"><div class="k">${k}</div>
          <div class="v">${v}</div>
          <div class="k" style="margin-top:6px;font-weight:400">${n}</div></div>`).join('')}
    </div>

    <section class="blk repo-split">
      <div class="card">
        <div class="ch"><h2>History</h2><span class="meta">click a commit ·
          ◦ merge</span></div>
        <div class="cb ghist" id="ghist">${hist ||
          '<div class="empty">No commits found.</div>'}</div>
      </div>
      <div class="card">
        <div class="ch"><h2>Commit</h2><span class="meta" id="cmtMeta"></span></div>
        <div class="cb gdetail" id="gdetail">
          <div class="skel">${'<div class="skel-row"></div>'.repeat(6)}</div>
        </div>
      </div>
    </section>`;

  hydrateTips($('#view'));
  $('#view').dataset.viewkey = viewKey;
  const detail = $('#gdetail');
  const load = async sha => {
    try {
      renderCommitDetail(detail, await api(`/api/git/repo/${rid}/commits/${sha}`));
      // Only the silent re-render restores; a user picking another commit
      // starts from that commit's defaults.
      if (pendingRestore) { restoreDetails(pendingRestore); pendingRestore = null; }
    } catch (e) {
      detail.innerHTML = `<div class="empty"><b>Couldn't load commit</b>${esc(e.message)}</div>`;
    }
  };
  $$('.grow-row', $('#ghist')).forEach(el => {
    const pick = () => {
      $$('.grow-row.sel').forEach(x => x.classList.remove('sel'));
      el.classList.add('sel');
      const p = new URLSearchParams(location.hash.split('?')[1] || '');
      p.set('c', el.dataset.sha);
      history.replaceState(null, '', `#/git/repo/${rid}?${p}`);
      load(el.dataset.sha);
    };
    el.addEventListener('click', pick);
    el.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); }
    });
  });
  if (sel) load(sel);
}

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

views.cost = async (params) => {
  const project = params.get('project') || '';
  const [d, planRaw] = await Promise.all([
    api('/api/summary', { project }),
    api('/api/plan').catch(() => null),
  ]);
  // Plan limits are account-level; inside a project scope they'd mislead.
  const plan = project ? null : planRaw;
  state.summary = d;
  const t = d.totals, c = d.cache, e = d.economics;
  // Spend within the window, from the same daily series the chart draws —
  // whole-session totals disagree with it whenever a session straddles the
  // window edge.
  const wSpend = d.daily.reduce((a, r) => a + r.cost, 0);
  const wUncached = d.daily.reduce((a, r) => a + (r.uncached || 0), 0);
  const wSaved = Math.max(0, wUncached - wSpend);
  const wSavedPct = wUncached ? 100 * wSaved / wUncached : 0;
  const typeRows = COST_ROWS;
  $('#view').innerHTML = `
    <div class="hd"><div><h1>Cost${project ? ` — ${esc(project)}` : ''}</h1>
      <p class="sub">Last ${winLabel()} · all figures are API list-price equivalents,
        not amounts billed.</p></div>
      <div class="right">
        ${project ? `<a class="btn" href="#/cost">All projects ✕</a>` : ''}
        ${windowPicker()}</div></div>

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

views.tools = async (params) => {
  const project = params.get('project') || '';
  const d = await api('/api/summary', { project });
  const total = d.tools.reduce((a, b) => a + b.total, 0) || 1;
  const max = Math.max(...d.tools.map(x => x.total), 1);
  $('#view').innerHTML = `
    <div class="hd"><div><h1>Tools${project ? ` — ${esc(project)}` : ''}</h1>
      <p class="sub">${total.toLocaleString()} tool calls in the last ${winLabel()}
        · main thread vs subagents · click a tool to see what it ran</p></div>
      <div class="right">
        ${project ? `<a class="btn" href="#/tools">All projects ✕</a>` : ''}
        ${windowPicker()}${legend([
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
  wireToolRows($('#view'), null, project);
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

  // Which sidebar entry owns this screen. Sessions are the primary
  // hierarchy: detail pages (one session, one agent run, one workflow) all
  // light Sessions, since that is the list they hang off.
  const PARENT = { session: 'sessions', agent: 'sessions',
                   workflow: 'sessions', workflows: 'sessions' };
  const section = PARENT[name] || name;

  $$('.nav a').forEach(a => {
    const on = a.getAttribute('href') === '#/' + section;
    a.classList.toggle('active', on);
    if (on) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });

  const proj = params.get('project') || '';
  const scopeName = proj ? ` — ${proj}` : '';
  document.title = `${(PAGE[name] || {}).title || 'Dashboard'}${scopeName} · Claude Code Monitor`;

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
    // A restarted server may be serving newer assets than this tab loaded.
    // Reload once so pills, endpoints and rendering can never disagree —
    // navigation state lives in the URL hash, so nothing is lost.
    if (d.asset_version && window.__ASSET_V__ &&
        String(d.asset_version) !== String(window.__ASSET_V__)) {
      location.reload();
      return;
    }
    state.live = d;

    // Repaint live-activity lines in place — the 12s page refresh is far too
    // slow for "what is it doing right now".
    const byId = new Map(d.live.map(s => [s.id, s]));
    $$('[data-actsid]').forEach(el => {
      const s = byId.get(el.dataset.actsid);
      if (s && s.activity) {
        const t = document.createElement('template');
        t.innerHTML = actHTML(s).trim();
        el.replaceWith(t.content.firstElementChild);
      } else el.remove();   // went idle; the periodic refresh fixes the pill
    });
    $$('[data-nowpanel]').forEach(el => {
      const s = byId.get(el.dataset.nowpanel);
      // The panel holds copyable commands — never rebuild it mid-selection.
      const sel = getSelection();
      if (sel && String(sel) && el.contains(sel.anchorNode)) return;
      if (s && s.activity) {
        const t = document.createElement('template');
        t.innerHTML = nowPanel(s, d.running_agents.filter(
          a => a.session_id === s.id)).trim();
        el.replaceWith(t.content.firstElementChild);
      } else el.remove();   // went idle; the periodic refresh redraws the page
    });

    const n = d.live.length;
    const tps = d.tps_now != null ? d.tps_now
      : d.live.reduce((a, s) => a + (s.output_tps || 0), 0);
    stream.pts.push(n ? tps : 0);
    while (stream.pts.length > STREAM_KEEP) stream.pts.shift();

    $('#sideInfo').classList.toggle('on', n > 0);
    $('#sideLive').innerHTML = n
      ? `<i></i>${n} session${n > 1 ? 's' : ''} live`
      : '<i></i>No sessions running';
    const t24 = d.tokens_24h || {};
    $('#sideStats').innerHTML = n
      ? `${Math.round(tps).toLocaleString()} tok/s out · ${
          d.running_agents.length} agents<br>${usd(d.burn_rate_hourly)}/hr burn`
      : `${tok(splitTotal(t24))} tokens in 24h · ${tok(t24.output)} out<br>${
          usd(d.spend_24h)} list-price`;
    drawSideSpark();

    const badge = $('#liveBadge');
    badge.hidden = n === 0;
    badge.textContent = n;
    if (!state.plan) fetchPlan();
  } catch (e) { /* server restarting; retry next tick */ }
}

// ── plan & limits → sidebar ───────────────────────────────────────────
// planReq shares the first request with any view that wants the payload
// (Sessions renders the session-window breakdown from it), so a page
// navigation never fires a second fetch.
function planFetch() { return fetch('/api/plan').then(r => r.json()); }
function planPaint(p) {
  state.plan = p;
  if (p) paintPlan(p);
  return p;
}
let planReq = planFetch().then(planPaint).catch(() => null);
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
    planReq = planFetch().then(planPaint).catch(() => null);
    await planReq;
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
// Tick the activity elapsed counters between polls so they read as live.
setInterval(() => {
  $$('.asince[data-since]').forEach(el => {
    const t0 = +el.dataset.since;
    if (t0) el.textContent = dur(Math.max(1, Date.now() / 1000 - t0));
  });
}, 1000);
setInterval(fetchPlan, 180000);
// Silent refresh for the pages that show live state — otherwise the running
// counts and status pills are a snapshot of whenever you navigated in.
const LIVE_PAGES = new Set(['overview', 'sessions', 'session',
  'workflows', 'workflow', 'git']);
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
