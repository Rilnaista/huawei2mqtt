// huawei2mqtt Portal: schlankes Frontend ohne Framework. Alle Texte stammen aus web/i18n/*.json.
import { detect, setLang, lang, t, has, applyDom, num, power, ago, time, hm } from './i18n.js';
import { $, h, icon, api, tryT, humanize, fieldName, toast, fmt } from './common.js';
import * as hist from './history.js';
import * as awards from './awards.js';

const POLL_MS = 5000;
const LOG_MS = 2000;
const LOG_MAX = 500;
const LEVELS = { DEBUG: 10, INFO: 20, WARNING: 30, WARN: 30, ERROR: 40, CRITICAL: 50 };

const S = {
  status: null, values: null, settings: null, down: false, clockOffset: 0, polling: false, fastAt: 0,
  logSeq: 0, logEntries: [], logStarted: null, ctlSig: null, setSig: null,
};

const loggedIn = () => !!S.status?.auth?.logged_in;
const passwordSet = () => !!S.status?.auth?.password_set;

function reasonText(r) {
  if (!r) return t('reason.none');
  return tryT('reason.' + r, String(r));
}

// ---------- Polling ----------
let pollTimer = null;
async function poll() {
  if (S.polling) return;
  S.polling = true;
  try {
    const [a, b] = await Promise.all([api('GET', '/api/status'), api('GET', '/api/values')]);
    if (a.ok && a.data) {
      S.status = a.data;
      // Uhrenabweichung Browser/Server aus dem HTTP-Date-Header (Sekundenaufloesung -> +0,5 s), geglaettet
      const sd = a.date ? Date.parse(a.date) : NaN;
      if (!Number.isNaN(sd)) {
        const off = sd + 500 - Date.now();
        S.clockOffset = S.clockOffsetSet ? S.clockOffset * 0.6 + off * 0.4 : off;
        S.clockOffsetSet = true;
      }
    }
    if (b.ok && b.data) S.values = b.data;
    S.down = !(a.ok && b.ok);
    renderAll();
    hist.onStatus(S.status);
  } finally { S.polling = false; }
}
function startPolling() {
  stopPolling();
  pollTimer = setInterval(poll, POLL_MS);
}
function stopPolling() { clearInterval(pollTimer); pollTimer = null; }

// ---------- Rendering: Uebersicht ----------
function lastTs() {
  return S.status?.inverter?.last_success || S.values?.grid?.ts || S.values?.energy?.ts || null;
}
function ageSeconds() {
  const ts = lastTs();
  if (!ts) return null;
  const ms = Date.parse(ts);
  return Number.isNaN(ms) ? null : Math.max(0, (Date.now() - ms) / 1000);
}
function statusText(key) {
  if (!key) return t('status.none');
  const alias = { initial_standby: 'standby_initializing', detecting_irradiation: 'standby_detecting_irradiation' };
  const k = alias[key] || key;
  if (has('status.' + k)) return t('status.' + k);
  if (k.startsWith('unknown')) return t('status.unknown', { code: k.replace(/^unknown_?/, '') || '?' });
  return t('status.unknown', { code: humanize(k) });
}
function statusTone(key) {
  if (!key) return 'idle';
  if (key.startsWith('on_grid')) return 'ok';
  if (key.startsWith('standby') || key === 'initial_standby') return 'idle';
  if (key === 'shutdown_fault') return 'bad';
  return 'warn';
}

const lost = () => { const i = S.status?.inverter?.state; return i === 'stale' || i === 'offline'; };

function bindValues(root) {
  const v = S.values || {};
  const gone = lost();
  root.querySelectorAll('[data-bind]').forEach((el) => {
    const [g, f] = el.dataset.bind.split('.');
    // Bei Verbindungsverlust keine alten Momentanwerte zeigen (irrefuehrend); Energiezaehler bleiben
    const val = gone && (g === 'pv' || g === 'grid' || g === 'inverter') ? null : v[g]?.[f];
    if (val === null || val === undefined) { el.textContent = '–'; return; }
    if (el.dataset.fmt === 'kpipower') {
      const p = power(val);
      el.replaceChildren(p.num + ' ', h('small', { class: 'u' }, p.unit));
    } else if (el.dataset.fmt === 'power') { const p = power(val); el.textContent = p.num + ' ' + p.unit; }
    else if (el.dataset.fmt === 'text') el.textContent = String(val);
    else el.textContent = num(val, Number(el.dataset.digits ?? 0)) + (el.dataset.unit ? ' ' + el.dataset.unit : '');
  });
}

