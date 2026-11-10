// Seite "Verlauf": Liniendiagramm als eigenes SVG (keine Bibliothek).
import { $, h, sv, api, fieldName, tryT, humanize, toast, nice, csvSep, download } from './common.js';
import { t, lang, num, power } from './i18n.js';
import * as yl from './yields.js';

const MAX_FIELDS = 6;
const MAX_UNITS = 2;
const DEFAULT_FIELDS = ['input_power_w', 'active_power_w'];
const DAY = 86400000;
const PRESET_DAYS = [3, 7, 14, 30, 90];
const LIVE_MAX_DAYS = 7;      // Live-Nachladen nur bis zu dieser Spanne
const GAP_MS = 600e3;         // Linien verbinden Luecken bis 600 s (5-Minuten-Daten)
const PAD = 15 * 60e3;        // Tagesansicht: 15 min vor erstem / nach letztem Ertrag
const CSV_MAX_DAYS = 400;
const MODE_KEY = { line: 'hist.mode_line', yields: 'hist.mode_yields' };
const CMP_KEY = { off: 'hist.cmp_off', prev: 'hist.cmp_prev', all: 'hist.cmp_all' };
const KIND_KEY = { week: 'hist.yld_week', month: 'hist.yld_month', year: 'hist.yld_year' };

const load = (k, d) => { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } };
const H = {
  preset: load('h2m-hist-preset', 'today'), fields: load('h2m-hist-fields', DEFAULT_FIELDS),
  data: null, available: [], err: null, seq: 0, active: false, lastLive: null, width: 0, range: load('h2m-hist-range', null), fb: null,
  getStatus: () => null, timer: null, builtSig: '', hover: null, r: null, pts: [], vt: [], geo: null,
  mode: load('h2m-hist-mode', 'line'), cmp: load('h2m-hist-cmp', 'off'), ypd: load('h2m-yld-kind', 'month'), yoff: 0,
  daily: undefined, dailyAt: 0, years: [], cmpData: [], cmpCache: new Map(),
};

// ---------- Zeitraum ----------
function presets() {
  const hs = H.getStatus()?.history;
  const days = hs?.days || 7;
  const long = hs && 'samples_5m' in hs;   // rc3-Backend: Verlauf ueber Jahre
  const list = ['today', '24h', ...PRESET_DAYS.filter((d) => long || d <= days).map((d) => 'd' + d)];
  if (!long && days > 3 && !PRESET_DAYS.includes(days)) list.push('d' + days);
  if (long) list.push('y1', 'all', 'range');
  return list;
}
function presetLabel(p) {
  if (p === 'today' || p === '24h' || p === 'y1' || p === 'all' || p === 'range') return t('hist.preset.' + p);
  return t('hist.preset.days', { n: p.slice(1) });
}
function midnight(ms) { const d = new Date(ms); d.setHours(0, 0, 0, 0); return d.getTime(); }
function nextMidnight(ms) { const d = new Date(ms); d.setDate(d.getDate() + 1); d.setHours(0, 0, 0, 0); return d.getTime(); }
const ymd = (ms) => { const d = new Date(ms); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; };
const parseYmd = (v) => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v || ''); return m ? new Date(+m[1], +m[2] - 1, +m[3]).getTime() : null; };
// Gleicher Kalenderzeitpunkt vor dy Jahren (29.02. -> 28.02.)
function shiftYear(ms, dy) {
  const d = new Date(ms); const mo = d.getMonth();
  d.setFullYear(d.getFullYear() - dy);
  if (d.getMonth() !== mo) d.setDate(0);
  return d.getTime();
}
const DM = () => H.daily || new Map();

// Vorjahre fuer den Vergleich (nur Jahre mit Daten, vor dem Bezugsjahr), neueste zuerst
function yearsBefore(ref) { return H.years.filter((y) => y < ref).sort((a, b) => b - a); }
function cmpYearsFor(ref) {
  if (H.cmp === 'off') return [];
  const ys = yearsBefore(ref);
  if (H.cmp === 'prev') return ys.includes(ref - 1) ? [ref - 1] : [];
  return ys;
}
function cmpOptions(ref) {
  const ys = yearsBefore(ref); const o = ['off'];
  if (ys.includes(ref - 1)) o.push('prev');
  if (ys.length >= 2) o.push('all');
  return o;
}

