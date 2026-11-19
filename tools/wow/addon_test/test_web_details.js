/* Headless test of wow/app.js: the auto lists in the FAVORITES dropdown and
   the item details block under the item name.
   Loads the REAL app.js (and index.html's #itemDetails host) in a vm with a
   small, permissive fake DOM, serves wow/_localdata (sim_fetch.py's simulated
   data, details chunks included) through a fake fetch, lets the page boot,
   and checks the HTML it writes. The pure helpers (autoFavIds,
   wowDetailLines) are also run on hand-made data.
   Usage: node tools/wow/addon_test/test_web_details.js [path/to/wow] */
'use strict';
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const WOW = process.argv[2] || path.join(__dirname, '..', '..', '..', 'wow');
const APP = path.join(WOW, 'app.js');
const DATA = path.join(WOW, '_localdata');
const APP_SRC = fs.readFileSync(APP, 'utf8');

let fails = 0, passes = 0;
function check(ok, what) {
  if (ok) passes++; else { fails++; console.log('FAIL: ' + what); }
}
const unesc = (s) => String(s).replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
const text = (h) => unesc(String(h).replace(/<[^>]*>/g, ' ')).replace(/\s+/g, ' ').trim();
const tick = (ms) => new Promise((r) => setTimeout(r, ms || 5));

