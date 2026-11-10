// Ansicht "Erträge": Säulendiagramm (Woche/Monat: Tage, Jahr: Monate) mit Vorjahren daneben.
// Eigenes SVG, keine Bibliothek. Daten kommen aus /api/daily (Map date -> Zeile), gerendert wird von history.js.
import { h, sv, nice } from './common.js';
import { t, lang, num } from './i18n.js';

export const KINDS = ['week', 'month', 'year'];
const pad = (n) => String(n).padStart(2, '0');
export const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const MAX_BAR = 28;

// Gleicher Kalendertag vor dy Jahren; null, wenn er nicht existiert (29.02.)
function sameDay(d, dy) {
  const x = new Date(d.getFullYear() - dy, d.getMonth(), d.getDate());
  return x.getMonth() === d.getMonth() ? x : null;
}

// Kalenderperiode (off = 0 aktuelle, -1 vorherige, ...)
export function periodOf(kind, off, now = new Date()) {
  const slots = [];
  let start; let end;
  if (kind === 'week') {
    const mon = new Date(now.getFullYear(), now.getMonth(), now.getDate() - ((now.getDay() + 6) % 7) + 7 * off);
    for (let i = 0; i < 7; i++) slots.push({ d: new Date(mon.getFullYear(), mon.getMonth(), mon.getDate() + i) });
  } else if (kind === 'month') {
    const first = new Date(now.getFullYear(), now.getMonth() + off, 1);
    const n = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate();
    for (let i = 0; i < n; i++) slots.push({ d: new Date(first.getFullYear(), first.getMonth(), i + 1) });
  } else {
    const y = now.getFullYear() + off;
    for (let i = 0; i < 12; i++) slots.push({ d: new Date(y, i, 1) });
  }
  start = slots[0].d; end = kind === 'year' ? new Date(start.getFullYear(), 11, 31) : slots[slots.length - 1].d;
  return { kind, off, slots, start, end, ref: start.getFullYear() };
}

export function periodLabel(p) {
  if (p.kind === 'year') return String(p.ref);
  if (p.kind === 'month') return new Intl.DateTimeFormat(lang, { month: 'long', year: 'numeric' }).format(p.start);
  const f = new Intl.DateTimeFormat(lang, { day: 'numeric', month: 'short', year: 'numeric' });
  return typeof f.formatRange === 'function' ? f.formatRange(p.start, p.end) : `${f.format(p.start)} – ${f.format(p.end)}`;
}

// Wert eines Slots fuer die Serie "dy Jahre zurueck": { v, n, days } (v = null ohne Daten)
function slotValue(kind, d, dy, daily) {
  if (kind !== 'year') {
    const x = dy ? sameDay(d, dy) : d;
    const r = x && daily.get(ymd(x));
    return { v: r && r.yield_kwh != null ? r.yield_kwh : null };
  }
  const y = d.getFullYear() - dy; const m = d.getMonth();
  const days = new Date(y, m + 1, 0).getDate();
  let s = 0; let n = 0;
  for (let i = 1; i <= days; i++) {
    const r = daily.get(`${y}-${pad(m + 1)}-${pad(i)}`);
    if (r && r.yield_kwh != null) { s += r.yield_kwh; n++; }
  }
  return { v: n ? s : null, n, days };
}

// Fruehester Tag mit Daten (zum Begrenzen von ‹)
export function oldestDate(daily) {
  let o = null;
  for (const k of daily.keys()) if (o == null || k < o) o = k;
  return o;
}

const fmtKwh = (v) => num(v, Math.abs(v) >= 100 ? 0 : 1);
const fmtSum = (v) => num(v, Math.abs(v) >= 1000 ? 0 : 1);
let hover = null;

export function hide(tip, svg) {
  hover = null; tip.hidden = true;
  const hl = svg.querySelector('.hl'); if (hl) hl.setAttribute('visibility', 'hidden');
}