// "HH:MM", bei aelteren Zeitpunkten (nicht heute) mit Datum
function sinceLabel(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '–';
  const today = new Date();
  const same = d.toDateString() === today.toDateString();
  return same ? fmt.hm(d) : fmt.dateShort(d) + ', ' + fmt.hm(d);
}

function renderBanner() {
  const b = $('banner');
  const inv = S.status?.inverter;
  let main = null, sub = null, tone = 'warn';
  if (S.down) { main = t('banner.portal_down'); tone = 'bad'; }
  else if (lost()) {
    main = inv.last_success ? t('conn.lost_since', { time: sinceLabel(inv.last_success) }) : t('conn.never');
    sub = t('conn.lost_help');
    tone = inv.state === 'offline' ? 'bad' : 'warn';
  } else if (S.status && !S.values?.grid) { main = t('banner.no_data'); tone = 'idle'; }
  b.hidden = !main;
  if (main) { b.replaceChildren(main, sub ? h('small', {}, sub) : null); b.className = 'banner tone-' + tone; }
  $('main').classList.toggle('is-stale', (lost() || S.down) && !!S.values);
}

// Countdown bis zur naechsten Abfrage (poll.next_live_at, mit Uhrenabgleich), sonst "Aktualisiert vor ..."
function renderAge() {
  const el = $('age');
  const p = S.status?.poll;
  const next = p?.next_live_at ? Date.parse(p.next_live_at) : NaN;
  if (S.down) { el.textContent = ''; return; }
  if (!Number.isNaN(next)) {
    const s = Math.ceil((next - (Date.now() + S.clockOffset)) / 1000);
    if (s <= 0) {
      el.textContent = t('ov.updating');
      // Neuen Wert zeitnah holen, ohne auf den 5-s-Takt zu warten
      if (Date.now() - S.fastAt > 2000) { S.fastAt = Date.now(); setTimeout(poll, 1200); }
      return;
    }
    el.textContent = t(lost() ? 'ov.next_retry' : 'ov.next_update', { s: num(s) });
    return;
  }
  const age = ageSeconds();
  el.textContent = age == null ? t('ov.no_update') : t('ov.updated', { age: ago(age) });
}

function lightsData() {
  const st = S.status || {};
  const toneOf = { online: 'ok', stale: 'warn', offline: 'bad', reachable: 'ok', unreachable: 'bad',
    connected: 'ok', disconnected: 'bad', verified: 'ok', unverified: 'idle', denied: 'bad' };
  const mk = (name, ic, state) => {
    const key = `light.${name}.${state}`;
    return { ic, title: t('light.' + name), text: state && has(key) ? t(key) : t('light.unknown'),
      tone: state && has(key) ? toneOf[state] || 'idle' : 'idle' };
  };
  const out = [
    mk('inverter', 'i-sun', st.inverter?.state),
    mk('dongle', 'i-plug', st.dongle?.state),
    mk('mqtt', 'i-cloud', st.mqtt?.state),
    mk('write', 'i-pen', st.write_access),
  ];
  // Kurze Aussetzer in 24 h: 1-2 dezenter Hinweis, ab 3 gelb statt gruen
  const n = Number(st.inverter?.dropouts_24h) || 0;
  if (n > 0) {
    out[0].hint = t(n === 1 ? 'light.dropouts_one' : 'light.dropouts_many', { n: num(n) });
    if (n >= 3 && out[0].tone === 'ok') out[0].tone = 'warn';
  }
  return out;
}

