// Gemeinsame Helfer fuer app.js, history.js und awards.js (kein Framework).
import { t, has, lang } from './i18n.js';

export const $ = (id) => document.getElementById(id);

export function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v == null) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const k of kids.flat()) if (k != null) el.append(k);
  return el;
}

const NS = 'http://www.w3.org/2000/svg';
export function sv(tag, attrs = {}, ...kids) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null && v !== false) el.setAttribute(k, v);
  for (const k of kids.flat()) if (k != null) el.append(k);
  return el;
}

export function icon(id) {
  const svg = sv('svg', { class: 'ico' });
  svg.append(sv('use', { href: '#' + id }));
  return svg;
}

// Liefert zusaetzlich den HTTP-Date-Header (Uhrenabgleich fuer den Countdown).
export async function api(method, path, body) {
  let r;
  try {
    r = await fetch(path, {
      method, credentials: 'same-origin', cache: 'no-store',
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch { return { ok: false, status: 0, data: null, date: null }; }
  let data = null;
  try { data = await r.json(); } catch { /* leerer Body */ }
  return { ok: r.ok, status: r.status, data, date: r.headers.get('Date') };
}

export const tryT = (key, fallback) => (has(key) ? t(key) : fallback);
export const humanize = (k) => String(k).replace(/_/g, ' ');
// registers[].name / available[].name ist der Feld-Key; Klartext kommt aus i18n (field.<key>).
export const fieldName = (key) => tryT('field.' + key, humanize(key));

export function toast(msg, tone = 'ok') {
  const el = h('div', { class: 'toast tone-' + tone }, icon(tone === 'ok' ? 'i-check' : 'i-alert'), h('span', {}, msg));
  el.addEventListener('click', () => el.remove());
  $('toasts').append(el);
  setTimeout(() => el.remove(), tone === 'ok' ? 5000 : 9000);
}

// Lokale Datums-/Zeitformate (Ortszeit des Browsers)
export const fmt = {
  hm: (d) => new Intl.DateTimeFormat(lang, { hour: '2-digit', minute: '2-digit' }).format(d),
  date: (d) => new Intl.DateTimeFormat(lang, { day: 'numeric', month: 'long', year: 'numeric' }).format(d),
  dateShort: (d) => new Intl.DateTimeFormat(lang, { day: 'numeric', month: 'numeric', year: 'numeric' }).format(d),
};
// 'YYYY-MM-DD' als lokales Datum (nicht als UTC)
export function ymd(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || ''));
  return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
}

// "Schoene" Achsenskala: Schrittweite 1/2/2,5/5 x 10^n
export function nice(lo, hi, n = 4) {
  let top = hi;
  if (!(top > lo)) top = lo + 1;
  const raw = (top - lo) / n; const mag = 10 ** Math.floor(Math.log10(raw)); const f = raw / mag;
  const step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * mag;
  return { min: Math.floor(lo / step + 1e-9) * step, max: Math.ceil(top / step - 1e-9) * step, step };
}

// CSV-Download ueber einen temporaeren Link (Trenner/Dezimalkomma: Semikolon ausser in Englisch)
export const csvSep = () => (lang === 'en' ? 'comma' : 'semicolon');
export function download(url) {
  const a = h('a', { href: url, download: '' });
  document.body.append(a); a.click(); a.remove();
}
