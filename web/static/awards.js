// Seite "Awards": Bestleistungen, Meilensteine, CO2 und Ertrag in Geld (Daten: GET /api/awards).
import { $, h, icon, api, fmt, ymd } from './common.js';
import { t, lang, num, power, hm } from './i18n.js';

const REFRESH_MS = 60000;
let active = false, timer = null, seq = 0, data = null, err = null;

const ICONS = { best_day: 'i-sun', best_week: 'i-calendar', best_month: 'i-calendar', best_year: 'i-calendar',
  peak_power: 'i-bolt', longest_day: 'i-clock', earliest_start: 'i-flag' };
const ORDER = ['best_day', 'best_week', 'best_month', 'best_year', 'peak_power', 'longest_day', 'earliest_start'];

// ---------- Formatierer ----------
function periodLabel(kind, p) {
  const s = String(p ?? '');
  let m;
  if (kind === 'best_week' && (m = /^(\d{4})-W(\d{1,2})$/.exec(s))) return t('aw.week', { w: +m[2], y: m[1] });
  if (kind === 'best_month' && (m = /^(\d{4})-(\d{2})$/.exec(s))) {
    return new Intl.DateTimeFormat(lang, { month: 'long', year: 'numeric' }).format(new Date(+m[1], +m[2] - 1, 1));
  }
  return s;
}
function dateLabel(s) { const d = ymd(s); return d ? fmt.date(d) : String(s ?? ''); }
function duration(sec) {
  const m = Math.round(Number(sec) / 60);
  return t('aw.hm', { h: Math.floor(m / 60), m: String(m % 60).padStart(2, '0') });
}
function entry(kind, e) {
  switch (kind) {
    case 'best_day': return { val: num(e.value, 1) + ' kWh', when: dateLabel(e.date) };
    case 'best_week': case 'best_month': case 'best_year':
      return { val: num(e.value, 1) + ' kWh', when: periodLabel(kind, e.period) };
    case 'peak_power': {
      const p = power(e.value); const tm = e.ts ? hm(e.ts) : null;
      return { val: p.num + ' ' + p.unit, when: dateLabel(e.date) + (tm ? ' · ' + tm : '') };
    }
    case 'longest_day': {
      const a = e.start ? hm(e.start) : null; const b = e.end ? hm(e.end) : null;
      return { val: duration(e.value), when: dateLabel(e.date) + (a && b ? ` · ${a}–${b}` : '') };
    }
    case 'earliest_start': return { val: String(e.value), when: dateLabel(e.date) };
    default: return { val: String(e.value), when: '' };
  }
}

function money(v, cur) {
  try { return new Intl.NumberFormat(lang, { style: 'currency', currency: cur || 'EUR' }).format(v); } catch { return num(v, 2) + ' ' + (cur || ''); }
}
function co2(kg) { return kg >= 1000 ? num(kg / 1000, 2) + ' t' : num(kg, 1) + ' kg'; }

// ---------- Rendering ----------
function medal(rank) { return h('span', { class: 'medal m' + rank, title: t('aw.place', { n: rank }), 'aria-label': t('aw.place', { n: rank }) }, String(rank)); }

function rankCard(kind, list) {
  const body = list?.length
    ? h('ol', { class: 'podium' }, list.slice(0, 3).map((e) => {
      const x = entry(kind, e);
      return h('li', { class: 'place p' + e.rank }, medal(e.rank), h('div', { class: 'txt' }, h('span', { class: 'val' }, x.val), h('span', { class: 'when' }, x.when)));
    }))
    : h('p', { class: 'aw-empty' }, t('aw.empty'));
  return h('article', { class: 'card' }, h('h2', {}, icon(ICONS[kind]), t('aw.rank.' + kind, { year: new Date().getFullYear() })), body);
}