function renderOverview() {
  const v = S.values || {};
  const gone = lost();
  bindValues($('view-overview'));

  const w = gone ? null : v.grid?.active_power_w;
  const max = v.device?.max_power_w;
  const p = power(w);
  $('pw-num').textContent = p.num;
  $('pw-unit').textContent = p.unit;
  const pct = w != null && max ? Math.min(100, Math.max(0, (w / max) * 100)) : 0;
  $('ring-fg').setAttribute('stroke-dasharray', `${pct.toFixed(1)} 100`);
  $('ring-fg').style.visibility = pct < 0.4 ? 'hidden' : '';
  if (w != null && max) {
    const m = power(max);
    $('pw-pct').textContent = num(pct, 0) + ' %';
    $('pw-of').textContent = t('ov.utilization', { max: m.num + ' ' + m.unit });
  } else { $('pw-pct').textContent = ''; $('pw-of').textContent = ''; }
  const inp = gone ? null : v.pv?.input_power_w;
  [1, 2].forEach((n) => {
    const pw = gone ? null : v.pv?.[`pv${n}_power_w`];
    const share = pw != null && inp > 0 ? Math.min(100, (pw / inp) * 100) : 0;
    const bar = $('bar-' + n);
    bar.style.width = share.toFixed(1) + '%';
    bar.parentElement.title = t('ov.share') + ': ' + num(share, 0) + ' %';
  });

  // Spitzenleistung: Uhrzeit (Ortszeit) klein dahinter, "–" wenn unbekannt
  const pts = v.energy?.peak_power_today_ts;
  const peakEl = $('peak-at');
  const hmTxt = pts ? hm(pts) : null;
  peakEl.textContent = v.energy?.peak_power_today_w == null ? '' : hmTxt ? t('ov.peak_at', { time: hmTxt }) : '–';

  const key = v.inverter?.status;
  const tone = gone ? (S.status.inverter.state === 'offline' ? 'bad' : 'warn') : statusTone(key);
  const pill = $('state-pill');
  pill.className = 'state-pill tone-' + tone + (gone ? ' lost' : '');
  pill.querySelector('use').setAttribute('href', gone ? '#i-alert' : tone === 'ok' ? '#i-bolt' : tone === 'idle' ? '#i-sun' : '#i-alert');
  $('state-text').textContent = gone ? t('conn.pill') : v.inverter ? statusText(key) : t('status.none');
  $('hero').classList.toggle('is-night', !gone && !!key && key.startsWith('standby'));
  $('night-hint').hidden = gone || !(key && key === 'standby_no_irradiation');

  const alarms = v.inverter?.alarms;
  $('alarms').textContent = gone ? '–' : Array.isArray(alarms) && alarms.length ? alarms.map(humanize).join(', ') : v.inverter ? t('ov.no_alarms') : '–';

  const ul = $('lights');
  ul.replaceChildren(...lightsData().map((l) => h('li', { class: 'light tone-' + l.tone },
    h('span', { class: 'dot', 'aria-hidden': 'true' }), icon(l.ic),
    h('div', {}, h('strong', {}, l.title), h('span', {}, l.text), l.hint ? h('span', { class: 'sub-hint' }, l.hint) : null))));
  renderAge();
}

// ---------- Rendering: Alle Werte ----------
let valBuilt = false;
const numAuto = (n) => new Intl.NumberFormat(lang, { maximumFractionDigits: 3 }).format(n);
function renderValues() {
  const regs = S.values?.registers || [];
  const sel = $('val-group');
  const groups = [...new Set(regs.map((r) => r.group).filter(Boolean))];
  const sig = groups.join('|') + '|' + lang;
  if (!valBuilt || sel.dataset.sig !== sig) {
    const cur = sel.value;
    sel.replaceChildren(h('option', { value: '' }, t('val.group_all')),
      ...groups.map((g) => h('option', { value: g }, tryT('group.' + g, humanize(g)))));
    sel.value = groups.includes(cur) ? cur : '';
    sel.dataset.sig = sig;
    valBuilt = true;
  }
  const q = $('val-filter').value.trim().toLowerCase();
  const g = sel.value;
  const rows = regs.filter((r) => (!g || r.group === g)
    && (!q || String(r.name).toLowerCase().includes(q) || fieldName(r.name).toLowerCase().includes(q) || String(r.address).includes(q)));
  const frag = document.createDocumentFragment();
  for (const r of rows) {
    frag.append(h('tr', {},
      h('td', { class: 'nm' }, h('span', {}, fieldName(r.name)), h('code', {}, r.name)), h('td', { class: 'num mono' }, String(r.address)),
      h('td', { class: 'num mono' }, r.raw == null ? '–' : String(r.raw)),
      h('td', { class: 'num' }, r.value == null ? '–' : typeof r.value === 'number' ? numAuto(r.value) : String(r.value)),
      h('td', {}, r.unit || ''),
      h('td', {}, r.group ? tryT('group.' + r.group, humanize(r.group)) : '')));
  }
  $('val-table').tBodies[0].replaceChildren(frag);
  $('val-count').textContent = t('val.count', { n: num(rows.length), total: num(regs.length) });
  $('val-empty').hidden = rows.length > 0;
}