// Produktionsfenster eines Tages (erster Ertrag -15 min .. letzter Ertrag +15 min), null ohne Ertragszeiten
function prodWindow(m, row) {
  const f = Date.parse(row?.first_production_ts || '');
  if (!Number.isFinite(f)) return null;
  const l = Date.parse(row.last_production_ts || '');
  const now = Date.now();
  let last = Number.isFinite(l) ? l : f;
  if (m === midnight(now) && (!Number.isFinite(l) || now - l < PAD)) last = Math.max(last, now);   // heute noch in Produktion
  return { x0: f, x1: Math.max(last, f) };
}
// Achse fuer genau einen Tag: Produktionszeit (aller verglichenen Jahre vereinigt) +/- 15 min, sonst 00-24 Uhr
function dayWindow(m) {
  const end = nextMidnight(m);
  const wins = [];
  const own = prodWindow(m, DM().get(ymd(m)));
  if (own) wins.push(own);
  const ref = new Date(m).getFullYear();
  for (const y of cmpYearsFor(ref)) {
    const row = DM().get(ymd(shiftYear(m, ref - y)));
    const f = Date.parse(row?.first_production_ts || ''); const l = Date.parse(row?.last_production_ts || '');
    if (!Number.isFinite(f) || !Number.isFinite(l)) continue;
    const mm = midnight(f);   // Tageszeit auf den angezeigten Tag abbilden
    wins.push({ x0: m + (f - mm), x1: m + (l - midnight(l)) });
  }
  if (!wins.length) return { x0: m, x1: end };
  return { x0: Math.max(m, Math.min(...wins.map((w) => w.x0)) - PAD), x1: Math.min(end, Math.max(...wins.map((w) => w.x1)) + PAD) };
}
function customRange() {
  const now = Date.now();
  let from = parseYmd(H.range?.from); let to = parseYmd(H.range?.to);
  if (from == null) from = midnight(now - 30 * DAY);
  if (to == null) to = midnight(now);
  if (to < from) [from, to] = [to, from];
  const e = nextMidnight(to);   // bis-Tag inklusive
  if (from === to) { const w = dayWindow(from); return { from: w.x0, to: Math.min(w.x1, now), x0: w.x0, x1: w.x1 }; }
  return { from, to: Math.min(e, now), x0: from, x1: Math.max(e, from + DAY) };
}
function range() {
  const now = Date.now();
  if (H.preset === 'today') {
    const m = midnight(now); const w = dayWindow(m);
    return { from: w.x0, to: Math.min(w.x1, now), x0: w.x0, x1: w.x1 };
  }
  if (H.preset === 'range') return customRange();
  if (H.preset === 'all') {
    const o = Date.parse(H.getStatus()?.history?.oldest || '');
    const from = Number.isFinite(o) ? midnight(o) : now - 365 * DAY;
    return { from, to: now, x0: from, x1: now };
  }
  const n = H.preset === '24h' ? 1 : H.preset === 'y1' ? 365 : Number(H.preset.slice(1)) || 1;
  return { from: now - n * DAY, to: now, x0: now - n * DAY, x1: now };
}
// "Live" und Nachladen pro Poll nur bei kurzen Zeitraeumen, die bis "jetzt" reichen (Heute, 24 h, bis 7 Tage)
const isLiveRange = () => H.mode === 'line' && (H.preset === 'today' || H.preset === '24h'
  || (/^d\d+$/.test(H.preset) && Number(H.preset.slice(1)) <= LIVE_MAX_DAYS));
const refYearOf = (r) => new Date(r.x1 - 1).getFullYear();
// Vergleich als Linie nur fuer Zeitraeume bis 7 Tage (ausser Preset 'all'/'y1')
const lineCmpOk = (r) => H.mode === 'line' && H.preset !== 'all' && H.preset !== 'y1' && r.x1 - r.x0 <= LIVE_MAX_DAYS * DAY + 3600e3;

async function loadDaily(force) {
  if (!force && H.daily !== undefined && Date.now() - H.dailyAt < 60e3) return;
  const res = await api('GET', `/api/daily?from=${ymd(Date.now() - 3650 * DAY)}&to=${ymd(Date.now() + DAY)}`);
  H.dailyAt = Date.now();
  if (res.ok && Array.isArray(res.data?.days)) {
    H.daily = new Map(res.data.days.map((d) => [d.date, d]));
    H.years = [...new Set(res.data.days.filter((d) => d.yield_kwh != null).map((d) => Number(d.date.slice(0, 4))))].sort();
  } else if (res.status === 404 || res.status === 501) { H.daily = null; H.years = []; }
  else if (H.daily === undefined) { H.daily = null; H.years = []; }
}