/* ---------- a permissive fake DOM ---------- */
function makeEnv(opts) {
  opts = opts || {};
  const reg = new Map();
  const errors = [];
  const fetched = [];
  const ls = Object.assign({}, opts.storage || {});
  class ClassList {
    constructor() { this.s = new Set(); }
    add(...n) { n.forEach((x) => this.s.add(String(x))); }
    remove(...n) { n.forEach((x) => this.s.delete(String(x))); }
    toggle(n, f) { if (f === undefined) f = !this.s.has(n); if (f) this.s.add(n); else this.s.delete(n); return !!f; }
    contains(n) { return this.s.has(n); }
    replace(a, b) { if (!this.s.has(a)) return false; this.s.delete(a); this.s.add(b); return true; }
    get length() { return this.s.size; }
    forEach(f) { [...this.s].forEach(f); }
  }
  function makeStyle() {
    const s = {};
    Object.defineProperty(s, 'setProperty', { value: (k, v) => { s[k] = String(v); } });
    Object.defineProperty(s, 'getPropertyValue', { value: (k) => (s[k] || '') });
    Object.defineProperty(s, 'removeProperty', { value: (k) => { delete s[k]; } });
    return s;
  }
  const ctx2d = new Proxy({}, {
    get(t, k) {
      if (k in t) return t[k];
      if (k === 'measureText') return (s) => ({ width: String(s).length * 6, actualBoundingBoxAscent: 8, actualBoundingBoxDescent: 2 });
      if (k === 'createLinearGradient' || k === 'createRadialGradient' || k === 'createPattern') return () => ({ addColorStop() {} });
      if (k === 'getImageData') return () => ({ data: new Uint8ClampedArray(4) });
      if (k === 'getTransform') return () => ({ a: 1, d: 1 });
      return typeof k === 'string' ? () => {} : undefined;
    },
    set(t, k, v) { t[k] = v; return true; }
  });
  let body = null;
  class El {
    constructor(tag) {
      this.tagName = String(tag || 'div').toUpperCase(); this.nodeType = 1;
      this._id = ''; this._html = ''; this.children = []; this.parentElement = body;
      this.attrs = {}; this.dataset = {}; this.style = makeStyle(); this.classList = new ClassList();
      this.hidden = false; this.textContent = ''; this.value = ''; this.checked = false; this.disabled = false;
      this.scrollLeft = 0; this.scrollTop = 0; this.handlers = {}; this.width = 300; this.height = 150;
      this.title = ''; this.src = ''; this.href = ''; this.alt = ''; this.type = ''; this.name = '';
    }
    get id() { return this._id; }
    set id(v) { this._id = String(v); if (v) reg.set(String(v), this); }
    get className() { return [...this.classList.s].join(' '); }
    set className(v) { this.classList.s = new Set(String(v).split(/\s+/).filter(Boolean)); }
    get innerHTML() { return this._html; }
    set innerHTML(v) {
      this._html = String(v);
      this.children.forEach((c) => { c.parentElement = null; });
      this.children = [];
      const re = /\sid="([^"]+)"/g;
      let m;
      while ((m = re.exec(this._html))) { const e = new El('x'); e.id = m[1]; e.parentElement = this; }
    }
    get outerHTML() { return this._html; }
    get firstElementChild() { return this.children[0] || null; }
    get lastElementChild() { return this.children[this.children.length - 1] || null; }
    get childElementCount() { return this.children.length; }
    get nextElementSibling() { const p = this.parentElement; if (!p) return null; const i = p.children.indexOf(this); return i >= 0 ? p.children[i + 1] || null : null; }
    get previousElementSibling() { const p = this.parentElement; if (!p) return null; const i = p.children.indexOf(this); return i > 0 ? p.children[i - 1] : null; }
    get offsetWidth() { return 0; } get offsetHeight() { return 0; } get clientWidth() { return 0; } get clientHeight() { return 0; }
    get scrollWidth() { return 0; } get scrollHeight() { return 0; } get offsetTop() { return 0; } get offsetLeft() { return 0; }
    get offsetParent() { return null; } get isConnected() { return true; }
    _detach(c) { if (c && c.parentElement && c.parentElement.children) { const i = c.parentElement.children.indexOf(c); if (i >= 0) c.parentElement.children.splice(i, 1); } }
    appendChild(c) {
      if (!c) return c;
      if (c.isFragment) { c.children.slice().forEach((x) => this.appendChild(x)); c.children = []; return c; }
      this._detach(c); c.parentElement = this; this.children.push(c); return c;
    }
    append(...cs) { cs.forEach((c) => { if (c && typeof c === 'object') this.appendChild(c); }); }
    prepend(...cs) { cs.reverse().forEach((c) => { if (c && typeof c === 'object') { this._detach(c); c.parentElement = this; this.children.unshift(c); } }); }
    insertBefore(c, ref) {
      if (!c) return c;
      this._detach(c); c.parentElement = this;
      const i = ref ? this.children.indexOf(ref) : -1;
      if (i >= 0) this.children.splice(i, 0, c); else this.children.push(c);
      return c;
    }
    removeChild(c) { this._detach(c); c.parentElement = null; return c; }
    after(...ns) { const p = this.parentElement; if (!p) return; let i = p.children.indexOf(this); ns.forEach((n) => { if (n && typeof n === 'object') { this._detach(n); n.parentElement = p; p.children.splice(++i, 0, n); } }); }
    before(...ns) { const p = this.parentElement; if (!p) return; ns.forEach((n) => { if (n && typeof n === 'object') p.insertBefore(n, this); }); }
    remove() { this._detach(this); this.parentElement = null; }
    replaceWith(n) { this.before(n); this.remove(); }
    replaceChildren(...cs) { this.children = []; this.append(...cs); }
    insertAdjacentHTML(pos, html) { this._html += String(html); }
    insertAdjacentElement(pos, el) { this.appendChild(el); return el; }
    /* "#id" finds the element (made from innerHTML or on demand); anything
       else gets a stand-in of its own, so the page can write into it. */
    querySelector(sel) {
      const m = /^#([\w-]+)$/.exec(String(sel).trim());
      if (m) return doc.getElementById(m[1]);
      this._sinks = this._sinks || {};
      return this._sinks[sel] || (this._sinks[sel] = new El('x'));
    }
    querySelectorAll() { return []; }
    getElementsByTagName() { return []; }
    getElementsByClassName() { return []; }
    closest() { return null; }
    matches() { return false; }
    contains(x) { while (x) { if (x === this) return true; x = x.parentElement; } return false; }
    getBoundingClientRect() { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0 }; }
    getClientRects() { return []; }
    getContext() { return ctx2d; }
    toDataURL() { return 'data:,'; }
    toBlob(cb) { if (cb) cb(null); }
    setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = v; if (k === 'class') this.className = v; }
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
    hasAttribute(k) { return k in this.attrs; }
    removeAttribute(k) { delete this.attrs[k]; }
    toggleAttribute(k, f) { if (f === undefined) f = !(k in this.attrs); if (f) this.attrs[k] = ''; else delete this.attrs[k]; return f; }
    addEventListener(t, f) { (this.handlers[t] = this.handlers[t] || []).push(f); }
    removeEventListener() {}
    dispatchEvent() { return true; }
    focus() {} blur() {} click() {} select() {} setSelectionRange() {} scrollIntoView() {} scrollTo() {} scrollBy() {}
    setPointerCapture() {} releasePointerCapture() {} hasPointerCapture() { return false; }
    animate() { return { finished: Promise.resolve(), cancel() {}, onfinish: null }; }
    cloneNode() { return new El(this.tagName); }
    showModal() {} close() {} show() {}
  }
  body = new El('body');
  body.parentElement = null;
  const html = new El('html');
  html.parentElement = null;
  body.parentElement = html;
  const head = new El('head');
  /* index.html's real #itemDetails host, as written */
  const doc = {
    readyState: 'complete', hidden: false, visibilityState: 'visible', title: '', cookie: '', referrer: '',
    body, documentElement: html, head, activeElement: null, fonts: { ready: Promise.resolve(), load: () => Promise.resolve() },
    handlers: {},
    createElement: (t) => new El(t),
    createElementNS: (ns, t) => new El(t),
    createDocumentFragment: () => { const f = new El('#fragment'); f.isFragment = true; return f; },
    createTextNode: (s) => ({ nodeType: 3, textContent: String(s), parentElement: null }),
    getElementById: (id) => { if (!reg.has(id)) { const e = new El('x'); e.id = id; } return reg.get(id); },
    querySelector: (sel) => body.querySelector(sel),
    querySelectorAll: () => [],
    getElementsByTagName: () => [],
    getElementsByClassName: () => [],
    elementFromPoint: () => null,
    addEventListener: (t, f) => { (doc.handlers[t] = doc.handlers[t] || []).push(f); },
    removeEventListener: () => {},
    dispatchEvent: () => true,
    execCommand: () => false,
    hasFocus: () => true
  };
  const host = doc.getElementById('itemDetails');
  host.hidden = true;
  const store = {
    getItem: (k) => (Object.prototype.hasOwnProperty.call(ls, k) ? ls[k] : null),
    setItem: (k, v) => { ls[k] = String(v); },
    removeItem: (k) => { delete ls[k]; },
    clear: () => { Object.keys(ls).forEach((k) => delete ls[k]); },
    key: (i) => Object.keys(ls)[i] || null,
    get length() { return Object.keys(ls).length; }
  };
  const files = opts.files || {};
  function fakeFetch(url) {
    const u = String(url);
    fetched.push(u);
    let body = null;
    const m = /^\/wow\/_localdata\/(.+)$/.exec(u.split('?')[0]);
    if (m) {
      if (Object.prototype.hasOwnProperty.call(files, m[1])) body = files[m[1]];
      else {
        const p = path.join(DATA, m[1]);
        if (fs.existsSync(p)) body = fs.readFileSync(p, 'utf8');
      }
    }
    if (body == null) return Promise.resolve({ ok: false, status: 404, json: () => Promise.reject(new Error('404')), text: () => Promise.resolve('') });
    const s = typeof body === 'string' ? body : JSON.stringify(body);
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(JSON.parse(s)), text: () => Promise.resolve(s) });
  }
  class Observer { constructor(cb) { this.cb = cb; } observe() {} unobserve() {} disconnect() {} takeRecords() { return []; } }
  const timers = new Set();
  const ctx = {
    console: { log: () => {}, info: () => {}, warn: () => {}, debug: () => {}, error: (...a) => { errors.push(a.map((x) => (x && x.stack) || String(x)).join(' ')); } },
    document: doc, localStorage: store, sessionStorage: store,
    location: { hostname: 'localhost', search: '', pathname: '/wow/', hash: '', href: 'http://localhost/wow/', origin: 'http://localhost', protocol: 'http:', host: 'localhost', reload() {}, replace() {}, assign() {} },
    history: { replaceState() {}, pushState() {}, back() {}, state: null, scrollRestoration: 'manual' },
    navigator: { userAgent: 'node', language: 'en-US', languages: ['en-US'], clipboard: { writeText: () => Promise.resolve() }, maxTouchPoints: 0, onLine: true },
    screen: { width: 1440, height: 900 },
    matchMedia: () => ({ matches: false, media: '', addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }),
    getComputedStyle: () => new Proxy({}, { get: (t, k) => (k === 'getPropertyValue' ? () => '' : '') }),
    requestAnimationFrame: () => 0, cancelAnimationFrame: () => {},
    requestIdleCallback: () => 0, cancelIdleCallback: () => {},
    setTimeout: (f, ms) => { if ((ms || 0) > 1500) return 0; const id = setTimeout(() => { timers.delete(id); try { f(); } catch (e) { errors.push('timer: ' + (e && e.stack || e)); } }, ms || 0); timers.add(id); return id; },
    clearTimeout: (id) => { if (id) clearTimeout(id); },
    setInterval: () => 0, clearInterval: () => {},
    queueMicrotask: (f) => Promise.resolve().then(f),
    fetch: fakeFetch,
    ResizeObserver: Observer, MutationObserver: Observer, IntersectionObserver: Observer, PerformanceObserver: Observer,
    Image: class { constructor() { this.onload = null; this.onerror = null; this.src = ''; } },
    Event: class { constructor(t) { this.type = t; } },
    CustomEvent: class { constructor(t, o) { this.type = t; this.detail = o && o.detail; } },
    KeyboardEvent: class { constructor(t) { this.type = t; } },
    DOMParser: class { parseFromString() { return { querySelector: () => null }; } },
    Blob: class { constructor() {} }, FileReader: class { readAsDataURL() {} },
    URL, URLSearchParams, TextEncoder, TextDecoder, AbortController, Intl, Promise, JSON, Math, Date,
    performance: { now: () => Date.now() },
    alert: () => {}, confirm: () => false, prompt: () => null, open: () => null, scrollTo: () => {}, scrollBy: () => {},
    print: () => {}, focus: () => {}, addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => true,
    gtag: () => {}, dataLayer: [], __PGE_PAGES__: [],
    devicePixelRatio: 1, innerWidth: 1440, innerHeight: 900, outerWidth: 1440, outerHeight: 900,
    scrollX: 0, scrollY: 0, pageXOffset: 0, pageYOffset: 0, isSecureContext: true
  };
  ctx.window = ctx; ctx.self = ctx; ctx.globalThis = ctx; ctx.top = ctx; ctx.parent = ctx;
  vm.createContext(ctx);
  return { ctx, doc, reg, ls, errors, fetched, host, run: (code) => vm.runInContext(code, ctx) };
}