// ---------- Log ----------
let logTimer = null;
function logRow(e) {
  const lvl = String(e.level || '').toUpperCase();
  return h('div', { class: 'logline lvl-' + lvl, 'data-rank': LEVELS[lvl] ?? 20 },
    h('span', { class: 'lt' }, time(e.ts)), h('span', { class: 'll' }, lvl),
    h('span', { class: 'lg' }, e.logger || ''), h('span', { class: 'lm' }, e.message));
}
function renderLog(full = false) {
  const box = $('log-box');
  const min = LEVELS[$('log-level').value] ?? 0;
  const shown = S.logEntries.filter((e) => (LEVELS[String(e.level).toUpperCase()] ?? 20) >= min);
  if (full) { box.querySelectorAll('.logline').forEach((n) => n.remove()); box.append(...shown.map(logRow)); }
  $('log-empty').hidden = shown.length > 0;
  $('log-count').textContent = t('log.count', { n: num(shown.length) });
  if ($('log-follow').checked) box.scrollTop = box.scrollHeight;
}
async function pollLog() {
  if (S.status?.bridge?.started && S.status.bridge.started !== S.logStarted) {
    S.logStarted = S.status.bridge.started; S.logSeq = 0; S.logEntries = [];
  }
  const r = await api('GET', '/api/logs?since=' + S.logSeq);
  const list = r.ok ? r.data?.entries : null;
  if (!list || !list.length) return;
  S.logSeq = Math.max(S.logSeq, ...list.map((e) => e.seq));
  S.logEntries = S.logEntries.concat(list).slice(-LOG_MAX);
  const box = $('log-box');
  const min = LEVELS[$('log-level').value] ?? 0;
  const add = list.filter((e) => (LEVELS[String(e.level).toUpperCase()] ?? 20) >= min);
  box.append(...add.map(logRow));
  while (box.querySelectorAll('.logline').length > LOG_MAX) box.querySelector('.logline').remove();
  renderLog();
}
function startLog() { stopLog(); pollLog(); logTimer = setInterval(pollLog, LOG_MS); }
function stopLog() { clearInterval(logTimer); logTimer = null; }

// ---------- Einstellungen & Bedienung ----------
function renderControl() {
  const st = S.status;
  if (!st) return;
  const sig = [passwordSet(), loggedIn(), !!st.shutdown_allowed, lang].join('|');
  const c = S.values?.control;
  const cur = $('ctl-current');
  cur.replaceChildren(...(c ? [
    h('span', { class: 'label' }, t('ctl.current')),
    h('span', { class: 'stat' }, c.power_limit_pct == null ? '–' : num(c.power_limit_pct, 1) + '\u00a0%'),
    h('span', { class: 'stat soft' }, c.power_limit_w == null ? '–' : num(c.power_limit_w, 0) + '\u00a0W')] : []));
  const max = S.values?.device?.max_power_w;
  if (max) $('lim-w-num').max = max;
  // Eingabefelder nur mit aktuellen Werten fuellen, solange der Nutzer nichts getippt hat
  if (c) {
    const fill = (el, v) => { if (v != null && !el.dataset.dirty && document.activeElement !== el) el.value = v; };
    fill($('lim-pct-num'), c.power_limit_pct);
    fill($('lim-pct-range'), c.power_limit_pct);
    fill($('lim-w-num'), c.power_limit_w);
  }
  if (sig === S.ctlSig) return;
  S.ctlSig = sig;
  const notice = $('ctl-notice');
  const body = $('ctl-body');
  if (!passwordSet()) {
    notice.replaceChildren(h('span', {}, t('ctl.no_password')));
    notice.hidden = false; body.hidden = true;
  } else if (!loggedIn()) {
    notice.replaceChildren(h('span', {}, t('ctl.login_hint')), ' ',
      h('button', { type: 'button', class: 'btn small primary', onclick: openLogin }, t('auth.login')));
    notice.hidden = false; body.hidden = true;
  } else {
    notice.hidden = true; body.hidden = false;
    $('power-block').hidden = !st.shutdown_allowed;
  }
}