// ---------- Laden ----------
async function fetchHistory() {
  const r = range();
  let plot = Math.max(240, Math.min(1000, Math.round((H.width || 700) - 80)));
  // Lange Zeitraeume: Bucketbreite als Teiler/Vielfaches von 24 h, sonst Alias-Muster zur Tageskurve
  const spanMs = r.to - r.from;
  if (spanMs > 2 * DAY) {
    const step = [1, 2, 3, 4, 6, 12, 24, 48, 96].map((x) => x * 3600e3).find((x) => spanMs / x <= plot) || 96 * 3600e3;
    plot = Math.max(2, Math.min(2000, Math.round(spanMs / step)));
  }
  const q = (withFields) => `/api/history?from=${encodeURIComponent(new Date(r.from).toISOString())}&to=${encodeURIComponent(new Date(r.to).toISOString())}`
    + (withFields ? '&fields=' + encodeURIComponent(H.fields.join(',')) : '') + `&points=${plot}`;
  let res = await api('GET', q(true));
  if (res.status === 400) res = await api('GET', q(false));
  return { res, r };
}
// Feld, das mit den Vorjahren verglichen wird: das erste Leistungsfeld, sonst das erste Feld
function cmpField() {
  const f = H.fields.filter((n) => H.available.some((a) => a.name === n));
  return f.find((n) => unitOf(n) === 'W') || f[0] || null;
}
// Summe yield_kwh der Kalendertage im Bereich (dy Jahre zurueck); null ohne Daten
function periodSum(x0, x1, dy) {
  const D = H.daily; if (!D) return null;
  let s = 0; let n = 0;
  for (let m = midnight(x0); m < x1; m = nextMidnight(m)) {
    const row = D.get(ymd(dy ? shiftYear(m, dy) : m));
    if (row && row.yield_kwh != null) { s += row.yield_kwh; n++; }
  }
  return n ? s : null;
}
// Vorjahreslinien: dasselbe Feld im gleichen Kalenderzeitraum, zeitlich ins aktuelle Jahr verschoben
async function fetchCmp(r, d) {
  const ref = refYearOf(r); const f = cmpField();
  const years = lineCmpOk(r) && f ? cmpYearsFor(ref) : [];
  if (!years.length) return [];
  const STEP = 15 * 60e3;
  const a0 = Math.floor(r.x0 / STEP) * STEP; const a1 = Math.ceil(r.x1 / STEP) * STEP;   // stabiler Schluessel (kein Neuladen je Poll)
  const pts = Math.max(2, Math.min(2000, Math.round((a1 - a0) / 1000 / Math.max(d.step_s || 60, 1))));
  const out = await Promise.all(years.map(async (y) => {
    const dy = ref - y; const from = shiftYear(a0, dy); const to = shiftYear(a1, dy);
    const key = [f, from, to, pts].join('|');
    let res = H.cmpCache.get(key);
    if (!res) {
      const r2 = await api('GET', `/api/history?from=${encodeURIComponent(new Date(from).toISOString())}&to=${encodeURIComponent(new Date(to).toISOString())}`
        + `&fields=${encodeURIComponent(f)}&points=${pts}`);
      res = r2.ok && r2.data?.series?.[f] ? r2.data : null;
      if (res) { if (H.cmpCache.size > 40) H.cmpCache.clear(); H.cmpCache.set(key, res); }
    }
    if (!res) return null;
    const s = res.series[f]; const off = a0 - from;
    const t2 = res.t.map((x) => x + off);
    if (!(s.avg || []).some((v) => v != null)) return null;
    return { year: y, field: f, t: t2, avg: s.avg, min: s.min, max: s.max, vt: t2.filter((_, i) => s.avg[i] != null), sum: periodSum(r.x0, r.x1, dy) };
  }));
  return out.filter(Boolean);
}
async function reload() {
  if (!H.active) return;
  const seq = ++H.seq;
  await loadDaily(false);
  if (seq !== H.seq) return;
  if (H.mode === 'yields') { H.err = H.daily === null ? 'unsupported' : null; build(); return; }
  const { res, r } = await fetchHistory();
  if (seq !== H.seq) return;
  if (res.status === 404 || res.status === 501) H.err = 'unsupported';
  else if (!res.ok || !res.data) H.err = 'error';
  else {
    H.err = null; H.data = res.data; H.r = r;
    if (Array.isArray(res.data.available) && res.data.available.length) H.available = res.data.available;
    sanitizeFields();
    try { H.cmpData = await fetchCmp(r, res.data); } catch { H.cmpData = []; }
    if (seq !== H.seq) return;
  }
  build();
}
function sanitizeFields() {
  const names = new Set(H.available.map((a) => a.name));
  if (!names.size) return;
  let f = H.fields.filter((n) => names.has(n));
  if (!f.length) f = DEFAULT_FIELDS.filter((n) => names.has(n));
  if (!f.length) f = [H.available[0].name];
  if (f.join() !== H.fields.join()) { H.fields = f; localStorage.setItem('h2m-hist-fields', JSON.stringify(f)); }
}