/* Boot the real page: app.js, then wait for its data, the watchlist and the first item. */
async function boot(opts) {
  const env = makeEnv(opts);
  try {
    vm.runInContext(APP_SRC, env.ctx, { filename: APP });
  } catch (e) {
    env.errors.push('load: ' + (e && e.stack || e));
  }
  for (let i = 0; i < 200; i++) {
    await tick(5);
    let ready = false;
    try { ready = env.run('!!(selected && mapping && mapping.length)'); } catch (e) {}
    if (ready && !env.run('autoFlipsBusy')) break;
  }
  await tick(20);
  return env;
}
function deepHtml(el) {
  if (!el) return '';
  return (el._html || '') + el.children.map(deepHtml).join('');
}
function favHtml(env) { return deepHtml(env.doc.getElementById('watchlistContent')); }
function favTitleHtml(env) { return deepHtml(env.doc.getElementById('favHeadTitle')); }
function favRows(env) {
  const h = favHtml(env);
  const ids = [];
  const re = /<div class="wl-item[^"]*" data-id="(\d+)"([^>]*)>/g;
  let m;
  while ((m = re.exec(h))) ids.push({ id: m[1], attrs: m[2] });
  return ids;
}
const load = (f) => JSON.parse(fs.readFileSync(path.join(DATA, f), 'utf8'));

/* What the auto lists should hold, worked out on their own from the data files. */
function expectedQualityIds(quality) {
  const mapping = load('mapping.json');
  const latest = load('latest.json').data;
  const vols = load('volumes.json').data;
  const st = load('status.json');
  const h = Math.floor(Number(st.salesHours));
  const scale = (h >= 1 && h < 24) ? (v) => Math.floor(v * 24 / h + 0.5) : (v) => v;
  const lumped = new Set(mapping.filter((m) => m.base != null && String(m.base) !== String(m.id)).map((m) => String(m.base)));
  const rows = [];
  for (const m of mapping) {
    const id = String(m.id);
    const nm = typeof m.name === 'string' ? m.name.trim() : '';
    if (String(m.quality || '').toUpperCase() !== quality || !nm || lumped.has(id)) continue;
    const n = latest[id];
    const price = n ? (n.high || n.low || 0) : 0;
    const sold = scale(vols[id] || 0);
    if (price > 0 && sold > 0) rows.push([id, price * sold, sold]);
  }
  rows.sort((a, b) => b[1] - a[1] || b[2] - a[2] || Number(a[0]) - Number(b[0]));
  return rows.slice(0, 25).map((r) => r[0]);
}
function expectedFlipIds() {
  const ids = new Set(load('mapping.json').map((m) => String(m.id)));
  const out = [];
  for (const f of load('flips.json').flips) {
    const id = String(f.id);
    if (ids.has(id) && !out.includes(id)) out.push(id);
    if (out.length >= 25) break;
  }
  return out;
}