function limitsFor(name) {
  const L = S.settings?.limits || {};
  const o = L[name];
  if (o && typeof o === 'object' && !Array.isArray(o)) return { min: o.min, max: o.max, choices: o.choices || o.options };
  return { min: L[name + '_min'], max: L[name + '_max'], choices: Array.isArray(o) ? o : undefined };
}

function fieldErrors(data) {
  if (!data || typeof data !== 'object') return null;
  const src = data.errors || data.fields || data.field_errors || (data.error && typeof data.error === 'object' ? data.error : null);
  if (!src || typeof src !== 'object') return null;
  const out = {};
  for (const [k, v] of Object.entries(src)) {
    const m = Array.isArray(v) ? v[0] : (v && typeof v === 'object' ? (v.message || v.error) : v);
    out[k] = tryT('reason.' + m, String(m));
  }
  return out;
}

function renderSettings() {
  const s = S.settings;
  if (!s) return;
  const sig = [loggedIn(), lang, JSON.stringify(s.editable)].join('|');
  if (sig !== S.setSig) {
    S.setSig = sig;
    const can = loggedIn();
    $('set-login-hint').hidden = can || !passwordSet();
    const form = $('set-form');
    const rows = Object.entries(s.editable || {}).map(([name, val]) => {
      const lim = limitsFor(name);
      const id = 'f-' + name;
      let input;
      if (typeof val === 'boolean') input = h('input', { type: 'checkbox', id, name, checked: val, disabled: !can });
      else if (typeof val === 'number') input = h('input', { type: 'number', id, name, value: val, min: lim.min, max: lim.max, step: 'any', inputmode: 'decimal', disabled: !can });
      else if (name === 'log_level' || lim.choices) {
        const ch = lim.choices || ['DEBUG', 'INFO', 'WARNING', 'ERROR'];
        input = h('select', { id, name, disabled: !can }, (ch.includes(val) ? ch : [val, ...ch]).map((c) => h('option', { value: c, selected: c === val }, c)));
      } else input = h('input', { type: 'text', id, name, value: val ?? '', disabled: !can, maxlength: name === 'currency' ? 3 : null, autocapitalize: name === 'currency' ? 'characters' : null });
      const range = lim.min != null && lim.max != null && typeof val === 'number'
        ? h('small', { class: 'muted' }, t('set.range', { min: num(lim.min, 0), max: num(lim.max, 0) })) : null;
      return h('div', { class: 'field' + (typeof val === 'boolean' ? ' check' : '') },
        h('label', { for: id }, tryT('field.' + name, name)), input, range,
        h('small', { class: 'field-error', id: 'err-' + name, role: 'alert', hidden: true }));
    });
    form.replaceChildren(...rows, can ? h('div', { class: 'btn-row' }, h('button', { type: 'submit', class: 'btn primary' }, t('set.save'))) : null);
    renderSystem();
  }
}