// ctx: { svg, box, tip, legend, daily: Map, period, years: [Vorjahre] }  ->  { hasData }
export function render(ctx) {
  const { svg, box, tip, legend, daily, period } = ctx;
  const W = Math.max(260, Math.round(box.clientWidth || 640));
  const mobile = W < 520;
  const week = period.kind === 'week';
  const Ht = (mobile ? 270 : 340) + (week ? 12 : 0);
  const n = period.slots.length;
  const sers = [{ year: period.ref, dy: 0 }, ...ctx.years.map((y) => ({ year: y, dy: period.ref - y }))];
  for (const s of sers) {
    s.vals = period.slots.map((sl) => slotValue(period.kind, sl.d, s.dy, daily));
    const have = s.vals.filter((v) => v.v != null);
    s.sum = have.length ? have.reduce((a, v) => a + v.v, 0) : null;
  }
  const keep = sers.filter((s, i) => i === 0 || s.sum != null);
  const hasData = keep.some((s) => s.sum != null);
  if (!hasData) { svg.replaceChildren(); legend.replaceChildren(); tip.hidden = true; return { hasData }; }

  let max = 0;
  for (const s of keep) for (const v of s.vals) if (v.v != null && v.v > max) max = v.v;
  const ax = nice(0, max * 1.04 || 1, mobile ? 4 : 5);
  const m = { l: mobile ? 38 : 48, r: 8, t: 20, b: week ? 42 : 28 };
  const pw = W - m.l - m.r; const ph = Ht - m.t - m.b;
  const gw = pw / n; const k = keep.length;
  const bw = Math.max(2, Math.min(MAX_BAR, (gw * 0.8) / k));
  const gx = (i) => m.l + i * gw + (gw - bw * k) / 2;
  const Y = (v) => m.t + ph - ((v - ax.min) / (ax.max - ax.min)) * ph;
  svg.setAttribute('viewBox', `0 0 ${W} ${Ht}`); svg.setAttribute('width', W); svg.setAttribute('height', Ht);

  const kids = [];
  const dec = Number.isInteger(Math.round(ax.step * 1e6) / 1e6) ? 0 : 1;
  for (let v = ax.min; v <= ax.max + ax.step / 2; v += ax.step) {
    const y = Y(v);
    kids.push(sv('line', { class: 'gl', x1: m.l, x2: W - m.r, y1: y, y2: y }));
    const tx = sv('text', { x: m.l - 6, y: y + 4, 'text-anchor': 'end' }); tx.textContent = num(v, dec); kids.push(tx);
  }
  const ul = sv('text', { x: m.l - 6, y: 11, 'text-anchor': 'end' }); ul.textContent = 'kWh'; kids.push(ul);

  // X-Beschriftung
  const wd = new Intl.DateTimeFormat(lang, { weekday: 'short' });
  const mo = new Intl.DateTimeFormat(lang, { month: gw >= 30 ? 'short' : 'narrow' });
  const every = period.kind === 'month' ? (gw >= 22 ? 1 : gw >= 11 ? 2 : 5) : 1;
  period.slots.forEach((sl, i) => {
    const cx = m.l + i * gw + gw / 2;
    if (period.kind === 'month' && i % every !== 0) return;
    const l1 = sv('text', { x: cx, y: Ht - (week ? 22 : 8), 'text-anchor': 'middle' });
    l1.textContent = period.kind === 'year' ? mo.format(sl.d) : period.kind === 'week' ? wd.format(sl.d) : String(sl.d.getDate());
    kids.push(l1);
    if (week) { const l2 = sv('text', { x: cx, y: Ht - 8, 'text-anchor': 'middle', class: 'day' }); l2.textContent = String(sl.d.getDate()); kids.push(l2); }
  });
  const hl = sv('rect', { class: 'hl', x: m.l, y: m.t, width: gw, height: ph, visibility: 'hidden' });
  kids.push(hl);
  const labels = k === 1 && gw >= 34;
  keep.forEach((s, si) => {
    const g = sv('g', { class: 'yb yb-' + (si % 5) });
    s.vals.forEach((v, i) => {
      if (v.v == null) return;
      const y = Y(v.v); const hgt = Math.max(1, m.t + ph - y);
      g.append(sv('rect', { x: gx(i) + si * bw + (k > 1 && bw > 4 ? 0.5 : 0), y: m.t + ph - hgt, width: Math.max(1.5, bw - (k > 1 && bw > 4 ? 1 : 0)),
        height: hgt, rx: bw >= 6 ? 2.5 : 0.8, class: v.n != null && v.n < v.days ? 'part' : '' }));
      if (labels) { const lt = sv('text', { x: gx(i) + bw / 2, y: y - 4, 'text-anchor': 'middle', class: 'bv' }); lt.textContent = fmtKwh(v.v); g.append(lt); }
    });
    kids.push(g);
  });
  const ov = sv('rect', { class: 'ov', x: m.l, y: m.t, width: pw, height: ph });
  svg.replaceChildren(...kids, ov);

  const dfTip = period.kind === 'year' ? new Intl.DateTimeFormat(lang, { month: 'long', year: 'numeric' })
    : new Intl.DateTimeFormat(lang, { weekday: 'short', day: 'numeric', month: 'long', year: 'numeric' });
  const show = (i) => {
    hover = i;
    const hlr = svg.querySelector('.hl');
    hlr.setAttribute('x', m.l + i * gw); hlr.setAttribute('width', gw); hlr.setAttribute('visibility', 'visible');
    const rows = [h('strong', {}, dfTip.format(period.slots[i].d))];
    keep.forEach((s, si) => {
      const v = s.vals[i];
      rows.push(h('div', { class: 'row yb-' + (si % 5) }, h('i'), h('span', {}, String(s.year)),
        h('b', {}, v.v == null ? '–' : fmtKwh(v.v) + ' kWh'),
        v.n != null && v.n < v.days && v.v != null ? h('small', {}, t('hist.yld_partial', { n: v.n, days: v.days })) : null));
    });
    tip.replaceChildren(...rows); tip.hidden = false;
    const x = m.l + (i + 0.5) * gw; const right = x > W * 0.5;
    tip.style.left = right ? 'auto' : Math.min(x + 12, W - 150) + 'px';
    tip.style.right = right ? (W - x + 12) + 'px' : 'auto';
  };
  const mv = (ev) => {
    const r = svg.getBoundingClientRect();
    const px = ((ev.clientX - r.left) / r.width) * W;
    const i = Math.floor((px - m.l) / gw);
    if (i >= 0 && i < n) show(i);
  };
  ov.addEventListener('pointermove', mv); ov.addEventListener('pointerdown', mv);
  ov.addEventListener('pointerleave', (ev) => { if (ev.pointerType === 'mouse') hide(tip, svg); });
  if (hover != null && hover < n) show(hover);

  legend.replaceChildren(...keep.map((s, si) => h('li', { class: 'yb-' + (si % 5) }, h('i'), h('span', {}, String(s.year)),
    h('b', {}, s.sum == null ? '–' : fmtSum(s.sum) + ' kWh'))));
  return { hasData };
}