function milestones(ms) {
  if (!ms) return null;
  const reached = (ms.reached || []).slice().sort((a, b) => b.kwh - a.kwh);
  const li = (r) => h('li', {}, h('span', { class: 'badge' }, icon('i-check')),
    h('div', { class: 'txt' }, h('strong', {}, num(r.kwh, 0) + ' kWh'),
      h('span', {}, r.ts ? fmt.date(new Date(r.ts)) : t('aw.ms_before'))));
  const parts = [];
  if (ms.next) {
    const step = ms.step_kwh || 1000;
    const pct = Math.max(2, Math.min(100, ((step - ms.next.remaining_kwh) / step) * 100));
    parts.push(h('div', { class: 'ms-next' },
      h('div', { class: 'big-line' }, h('span', { class: 'muted' }, t('aw.ms_next', { kwh: num(ms.next.kwh, 0) })),
        h('strong', {}, t('aw.ms_remaining', { kwh: num(ms.next.remaining_kwh, 1) }))),
      h('div', { class: 'ms-bar', role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': Math.round(pct) }, h('i', { style: `width:${pct.toFixed(1)}%` }))));
  }
  if (reached.length) {
    parts.push(h('ul', { class: 'ms-list' }, reached.slice(0, 6).map(li)));
    if (reached.length > 6) parts.push(h('details', { class: 'ms-more' }, h('summary', {}, t('aw.ms_more', { n: reached.length - 6 })), h('ul', { class: 'ms-list' }, reached.slice(6).map(li))));
  } else parts.push(h('p', { class: 'aw-empty' }, t('aw.ms_none')));
  return h('article', { class: 'card ms-card' }, h('h2', {}, icon('i-flag'), t('aw.milestones'), h('span', { class: 'muted', style: 'font-weight:400;font-size:.85rem' }, t('aw.ms_step', { step: num(ms.step_kwh || 0, 0) }))), parts);
}

function tiles(rows) {
  return h('div', { class: 'stat-tiles' }, rows.map(([label, val, main]) => h('div', { class: 'tile' + (main ? ' main' : '') }, h('span', { class: 'label' }, label), h('span', { class: 'v' }, val))));
}

function render() {
  const body = $('aw-body'); const note = $('aw-note'); const since = $('aw-since');
  note.hidden = true;
  if (err || !data) {
    body.replaceChildren();
    note.hidden = false;
    note.textContent = err === 'unsupported' ? t('aw.unsupported') : err === 'error' ? t('aw.error') : t('aw.loading');
    return;
  }
  const d = ymd(data.tracking_since);
  since.textContent = d ? t('aw.since', { date: fmt.date(d) }) : t('aw.intro');
  const rk = data.rankings || {};
  const anyRank = ORDER.some((k) => rk[k]?.length);
  const kids = [];
  if (!anyRank) kids.push(h('p', { class: 'notice' }, t('aw.empty')));
  kids.push(h('div', { class: 'aw-grid' }, ORDER.map((k) => rankCard(k, rk[k]))));
  const ms = milestones(data.milestones); if (ms) kids.push(ms);

  const grid = [];
  if (data.co2) {
    const c = data.co2;
    grid.push(h('article', { class: 'card' }, h('h2', {}, icon('i-leaf'), t('aw.co2')),
      tiles([[t('aw.today'), co2(c.today_kg ?? 0)], [t('aw.year'), co2(c.year_kg ?? 0)], [t('aw.total'), co2(c.total_kg ?? 0), true]]),
      h('p', { class: 'note' }, t('aw.co2_note', { factor: num(c.factor_kg_kwh, 2) }))));
  }
  if (data.money) {
    const m = data.money; const cur = m.currency;
    grid.push(h('article', { class: 'card' }, h('h2', {}, icon('i-coin'), t('aw.money')),
      tiles([[t('aw.today'), money(m.today ?? 0, cur)], [t('aw.month'), money(m.month ?? 0, cur)], [t('aw.year'), money(m.year ?? 0, cur)], [t('aw.total'), money(m.total ?? 0, cur), true]]),
      h('p', { class: 'note' }, t('aw.money_note', { tariff: num(m.tariff_per_kwh, 3), currency: cur || '' }))));
  }
  if (grid.length) kids.push(h('div', { class: 'aw-grid' }, grid));
  body.replaceChildren(...kids);
}

async function load() {
  if (!active) return;
  const my = ++seq;
  const r = await api('GET', '/api/awards');
  if (my !== seq) return;
  if (r.status === 404 || r.status === 501) err = 'unsupported';
  else if (!r.ok || !r.data) err = 'error';
  else { err = null; data = r.data; }
  render();
}

export function activate() { active = true; render(); load(); clearInterval(timer); timer = setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS); }
export function deactivate() { active = false; seq++; clearInterval(timer); }
export function relang() { if (active) render(); }