// "Verbindung und System": eine Schrift, gleiche Zeilen, Zustaende dezent; Monospace nur fuer Host:Port/Topic
function renderSystem() {
  const ro = S.settings?.readonly || {};
  const st = S.status || {};
  const used = new Set();
  const take = (k) => { used.add(k); return ro[k]; };
  const text = (v) => (v === '' || v == null ? '–' : String(v));
  const hp = (hk, pk) => { const a = take(hk), b = take(pk); return a ? (b ? `${a}:${b}` : String(a)) : '–'; };
  const mono = (v) => ({ mono: true, v });
  const flag = (on, yes, no) => ({ node: h('span', { class: 'state ' + (on ? 'on' : 'off') }, h('i'), on ? yes : no) });
  const secret = (k) => flag(!!take(k + '_set'), t('set.secret_set'), t('set.secret_unset'));
  const onoff = (k) => flag(!!take(k), t('common.on'), t('common.off'));
  const grp = (title, rows, hint) => h('div', { class: 'sysgrp' }, h('h3', {}, title),
    h('dl', { class: 'kv' }, rows.flatMap(([label, c]) => [h('dt', {}, label),
      h('dd', { class: c.mono ? 'mono' : '' }, c.node || (c.mono ? c.v : text(c.v)))])),
    hint ? h('p', { class: 'muted small-text' }, hint) : null);
  const sw = take('software_version'), fw = take('firmware_version');
  const groups = [
    grp(t('set.grp_device'), [[fieldName('software_version'), { v: sw ?? S.values?.device?.software_version }],
      [fieldName('firmware_version'), { v: fw ?? S.values?.device?.firmware_version }]], t('ov.version_hint')),
    grp(t('set.grp_conn'), [
      [t('sys.dongle'), mono(hp('huawei_host', 'huawei_port'))],
      [fieldName('huawei_unit_id'), { v: take('huawei_unit_id') }],
      [fieldName('mqtt_host'), mono(hp('mqtt_host', 'mqtt_port'))],
      [fieldName('base_topic'), mono(text(take('base_topic')))],
      ...(st.bridge?.version ? [[fieldName('version'), { v: st.bridge.version }]] : [])]),
    grp(t('set.grp_sec'), [
      [fieldName('mqtt_username'), secret('mqtt_username')], [fieldName('mqtt_password'), secret('mqtt_password')],
      [fieldName('mqtt_tls'), onoff('mqtt_tls')], [fieldName('portal_password'), secret('portal_password')],
      [fieldName('allow_shutdown'), onoff('allow_shutdown')]]),
  ];
  // unbekannte weitere Schluessel (neue Backend-Felder) trotzdem anzeigen
  const rest = Object.entries(ro).filter(([k]) => !used.has(k)).map(([k, v]) => {
    const sec = k.endsWith('_set');
    const name = sec ? k.slice(0, -4) : k;
    return [fieldName(name), sec ? flag(!!v, t('set.secret_set'), t('set.secret_unset'))
      : typeof v === 'boolean' ? flag(v, t('common.on'), t('common.off')) : { v }];
  });
  if (rest.length) groups.push(grp(t('set.grp_more'), rest));
  $('set-readonly').replaceChildren(...groups);
}

function fmtBytes(n) {
  if (n == null) return '–';
  if (n >= 1048576) return num(n / 1048576, 1) + ' MB';
  return num(Math.max(1, n / 1024), 0) + ' KB';
}
function renderMemory() {
  const hs = S.status?.history;
  $('mem-info').textContent = hs ? t('mem.info', { samples: num(hs.samples ?? 0), size: fmtBytes(hs.db_size_bytes),
    oldest: hs.oldest ? fmt.dateShort(new Date(hs.oldest)) : '–', days: num(hs.days ?? 0) }) : '';
  $('mem-info').hidden = !hs;
  const can = loggedIn();
  $('mem-login').hidden = can || !passwordSet();
  document.querySelectorAll('#mem-card [data-scope]').forEach((b) => { b.disabled = !can; });
}
const MEM_CONFIRM = { samples: 'mem.confirm_samples', daily: 'mem.confirm_daily', all: 'mem.confirm_all' };
async function clearMemory(scope, btn) {
  if (!(await confirmDialog(t(MEM_CONFIRM[scope])))) return;
  btn.disabled = true;
  const r = await api('POST', '/api/history/clear', { scope });
  btn.disabled = false;
  if (r.status === 401) return sessionExpired();
  if (r.status === 404) return toast(t('hist.unsupported'), 'warn');
  if (!r.ok) return toast(t(r.status === 403 ? 'err.forbidden' : r.status === 0 ? 'err.network' : 'mem.failed'), 'bad');
  const d = r.data?.deleted || {};
  toast(t('mem.done', { samples: num(d.samples ?? 0), daily: num(d.daily ?? 0), milestones: num(d.milestones ?? 0) }));
  poll();
}

async function loadSettings() {
  const r = await api('GET', '/api/settings');
  if (r.ok && r.data) { S.settings = r.data; S.setSig = null; renderSettings(); }
}