// ---------- Skala & Ticks ----------
const TSTEPS = [60e3, 120e3, 300e3, 600e3, 900e3, 1800e3, 3600e3, 7200e3, 10800e3, 21600e3, 43200e3, DAY, 2 * DAY, 7 * DAY];
const MSTEPS = [1, 2, 3, 6, 12, 24];   // Monate
function timeTicks(x0, x1, pw, minGap) {
  const want = Math.max(2, Math.floor(pw / minGap));
  let step = TSTEPS.find((s) => (x1 - x0) / s <= want);
  if (!step) {  // sehr lange Spannen: Monatsraster
    const mths = (x1 - x0) / (30.44 * DAY);
    const ms = MSTEPS.find((m) => mths / m <= want) || 24;
    const out = []; const d = new Date(x0); d.setDate(1); d.setHours(0, 0, 0, 0);
    if (ms > 1) d.setMonth(Math.floor(d.getMonth() / ms) * ms);
    for (; d.getTime() <= x1; d.setMonth(d.getMonth() + ms)) if (d.getTime() >= x0) out.push(d.getTime());
    return { ticks: out, step: ms * 30 * DAY, months: ms };
  }
  const out = [];
  if (step < DAY) {
    const base = midnight(x0);
    for (let tt = base + Math.ceil((x0 - base) / step) * step; tt <= x1 + 1; tt += step) out.push(tt);
  } else {
    const d = new Date(x0); d.setHours(0, 0, 0, 0); if (d.getTime() < x0) d.setDate(d.getDate() + 1);
    for (; d.getTime() <= x1; d.setDate(d.getDate() + step / DAY)) out.push(d.getTime());
  }
  return { ticks: out, step, months: 0 };
}
const dfHM = () => new Intl.DateTimeFormat(lang, { hour: '2-digit', minute: '2-digit' });
const dfDay = () => new Intl.DateTimeFormat(lang, { weekday: 'short', day: 'numeric', month: 'numeric' });
const dfDayShort = () => new Intl.DateTimeFormat(lang, { day: 'numeric', month: 'numeric' });
const dfMonth = (mobile) => new Intl.DateTimeFormat(lang, { month: 'short', year: mobile ? '2-digit' : 'numeric' });
const dfTipDate = () => new Intl.DateTimeFormat(lang, { day: 'numeric', month: 'short', year: 'numeric' });
const dfTipMin = () => new Intl.DateTimeFormat(lang, { weekday: 'short', day: 'numeric', month: 'numeric', year: '2-digit', hour: '2-digit', minute: '2-digit' });
const tipFmt = (stepS) => (stepS >= 43200 ? dfTipDate() : stepS >= 60 ? dfTipMin() : dfTip());
const dfTip = () => new Intl.DateTimeFormat(lang, { weekday: 'short', day: 'numeric', month: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' });

function fmtVal(v, unit) {
  if (v == null || Number.isNaN(v)) return '–';
  if (unit === 'W') { const p = power(v); return p.num + ' ' + p.unit; }
  const a = Math.abs(v);
  const d = a >= 1000 ? 0 : a >= 100 ? 1 : a >= 1 ? 2 : 3;
  return num(v, d) + (unit ? ' ' + unit : '');
}
function unitOf(name) { return H.available.find((a) => a.name === name)?.unit || ''; }

// ---------- Aufbau ----------
function updateLive() {
  const st = H.getStatus();
  $('hist-live').hidden = !(H.active && isLiveRange() && !H.err && st?.inverter?.state === 'online' && H.data);
}
function build() {
  buildControls();
  updateLive();
  const svg = $('hist-svg'); const note = $('hist-note');
  if (H.mode === 'yields') { buildYields(svg, note); return; }
  $('hist-nav').hidden = true;
  const d = H.data;
  const series = d ? H.fields.filter((f) => d.series?.[f]) : [];
  const hasData = d && series.some((f) => (d.series[f].avg || []).some((v) => v != null));
  if (H.err || !d || !hasData) {
    svg.replaceChildren(); $('hist-res').hidden = true;
    $('hist-legend').replaceChildren();
    $('hist-tip').hidden = true;
    note.hidden = false;
    note.textContent = H.err === 'unsupported' ? t('hist.unsupported') : H.err === 'error' ? t('hist.error') : !d ? t('hist.loading') : t('hist.empty');
    svg.setAttribute('height', 220); svg.setAttribute('viewBox', '0 0 10 220');
    return;
  }
  note.hidden = true;
  draw(d, series);
  buildRes(d);
}

function buildYields(svg, note) {
  const per = yl.periodOf(H.ypd, H.yoff);
  $('hist-nav').hidden = false;
  $('yld-label').textContent = yl.periodLabel(per);
  const old = H.daily ? yl.oldestDate(H.daily) : null;
  $('yld-next').disabled = H.yoff >= 0;
  $('yld-prev').disabled = !old || yl.ymd(per.start) <= old;
  $('yld-csv').disabled = !H.daily;
  $('hist-res').hidden = true;
  if (H.daily === null || H.daily === undefined) {
    svg.replaceChildren(); $('hist-legend').replaceChildren(); $('hist-tip').hidden = true;
    note.hidden = false; note.textContent = H.daily === null ? t('hist.unsupported') : t('hist.loading');
    svg.setAttribute('height', 220); svg.setAttribute('viewBox', '0 0 10 220');
    return;
  }
  const out = yl.render({ svg, box: $('hist-box'), tip: $('hist-tip'), legend: $('hist-legend'), daily: H.daily, period: per, years: cmpYearsFor(per.ref) });
  H.width = Math.round($('hist-box').clientWidth);
  note.hidden = out.hasData;
  if (!out.hasData) {
    note.textContent = t('hist.empty');
    svg.setAttribute('height', 220); svg.setAttribute('viewBox', '0 0 10 220');
  }
}

function buildRes(d) {
  const el = $('hist-res'); const r = d?.resolution_s;
  const key = r === 300 ? 'hist.res_5m' : r === 'mixed' ? 'hist.res_mixed' : null;
  el.hidden = !key; el.textContent = key ? t(key) : '';
}

// Index des Eintrags in der sortierten Zeitliste arr, der tt am naechsten liegt
function nearestIdx(arr, tt) {
  let lo = 0; let hi = arr.length - 1;
  if (hi < 0) return -1;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (arr[mid] < tt) lo = mid + 1; else hi = mid; }
  if (lo > 0 && Math.abs(arr[lo - 1] - tt) < Math.abs(arr[lo] - tt)) lo--;
  return lo;
}
// Teilstuecke zusammenhaengender Werte (Luecken > gapMs werden nicht ueberbrueckt)
function segments(tArr, avg, gapMs) {
  const segs = []; let cur = null; let lastT = null;
  tArr.forEach((tt, i) => {
    if (avg?.[i] == null) return;
    if (!cur || tt - lastT > gapMs) { cur = []; segs.push(cur); }
    cur.push(i); lastT = tt;
  });
  return segs;
}

function draw(d, series) {
  const box = $('hist-box'); const svg = $('hist-svg');
  const W = Math.max(260, Math.round(box.clientWidth || 640)); H.width = W;
  const mobile = W < 520; const Ht = mobile ? 270 : 350;
  const r = H.r || range();
  const { x0, x1 } = r;
  // Einheiten -> Achsen (max. zwei)
  const units = [];
  for (const f of series) { const u = unitOf(f); if (!units.includes(u)) units.push(u); }
  const axes = units.slice(0, MAX_UNITS).map((u) => {
    let lo = Infinity, hi = -Infinity;
    for (const f of series) {
      if (unitOf(f) !== u) continue;
      for (const k of ['min', 'avg', 'max']) for (const v of d.series[f][k] || []) if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    }
    for (const c of H.cmpData) if (unitOf(c.field) === u) for (const v of c.avg) if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    if (!Number.isFinite(lo)) { lo = 0; hi = 1; }
    if (u === 'W' && lo > 0) lo = 0;   // Leistungsachse beginnt bei 0
    if (lo >= 0 && lo < 0.5 * hi) lo = 0; else { const p = (hi - lo || 1) * 0.06; lo -= p; hi += p; }
    const sc = u === 'W' && hi >= 2000 ? 1000 : 1;
    return { unit: u, sc, label: sc === 1000 ? 'kW' : u, ...nice(lo, hi, mobile ? 4 : 5) };
  });
  const axisOf = (f) => Math.max(0, Math.min(axes.length - 1, units.indexOf(unitOf(f))));
  const m = { l: mobile ? 40 : 52, r: axes.length > 1 ? (mobile ? 40 : 52) : 12, t: 22, b: 28 };
  const pw = W - m.l - m.r; const ph = Ht - m.t - m.b;
  const X = (tt) => m.l + ((tt - x0) / (x1 - x0)) * pw;
  const Y = (v, a) => m.t + ph - ((v - axes[a].min) / (axes[a].max - axes[a].min)) * ph;
  svg.setAttribute('viewBox', `0 0 ${W} ${Ht}`); svg.setAttribute('width', W); svg.setAttribute('height', Ht);

  const kids = [];
  axes.forEach((ax, ai) => {
    const st = ax.step / ax.sc;
    const dec = Number.isInteger(Math.round(st * 1e6) / 1e6) ? 0 : Number.isInteger(Math.round(st * 10 * 1e6) / 1e6) ? 1 : 2;
    for (let v = ax.min; v <= ax.max + ax.step / 2; v += ax.step) {
      const y = Y(v, ai);
      if (ai === 0) kids.push(sv('line', { class: 'gl', x1: m.l, x2: W - m.r, y1: y, y2: y }));
      const txt = sv('text', { x: ai === 0 ? m.l - 6 : W - m.r + 6, y: y + 4, 'text-anchor': ai === 0 ? 'end' : 'start' });
      txt.textContent = num(v / ax.sc, dec);
      kids.push(txt);
    }
    const ul = sv('text', { x: ai === 0 ? m.l - 6 : W - m.r + 6, y: 11, 'text-anchor': ai === 0 ? 'end' : 'start' });
    ul.textContent = ax.label; kids.push(ul);
  });
  // Zeitachse (Ortszeit)
  const span = x1 - x0;
  const { ticks, step, months } = timeTicks(x0, x1, pw, mobile ? 62 : 80);
  const multi = x1 - x0 > 36 * 3600e3;
  for (const tt of ticks) {
    const x = X(tt);
    kids.push(sv('line', { class: 'gl v', x1: x, x2: x, y1: m.t, y2: m.t + ph }));
    const dd = new Date(tt); const isMid = dd.getHours() === 0 && dd.getMinutes() === 0;
    const lab = sv('text', { x, y: Ht - 8, 'text-anchor': x < m.l + 14 ? 'start' : x > W - m.r - 14 ? 'end' : 'middle', class: multi && isMid ? 'day' : '' });
    lab.textContent = months ? dfMonth(mobile).format(tt)
      : step >= DAY || (multi && isMid) ? (span > 21 * DAY ? dfDayShort() : dfDay()).format(tt) : dfHM().format(tt);
    kids.push(lab);
  }
  const now = Date.now();
  if (H.preset === 'today' && now < x1) kids.push(sv('line', { class: 'now', x1: X(now), x2: X(now), y1: m.t, y2: m.t + ph }));

  const gapMs = Math.max(GAP_MS, 3 * (H.getStatus()?.poll?.live_interval_s || 10) * 1000, 2.5 * (d.step_s || 0) * 1000);
  // Vorjahreslinien zuerst (hinter der aktuellen Linie): gestrichelt, gedaempft
  H.cpts = [];
  H.cmpData.forEach((c, ci) => {
    const a = axisOf(c.field); const cls = 'yc-' + (ci % 4);
    let line = ''; const dots = [];
    for (const sg of segments(c.t, c.avg, gapMs)) {
      if (sg.length === 1) { dots.push(sg[0]); continue; }
      line += 'M' + sg.map((i) => `${X(c.t[i]).toFixed(1)} ${Y(c.avg[i], a).toFixed(1)}`).join('L');
    }
    const g = sv('g', { class: 'cmp ' + cls });
    if (line) g.append(sv('path', { class: 'cl', d: line }));
    for (const i of dots) g.append(sv('circle', { class: 'dot', cx: X(c.t[i]), cy: Y(c.avg[i], a), r: 2 }));
    kids.push(g);
    H.cpts.push({ c, a, cls });
  });
  // Serien: Linie = avg, Flaeche = min..max; Luecken nicht ueberbruecken
  H.pts = [];
  for (const f of series) {
    const s = d.series[f]; const a = axisOf(f);
    const cls = 'hs-' + (H.fields.indexOf(f) % 6);
    const segs = segments(d.t, s.avg, gapMs);
    let line = '', band = ''; const dots = [];
    const P = (i, arr) => `${X(d.t[i]).toFixed(1)} ${Y(arr?.[i] ?? s.avg[i], a).toFixed(1)}`;
    for (const sg of segs) {
      if (sg.length === 1) { dots.push(sg[0]); continue; }
      line += 'M' + sg.map((i) => P(i, s.avg)).join('L');
      band += 'M' + sg.map((i) => P(i, s.max)).join('L') + 'L' + sg.slice().reverse().map((i) => P(i, s.min)).join('L') + 'Z';
    }
    const g = sv('g', { class: cls });
    if (band) g.append(sv('path', { class: 'bd', d: band }));
    if (line) g.append(sv('path', { class: 'ln', d: line }));
    for (const i of dots) g.append(sv('circle', { class: 'dot', cx: X(d.t[i]), cy: Y(s.avg[i], a), r: 2.4 }));
    kids.push(g);
    H.pts.push({ f, a, cls, s });
  }

  // Fadenkreuz + Tooltip (Maus und Touch)
  const hov = sv('g', { id: 'hist-hov', visibility: 'hidden' },
    sv('line', { class: 'cross', y1: m.t, y2: m.t + ph }),
    ...H.pts.map((p) => sv('circle', { class: 'hd ' + p.cls, r: 4.5 })),
    ...H.cpts.map((p) => sv('circle', { class: 'hd cmp ' + p.cls, r: 4 })));
  const ov = sv('rect', { class: 'ov', x: m.l, y: m.t, width: pw, height: ph });
  svg.replaceChildren(...kids, hov, ov);
  H.geo = { m, pw, ph, W, Ht, x0, x1, X, Y };
  H.vt = d.t.filter((_, i) => H.pts.some((p) => p.s.avg?.[i] != null));
  const mv = (ev) => hover(ev, d);
  ov.addEventListener('pointermove', mv);
  ov.addEventListener('pointerdown', mv);
  ov.addEventListener('pointerleave', (ev) => { if (ev.pointerType === 'mouse') hideHover(); });
  if (H.hover != null) showAt(H.hover, d);
  buildLegend(d, series, r);
}

function hover(ev, d) {
  const rect = $('hist-svg').getBoundingClientRect();
  const px = ((ev.clientX - rect.left) / rect.width) * H.geo.W;
  const tt = H.geo.x0 + ((px - H.geo.m.l) / H.geo.pw) * (H.geo.x1 - H.geo.x0);
  let best = null;
  for (const arr of [H.vt, ...H.cmpData.map((c) => c.vt)]) {
    const i = nearestIdx(arr, tt); if (i < 0) continue;
    const dist = Math.abs(arr[i] - tt);
    if (!best || dist < best.dist) best = { t: arr[i], dist };
  }
  if (!best) return;
  H.hover = best.t;
  showAt(H.hover, d);
}
function showAt(tms, d) {
  const hov = $('hist-hov');
  if (!hov || !H.geo) { hideHover(); return; }
  const { X, Y, W } = H.geo; const x = X(tms);
  const tol = Math.max((d.step_s || 0) * 600, 1);
  const at = (tArr, arr) => { const i = nearestIdx(tArr, tms); return i >= 0 && Math.abs(tArr[i] - tms) <= tol ? { i, v: arr?.[i] } : null; };
  hov.setAttribute('visibility', 'visible');
  const cross = hov.querySelector('.cross'); cross.setAttribute('x1', x); cross.setAttribute('x2', x);
  const circles = hov.querySelectorAll('circle');
  const rows = [h('strong', {}, tipFmt(d.step_s || 0).format(tms))];
  H.pts.forEach((p, k) => {
    const hit = at(d.t, p.s.avg); const av = hit?.v ?? null; const c = circles[k];
    if (av == null) c.setAttribute('visibility', 'hidden');
    else { c.setAttribute('visibility', 'visible'); c.setAttribute('cx', x); c.setAttribute('cy', Y(av, p.a)); }
    const u = unitOf(p.f); const mn = hit ? p.s.min?.[hit.i] : null; const mx = hit ? p.s.max?.[hit.i] : null;
    const spread = av != null && mn != null && mx != null && Math.abs(mx - mn) > 1e-9 * (Math.abs(av) + 1) && (d.step_s || 0) > 15;
    rows.push(h('div', { class: 'row ' + p.cls }, h('i'), h('span', {}, fieldName(p.f)), h('b', {}, fmtVal(av, u)),
      spread ? h('small', {}, `${fmtVal(mn, '')}–${fmtVal(mx, '')}`) : null));
  });
  H.cpts.forEach((p, k) => {
    const hit = at(p.c.t, p.c.avg); const av = hit?.v ?? null; const c = circles[H.pts.length + k];
    if (av == null) c.setAttribute('visibility', 'hidden');
    else { c.setAttribute('visibility', 'visible'); c.setAttribute('cx', x); c.setAttribute('cy', Y(av, p.a)); }
    rows.push(h('div', { class: 'row cmp ' + p.cls }, h('i'), h('span', {}, String(p.c.year)), h('b', {}, fmtVal(av, unitOf(p.c.field)))));
  });
  const tip = $('hist-tip');
  tip.replaceChildren(...rows); tip.hidden = false;
  const right = x > W * 0.55;
  tip.style.left = right ? 'auto' : Math.min(x + 12, W - 150) + 'px';
  tip.style.right = right ? (W - x + 12) + 'px' : 'auto';
}
function hideHover() {
  H.hover = null;
  const hov = $('hist-hov'); if (hov) hov.setAttribute('visibility', 'hidden');
  $('hist-tip').hidden = true;
}

const fmtSum = (v) => (v == null ? '–' : num(v, Math.abs(v) >= 1000 ? 0 : 1) + ' kWh');
function buildLegend(d, series, r) {
  const items = series.map((f) => {
    const arr = d.series[f].avg || []; let last = null;
    for (let i = arr.length - 1; i >= 0; i--) if (arr[i] != null) { last = arr[i]; break; }
    return h('li', { class: 'hs-' + (H.fields.indexOf(f) % 6) }, h('i'), h('span', {}, fieldName(f)), h('b', {}, fmtVal(last, unitOf(f))));
  });
  if (H.cpts.length) {   // Jahre mit kWh-Summe: aktuelles Jahr (durchgezogen) und Vorjahre (gestrichelt)
    const cf = H.cpts[0].c.field; const multi = H.fields.length > 1;
    const sub = multi ? h('small', {}, fieldName(cf)) : null;
    items.push(h('li', { class: 'yr hs-' + (H.fields.indexOf(cf) % 6), title: fieldName(cf) }, h('i'), h('span', {}, String(refYearOf(r))), h('b', {}, fmtSum(periodSum(r.x0, r.x1, 0))), sub));
    H.cpts.forEach((p) => items.push(h('li', { class: 'yr cmp ' + p.cls, title: fieldName(cf) }, h('i'), h('span', {}, String(p.c.year)), h('b', {}, fmtSum(p.c.sum)))));
  }
  $('hist-legend').replaceChildren(...items);
}

// ---------- Bedienelemente ----------
function csvUrl() {
  const r = range();
  return `/api/export/samples.csv?from=${encodeURIComponent(new Date(r.from).toISOString())}&to=${encodeURIComponent(new Date(r.to).toISOString())}`
    + `&fields=${encodeURIComponent(H.fields.join(','))}&sep=${csvSep()}`;
}
function buildControls() {
  const list = presets();
  if (!list.includes(H.preset)) H.preset = 'today';
  const ref = H.mode === 'yields' ? yl.periodOf(H.ypd, H.yoff).ref : refYearOf(range());
  const opts = cmpOptions(ref);
  const sig = [lang, H.mode, H.ypd, list.join(), H.preset, H.fields.join(), H.available.map((a) => a.name).join(), H.getStatus()?.history?.oldest, opts.join(), H.cmp].join('|');
  const yieldsMode = H.mode === 'yields';
  $('hist-presets').hidden = yieldsMode; $('yld-periods').hidden = !yieldsMode;
  $('hist-fields').hidden = yieldsMode; $('hist-csv').hidden = yieldsMode;
  $('hist-cmp-wrap').hidden = opts.length < 2 || (!yieldsMode && !lineCmpOk(range()));
  $('hist-band').textContent = t(yieldsMode ? 'hist.yld_note' : 'hist.band');
  if (!yieldsMode) { const r = range(); $('hist-csv').disabled = r.to - r.from > CSV_MAX_DAYS * DAY; $('hist-csv').title = $('hist-csv').disabled ? t('hist.csv_range', { n: CSV_MAX_DAYS }) : ''; }
  if (sig === H.builtSig) return;
  H.builtSig = sig;
  $('hist-mode').replaceChildren(...['line', 'yields'].map((m) => h('button', { type: 'button', 'aria-pressed': m === H.mode ? 'true' : 'false', onclick: () => setMode(m) }, t(MODE_KEY[m]))));
  $('hist-presets').replaceChildren(...list.map((p) => h('button', { type: 'button', 'aria-pressed': p === H.preset ? 'true' : 'false', onclick: () => setPreset(p) }, presetLabel(p))));
  $('yld-periods').replaceChildren(...yl.KINDS.map((k) => h('button', { type: 'button', 'aria-pressed': k === H.ypd ? 'true' : 'false', onclick: () => setKind(k) }, t(KIND_KEY[k]))));
  const eff = opts.includes(H.cmp) ? H.cmp : (H.cmp === 'all' && opts.includes('prev') ? 'prev' : 'off');
  $('hist-cmp').replaceChildren(...opts.map((o) => h('option', { value: o, selected: o === eff }, t(CMP_KEY[o]))));
  $('hist-fcount').textContent = `(${H.fields.length})`;
  buildRange();
  const groups = new Map();
  for (const a of H.available) { if (!groups.has(a.group)) groups.set(a.group, []); groups.get(a.group).push(a); }
  const body = [];
  for (const [g, items] of groups) {
    body.push(h('div', {}, h('h3', {}, tryT('group.' + g, humanize(g))),
      items.map((a) => h('label', {}, h('input', { type: 'checkbox', checked: H.fields.includes(a.name), onchange: (e) => toggle(a.name, e.target) }),
        h('span', {}, fieldName(a.name), a.unit ? h('small', {}, ' · ' + a.unit) : null)))));
  }
  body.push(h('p', { class: 'help muted' }, t('hist.fields_help', { max: MAX_FIELDS })));
  $('hist-fbody').replaceChildren(...body);
}
function buildRange() {
  const box = $('hist-range'); box.hidden = H.preset !== 'range' || H.mode !== 'line';
  if (box.hidden) return;
  const e = parseYmd(H.range?.to); const f0 = parseYmd(H.range?.from);
  const o = Date.parse(H.getStatus()?.history?.oldest || '');
  const f = $('hist-from'); const to = $('hist-to');
  const now = Date.now();
  f.value = ymd(f0 ?? midnight(now - 30 * DAY)); to.value = ymd(e ?? midnight(now));
  f.max = to.max = ymd(now);
  if (Number.isFinite(o)) f.min = to.min = ymd(o);
}
function applyRange() {
  const from = $('hist-from').value; const to = $('hist-to').value;
  if (!parseYmd(from) || !parseYmd(to)) return;
  H.range = { from, to }; localStorage.setItem('h2m-hist-range', JSON.stringify(H.range)); H.hover = null;
  buildRange(); reload();
}
function setPreset(p) {
  H.preset = p; localStorage.setItem('h2m-hist-preset', p); H.hover = null; H.cmpData = [];
  buildControls(); reload();
}
function setMode(m) {
  H.mode = m; localStorage.setItem('h2m-hist-mode', m); H.hover = null; H.cmpData = []; H.builtSig = '';
  build(); reload();
}
function setKind(k) {
  H.ypd = k; localStorage.setItem('h2m-yld-kind', k); H.yoff = 0; H.hover = null; yl.hide($('hist-tip'), $('hist-svg')); H.builtSig = '';
  build();
}
function setCmp(v) {
  H.cmp = v; localStorage.setItem('h2m-hist-cmp', v); H.hover = null; H.cmpData = []; H.builtSig = '';
  yl.hide($('hist-tip'), $('hist-svg'));
  if (H.mode === 'yields') build(); else { build(); reload(); }
}
function stepPeriod(dir) {
  H.yoff = Math.min(0, H.yoff + dir); yl.hide($('hist-tip'), $('hist-svg')); build();
}
function toggle(name, box) {
  let f = H.fields.slice();
  if (box.checked) {
    f.push(name);
    if (f.length > MAX_FIELDS) { box.checked = false; toast(t('hist.too_many', { max: MAX_FIELDS }), 'warn'); return; }
    if (new Set(f.map(unitOf)).size > MAX_UNITS) { box.checked = false; toast(t('hist.too_units'), 'warn'); return; }
  } else {
    f = f.filter((n) => n !== name);
    if (!f.length) { box.checked = true; toast(t('hist.keep_one'), 'warn'); return; }
  }
  H.fields = f; localStorage.setItem('h2m-hist-fields', JSON.stringify(f));
  $('hist-fcount').textContent = `(${f.length})`;
  H.builtSig = '';
  buildControls();
  reload();
}

// ---------- Schnittstelle fuer app.js ----------
const hasLive = () => !!H.getStatus()?.poll?.last_live_at;
export function init(getStatus) {
  H.getStatus = getStatus;
  $('hist-from').addEventListener('change', applyRange);
  $('hist-to').addEventListener('change', applyRange);
  $('hist-cmp').addEventListener('change', (e) => setCmp(e.target.value));
  $('hist-csv').addEventListener('click', () => download(csvUrl()));
  $('yld-prev').addEventListener('click', () => stepPeriod(-1));
  $('yld-next').addEventListener('click', () => stepPeriod(1));
  $('yld-csv').addEventListener('click', () => {
    const p = yl.periodOf(H.ypd, H.yoff);
    download(`/api/export/daily.csv?from=${yl.ymd(p.start)}&to=${yl.ymd(p.end)}&sep=${csvSep()}`);
  });
  new ResizeObserver(() => {
    if (!H.active) return;
    if (H.mode === 'line' && !H.data) return;
    const w = Math.round($('hist-box').clientWidth);
    if (w && w !== H.width) { cancelAnimationFrame(H.fb); H.fb = requestAnimationFrame(() => build()); }
  }).observe($('hist-box'));
}
export function activate() {
  H.active = true; H.builtSig = ''; H.yoff = 0;
  build();
  reload();
  clearInterval(H.timer);
  H.timer = setInterval(() => { if (!document.hidden && !hasLive() && isLiveRange()) reload(); }, 10000); // Fallback ohne poll-Info
}
export function deactivate() { H.active = false; clearInterval(H.timer); H.seq++; hideHover(); }
// Pro Poll der Bridge nachladen
export function onStatus(st) {
  if (!H.active) return;
  const ll = st?.poll?.last_live_at;
  if (ll && ll !== H.lastLive) { const first = H.lastLive == null; H.lastLive = ll; if (!first && isLiveRange()) reload(); }
  buildControls();
  updateLive();
}
export function relang() { H.builtSig = ''; if (H.active) build(); }
