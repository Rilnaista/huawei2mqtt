// Mini-i18n: Sprachdateien laden, Texte einsetzen, Zahlen/Datum per Intl formatieren.
export const LANGS = ['de', 'en', 'nl', 'fr', 'it', 'es', 'pl'];
const FALLBACK = 'en';
let dict = {};
let fallback = {};
export let lang = FALLBACK;

export function detect() {
  const saved = localStorage.getItem('lang');
  if (LANGS.includes(saved)) return saved;
  for (const l of navigator.languages || [navigator.language]) {
    const c = (l || '').slice(0, 2).toLowerCase();
    if (LANGS.includes(c)) return c;
  }
  return FALLBACK;
}

async function fetchDict(l) {
  // Das Backend darf web/i18n unter /i18n oder /static/i18n ausliefern.
  for (const base of ['/i18n/', '/static/i18n/']) {
    try {
      const r = await fetch(base + l + '.json', { cache: 'no-cache' });
      if (r.ok) return await r.json();
    } catch { /* naechster Versuch */ }
  }
  return {};
}

export async function setLang(l) {
  lang = LANGS.includes(l) ? l : FALLBACK;
  localStorage.setItem('lang', lang);
  if (!Object.keys(fallback).length) fallback = await fetchDict(FALLBACK);
  dict = lang === FALLBACK ? fallback : await fetchDict(lang);
  document.documentElement.lang = lang;
  nf.clear();
}

export function has(key) { return key in dict || key in fallback; }

export function t(key, params) {
  let s = dict[key] ?? fallback[key] ?? key;
  if (params) for (const [k, v] of Object.entries(params)) s = s.replaceAll('{' + k + '}', v);
  return s;
}

// Statische Texte im DOM ersetzen: data-i18n, data-i18n-<attr>, optional data-n als {n}.
export function applyDom(root = document) {
  root.querySelectorAll('[data-i18n]').forEach((el) => {
    el.textContent = t(el.dataset.i18n, el.dataset.n ? { n: el.dataset.n } : undefined);
  });
  for (const attr of ['aria-label', 'placeholder', 'title']) {
    root.querySelectorAll(`[data-i18n-${attr}]`).forEach((el) => {
      el.setAttribute(attr, t(el.getAttribute(`data-i18n-${attr}`)));
    });
  }
  document.title = 'huawei2mqtt · ' + t('app.title');
}

// ---- Formatierung ----
const nf = new Map();
export function num(v, digits = 0) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '–';
  let f = nf.get(digits);
  if (!f) {
    f = new Intl.NumberFormat(lang, { minimumFractionDigits: digits, maximumFractionDigits: digits });
    nf.set(digits, f);
  }
  return f.format(Number(v));
}

// Leistung: unter 1000 W in W, sonst in kW.
export function power(w) {
  if (w === null || w === undefined || Number.isNaN(Number(w))) return { num: '–', unit: '' };
  const v = Number(w);
  return Math.abs(v) >= 1000 ? { num: num(v / 1000, 2), unit: 'kW' } : { num: num(v, 0), unit: 'W' };
}

export function ago(seconds) {
  const rtf = new Intl.RelativeTimeFormat(lang, { numeric: 'auto', style: 'long' });
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return rtf.format(-s, 'second');
  if (s < 3600) return rtf.format(-Math.floor(s / 60), 'minute');
  if (s < 86400) return rtf.format(-Math.floor(s / 3600), 'hour');
  return rtf.format(-Math.floor(s / 86400), 'day');
}

export function hm(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return new Intl.DateTimeFormat(lang, { hour: '2-digit', minute: '2-digit' }).format(d);
}

export function time(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso ?? '');
  return new Intl.DateTimeFormat(lang, { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(d);
}