async function saveSettings(ev) {
  ev.preventDefault();
  const cur = S.settings?.editable || {};
  const patch = {};
  for (const [name, val] of Object.entries(cur)) {
    const el = $('f-' + name);
    if (!el) continue;
    let nv = typeof val === 'boolean' ? el.checked : typeof val === 'number' ? (el.value === '' ? NaN : Number(el.value)) : el.value;
    if (name === 'currency') nv = String(nv).trim().toUpperCase();
    if (nv !== val) patch[name] = nv;
  }
  document.querySelectorAll('#set-form .field-error').forEach((e) => { e.hidden = true; });
  if (!Object.keys(patch).length) { toast(t('set.nochange'), 'idle'); return; }
  const r = await api('PUT', '/api/settings', patch);
  if (r.ok) { toast(t('set.saved')); await loadSettings(); return; }
  if (r.status === 401) { sessionExpired(); return; }
  if (r.status === 0) { toast(t('err.network'), 'bad'); return; }
  const fe = fieldErrors(r.data);
  if (fe) {
    for (const [k, msg] of Object.entries(fe)) {
      const e = $('err-' + k);
      if (e) { e.textContent = msg; e.hidden = false; }
    }
    toast(t('set.invalid'), 'bad');
  } else toast(t(r.status === 403 ? 'err.forbidden' : 'err.generic'), 'bad');
}

function sessionExpired() {
  toast(t('err.session'), 'warn');
  if (S.status?.auth) S.status.auth.logged_in = false;
  poll();
}

async function runCommand(command, value, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await api('POST', '/api/command', { command, value });
    showResult(t('cmd.' + command), r);
    if (r.ok) document.querySelectorAll('#ctl-body input').forEach((i) => delete i.dataset.dirty);
  } finally { if (btn) btn.disabled = false; }
  poll();
}
function showResult(label, r) {
  if (r.status === 401) return sessionExpired();
  if (r.status === 0) return toast(t('err.network'), 'bad');
  const d = r.data || {};
  if (r.status === 403 && !d.status) return toast(t('err.forbidden'), 'bad');
  const status = d.status || (r.ok ? 'ok' : 'error');
  const reason = reasonText(d.reason ?? d.error);
  if (status === 'ok') toast(t('res.ok', { cmd: label }), 'ok');
  else if (status === 'rejected') toast(t('res.rejected', { cmd: label, reason }), 'warn');
  else toast(t('res.error', { cmd: label, reason }), 'bad');
}

function confirmDialog(text) {
  return new Promise((resolve) => {
    const dlg = $('dlg-confirm');
    $('confirm-text').textContent = text;
    dlg.returnValue = '';
    dlg.addEventListener('close', () => resolve(dlg.returnValue === 'ok'), { once: true });
    dlg.showModal();
  });
}

// ---------- Login ----------
function openLogin() {
  $('login-error').hidden = true;
  $('login-pw').value = '';
  $('login-forgot-text').hidden = true;
  $('dlg-login').showModal();
  $('login-pw').focus();
}
async function doLogin(ev) {
  ev.preventDefault();
  const err = $('login-error');
  err.hidden = true;
  const r = await api('POST', '/api/login', { password: $('login-pw').value });
  if (r.ok) {
    $('dlg-login').close();
    toast(t('login.ok'));
    S.ctlSig = null; S.setSig = null;
    await poll();
    if (currentRoute() === 'settings') loadSettings();
    return;
  }
  const key = r.status === 401 ? 'login.wrong' : r.status === 429 ? 'login.rate' : r.status === 403 ? 'login.disabled' : r.status === 0 ? 'err.network' : 'login.failed';
  err.textContent = t(key);
  err.hidden = false;
}
async function doLogout() {
  await api('POST', '/api/logout');
  toast(t('logout.ok'));
  S.ctlSig = null; S.setSig = null;
  await poll();
  if (currentRoute() === 'settings') loadSettings();
}
function renderAuth() {
  const b = $('btn-auth');
  b.hidden = !S.status || !passwordSet();
  $('btn-auth-label').textContent = loggedIn() ? t('auth.logout') : t('auth.login');
  $('acct-text').textContent = !S.status ? '' : !passwordSet() ? t('ctl.no_password') : loggedIn() ? t('set.access_in') : t('set.access_out');
}