(async function main() {
  console.log('app.js under test: ' + APP);
  check(fs.existsSync(path.join(DATA, 'details')), 'wow/_localdata has details chunks (run sim_fetch.py)');
  const mapping = load('mapping.json');
  const byName = (n) => mapping.find((m) => m.name === n);
  const cloak = byName("Archer's Cloak");
  const cloakV = mapping.find((m) => m.base === cloak.id && / of the /.test(m.name));
  const blade = byName('Thrash Blade');
  const ironBar = byName('Iron Bar');
  const linen = byName('Linen Cloth');
  check(cloak && cloakV && blade && ironBar && linen, 'simulated items found');

  /* ===== the page boots, opens Thrash Blade (remembered last item) ===== */
  const savedLists = JSON.stringify([{ id: 'default', name: 'Favorites', items: ['2589', '4306', String(cloakV.id)] },
    { id: 'fl-x', name: 'Long holds', items: ['14047'] }]);
  const env = await boot({ storage: { ge_favoriteLists: savedLists, ge_activeFavoriteList: 'default', ge_lastItem: String(blade.id) } });
  check(env.errors.length === 0, 'boot without errors: ' + env.errors.slice(0, 3).join(' | '));
  const sel = () => env.run('selected && String(selected.id)');
  check(sel() === String(blade.id), 'landing item is the remembered Thrash Blade (' + sel() + ')');

  /* ---- item details: a blue weapon with Chance on hit and flavor text ---- */
  const host = env.doc.getElementById('itemDetails');
  const lines = () => {
    const out = [];
    const re = /<div class="idt-line idt-(\w)( idt-pair)?">([\s\S]*?)<\/div>/g;
    let m;
    while ((m = re.exec(host.innerHTML))) {
      const parts = m[3].match(/<span>([\s\S]*?)<\/span><span class="idt-right">([\s\S]*?)<\/span>/);
      out.push(parts ? [unesc(parts[1]), m[1], unesc(parts[2])] : [unesc(m[3]), m[1]]);
    }
    return out;
  };
  const chunk17 = load('details/17.json');
  check(!host.hidden, 'details block shown for Thrash Blade');
  check(JSON.stringify(lines().slice(0, -1)) === JSON.stringify(chunk17[String(blade.id)]),
    'Thrash Blade: the chunk lines in order, right-hand parts kept (' + JSON.stringify(lines()) + ')');
  check(JSON.stringify(lines().slice(-1)) === JSON.stringify([['Sell Price: ' + env.run('moneyExact(' + blade.highalch + ')'), 'w']]),
    'Thrash Blade: sell price last, from the mapping');
  check(/idt-g">Chance on hit: /.test(host.innerHTML) && /idt-y">&quot;Simulated flavor text.&quot;<\/div>/.test(host.innerHTML),
    'Chance on hit in green, flavor text in gold with quotes');
  check(host.attrs['aria-label'] === 'Thrash Blade: item details', 'block is labelled for screen readers');
  check(env.fetched.filter((u) => /\/details\//.test(u)).length === 1 && env.fetched.some((u) => u === '/wow/_localdata/details/17.json'),
    'only the shown item\'s chunk is read: ' + env.fetched.filter((u) => /\/details\//.test(u)).join(','));

  async function open(m) {
    env.run('setItem._userPicked = false');
    env.ctx.setItem(m);
    for (let i = 0; i < 40; i++) { await tick(5); if (sel() === String(m.id)) break; }
    await tick(20);
  }
  /* a version: its item's lines plus its random enchantment, after the stats */
  await open(cloakV);
  const suffix = cloakV.name.slice(cloak.name.length + 1);
  const cl = lines();
  const armorAt = cl.findIndex((l) => l[0] === '23 Armor');
  check(!host.hidden && armorAt > 0 && cl[armorAt + 1][0] === 'Random enchantment: ' + suffix && cl[armorAt + 1][1] === 'g',
    'version: "Random enchantment: ' + suffix + '" right after the armor line (' + JSON.stringify(cl) + ')');
  check(cl[0][0] === 'Item Level 34' && cl.some((l) => l[0] === 'Back'), 'version: its item\'s lines');
  /* the item made of versions: the game's own <Random enchantment> */
  await open(cloak);
  check(lines().some((l) => l[0] === '<Random enchantment>' && l[1] === 'g') && /&lt;Random enchantment&gt;/.test(host.innerHTML),
    'item made of versions: "<Random enchantment>", escaped');
  /* an item Blizzard has no lines for: hidden, and nothing of the last item left */
  await open(ironBar);
  check(host.hidden && host.innerHTML === '', 'Iron Bar ([] in its chunk): block hidden and empty');
  /* trade goods: one line plus the sell price */
  await open(linen);
  check(!host.hidden && JSON.stringify(lines()) === JSON.stringify([['Crafting Reagent', 'b'], ['Sell Price: ' + env.run('moneyExact(' + linen.highalch + ')'), 'w']]),
    'Linen Cloth: crafting reagent in blue and the sell price ' + JSON.stringify(lines()));
  /* a quick switch: the slower item never paints over the newer one */
  env.run('setItem._userPicked = false');
  env.ctx.setItem(blade);
  env.ctx.setItem(ironBar);
  await tick(60);
  check(sel() === String(ironBar.id) && host.hidden, 'two quick switches: the last item wins (hidden for Iron Bar)');
  const reads = env.fetched.filter((u) => /\/details\//.test(u)).length;
  await open(blade);
  check(env.fetched.filter((u) => /\/details\//.test(u)).length === reads, 'a chunk read once is kept (no second read)');

  /* ---- the block's height goes to the desktop chart: --idt-h on .main-column ----
     (index.html: #itemDetails is a child of .main-column, whose gap is 8px) */
  const col = env.doc.createElement('div');
  col.className = 'main-column';
  host.parentElement = col;
  col.children.push(host);
  let hostH = 87;
  host.getBoundingClientRect = () => ({ top: 0, left: 0, right: 600, bottom: 0, width: 600, height: host.hidden ? 0 : hostH, x: 0, y: 0 });
  const gcs = env.ctx.getComputedStyle;
  env.ctx.getComputedStyle = (el) => (el === col ? { rowGap: '8px', getPropertyValue: () => '' } : gcs(el));
  const watched = [];
  env.ctx.ResizeObserver = class {
    constructor(cb) { this.cb = cb; this.targets = []; watched.push(this); }
    observe(t) { this.targets.push(t); } unobserve() {} disconnect() {} takeRecords() { return []; }
  };
  await open(ironBar);
  check(!('--idt-h' in col.style), 'no details: no --idt-h on .main-column');
  await open(blade);
  check(!host.hidden && col.style['--idt-h'] === '95px', 'details shown: --idt-h is the block plus the column gap (' + col.style['--idt-h'] + ')');
  const ro = watched.filter((o) => o.targets.includes(host));
  check(ro.length === 1, 'the block\'s size is watched, by one observer (' + ro.length + ')');
  hostH = 120.4;                                  // a narrower window wraps the block to more rows
  if (ro[0]) ro[0].cb([]);
  check(col.style['--idt-h'] === '129px', 'the block grows: --idt-h follows, rounded up (' + col.style['--idt-h'] + ')');
  await open(ironBar);
  check(host.hidden && !('--idt-h' in col.style), 'hidden again: --idt-h removed');
  hostH = 87;
  await open(blade);
  check(col.style['--idt-h'] === '95px' && watched.filter((o) => o.targets.includes(host)).length === 1,
    'shown again: --idt-h back, still one observer');

  /* ---- the auto lists ---- */
  const before = { lists: env.ls.ge_favoriteLists, active: env.ls.ge_activeFavoriteList };
  check(favRows(env).length === 3 && favRows(env).every((r) => /data-fav-row="1"/.test(r.attrs)),
    'the player\'s list shows first, editable rows (' + favRows(env).map((r) => r.id).join(',') + ')');
  const title = favTitleHtml(env);
  check(/fls-name">Favorites</.test(title), 'title: the player\'s list');
  const autoRows = (title.match(/data-auto-id="auto:[a-z]+"/g) || []).map((s) => s.slice(14, -1));
  check(JSON.stringify(autoRows) === JSON.stringify(['auto:flips', 'auto:greens', 'auto:blues', 'auto:epics']),
    'dropdown: the four auto lists under the player\'s ' + JSON.stringify(autoRows));
  check(/fls-menu-head">Auto lists</.test(title) && title.indexOf('data-list-id="fl-x"') < title.indexOf('Auto lists'),
    'dropdown: an "Auto lists" heading after the player\'s lists');
  check(!/data-auto-id="auto:[a-z]+"[^>]*>[^<]*<\/span>\s*<button/.test(title), 'auto rows have no rename or delete');

  for (const [id, quality, name] of [['auto:greens', 'UNCOMMON', 'Greens (auto)'], ['auto:blues', 'RARE', 'Blues (auto)'], ['auto:epics', 'EPIC', 'Epics (auto)']]) {
    env.ctx.showAutoFavList(id);
    await tick(10);
    const want = expectedQualityIds(quality);
    const got = favRows(env).map((r) => r.id);
    check(JSON.stringify(got) === JSON.stringify(want), name + ': most gold traded a day first ' + JSON.stringify(got) + ' want ' + JSON.stringify(want));
    const t = favTitleHtml(env);
    check(new RegExp('fls-name">' + name.replace(/[()]/g, '\\$&') + '<').test(t), name + ': shown as the list name');
    check(new RegExp('fls-auto active">\\s*<span class="fls-menu-name" data-auto-id="' + id + '"').test(t)
      && !/fls-menu-row active">/.test(t), name + ': marked in the dropdown, no player list marked');
    const h = favHtml(env);
    check(/class="wl-auto-hint">[^<]*Updates by itself\. Star an item to keep it in Favorites\.</.test(h), name + ': hint says it updates by itself');
    check(!/data-fav-row|wl-fav-remove|wl-swipe-bg/.test(h), name + ': no drag, remove or swipe on its rows');
    if (!want.length) check(/No (greens|blues|epics) sold in the last day\./.test(text(h)), name + ': empty text ' + text(h).slice(0, 120));
    check(env.ls.ge_autoFavList === id && env.ls.ge_favoriteLists === before.lists && env.ls.ge_activeFavoriteList === before.active,
      name + ': only ge_autoFavList is written, the player\'s lists untouched');
    check(env.doc.getElementById('btnClearFavs').hidden === true, name + ': "Clear this list" hidden');
  }
  const greens = expectedQualityIds('UNCOMMON');
  check(greens.length >= 3 && greens.some((id) => mapping.find((m) => String(m.id) === id && m.base != null)),
    'greens include versions ("of the Bear"), not their lumped item (' + greens.join(',') + ')');
  check(!greens.includes(String(cloak.id)), 'the lumped Archer\'s Cloak is not in Greens');

  env.ctx.showAutoFavList('auto:flips');
  for (let i = 0; i < 40; i++) { await tick(5); if (!env.run('autoFlipsBusy')) break; }
  await tick(10);
  const wantF = expectedFlipIds();
  const gotF = favRows(env).map((r) => r.id);
  check(wantF.length > 0 && JSON.stringify(gotF) === JSON.stringify(wantF), 'Flip picks: flips.json order, one row per item ' + JSON.stringify(gotF) + ' want ' + JSON.stringify(wantF));
  check(env.fetched.filter((u) => /flips\.json$/.test(u)).length === 1, 'flips.json read once');
  env.ctx.renderWatchlist();
  await tick(10);
  check(env.fetched.filter((u) => /flips\.json$/.test(u)).length === 1, 'a re-render within 4 minutes does not read flips.json again');
  env.run('autoFlipsAt = Date.now() - AUTO_FLIPS_TTL_MS - 1');
  env.ctx.renderWatchlist();
  for (let i = 0; i < 40; i++) { await tick(5); if (!env.run('autoFlipsBusy')) break; }
  check(env.fetched.filter((u) => /flips\.json$/.test(u)).length === 2, 'the 5-minute refresh reads flips.json again once it is old');

  /* back to the player's list (the one that was active all along) */
  env.ctx.switchFavoriteList('default');
  await tick(10);
  check(favRows(env).length === 3 && favRows(env).every((r) => /data-fav-row="1"/.test(r.attrs)) && /fls-name">Favorites</.test(favTitleHtml(env)),
    'picking the player\'s active list leaves the auto list');
  check(!('ge_autoFavList' in env.ls) && env.ls.ge_favoriteLists === before.lists && env.ls.ge_activeFavoriteList === before.active,
    'auto choice forgotten, the player\'s lists exactly as they were');
  check(env.doc.getElementById('btnClearFavs').hidden === false, '"Clear this list" back for the player\'s list');
  check(env.errors.length === 0, 'no errors: ' + env.errors.slice(0, 3).join(' | '));

  /* a reload remembers the auto list; the star still saves to the player's list */
  const env2 = await boot({ storage: { ge_favoriteLists: savedLists, ge_activeFavoriteList: 'fl-x', ge_autoFavList: 'auto:greens', ge_lastItem: String(blade.id) } });
  check(/fls-name">Greens \(auto\)</.test(favTitleHtml(env2)) && /keep it in Long holds\./.test(favHtml(env2)), 'reload: Greens (auto) still showing, hint names the active list');
  env2.doc.getElementById('btnFav').onclick();
  await tick(10);
  const lists2 = JSON.parse(env2.ls.ge_favoriteLists);
  check(lists2[1].items.includes(String(blade.id)) && lists2[0].items.length === 3 && /fls-name">Greens \(auto\)</.test(favTitleHtml(env2)),
    'the star saves to the player\'s active list, the auto list stays on show');
  env2.ctx.createFavoriteListLocal('New one');
  await tick(10);
  check(/fls-name">New one</.test(favTitleHtml(env2)) && !('ge_autoFavList' in env2.ls), 'a new list leaves the auto list');
  check(env2.errors.length === 0, 'reload: no errors: ' + env2.errors.slice(0, 3).join(' | '));

  /* a page with no details files at all (data from before item details): hidden, no errors */
  const env3 = await boot({ storage: { ge_lastItem: String(blade.id) }, files: { 'details/17.json': null } });
  check(env3.host.hidden && env3.host.innerHTML === '' && env3.errors.length === 0, 'no details file: block hidden, no errors');

  /* ---- the pure helpers on hand-made data ---- */
  const ids = (list, rows, latest, vols, lumped, flips) =>
    env.ctx.autoFavIds(env.run('AUTO_FAV_LISTS').find((l) => l.id === list), rows, latest, (id) => vols[id] || 0, (id) => lumped.has(String(id)), flips);
  const rows = [];
  const latest = {}, vols = {};
  for (let i = 1; i <= 40; i++) {
    rows.push({ id: 100 + i, name: 'G' + i, quality: 'UNCOMMON' });
    latest[100 + i] = { high: 1000 * i, low: 900 * i }; vols[100 + i] = i % 3;   // some sold 0
  }
  rows.push({ id: 900, name: 'Blue', quality: 'RARE' }, { id: 901, name: 'Unknown item #901', quality: 'UNCOMMON', unknownName: true },
    { id: 902, name: 'Lumped', quality: 'UNCOMMON' }, { id: 902001, base: 902, name: 'Lumped of the Bear', quality: 'UNCOMMON' },
    { id: 903, name: 'Tie A', quality: 'UNCOMMON' }, { id: 904, name: 'Tie B', quality: 'UNCOMMON' }, { id: 905, name: 'Low only', quality: 'EPIC' });
  Object.assign(latest, { 900: { high: 5 }, 901: { high: 1e9 }, 902: { high: 1e9 }, 902001: { high: 4e6 }, 903: { high: 2e6 }, 904: { high: 1e6 }, 905: { high: 0, low: 70 } });
  Object.assign(vols, { 900: 1, 901: 5, 902: 5, 902001: 5, 903: 1, 904: 2, 905: 3 });
  const g = ids('auto:greens', rows, latest, vols, new Set(['902']), null);
  check(g.length === 25 && g[0] === '902001' && g.indexOf('904') < g.indexOf('903') && !g.includes('901') && !g.includes('902') && !g.includes('900'),
    'greens: max 25, a version in, its lumped item and unknown names out, a tie on gold goes to the one sold more ' + g.slice(0, 6).join(','));
  check(g.every((id) => !['103', '106', '109'].includes(id)), 'greens: nothing that sold 0');
  check(JSON.stringify(ids('auto:epics', rows, latest, vols, new Set(), null)) === '["905"]', 'epics: the cheapest listing when there is no market value');
  check(JSON.stringify(ids('auto:blues', rows, latest, vols, new Set(), null)) === '["900"]', 'blues');
  const fl = [{ id: 903 }, { id: 902001 }, { id: 903 }, { id: 77777 }, { id: null }, { id: 904 }];
  for (let i = 1; i <= 40; i++) fl.push({ id: 100 + i });
  const f = ids('auto:flips', rows, latest, vols, new Set(), fl);
  check(f.length === 25 && JSON.stringify(f.slice(0, 3)) === '["903","902001","904"]', 'flips: best first, one row per item, only listed items, max 25 ' + f.slice(0, 4).join(','));
  check(JSON.stringify(ids('auto:flips', rows, latest, vols, new Set(), null)) === '[]', 'flips: none loaded yet');

  const L = (m, chunk) => env.ctx.wowDetailLines(m, chunk);
  const ch = { 5: [['Binds when equipped', 'w'], ['Chest', 'w', 'Mail'], ['208 Armor', 'w'], ['+10 Strength', 'w'], ['+7 Stamina', 'x'],
    ['Durability 105 / 110', 'w'], ['Requires Level 22', 'w'], null, ['', 'w'], [7, 'w'], ['"Old."', 'y', '']] };
  const one = L({ id: 5, name: 'Shirt', highalch: 12345 }, ch);
  check(JSON.stringify(one) === JSON.stringify([['Binds when equipped', 'w'], ['Chest', 'w', 'Mail'], ['208 Armor', 'w'], ['+10 Strength', 'w'],
    ['+7 Stamina', 'w'], ['Durability 105 / 110', 'w'], ['Requires Level 22', 'w'], ['"Old."', 'y'], ['Sell Price: ' + env.run('moneyExact(12345)'), 'w']]),
  'lines: junk skipped, unknown colour is white, sell price last ' + JSON.stringify(one));
  env.run('mapping.push({ id: 5, name: "Shirt" }, { id: 5001, base: 5, variant: "12722", name: "Shirt of the Bear" }, { id: 5002, base: 5, variant: "99001", name: "Shirt (version 2)" }); wowIndexVersions();');
  const v = L({ id: 5001, base: 5, name: 'Shirt of the Bear', highalch: 0 }, ch);
  check(v[4][0] === '+7 Stamina' && v[5][0] === 'Random enchantment: of the Bear' && v[5][1] === 'g' && v[6][0] === 'Durability 105 / 110',
    'version: green bonus line right after the last stat line ' + JSON.stringify(v));
  check(!v.some((l) => /^Sell Price/.test(l[0])), 'no sell price line when a vendor pays nothing');
  const v2 = L({ id: 5002, base: 5, name: 'Shirt (version 2)' }, ch);
  check(v2.some((l) => l[0] === 'Random enchantment: version 2'), 'version without a known suffix: "version 2"');
  const lumpedLines = L({ id: 5, name: 'Shirt' }, ch);
  check(lumpedLines[5][0] === '<Random enchantment>', 'item made of versions: <Random enchantment> with the stats');
  const noStats = L({ id: 5001, base: 5, name: 'Shirt of the Bear' }, { 5: [['Binds when equipped', 'w'], ['Finger', 'w', 'x'], ['Requires Level 3', 'w']] });
  check(noStats[2][0] === 'Random enchantment: of the Bear', 'no stat lines: after the slot line');
  const spell = L({ id: 5001, base: 5, name: 'Shirt of the Bear' }, { 5: [['One-Hand', 'w', 'Sword'], ['10 - 20 Damage', 'w', 'Speed 2.00'],
    ['+3 Agility', 'w'], ['Requires Level 9', 'w'], ['Chance on hit: Deals 18 Fire Damage.', 'g']] });
  check(spell[3][0] === 'Random enchantment: of the Bear' && spell[5][0].indexOf('Chance on hit') === 0,
    'a green line naming damage does not move the bonus line ' + JSON.stringify(spell));
  check(JSON.stringify(L({ id: 6, name: 'X' }, ch)) === '[]' && JSON.stringify(L({ id: 5, name: 'X' }, null)) === '[]'
    && JSON.stringify(L({ id: 7, name: 'X' }, { 7: [] })) === '[]' && JSON.stringify(L({ id: 8, name: 'X' }, { 8: 'x' })) === '[]',
  'no entry, no chunk, [] or junk: nothing');
  const many = { 9: Array.from({ length: 90 }, (_, i) => ['L' + i, 'w']) };
  check(L({ id: 9, name: 'X' }, many).length === 60, 'at most 60 lines');
  const htm = env.ctx.wowDetailsHtml([['<b>&"', 'g'], ['Main Hand', 'w', 'Sword'], ['x', 'evil']]);
  check(htm === '<div class="idt-lines"><div class="idt-line idt-g">&lt;b&gt;&amp;&quot;</div><div class="idt-line idt-w idt-pair"><span>Main Hand</span>'
    + '<span class="idt-right">Sword</span></div><div class="idt-line idt-w">x</div></div>', 'html: escaped, right-hand part, safe colour class ' + htm);
  check(env.ctx.wowDetailsChunkId(9860) === 9 && env.ctx.wowDetailsChunkId(999) === 0 && env.ctx.wowDetailsChunkId(288206) === 288
    && env.ctx.wowDetailsChunkId('x') === null, 'chunk numbers');

  /* ---- markup and styles ---- */
  const idx = fs.readFileSync(path.join(WOW, 'index.html'), 'utf8');
  const at = idx.indexOf('<section class="item-details" id="itemDetails"');
  check(at > idx.indexOf('id="timeToolbar"') && at < idx.indexOf('<div class="price-strip">') && /id="itemDetails" aria-label="Item details" hidden><\/section>/.test(idx),
    'index.html: #itemDetails (hidden) between the name row and the Target boxes');
  const css = fs.readFileSync(path.join(WOW, 'app.css'), 'utf8');
  check(/\.item-details\[hidden\] \{ display: none; \}/.test(css) && /\.idt-lines \{ column-width: 190px;/.test(css)
    && /max-width: 640px\) \{\s*\.item-details/.test(css) && /pointer: coarse\) \{\s*\/\*[^*]*\*\/\s*\.item-details/.test(css),
  'app.css: hidden rule, columns, phone and landscape-phone sizes');
  check(/--tip-g: #1EFF00/.test(css) && /\.col-menu-danger\[hidden\], \.col-menu-sep\[hidden\]/.test(css) && /\.wl-auto-hint/.test(css),
    'app.css: tooltip colours, auto list styles');
  /* The box's row cap counts its padding AND its 1px border (box-sizing is
     border-box), so a block of exactly that many rows has no scrollbar; no
     overscroll-behavior, so a wheel or swipe over a box at its end moves the
     page on; the desktop chart gives the shown block's height back. */
  const cssBare = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const tipRules = [...cssBare.matchAll(/\.item-details \{([^}]*)\}/g)].map((x) => x[1]);
  const decl = (body, prop) => { const x = new RegExp('(?:^|;)\\s*' + prop + '\\s*:\\s*([^;]+)').exec(body); return x ? x[1].trim() : null; };
  check(tipRules.length === 3, 'app.css: three .item-details rules (desktop, phone, landscape phone): ' + tipRules.length);
  const tipBase = tipRules[0] || '';
  const tipBorder = parseFloat((/(?:^|;)\s*border\s*:\s*([\d.]+)px/.exec(tipBase) || [])[1]);
  check(tipBorder === 1, 'app.css: the box has a 1px border (' + tipBorder + ')');
  for (const [i, label, rows] of [[0, 'desktop', 6], [1, 'phone', 7], [2, 'landscape phone', 3]]) {
    const b = tipRules[i] || '';
    const fs = parseFloat(decl(b, 'font-size'));
    const lh = parseFloat(decl(b, 'line-height') || decl(tipBase, 'line-height'));
    const pad = (decl(b, 'padding') || '').split(/\s+/).map(parseFloat);
    const pt = pad[0], pb = pad.length >= 3 ? pad[2] : pad[0];
    const mh = /^calc\(\s*([\d.]+)em\s*\*\s*(\d+)\s*\+\s*([\d.]+)px\s*\)$/.exec(decl(b, 'max-height') || '');
    const room = mh ? Number(mh[1]) * fs * Number(mh[2]) + Number(mh[3]) - pt - pb - 2 * tipBorder : NaN;
    check(!!mh && Number(mh[2]) === rows && Number(mh[1]) === lh && room >= rows * lh * fs - 1e-6 && room < (rows + 0.5) * lh * fs,
      'app.css ' + label + ': ' + rows + ' rows of ' + (lh * fs).toFixed(2) + 'px fit inside the padding and border (room ' + room.toFixed(2) + 'px)');
    check(!/overscroll-behavior/.test(b), 'app.css ' + label + ': no overscroll-behavior on the box');
  }
  const cssFlat = cssBare.replace(/\s+/g, ' ');
  const chartRules = [...cssFlat.matchAll(/\.chart-container \{ min-height: calc\(100dvh - (\d+)px\); max-height: calc\(100dvh - (\d+)px\); \}/g)];
  check(chartRules.length === 2, 'app.css: the two desktop chart heights (' + chartRules.length + ')');
  for (const [whole, lo, hi] of chartRules) {
    const want = '.main-column:has(> #itemDetails:not([hidden])) > .chart-container:not(.user-sized) {'
      + ' min-height: max(min(200px, calc(100dvh - ' + lo + 'px)), calc(100dvh - ' + lo + 'px - var(--idt-h, 0px)));'
      + ' max-height: max(min(200px, calc(100dvh - ' + hi + 'px)), calc(100dvh - ' + hi + 'px - var(--idt-h, 0px))); }';
    const from = cssFlat.indexOf(whole), at = cssFlat.indexOf(want, from);
    check(from > 0 && at > from && cssFlat.slice(from, at).indexOf('@media') < 0 && cssFlat.slice(from, at).indexOf('}', whole.length) < 0,
      'app.css: right after the ' + lo + '/' + hi + ' desktop chart, the chart gives the shown details block its height back (--idt-h), not below 200px');
  }

  /* ---- item names are escaped wherever a list writes them (real names have
     quotes: '"Mage-Eye" Blunderbuss', 'BKP "Sparrow" Smallbore') ---- */
  const odd = 'Tom\'s "Odd" <b>Bold</b> & Co';
  const oddEsc = 'Tom&#39;s &quot;Odd&quot; &lt;b&gt;Bold&lt;/b&gt; &amp; Co';
  const greenIds = expectedQualityIds('UNCOMMON');
  const oddItem = mapping.find((m) => greenIds.includes(String(m.id)) && m.base == null) || {};
  check(oddItem.id != null, 'a green item (not a version) to rename');
  const env4 = await boot({
    files: { 'mapping.json': JSON.stringify(mapping.map((m) => (m.id === oddItem.id ? Object.assign({}, m, { name: odd }) : m))) },
    storage: { ge_favoriteLists: JSON.stringify([{ id: 'default', name: 'Favorites', items: [String(oddItem.id)] }]),
      ge_activeFavoriteList: 'default', ge_lastItem: String(blade.id) }
  });
  const own4 = favHtml(env4);
  check(own4.includes('<span class="wl-item-name">' + oddEsc + '</span>') && own4.includes('alt="' + oddEsc + ' icon"')
    && own4.includes('aria-label="Remove ' + oddEsc + ' from favorites"') && !own4.includes(odd),
  'the player\'s list: the name escaped in the row, its icon and its remove button');
  env4.ctx.showAutoFavList('auto:greens');
  await tick(10);
  const auto4 = favHtml(env4);
  check(favRows(env4).some((r) => r.id === String(oddItem.id)) && auto4.includes('<span class="wl-item-name">' + oddEsc + '</span>')
    && auto4.includes('alt="' + oddEsc + ' icon"') && !auto4.includes(odd), 'Greens (auto): the name escaped');
  const query = env4.doc.getElementById('query');
  query.value = 'bold';
  (query.handlers.input || []).forEach((f) => f());
  const sug = env4.doc.getElementById('suggestBox').innerHTML;
  check((query.handlers.input || []).length > 0 && sug.includes('alt="' + oddEsc + ' icon">' + oddEsc + '</div>') && !sug.includes(odd),
    'search suggestions: the name escaped');
  check(env4.errors.length === 0, 'odd names: no errors ' + env4.errors.slice(0, 3).join(' | '));
  const rawNames = APP_SRC.match(/(?:>\s*|="[^"]*)\$\{\s*[\w.]+\.name\s*\}/g) || [];
  check(rawNames.length === 0, 'app.js: no item name put into HTML unescaped ' + JSON.stringify(rawNames.slice(0, 4)));

  /* ===== 1D targets after a drop that lasts (settledSellCap, the addon's settled rule) ===== */
  // Tender Wolf Steak (18045): the real hourly rows [time, cheapest, market, listed, sold] of the
  // 2026-11-18 data. 17s 74c on Tuesday evening, then 3s 95c to 4s 98c since Wednesday 8AM CST.
  const STEAK_H = [[1794952800, 460, 460, 432, 0], [1794956400, 469, 469, 223, 107], [1794960000, 1774, 1774, 459, 150],
    [1794963600, 1190, 1200, 1915, 3], [1794967200, 1187, 1188, 1755, 175], [1794970800, 1098, 1186, 1728, 61],
    [1794974400, 999, 1059, 2069, 265], [1794985200, 809, 829, 3035, 0], [1795006800, 697, 699, 3043, 0],
    [1795010400, 455, 645, 2652, 142], [1795014000, 455, 498, 2488, 98], [1795017600, 454, 455, 2437, 160],
    [1795021200, 448, 453, 2747, 359], [1795024800, 447, 448, 2877, 155], [1795028400, 446, 447, 3290, 257],
    [1795032000, 444, 445, 3371, 64], [1795035600, 443, 445, 2828, 136], [1795039200, 379, 395, 2896, 292],
    [1795042800, 399, 418, 2839, 200], [1795046400, 398, 399, 3131, 150]];
  const STEAK_D = [[1794873600, 464, 464, 327, 107], [1794960000, 713, 740, 2495, 2517], [1795046400, 398, 399, 3131, 150]];
  const envS = await boot({});
  let haveCap = false;
  try { haveCap = envS.run('typeof settledSellCap') === 'function'; } catch (e) {}
  check(haveCap, 'app.js has settledSellCap');
  envS.run(`globalThis.__capOf = (h) => settledSellCap(filterSeries(seriesFromTS({ data: h.map(wowPoint) }), getPeriod('1d')));
    globalThis.__day = (h, d, n) => {
      const ts = (pts) => ({ data: pts.map(wowPoint) });
      const first = h.length ? h[0][0] : Infinity;
      currentItemSrc = { hist: emptySeries(), ts5m: seriesFromTS(ts(h.slice(-48))), ts1h: seriesFromTS(ts(h.slice(-365))),
        ts6h: seriesFromTS(ts(wowBucket(d.filter((p) => p[0] < first), 21600).concat(wowBucket(h, 21600)).slice(-365))),
        ts24h: seriesFromTS(ts(d.slice(-365))) };
      view = '1d'; currentItemLowVol = false;
      liveBuyRaw = h[h.length - 1][1]; liveSellRaw = h[h.length - 1][2];
      currentSeries = filterSeries(seriesForView('1d', currentItemSrc, false), getPeriod('1d'));
      const t = computeViewTargets(n === undefined ? { low: liveBuyRaw, high: liveSellRaw } : { low: liveBuyRaw, high: liveSellRaw, n }, 18045);
      return { buy: t.buy, sell: t.sell };
    };`);
  const capOf = (h) => { try { return envS.ctx.__capOf(h); } catch (e) { return 'error: ' + e.message; } };
  const dayAt = (T) => {
    try { return envS.ctx.__day(STEAK_H.filter((p) => p[0] <= T), STEAK_D.filter((p) => p[0] <= T)); } catch (e) { return { sell: 'error: ' + e.message }; }
  };
  // 6PM CST: the last 6 hours (4s 47c, 4s 45c, 4s 45c, 3s 95c, 4s 18c, 3s 99c) are under 80% of the day's
  // 70th percentile, so the most to ask is 4s 47c; the live spread is real again, the engine's 4s 18c stands
  check(capOf(STEAK_H) === 447, 'steak 6PM: the settled cap is 4s 47c (' + capOf(STEAK_H) + ')');
  check(dayAt(1795046400).sell === 418, 'steak 6PM: 1D Target Sell is the engine\'s 4s 18c (' + JSON.stringify(dayAt(1795046400)) + ')');
  // 1PM: the day's high hours (17s 74c to 8s 29c) gave a Target Sell of 10s 59c; the last 6 hours top out at 6s 45c
  check(dayAt(1795028400).sell === 645, 'steak 1PM: 1D Target Sell 6s 45c, not 10s 59c (' + JSON.stringify(dayAt(1795028400)) + ')');
  // 3PM: capped at 4s 55c the day pair no longer beats the 5% cut: the engine's honest 4s 45c (was 8s 29c)
  check(dayAt(1795035600).sell === 445, 'steak 3PM: 1D Target Sell 4s 45c, not 8s 29c (' + JSON.stringify(dayAt(1795035600)) + ')');
  // 11AM: the drop is only 3 hours old (7AM 6s 99c is in the last 6 hours): the cap waits
  check(dayAt(1795021200).sell === 699, 'steak 11AM: a 3-hour-old drop waits at 6s 99c (' + JSON.stringify(dayAt(1795021200)) + ')');
  // hand-made hourly markets: a real evening premium (+20% for 8 hours a day, now 6 morning hours) keeps its upside,
  // +25% is exactly at 80%: capped; a one-hour dip and 3 prices in 6 hours never cap
  const T0 = 1795046400;
  const hourRows = (mkts) => mkts.map((m, i) => [T0 - (mkts.length - 1 - i) * 3600, m == null ? 0 : m - 1, m == null ? 0 : m, 500, 5]);
  const eve = (b, e) => [].concat(Array(16).fill(b), Array(8).fill(e));
  check(capOf(hourRows(eve(100, 120).concat(eve(100, 120)).slice(6).concat(Array(6).fill(100)))) === 0,
    'evening +20%: no cap in the morning');
  check(capOf(hourRows(eve(100, 125).concat(eve(100, 125)).slice(6).concat(Array(6).fill(100)))) === 100,
    'evening +25%: at exactly 80% the cap applies');
  check(capOf(hourRows(Array(23).fill(1000).concat([500]))) === 0, 'a one-hour dip: no cap');
  check(capOf(hourRows(Array(18).fill(1000).concat(Array(6).fill(500)))) === 500, '6 hours at 500: capped at 500');
  check(capOf(hourRows(Array(18).fill(1000).concat([400, null, null, null, 400, 400]))) === 0, '3 prices in 6 hours: no cap');
  check(capOf(hourRows(Array(18).fill(100).concat(Array(6).fill(300)))) === 0, 'a rise: no cap');
  // nothing listed now (latest.json n 0; it keeps the last price): no cheaper listing holds the old
  // price back, so the day pair is not capped, as in the addon. One auction at 3g 39s, then one at
  // 1g 68s 74c for 6 hours: the day pair is 1g 68s 73c / 3g 39s; capped it fell back to the engine's
  // 1g 68s 74c. With the auction still listed (n 1) the cap applies as before.
  {
    const gone = hourRows(Array(16).fill(33900).concat(Array(6).fill(16874)));
    const T = gone[gone.length - 1][0];
    const gd = [[T - 86400 * 2, 33899, 33900, 1, 0], [T - 86400, 33899, 33900, 1, 0]];
    const at = (n) => { try { return envS.ctx.__day(gone, gd, n); } catch (e) { return { sell: 'error: ' + e.message }; } };
    check(at(0).sell === 33900, 'nothing listed now: 1D Target Sell 3g 39s, no cap (' + JSON.stringify(at(0)) + ')');
    check(at(1).sell === 16874, 'one auction listed: capped as before (' + JSON.stringify(at(1)) + ')');
  }
  check(envS.errors.length === 0, 'settled cap: no errors ' + envS.errors.slice(0, 3).join(' | '));

  /* house rules: plain ASCII in what the new code writes and in the code itself, no long dashes */
  env.ctx.showAutoFavList('auto:epics');
  await tick(10);
  const ours = [host.innerHTML, JSON.stringify(env.run('AUTO_FAV_LISTS')), env.run("autoFavEmptyText(AUTO_FAV_LISTS[0])"),
    (favHtml(env).match(/<div class="wl-auto-hint">[^<]*<\/div>/) || [''])[0],
    (favTitleHtml(env).match(/<div class="fls-menu-sep"[\s\S]*$/) || [''])[0],
    (favHtml(env).match(/No epics sold in the last day\./) || [''])[0]].join('\n');
  check(ours.length > 300 && /^[\x09\x0a\x0d\x20-\x7e]*$/.test(ours), 'the new text on the page is plain ASCII (no long dashes)');
  const a0 = APP_SRC.indexOf('/* -- Auto lists'), a1 = APP_SRC.indexOf('/* Drag-to-reorder favorites');
  const d0 = APP_SRC.indexOf('/* -- Item details');
  const dm = /\r?\n\}\r?\n/.exec(APP_SRC.slice(APP_SRC.indexOf('function renderItemDetails')));
  const d1 = dm ? APP_SRC.indexOf('function renderItemDetails') + dm.index : -1;
  const newCode = APP_SRC.slice(a0, a1) + APP_SRC.slice(d0, d1);
  check(a0 > 0 && a1 > a0 && d0 > 0 && d1 > d0 && d1 - d0 < 8000 && /^[\x09\x0a\x0d\x20-\x7e]*$/.test(newCode),
    'the new app.js sections are plain ASCII');

  console.log(passes + ' passed, ' + fails + ' failed');
  process.exit(fails ? 1 : 0);
})().catch((e) => { console.log('ERROR: ' + (e && e.stack || e)); process.exit(2); });