// ---------- Router ----------
const ROUTES = ['overview', 'history', 'awards', 'values', 'log', 'settings'];
const currentRoute = () => { const r = location.hash.slice(1); return ROUTES.includes(r) ? r : 'overview'; };
function route() {
  const r = currentRoute();
  document.querySelectorAll('[data-view]').forEach((s) => { s.hidden = s.dataset.view !== r; });
  document.querySelectorAll('.tabs a[data-route]').forEach((a) => {
    if (a.dataset.route === r) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  stopLog();
  hist.deactivate(); awards.deactivate();
  if (r === 'log' && !document.hidden) startLog();
  if (r === 'settings') loadSettings();
  if (r === 'history') hist.activate();
  if (r === 'awards') awards.activate();
  renderAll();
  window.scrollTo(0, 0);
}

function renderAll() {
  renderAuth();
  renderBanner();
  const r = currentRoute();
  if (r === 'overview') renderOverview();
  if (r === 'values') renderValues();
  if (r === 'settings') { renderControl(); renderSettings(); renderMemory(); }
}

// ---------- Start ----------
async function init() {
  const sel = $('lang');
  await setLang(detect());
  sel.value = lang;
  applyDom();
  sel.addEventListener('change', async () => {
    await setLang(sel.value);
    applyDom();
    S.ctlSig = null; S.setSig = null; valBuilt = false;
    renderAll();
    renderLog(true);
    hist.relang(); awards.relang();
  });
  $('btn-auth').addEventListener('click', () => (loggedIn() ? doLogout() : openLogin()));
  $('login-form').addEventListener('submit', doLogin);
  $('login-cancel').addEventListener('click', () => $('dlg-login').close());
  $('login-forgot').addEventListener('click', () => { $('login-forgot-text').hidden = !$('login-forgot-text').hidden; });
  document.querySelectorAll('#mem-card [data-scope]').forEach((b) => b.addEventListener('click', () => clearMemory(b.dataset.scope, b)));
  hist.init(() => S.status);
  $('set-form').addEventListener('submit', saveSettings);
  $('val-filter').addEventListener('input', renderValues);
  $('val-group').addEventListener('change', renderValues);
  $('log-level').addEventListener('change', () => renderLog(true));
  $('log-clear').addEventListener('click', () => { S.logEntries = []; renderLog(true); });
  $('log-follow').addEventListener('change', () => { if ($('log-follow').checked) $('log-box').scrollTop = $('log-box').scrollHeight; });
  $('log-box').addEventListener('scroll', () => {
    const b = $('log-box');
    if (b.scrollHeight - b.scrollTop - b.clientHeight > 40 && $('log-follow').checked) $('log-follow').checked = false;
  });

  // Bedienung
  const pctNum = $('lim-pct-num'), pctRange = $('lim-pct-range'), wNum = $('lim-w-num');
  pctRange.addEventListener('input', () => { pctNum.value = pctRange.value; pctNum.dataset.dirty = '1'; pctRange.dataset.dirty = '1'; });
  pctNum.addEventListener('input', () => { pctRange.value = pctNum.value; pctNum.dataset.dirty = '1'; pctRange.dataset.dirty = '1'; });
  wNum.addEventListener('input', () => { wNum.dataset.dirty = '1'; });
  $('lim-pct-apply').addEventListener('click', (e) => {
    const v = Number(pctNum.value);
    if (pctNum.value === '' || Number.isNaN(v)) return toast(t('reason.invalid_value'), 'warn');
    runCommand('power_limit_pct', v, e.currentTarget);
  });
  $('lim-w-apply').addEventListener('click', (e) => {
    const v = Number(wNum.value);
    if (wNum.value === '' || Number.isNaN(v)) return toast(t('reason.invalid_value'), 'warn');
    runCommand('power_limit_w', Math.round(v), e.currentTarget);
  });
  $('btn-on').addEventListener('click', async (e) => { if (await confirmDialog(t('ctl.confirm_on'))) runCommand('power', 'on', e.target.closest('button')); });
  $('btn-off').addEventListener('click', async (e) => { if (await confirmDialog(t('ctl.confirm_off'))) runCommand('power', 'off', e.target.closest('button')); });
  $('btn-write-test').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    const r = await api('POST', '/api/write-test');
    btn.disabled = false;
    showResult(t('cmd.write_test'), r);
    poll();
  });

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { stopPolling(); stopLog(); }
    else { poll(); startPolling(); if (currentRoute() === 'log') startLog(); }
  });
  window.addEventListener('hashchange', route);
  setInterval(() => { if (!document.hidden && currentRoute() === 'overview') renderAge(); }, 500);

  await poll();
  route();
  startPolling();
}

init();
