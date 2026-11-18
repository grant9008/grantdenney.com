/* Browse and Flip Finder.
   An auction-house style window over the collector's files: Browse lists
   every item with in-game style filters (search, category tree, rarity,
   required level, green versions), and Flip Finder lists flips.json, the
   flips found in the latest snapshot. Clicking a row opens that item in the
   normal item view and chart.
   It only reads what app.js already loaded (mapping, latest, past24h,
   volumes) plus flips.json, and every newer field is optional: older data
   files just give fewer filters, and a missing flips.json gives a "starts
   with the next price update" note. */
(function () {
  'use strict';

  /* ---------- small helpers ---------- */
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function num(v) { var n = Number(v); return isFinite(n) ? n : 0; }
  function store(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
  function recall(k) { try { return JSON.parse(localStorage.getItem(k)); } catch (e) { return null; } }
  function isTouch() { try { return window.matchMedia('(pointer: coarse)').matches; } catch (e) { return false; } }

  function appMapping() { return (typeof mapping !== 'undefined' && Array.isArray(mapping)) ? mapping : []; }
  function appLatest() { return (typeof latest !== 'undefined' && latest && latest.data) ? latest.data : {}; }
  function app24h() { return (typeof past24h !== 'undefined' && past24h && past24h.data) ? past24h.data : {}; }
  function appVolumes() { return (typeof volumes !== 'undefined' && volumes && volumes.data) ? volumes.data : null; }
  function dataBase() { return typeof WOW_DATA !== 'undefined' ? WOW_DATA : 'https://raw.githubusercontent.com/grant9008/grantdenney.com/wow-data'; }
  function getJSON(url) {
    if (typeof jget === 'function') return jget(url);
    return fetch(url).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); });
  }
  /* Copper to "12g 34s" (app.js moneyShort); empty values show "-". */
  function money(n, allowZero) {
    if (n == null || isNaN(n) || (!allowZero && !(n > 0))) return '-';
    try { if (typeof moneyShort === 'function') return moneyShort(n); } catch (e) {}
    return (Math.round(n) / 10000).toFixed(2) + 'g';
  }
  function moneyLong(n) {
    if (n == null || isNaN(n)) return '-';
    try { if (typeof moneyExact === 'function') return moneyExact(n); } catch (e) {}
    return money(n, true);
  }
  /* A price to type in game (Buy up to, Relist at, the listing to undercut):
     every coin, like the game shows it ("312g 50s 49c"). money() rounds to
     the nearest silver (whole gold from 100g) and moneyExact drops the
     copper from 100g, so a relist 1c under the next listing could show at
     or above that listing. Empty values show "-". */
  function price(n) {
    if (n == null || isNaN(n) || !(n > 0)) return '-';
    var a = Math.round(Number(n)), g = Math.floor(a / 10000), s = Math.floor((a % 10000) / 100), c = a % 100, p = [];
    if (g) p.push(g.toLocaleString() + 'g');
    if (s) p.push(s + 's');
    if (c) p.push(c + 'c');
    return p.join(' ');
  }
  function pct(roi) {
    if (roi == null || isNaN(roi)) return '-';
    var p = roi / 10;
    return (Math.abs(p) >= 100 ? Math.round(p).toLocaleString() : p.toFixed(1)) + '%';
  }
  function count(n) { return n == null ? '-' : Math.round(n).toLocaleString(); }
  function ago(t) {
    if (!t) return '';
    var m = Math.round((Date.now() / 1000 - t) / 60);
    if (m < 2) return 'just now';
    if (m < 90) return m + ' min ago';
    return Math.round(m / 60) + ' h ago';
  }

  /* ---------- categories, like the in-game auction house ---------- */
  var CLASS_ORDER = ['Weapon', 'Armor', 'Container', 'Consumable', 'Trade Goods', 'Projectile', 'Quiver',
    'Recipe', 'Reagent', 'Gem', 'Key', 'Miscellaneous', 'Quest', 'Other'];
  var SUB_ORDER = {
    'Weapon': ['One-Handed Axes', 'Two-Handed Axes', 'Axes', 'Bows', 'Guns', 'One-Handed Maces', 'Two-Handed Maces', 'Maces',
      'Polearms', 'One-Handed Swords', 'Two-Handed Swords', 'Swords', 'Staves', 'Fist Weapons', 'Miscellaneous', 'Daggers',
      'Thrown', 'Crossbows', 'Wands', 'Fishing Poles'],
    'Armor': ['Miscellaneous', 'Cloth', 'Leather', 'Mail', 'Plate', 'Shields', 'Librams', 'Idols', 'Totems'],
    'Consumable': ['Potions', 'Elixirs', 'Flasks', 'Food & Drink', 'Bandages', 'Scrolls', 'Item Enhancements', 'Explosives', 'Devices', 'Other'],
    'Trade Goods': ['Cloth', 'Leather', 'Metal & Stone', 'Herb', 'Elemental', 'Enchanting', 'Meat and Cooking', 'Parts',
      'Explosives', 'Devices', 'Other']
  };
  var SLOT_ORDER = ['Head', 'Neck', 'Shoulder', 'Back', 'Chest', 'Shirt', 'Tabard', 'Wrist', 'Hands', 'Waist', 'Legs', 'Feet',
    'Finger', 'Trinket', 'Off Hand', 'Held In Off-hand', 'Relic'];
  /* Blizzard calls one- and two-handed axes, maces and swords all "Axe",
     "Mace", "Sword"; the slot tells them apart, like the game's own list. */
  var SPLIT_HANDS = { 'Axe': 'Axes', 'Mace': 'Maces', 'Sword': 'Swords' };
  var SUB_ALIAS = {
    'Weapon': { 'Bow': 'Bows', 'Gun': 'Guns', 'Polearm': 'Polearms', 'Staff': 'Staves', 'Fist Weapon': 'Fist Weapons',
      'Dagger': 'Daggers', 'Crossbow': 'Crossbows', 'Wand': 'Wands', 'Fishing Pole': 'Fishing Poles', 'Spear': 'Spears' },
    'Armor': { 'Shield': 'Shields', 'Idol': 'Idols', 'Libram': 'Librams', 'Totem': 'Totems' },
    'Consumable': { 'Device': 'Devices', 'Permanent': 'Item Enhancements', 'Potion': 'Potions', 'Elixir': 'Elixirs', 'Flask': 'Flasks' },
    'Trade Goods': { 'Cooking': 'Meat and Cooking', 'Meat': 'Meat and Cooking' },
    'Container': { 'Bag': 'Bags' },
    'Projectile': { 'Arrow': 'Arrows', 'Bullet': 'Bullets' }
  };
  /* Slot names arrive as Blizzard's inventory type names ("Chest"); the
     type codes are accepted too, just in case. */
  var SLOT_CODES = { HEAD: 'Head', NECK: 'Neck', SHOULDER: 'Shoulder', BODY: 'Shirt', SHIRT: 'Shirt', CHEST: 'Chest', ROBE: 'Chest',
    WAIST: 'Waist', LEGS: 'Legs', FEET: 'Feet', WRIST: 'Wrist', HAND: 'Hands', HANDS: 'Hands', FINGER: 'Finger', TRINKET: 'Trinket',
    WEAPON: 'One-Hand', SHIELD: 'Off Hand', RANGED: 'Ranged', CLOAK: 'Back', TWOHWEAPON: 'Two-Hand', BAG: 'Bag', TABARD: 'Tabard',
    WEAPONMAINHAND: 'Main Hand', WEAPONOFFHAND: 'Off Hand', HOLDABLE: 'Held In Off-hand', AMMO: 'Ammo', THROWN: 'Thrown',
    RANGEDRIGHT: 'Ranged', QUIVER: 'Quiver', RELIC: 'Relic', NON_EQUIP: '' };

  function clsOf(o) {
    var c = (typeof o.clsName === 'string' && o.clsName) ? o.clsName : (typeof o.cls === 'string' ? o.cls : '');
    return c || 'Other';
  }
  function subOf(o) {
    return (typeof o.subName === 'string' && o.subName) ? o.subName : (typeof o.sub === 'string' ? o.sub : '');
  }
  function slotOf(o) {
    var s = typeof o.slot === 'string' ? o.slot.trim() : '';
    if (!s) return '';
    if (Object.prototype.hasOwnProperty.call(SLOT_CODES, s.toUpperCase()) && s === s.toUpperCase()) s = SLOT_CODES[s.toUpperCase()];
    if (s === 'Robe') s = 'Chest';
    if (/^non-?equip/i.test(s)) s = '';
    return s;
  }
  function hasCat(o) { return !!((typeof o.clsName === 'string' && o.clsName) || (typeof o.cls === 'string' && o.cls)); }
  function catOf(o) {
    var c = clsOf(o), s = subOf(o), t = slotOf(o);
    if (c === 'Weapon' && SPLIT_HANDS[s]) {
      s = t === 'Two-Hand' ? 'Two-Handed ' + SPLIT_HANDS[s] : (t ? 'One-Handed ' + SPLIT_HANDS[s] : SPLIT_HANDS[s]);
    } else if (SUB_ALIAS[c] && SUB_ALIAS[c][s]) {
      s = SUB_ALIAS[c][s];
    }
    if (s === c) s = '';
    return { c: c, s: s, t: c === 'Armor' ? t : '' };
  }
  function typeText(cat, o) {
    var slot = slotOf(o);
    if (cat.c === 'Armor') {
      if (cat.s === 'Shields') return 'Shield';
      if (cat.s === 'Miscellaneous') return slot || 'Armor';
      return [slot, cat.s].filter(Boolean).join(', ') || 'Armor';
    }
    if (cat.c === 'Weapon') return cat.s || 'Weapon';
    if (cat.c === 'Trade Goods') return cat.s ? (cat.s === 'Other' ? 'Trade Goods' : cat.s) : 'Trade Goods';
    if (cat.s && cat.s !== 'Other') return cat.s;
    return cat.c === 'Other' ? '' : cat.c;
  }
  function orderBy(list, order) {
    order = order || [];
    return list.slice().sort(function (a, b) {
      var ia = order.indexOf(a), ib = order.indexOf(b);
      if (ia < 0) ia = 999; if (ib < 0) ib = 999;
      return ia !== ib ? ia - ib : (a < b ? -1 : a > b ? 1 : 0);
    });
  }

  /* ---------- rarity ---------- */
  var RARITIES = [['POOR', 'Poor'], ['COMMON', 'Common'], ['UNCOMMON', 'Uncommon'], ['RARE', 'Rare'], ['EPIC', 'Epic']];
  function qClass(q) { return 'ahb-q-' + String(q || 'COMMON').toLowerCase(); }

  /* ---------- wowhead ---------- */
  function bonusIds(key) { return String(key || '').split(':').filter(function (p) { return /^\d+$/.test(p); }); }
  function wowheadUrl(base, key) {
    var b = bonusIds(key);
    return 'https://www.wowhead.com/classic/item=' + encodeURIComponent(base) + (b.length ? '?bonus=' + b.join(':') : '');
  }

  /* ---------- flip risk flags ---------- */
  var RISK = {
    thin: ['Few listings', 'Only a few listings sit above your relist price, so another seller can undercut you quickly.'],
    slow: ['Slow seller', 'Fewer of these sell in a day than you would be buying, or sales have been measured for less than a day. Expect to wait, or to relist.'],
    spike: ['Price spike', 'The relist price is far above what this item usually sells for (or far above what you pay, with only a few listings up there), so it may not sell at that price.'],
    suffix: ['Check version', 'This gear comes in versions (like "of the Bear") that sell for different prices. Buy the exact version shown.'],
    unsure: ['Unsure price', 'Most of the time only one of these is up for sale, so its usual price may just be one seller\'s asking price, not what it really sells for.']
  };
  function riskBadges(list) {
    return (list || []).map(function (r) {
      var k = String(r).trim().toLowerCase();
      if (!k) return '';
      var d = RISK[k] || [k, 'A warning from the price job about this flip.'];
      return '<span class="ahb-risk ahb-risk-' + esc(k) + '" tabindex="0" role="note" data-tip="' + esc(d[0] + ': ' + d[1]) +
        '" title="' + esc(d[1]) + '">' + esc(d[0]) + '</span>';
    }).join('');
  }

  /* ---------- flip kinds ---------- */
  /* [long label, short label (phones), what it means, extra class].
     gap: buy the cheap listings, relist them under the next one. vendor:
     listings under what a vendor pays. deal: a green, blue or epic item
     (gear or not) listed well under its usual price (its 7-day median). Its
     relist price is never above the usual price, and it is lower when
     another listing sits in between, so it can be far under it.
     A kind this page does not know shows as a relist flip. */
  var KINDS = {
    gap: ['Buy and relist', 'Relist', 'Buy the cheap listings, then list them again higher', ''],
    vendor: ['Sell to vendor', 'Vendor', 'Buy them, then sell them to any vendor', 'ahb-kind-v'],
    deal: ['Under usual price', 'Deal', 'Listed well under its usual price: buy it, then list it again at the Relist at price (never above its usual price)', 'ahb-kind-d']
  };
  function kindOf(k) { return KINDS[k] || KINDS.gap; }
  function kindBadge(x) {
    var k = kindOf(x.kind);
    return '<span class="ahb-kind' + (k[3] ? ' ' + k[3] : '') + '" title="' + esc(k[2]) + '"><span class="ahb-long">' + esc(k[0]) +
      '</span><span class="ahb-short">' + esc(k[1]) + '</span></span>';
  }
  /* How far the dearest listing bought sits under the usual price, in whole
     percent, rounded down so it never says more than it is (0 = unknown). */
  function underPct(x) {
    return x.usual > 0 && x.buyMax > 0 && x.buyMax < x.usual ? Math.floor((x.usual - x.buyMax) * 100 / x.usual) : 0;
  }
  /* A deal's discount, which is about the price you BUY at: "40% off its
     usual 2g" ("usual 2g" when it is not under; '' when the usual
     price is not known, as in flips from an older price job). The usual
     price shows every coin, like the relist price, so a relist at the usual
     price never shows above it. */
  function dealNote(x) {
    if (!(x.usual > 0)) return '';
    var p = underPct(x);
    return (p > 0 ? p + '% off its usual ' : 'usual ') + price(x.usual);
  }
  /* What to do, in words: "Under usual price: buy up to 1g 20s (40% off its
     usual 2g), relist at 1g 95s". The discount sits next to the buy
     price, the number it is about, not next to the relist price. */
  function flipAction(x) {
    var u = x.kind === 'deal' ? dealNote(x) : '';
    var t = kindOf(x.kind)[0] + ': buy up to ' + price(x.buyMax) + (u ? ' (' + u + ')' : '');
    if (x.kind !== 'vendor') t += ', relist at ' + price(x.relist);
    return t;
  }

  /* ---------- state ---------- */
  var saved = recall('ahb_state') || {};
  var S = {
    tab: saved.tab === 'flips' ? 'flips' : 'browse',
    cat: saved.cat && saved.cat.c ? { c: saved.cat.c, s: saved.cat.s || '', t: saved.cat.t || '' } : null,
    rar: saved.rar && typeof saved.rar === 'object' ? saved.rar : {},
    lvMin: saved.lvMin != null ? saved.lvMin : '',
    lvMax: saved.lvMax != null ? saved.lvMax : '',
    versions: saved.versions === 'split' ? 'split' : 'group',
    chip: ['all', 'deals', 'greens', 'mats', 'vendor'].indexOf(saved.chip) >= 0 ? saved.chip : 'all',
    sortB: saved.sortB && saved.sortB.k ? saved.sortB : { k: 'profit', d: -1 },
    sortF: saved.sortF && saved.sortF.k ? saved.sortF : { k: 'profit', d: -1 },
    q: '',
    limit: 100,
    open: {}
  };
  RARITIES.forEach(function (r) { if (S.rar[r[0]] === undefined) S.rar[r[0]] = true; });
  function persist() {
    store('ahb_state', { tab: S.tab, cat: S.cat, rar: S.rar, lvMin: S.lvMin, lvMax: S.lvMax, versions: S.versions,
      chip: S.chip, sortB: S.sortB, sortF: S.sortF });
  }

  /* ---------- data ---------- */
  var D = { ready: false, entries: [], byId: new Map(), versionsOf: new Map(), hasVersions: false, hasSlots: false,
    flips: null, flipState: 'loading', flipsUpdated: 0, bestFlip: new Map(), salesScale: 1 };

  function mkEntry(m) {
    var cat = catOf(m);
    var base = (m.base != null && String(m.base) !== String(m.id)) ? String(m.base) : null;
    return {
      m: m, id: String(m.id), base: base, variant: base ? String(m.variant || '') : '',
      name: String(m.name || ('Item #' + m.id)), nl: String(m.name || '').toLowerCase(),
      q: String(m.quality || 'COMMON').toUpperCase(), cat: cat, type: typeText(cat, m),
      req: num(m.reqlvl != null ? m.reqlvl : m.lvl)
    };
  }
  function buildIndex() {
    var icons = window._wowIconById || (window._wowIconById = {});
    D.byId = new Map(); D.entries = []; D.versionsOf = new Map(); D.hasVersions = false; D.hasSlots = false;
    appMapping().forEach(function (m) {
      if (!m || m.id == null) return;
      var e = mkEntry(m);
      D.byId.set(e.id, e); D.entries.push(e);
      if (e.cat.t) D.hasSlots = true;
    });
    D.entries.forEach(function (e) {
      if (!e.base) return;
      /* A version without its own icon uses its item's icon, here and in
         the item view. */
      if (!icons[e.id] && icons[e.base]) icons[e.id] = icons[e.base];
      var arr = D.versionsOf.get(e.base);
      if (!arr) { arr = []; D.versionsOf.set(e.base, arr); }
      arr.push(e);
      D.hasVersions = true;
      e.orphan = !D.byId.has(e.base);
    });
    D.versionsOf.forEach(function (arr) { arr.sort(function (a, b) { return num(a.m.vindex) - num(b.m.vindex); }); });
    D.ready = true;
    indexFlips();
  }

  function mkFlip(f) {
    var e = D.byId.get(String(f.id)) || null, m = e ? e.m : null;
    var src = hasCat(f) ? f : (m || f);
    var cat = catOf(src);
    var variant = String(f.variant || (m && m.variant) || '');
    var base = String(f.base != null ? f.base : (m && m.base != null ? m.base : f.id));
    var reqSrc = f.reqlvl != null ? f.reqlvl : (m ? (m.reqlvl != null ? m.reqlvl : m.lvl) : 0);
    var risk = Array.isArray(f.risk) ? f.risk : String(f.risk || '').split(',');
    risk = risk.map(function (r) { return String(r).trim(); }).filter(Boolean);
    var sold = f.sold24 != null ? Math.round(num(f.sold24) * D.salesScale) : null;
    var vols = appVolumes();
    if (vols && vols[String(f.id)] != null) sold = num(vols[String(f.id)]);
    return {
      f: f, id: String(f.id), base: base, variant: variant, isVersion: base !== String(f.id) || !!variant,
      name: String(f.name || (m && m.name) || ('Item #' + f.id)),
      nl: String(f.name || (m && m.name) || '').toLowerCase(),
      q: String(f.quality || (m && m.quality) || 'COMMON').toUpperCase(),
      cat: cat, type: typeText(cat, src), req: num(reqSrc),
      kind: f.kind === 'vendor' || f.kind === 'deal' ? f.kind : 'gap', risk: risk, sold: sold,
      buyMax: num(f.buyMax), units: num(f.units), cost: num(f.cost), relist: num(f.relist),
      usual: Math.max(0, Math.round(num(f.usual))),
      profit: num(f.profit), roi: f.roi != null ? num(f.roi) : (num(f.cost) > 0 ? Math.floor(num(f.profit) * 1000 / num(f.cost)) : 0)
    };
  }
  function indexFlips() {
    D.bestFlip = new Map();
    if (!Array.isArray(D.rawFlips)) { D.flips = null; return; }
    D.flips = D.rawFlips.filter(function (f) { return f && f.id != null; }).map(mkFlip);
    D.flips.forEach(function (x) {
      var b = D.bestFlip.get(x.id);
      if (!b || x.profit > b.profit) D.bestFlip.set(x.id, x);
    });
  }
  function loadFlips() {
    return getJSON(dataBase() + '/flips.json').then(function (j) {
      var list = j && Array.isArray(j.flips) ? j.flips : (Array.isArray(j) ? j : null);
      if (!list) throw new Error('no flips');
      D.rawFlips = list; D.flipsUpdated = num(j && j.updated); D.flipState = 'ok';
    }).catch(function () {
      if (!Array.isArray(D.rawFlips)) { D.rawFlips = null; D.flipState = 'missing'; }
    }).then(function () { if (D.ready) indexFlips(); });
  }
  function loadSalesScale() {
    if (typeof loadStatus !== 'function') return Promise.resolve();
    return Promise.resolve(loadStatus()).then(function (st) {
      var h = Math.floor(Number(st && st.salesHours));
      D.salesScale = (h >= 1 && h < 24) ? 24 / h : 1;
    }).catch(function () {});
  }

  /* Live numbers for one id, read fresh each render (latest refreshes). */
  function stats(id) {
    var l = appLatest()[id] || {}, d = app24h()[id] || {}, vols = appVolumes();
    var sold = null;
    if (vols) {
      try { sold = typeof dailyVolume === 'function' ? dailyVolume(id) : num(vols[id]); } catch (e) { sold = num(vols[id]); }
    }
    return {
      low: l.low > 0 ? l.low : null, high: l.high > 0 ? l.high : null,
      listed: d.listed != null ? num(d.listed) : null, n: l.n != null ? num(l.n) : null, sold: sold
    };
  }

  /* ---------- filtering ---------- */
  function qTerms() { return S.q.toLowerCase().split(/\s+/).filter(Boolean); }
  function lvBound(v) { var n = parseInt(v, 10); return isNaN(n) ? null : n; }
  function makeBasic() {
    var terms = qTerms(), lo = lvBound(S.lvMin), hi = lvBound(S.lvMax);
    return function (x) {
      if (S.rar[x.q] === false) return false;
      if (lo != null && x.req < lo) return false;
      if (hi != null && x.req > hi) return false;
      for (var i = 0; i < terms.length; i++) if (x.nl.indexOf(terms[i]) < 0) return false;
      return true;
    };
  }
  function passCat(cat) {
    if (!S.cat || !S.cat.c) return true;
    if (cat.c !== S.cat.c) return false;
    if (S.cat.s && cat.s !== S.cat.s) return false;
    if (S.cat.t && cat.t !== S.cat.t) return false;
    return true;
  }
  function isGreenGear(x) { return x.q === 'UNCOMMON' && (x.cat.c === 'Armor' || x.cat.c === 'Weapon'); }
  function isMaterial(x) { return x.cat.c === 'Trade Goods' || x.cat.c === 'Reagent' || (x.cat.c === 'Miscellaneous' && x.cat.s === 'Reagent'); }
  function passChip(x) {
    if (S.chip === 'deals') return x.kind === 'deal';
    if (S.chip === 'greens') return isGreenGear(x);
    if (S.chip === 'mats') return isMaterial(x);
    if (S.chip === 'vendor') return x.kind === 'vendor';
    return true;
  }

  /* Browse candidates before the category filter (the tree counts them). */
  function browseCandidates() {
    var basic = makeBasic(), out = [], searching = qTerms().length > 0;
    if (S.versions === 'split' || !D.hasVersions) {
      D.entries.forEach(function (e) { if (basic(e)) out.push({ e: e, kids: [], force: false }); });
      return out;
    }
    D.entries.forEach(function (e) {
      if (e.base && !e.orphan) return;          // listed under its item
      var vs = D.versionsOf.get(e.id) || [];
      var selfOk = basic(e);
      var kids = vs.filter(basic);
      if (!selfOk && !kids.length) return;
      out.push({ e: e, kids: kids, all: vs.length, force: !selfOk || (searching && kids.length > 0 && !nameHit(e)) });
    });
    return out;
  }
  function nameHit(e) { var t = qTerms(); return t.every(function (w) { return e.nl.indexOf(w) >= 0; }); }
  function flipCandidates() {
    if (!D.flips) return [];
    var basic = makeBasic();
    return D.flips.filter(function (x) { return basic(x) && passChip(x); });
  }

  /* ---------- DOM ---------- */
  var root, els = {};
  var ICON_BROWSE = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1"/>' +
    '<rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/>' +
    '<circle cx="17.5" cy="17.5" r="3"/><line x1="20" y1="20" x2="22" y2="22"/></svg>';

  function build() {
    root = document.createElement('div');
    root.id = 'ahBrowse';
    root.className = 'ahb';
    root.hidden = true;
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-modal', 'true');
    root.setAttribute('aria-label', 'Browse the auction house');
    root.innerHTML =
      '<div class="ahb-shell">' +
        '<div class="ahb-top">' +
          '<div class="ahb-tabs" role="tablist">' +
            '<button type="button" role="tab" class="ahb-tab" data-tab="browse">Browse</button>' +
            '<button type="button" role="tab" class="ahb-tab" data-tab="flips">Flip Finder <span class="ahb-tabn" id="ahbFlipN"></span></button>' +
          '</div>' +
          '<button type="button" class="ahb-close" id="ahbClose" aria-label="Close and go back to the chart" title="Close (Esc)">' +
            '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true">' +
            '<line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg></button>' +
        '</div>' +
        '<div class="ahb-body">' +
          '<aside class="ahb-filters" id="ahbFilters" aria-label="Filters">' +
            '<div class="ahb-fsec"><div class="ahb-flabel">Rarity</div><div class="ahb-rar" id="ahbRar"></div></div>' +
            '<div class="ahb-fsec"><div class="ahb-flabel">Required level</div>' +
              '<div class="ahb-lv"><input id="ahbLvMin" type="number" inputmode="numeric" min="0" max="80" placeholder="Min" aria-label="Lowest required level">' +
              '<span>to</span><input id="ahbLvMax" type="number" inputmode="numeric" min="0" max="80" placeholder="Max" aria-label="Highest required level"></div></div>' +
            '<div class="ahb-fsec" id="ahbVerSec"><div class="ahb-flabel">Green versions</div>' +
              '<div class="ahb-seg" role="group" aria-label="How to show green versions">' +
                '<button type="button" data-ver="group">Under their item</button><button type="button" data-ver="split">Each on its own row</button></div>' +
              '<div class="ahb-fnote">Versions are the same green with a different bonus, like "of the Bear".</div></div>' +
            '<div class="ahb-fsec"><div class="ahb-flabel">Category</div><ul class="ahb-tree" id="ahbTree"></ul></div>' +
            '<div class="ahb-fsec ahb-factions"><button type="button" class="ahb-clear" id="ahbClear">Clear filters</button>' +
              '<button type="button" class="ahb-done" id="ahbDone">Show results</button></div>' +
          '</aside>' +
          '<section class="ahb-main">' +
            '<div class="ahb-searchrow">' +
              '<input id="ahbQ" type="search" placeholder="Search items" autocomplete="off" spellcheck="false" aria-label="Search items">' +
              '<button type="button" class="ahb-fbtn" id="ahbFBtn" aria-expanded="false" aria-controls="ahbFilters">Filters <span id="ahbFCount"></span></button>' +
            '</div>' +
            '<div class="ahb-flipbar" id="ahbFlipBar"></div>' +
            '<div class="ahb-summary" id="ahbSummary"></div>' +
            '<div class="ahb-tablewrap" id="ahbWrap"><table class="ahb-table" id="ahbTable"></table>' +
              '<div class="ahb-empty" id="ahbEmpty" hidden></div>' +
              '<button type="button" class="ahb-more" id="ahbMore" hidden>Show more</button></div>' +
            '<div class="ahb-tip" id="ahbTip" role="status" hidden></div>' +
          '</section>' +
        '</div>' +
      '</div>';
    document.body.appendChild(root);
    ['ahbFilters', 'ahbRar', 'ahbLvMin', 'ahbLvMax', 'ahbVerSec', 'ahbTree', 'ahbClear', 'ahbDone', 'ahbQ', 'ahbFBtn',
      'ahbFCount', 'ahbFlipBar', 'ahbSummary', 'ahbWrap', 'ahbTable', 'ahbEmpty', 'ahbMore', 'ahbTip', 'ahbFlipN', 'ahbClose']
      .forEach(function (id) { els[id] = document.getElementById(id); });

    els.ahbRar.innerHTML = RARITIES.map(function (r) {
      return '<label class="ahb-rchk ' + qClass(r[0]) + '"><input type="checkbox" data-q="' + r[0] + '"><span>' + r[1] + '</span></label>';
    }).join('');
    els.ahbLvMin.value = S.lvMin; els.ahbLvMax.value = S.lvMax;
    wire();
  }

  function wire() {
    root.querySelectorAll('.ahb-tab').forEach(function (b) {
      b.addEventListener('click', function () { setTab(b.dataset.tab); });
    });
    els.ahbClose.addEventListener('click', close);
    var qTimer = 0;
    els.ahbQ.addEventListener('input', function () {
      clearTimeout(qTimer);
      qTimer = setTimeout(function () { S.q = els.ahbQ.value.trim(); S.limit = 100; render(); }, 140);
    });
    els.ahbRar.addEventListener('change', function (ev) {
      var q = ev.target && ev.target.dataset && ev.target.dataset.q;
      if (!q) return;
      S.rar[q] = !!ev.target.checked; changed();
    });
    function lv() { S.lvMin = els.ahbLvMin.value.trim(); S.lvMax = els.ahbLvMax.value.trim(); changed(); }
    els.ahbLvMin.addEventListener('input', lv);
    els.ahbLvMax.addEventListener('input', lv);
    root.querySelectorAll('[data-ver]').forEach(function (b) {
      b.addEventListener('click', function () { S.versions = b.dataset.ver; S.open = {}; changed(); });
    });
    els.ahbTree.addEventListener('click', function (ev) {
      var n = ev.target.closest('[data-c]');
      if (!n) return;
      var c = n.dataset.c, s = n.dataset.s || '', t = n.dataset.t || '';
      var cur = S.cat || { c: '', s: '', t: '' };
      if (!c) S.cat = null;
      else if (cur.c === c && cur.s === s && cur.t === t) {
        /* Clicking the selected node again steps back up one level. */
        S.cat = t ? { c: c, s: s, t: '' } : (s ? { c: c, s: '', t: '' } : null);
      } else S.cat = { c: c, s: s, t: t };
      changed();
    });
    els.ahbClear.addEventListener('click', function () {
      S.cat = null; S.lvMin = ''; S.lvMax = ''; S.chip = 'all'; S.q = ''; els.ahbQ.value = ''; S.open = {};
      RARITIES.forEach(function (r) { S.rar[r[0]] = true; });
      els.ahbLvMin.value = ''; els.ahbLvMax.value = '';
      changed();
    });
    els.ahbFBtn.addEventListener('click', function () { showFilters(!root.classList.contains('ahb-fopen')); });
    els.ahbDone.addEventListener('click', function () { showFilters(false); });
    els.ahbMore.addEventListener('click', function () { S.limit += 100; render(); });
    els.ahbFlipBar.addEventListener('click', function (ev) {
      var c = ev.target.closest('[data-chip]');
      if (!c) return;
      S.chip = c.dataset.chip; changed();
    });
    els.ahbFlipBar.addEventListener('toggle', function (ev) {
      if (ev.target && ev.target.classList && ev.target.classList.contains('ahb-how')) store('ahb_how', ev.target.open);
    }, true);
    els.ahbTable.addEventListener('click', onTableClick);
    els.ahbTable.addEventListener('keydown', function (ev) {
      if (ev.key !== 'Enter' && ev.key !== ' ') return;
      var th = ev.target.closest('th[data-k]');
      if (th) { ev.preventDefault(); sortBy(th.dataset.k); return; }
      var risk = ev.target.closest('.ahb-risk');
      if (risk) { ev.preventDefault(); tip(risk.dataset.tip); return; }
      var tr = ev.target.closest('tr[data-id]');
      if (tr && ev.target === tr) { ev.preventDefault(); openItem(tr.dataset.id); }
    });
  }

  function onTableClick(ev) {
    var th = ev.target.closest('th[data-k]');
    if (th) { sortBy(th.dataset.k); return; }
    if (ev.target.closest('a')) return;               // Wowhead links open on their own
    var tog = ev.target.closest('[data-tog]');
    if (tog) {
      S.open[tog.dataset.tog] = tog.getAttribute('aria-expanded') !== 'true';
      render(true);
      return;
    }
    var risk = ev.target.closest('.ahb-risk');
    if (risk) { tip(risk.dataset.tip); return; }
    var tr = ev.target.closest('tr[data-id]');
    if (tr) openItem(tr.dataset.id);
  }
  var tipTimer = 0;
  function tip(text) {
    if (!text) return;
    els.ahbTip.textContent = text;
    els.ahbTip.hidden = false;
    clearTimeout(tipTimer);
    tipTimer = setTimeout(function () { els.ahbTip.hidden = true; }, 6000);
  }
  function sortBy(k) {
    var st = S.tab === 'flips' ? S.sortF : S.sortB;
    if (st.k === k) st.d = -st.d;
    else { st.k = k; st.d = (k === 'name' || k === 'type' || k === 'kind') ? 1 : -1; }
    S.limit = 100;
    persist(); render();
  }
  function changed() { S.limit = 100; persist(); render(); }
  function setTab(t) {
    S.tab = t === 'flips' ? 'flips' : 'browse';
    S.limit = 100;
    persist(); render();
    els.ahbWrap.scrollTop = 0;
  }
  function showFilters(on) {
    root.classList.toggle('ahb-fopen', on);
    els.ahbFBtn.setAttribute('aria-expanded', on ? 'true' : 'false');
  }

  /* ---------- open / close ---------- */
  var lastFocus = null;
  function open(tab) {
    if (!root) build();
    if (tab) S.tab = tab === 'flips' ? 'flips' : 'browse';
    lastFocus = document.activeElement;
    root.hidden = false;
    document.documentElement.classList.add('ahb-lock');
    showFilters(false);
    render();
    if (!D.ready) waitForData();
    D.flipsWanted = true;
    if (D.flipState !== 'ok') loadFlips().then(function () { if (!root.hidden) render(true); });
    if (!isTouch()) setTimeout(function () { try { els.ahbQ.focus(); } catch (e) {} }, 30);
  }
  function close() {
    if (!root || root.hidden) return;
    root.hidden = true;
    document.documentElement.classList.remove('ahb-lock');
    els.ahbTip.hidden = true;
    try { if (lastFocus && lastFocus.focus) lastFocus.focus(); } catch (e) {}
  }
  /* A flip on a version first listed after this page loaded its price list
     (flips.json is read again every few minutes, the list is not): make the
     version's entry from the flip itself, so the version opens, never the
     whole item with every version's prices mixed. Its price history file
     exists for every version a flip is about. */
  function entryFromFlip(x) {
    var f = x.f || {}, be = D.byId.get(x.base), bm = be ? be.m : {};
    var m = {
      id: Number(x.id), name: x.name, base: Number(x.base), variant: x.variant,
      vindex: num(f.vindex) || (Number(x.id) % 1000), examine: f.subName || f.sub || bm.examine || '',
      members: true, value: num(bm.value), lowalch: num(bm.lowalch), highalch: num(bm.highalch), limit: null,
      icon: bm.icon || '', lvl: num(f.reqlvl != null ? f.reqlvl : bm.lvl), quality: x.q,
      cls: f.cls != null ? f.cls : (bm.cls || ''), sub: f.sub != null ? f.sub : (bm.sub || ''),
      clsName: f.clsName != null ? f.clsName : (bm.clsName || ''), subName: f.subName != null ? f.subName : (bm.subName || ''),
      clsId: f.clsId != null ? f.clsId : bm.clsId, subId: f.subId != null ? f.subId : bm.subId,
      slot: f.slot != null ? f.slot : (bm.slot || ''), ilvl: num(f.ilvl != null ? f.ilvl : bm.ilvl),
      reqlvl: num(f.reqlvl != null ? f.reqlvl : bm.reqlvl)
    };
    var list = appMapping();
    if (list.push) list.push(m);
    var icons = window._wowIconById || (window._wowIconById = {});
    if (!icons[x.id] && icons[x.base]) icons[x.id] = icons[x.base];
    var e = mkEntry(m);
    D.byId.set(e.id, e); D.entries.push(e);
    var arr = D.versionsOf.get(e.base);
    if (!arr) { arr = []; D.versionsOf.set(e.base, arr); }
    if (arr.indexOf(e) < 0) arr.push(e);
    e.orphan = !D.byId.has(e.base);
    if (typeof wowIndexVersions === 'function') { try { wowIndexVersions(); } catch (err) {} }
    return e;
  }
  function openItem(id) {
    var e = D.byId.get(String(id));
    if (!e) {
      var f = D.flips && D.flips.find(function (x) { return x.id === String(id); });
      if (f && f.isVersion) e = entryFromFlip(f);
    }
    if (!e || typeof setItem !== 'function') { tip('This item is not in the price list right now.'); return; }
    close();
    try { setItem._userPicked = false; } catch (err) {}
    try { setItem(e.m); } catch (err) {}
    try { window.scrollTo({ top: 0, behavior: 'smooth' }); } catch (err) { window.scrollTo(0, 0); }
  }

  /* ---------- rendering ---------- */
  /* Versions show when opened by hand, or when only a version matched the
     search (say "of the Bear"), until closed by hand. */
  function isExpanded(r) {
    if (!r.kids.length) return false;
    var o = S.open[r.e.id];
    return o === undefined ? !!r.force : o;
  }

  function renderTabs() {
    root.querySelectorAll('.ahb-tab').forEach(function (b) {
      var on = b.dataset.tab === S.tab;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    root.classList.toggle('ahb-tab-flips', S.tab === 'flips');
    els.ahbFlipN.textContent = D.flips ? String(D.flips.length) : '';
  }
  function renderFilters(cands) {
    root.querySelectorAll('[data-q]').forEach(function (i) { i.checked = S.rar[i.dataset.q] !== false; });
    root.querySelectorAll('[data-ver]').forEach(function (b) { b.classList.toggle('active', b.dataset.ver === S.versions); });
    els.ahbVerSec.hidden = !(D.hasVersions && S.tab === 'browse');
    var n = 0;
    if (S.cat) n++;
    if (RARITIES.some(function (r) { return S.rar[r[0]] === false; })) n++;
    if (lvBound(S.lvMin) != null || lvBound(S.lvMax) != null) n++;
    els.ahbFCount.textContent = n ? '(' + n + ')' : '';
    renderTree(cands);
  }
  function renderTree(cands) {
    var tree = {}, total = 0;
    cands.forEach(function (x) {
      var cat = (x.e || x).cat;
      total++;
      var c = tree[cat.c] || (tree[cat.c] = { n: 0, subs: {} });
      c.n++;
      if (!cat.s) return;
      var s = c.subs[cat.s] || (c.subs[cat.s] = { n: 0, slots: {} });
      s.n++;
      if (cat.t) s.slots[cat.t] = (s.slots[cat.t] || 0) + 1;
    });
    var sel = S.cat || { c: '', s: '', t: '' };
    if (sel.c && !tree[sel.c]) tree[sel.c] = { n: 0, subs: {} };
    if (sel.c && sel.s && !tree[sel.c].subs[sel.s]) tree[sel.c].subs[sel.s] = { n: 0, slots: {} };
    if (sel.c && sel.s && sel.t && !(sel.t in tree[sel.c].subs[sel.s].slots)) tree[sel.c].subs[sel.s].slots[sel.t] = 0;
    function node(label, n, attrs, on, lvl, hasKids, expanded) {
      return '<li><button type="button" class="ahb-node ahb-l' + lvl + (on ? ' active' : '') + '"' + attrs +
        (hasKids ? ' aria-expanded="' + (expanded ? 'true' : 'false') + '"' : '') + '>' +
        (hasKids ? '<i class="ahb-caret" aria-hidden="true"></i>' : '<i class="ahb-caret ahb-nocaret" aria-hidden="true"></i>') +
        '<span class="ahb-nl">' + esc(label) + '</span><span class="ahb-nn">' + count(n) + '</span></button>';
    }
    var html = node(S.tab === 'flips' ? 'All flips' : 'All items', total, ' data-c=""', !sel.c, 0, false, false) + '</li>';
    orderBy(Object.keys(tree), CLASS_ORDER).forEach(function (c) {
      var cn = tree[c], subs = orderBy(Object.keys(cn.subs), SUB_ORDER[c]);
      var cOpen = sel.c === c;
      html += node(c, cn.n, ' data-c="' + esc(c) + '"', cOpen && !sel.s, 0, subs.length > 0, cOpen);
      if (cOpen && subs.length) {
        html += '<ul>';
        subs.forEach(function (s) {
          var sn = cn.subs[s], slots = orderBy(Object.keys(sn.slots), SLOT_ORDER);
          var sOpen = sel.s === s;
          html += node(s, sn.n, ' data-c="' + esc(c) + '" data-s="' + esc(s) + '"', sOpen && !sel.t, 1, slots.length > 0, sOpen);
          if (sOpen && slots.length) {
            html += '<ul>';
            slots.forEach(function (t) {
              html += node(t, sn.slots[t], ' data-c="' + esc(c) + '" data-s="' + esc(s) + '" data-t="' + esc(t) + '"', sel.t === t, 2, false, false) + '</li>';
            });
            html += '</ul>';
          }
          html += '</li>';
        });
        html += '</ul>';
      }
      html += '</li>';
    });
    els.ahbTree.innerHTML = html;
  }

  function filterText() {
    var parts = [];
    if (S.cat) parts.push([S.cat.c, S.cat.s, S.cat.t].filter(Boolean).join(' > '));
    var off = RARITIES.filter(function (r) { return S.rar[r[0]] === false; });
    if (off.length) parts.push(RARITIES.filter(function (r) { return S.rar[r[0]] !== false; }).map(function (r) { return r[1]; }).join(', ') || 'no rarity picked');
    var lo = lvBound(S.lvMin), hi = lvBound(S.lvMax);
    if (lo != null || hi != null) parts.push('level ' + (lo != null ? lo : 0) + ' to ' + (hi != null ? hi : 'any'));
    if (S.q) parts.push('"' + S.q + '"');
    return parts.join(', ');
  }

  /* Empty values always sort last; ties (and empties) fall back to a second
     key, biggest first: sold per day on Browse, profit on Flip Finder. */
  function sortRows(rows, st, val, k2) {
    var k = st.k, d = st.d;
    function cmp(a, b, key, dir) {
      var va = val(a, key), vb = val(b, key);
      var na = va == null || va === '', nb = vb == null || vb === '';
      if (na || nb) return na === nb ? 0 : (na ? 1 : -1);
      if (typeof va === 'string') return dir * va.localeCompare(vb);
      return dir * (va - vb);
    }
    return rows.sort(function (a, b) {
      return cmp(a, b, k, d) || (k2 && k2 !== k ? cmp(a, b, k2, -1) : 0);
    });
  }
  function thead(cols, st) {
    return '<thead><tr>' + cols.map(function (c) {
      var on = st.k === c[0];
      return '<th scope="col" data-k="' + c[0] + '" tabindex="0" class="' + (c[2] || '') + (on ? ' sorted' : '') + '"' +
        (on ? ' aria-sort="' + (st.d > 0 ? 'ascending' : 'descending') + '"' : '') +
        (c[3] ? ' title="' + esc(c[3]) + '"' : '') + '>' + esc(c[1]) +
        (on ? '<i class="ahb-sort' + (st.d > 0 ? ' up' : '') + '" aria-hidden="true"></i>' : '') + '</th>';
    }).join('') + '</tr></thead>';
  }
  function icon(id) {
    var u = '';
    try { u = typeof itemIconUrl === 'function' ? itemIconUrl(id) : ''; } catch (e) {}
    return '<img class="ahb-ic" loading="lazy" decoding="async" alt="" src="' + esc(u) + '">';
  }

  var BROWSE_COLS = [
    ['name', 'Item', 'ahb-c-item'], ['type', 'Type', 'ahb-c-type'], ['req', 'Req', 'num', 'Required level'],
    ['low', 'Cheapest', 'num', 'Cheapest buyout per unit right now'],
    ['high', 'Market', 'num', 'Market value: what it usually sells for'],
    ['listed', 'Listed', 'num', 'Units listed on the auction house'],
    ['sold', 'Sold/day', 'num', 'Estimated units sold per day'],
    ['profit', 'Flip profit', 'num', 'Profit of the best flip for this item right now (see Flip Finder)'],
    ['roi', 'Return', 'num', 'Flip profit as a share of what the flip costs']
  ];
  function browseVals(e) {
    var s = stats(e.id), f = D.bestFlip.get(e.id) || null;
    return { e: e, s: s, f: f };
  }
  function bVal(r, k) {
    switch (k) {
      case 'name': return r.e.name.toLowerCase();
      case 'type': return r.e.type.toLowerCase();
      case 'req': return r.e.req || null;
      case 'low': return r.s.low;
      case 'high': return r.s.high;
      case 'listed': return r.s.listed;
      case 'sold': return r.s.sold;
      case 'profit': return r.f ? r.f.profit : null;
      case 'roi': return r.f ? r.f.roi : null;
    }
    return null;
  }
  function bestOf(a, b) { return !a ? b : (!b ? a : (b.profit > a.profit ? b : a)); }

  function renderBrowse(cands) {
    var rows = cands.filter(function (c) { return passCat(c.e.cat); }).map(function (c) {
      var r = browseVals(c.e);
      r.kids = c.kids || []; r.all = c.all || 0; r.force = c.force;
      /* Grouped: the item's flip column shows its best flip, its own or a version's. */
      if (r.kids.length) {
        r.kidRows = r.kids.map(browseVals);
        r.kidRows.forEach(function (k) { if (k.f && (!r.f || k.f.profit > r.f.profit)) { r.f = k.f; r.fromVersion = true; } });
      }
      return r;
    });
    sortRows(rows, S.sortB, bVal, 'sold');
    var shown = rows.slice(0, S.limit), html = thead(BROWSE_COLS, S.sortB) + '<tbody>';
    shown.forEach(function (r) {
      var expanded = isExpanded(r);
      html += browseRow(r, false, expanded);
      if (expanded) sortRows(r.kidRows.slice(), S.sortB, bVal, 'sold').forEach(function (k) { html += browseRow(k, true, false); });
    });
    html += '</tbody>';
    els.ahbTable.className = 'ahb-table ahb-browse';
    els.ahbTable.innerHTML = html;
    els.ahbSummary.innerHTML = '<span><b>' + count(rows.length) + '</b> ' + (rows.length === 1 ? 'item' : 'items') +
      (filterText() ? ' for ' + esc(filterText()) : '') + '</span>' +
      (rows.length > shown.length ? '<span class="ahb-muted">showing ' + count(shown.length) + '</span>' : '');
    emptyAndMore(rows.length, shown.length, 'Nothing matches. Try another search, a wider level range, or Clear filters.');
  }
  function browseRow(r, child, expanded) {
    var e = r.e, s = r.s, f = r.f;
    var tags = '';
    if (e.base) {
      if (!child) tags += '<span class="ahb-tag" title="One version of this green. Its bonus decides the stats.">version</span>';
      tags += '<a class="ahb-wh" href="' + esc(wowheadUrl(e.base, e.variant)) + '" target="_blank" rel="noopener" ' +
        'title="See this exact version, with its stats, on Wowhead">Wowhead</a>';
    } else if (r.all && r.kids && r.kids.length && S.versions !== 'split') {
      var nk = r.kids.length, label = (nk === r.all ? nk : nk + ' of ' + r.all) + (r.all === 1 ? ' version' : ' versions');
      tags += '<button type="button" class="ahb-vtog' + (expanded ? ' open' : '') + '" data-tog="' + esc(e.id) + '" aria-expanded="' +
        (expanded ? 'true' : 'false') + '" title="Show the separate versions of this green and their prices">' +
        '<i class="ahb-caret" aria-hidden="true"></i>' + label + '</button>';
    } else if (S.versions === 'split' && D.versionsOf.has(e.id)) {
      tags += '<span class="ahb-tag" title="This row counts every version of the item together">all versions</span>';
    }
    if (s.n === 1 || s.n === 2) tags += '<span class="ahb-tag ahb-tag-thin" title="Only ' + s.n + (s.n === 1 ? ' listing' : ' listings') +
      ' behind this price, so it can move a lot.">' + s.n + (s.n === 1 ? ' listing' : ' listings') + '</span>';
    var flipTitle = f ? flipAction(f) + ', ' + count(f.units) + (f.units === 1 ? ' unit' : ' units') +
      (r.fromVersion ? ' (a version of this item)' : '') : '';
    return '<tr data-id="' + esc(e.id) + '" tabindex="0" class="ahb-row' + (child ? ' ahb-child' : '') + '">' +
      '<td class="ahb-c-item"><div class="ahb-item">' + icon(e.id) +
        '<div class="ahb-nmwrap"><span class="ahb-nm ' + qClass(e.q) + '">' + esc(e.name) + '</span>' +
        (tags ? '<span class="ahb-tags">' + tags + '</span>' : '') +
        (e.type ? '<span class="ahb-sub ahb-sub-m">' + esc(e.type) + '</span>' : '') + '</div></div></td>' +
      '<td class="ahb-c-type">' + esc(e.type) + '</td>' +
      '<td class="num">' + (e.req ? e.req : '<span class="ahb-muted">-</span>') + '</td>' +
      '<td class="num ahb-buy" title="' + esc(moneyLong(s.low)) + '">' + money(s.low) + '</td>' +
      '<td class="num ahb-sell" title="' + esc(moneyLong(s.high)) + '">' + money(s.high) + '</td>' +
      '<td class="num">' + count(s.listed) + '</td>' +
      '<td class="num">' + count(s.sold) + '</td>' +
      '<td class="num ahb-profit"' + (f ? ' title="' + esc(flipTitle) + '"' : '') + '>' + (f ? money(f.profit) + (r.fromVersion ? '*' : '') : '<span class="ahb-muted">-</span>') + '</td>' +
      '<td class="num">' + (f ? pct(f.roi) : '<span class="ahb-muted">-</span>') + '</td>' +
      '</tr>';
  }

  var FLIP_COLS = [
    ['name', 'Item', 'ahb-c-item'], ['kind', 'Kind', 'ahb-c-kind'],
    ['buyMax', 'Buy up to', 'num', 'Buy every listing priced at or under this (per unit)'],
    ['units', 'Units', 'num ahb-c-units', 'How many units that buys'],
    ['cost', 'Cost', 'num ahb-c-cost', 'What buying them all costs'],
    ['relist', 'Relist at', 'num ahb-c-relist', 'Price per unit to list them at again'],
    ['profit', 'Profit', 'num', 'What you keep if they all sell, after the 5% auction house cut (or what the vendor pays)'],
    ['roi', 'Return', 'num ahb-c-roi', 'Profit as a share of the cost'],
    ['risk', 'Warnings', 'ahb-c-risk', 'Things that could go wrong. Point at one (or tap it) to read it.']
  ];
  function fVal(x, k) {
    switch (k) {
      case 'name': return x.name.toLowerCase();
      case 'kind': return kindOf(x.kind)[0].toLowerCase();      // by the name shown in the Kind column
      case 'risk': return x.risk.length;
      case 'relist': return x.kind === 'vendor' ? null : x.relist;
      default: return x[k];
    }
  }
  function flipHow() {
    var openHow = recall('ahb_how');
    if (openHow == null) openHow = !window.matchMedia || !window.matchMedia('(max-width: 760px)').matches;
    return '<details class="ahb-how"' + (openHow ? ' open' : '') + '><summary>How to do a flip in game</summary>' +
      '<ol>' +
        '<li><b>Buy and relist:</b> at the auction house, search the item and buy every listing priced at or under ' +
          '<b>Buy up to</b> (that is the <b>Units</b> count). Then list them again at <b>Relist at</b>. ' +
          '<b>Profit</b> is what you keep once they all sell, after the auction house takes its 5% cut.</li>' +
        '<li><b>Sell to vendor:</b> buy the listings at or under <b>Buy up to</b> and sell them to any vendor. ' +
          'The vendor pays more than they cost, so the profit is locked in as soon as you sell.</li>' +
        '<li><b>Under usual price:</b> green, blue or epic items listed well under what they usually sell for. Buy them the same way, ' +
          'then list them again at <b>Relist at</b>. That price is never above the usual price, and it is lower when another listing ' +
          'sits between your buy price and the usual price, so yours is the cheapest one for sale.</li>' +
        '<li>Prices move all the time. If the cheap listings are already gone when you look, skip that flip. ' +
          'Click a row to see the item\'s price chart first.</li>' +
      '</ol>' +
      '<div class="ahb-legend">' + Object.keys(RISK).map(function (k) {
        return '<div>' + riskBadges([k]) + '<span>' + esc(RISK[k][1]) + '</span></div>';
      }).join('') + '</div>' +
    '</details>';
  }
  function renderFlipBar() {
    if (S.tab !== 'flips' || !D.flips) { els.ahbFlipBar.innerHTML = ''; return; }
    var chips = [['all', 'All flips', 'Every flip, biggest profit first'],
      ['deals', 'Deals', 'Greens, blues and epics listed well under their usual price'],
      ['greens', 'Greens', 'Green (uncommon) armor and weapons'],
      ['mats', 'Materials', 'Crafting materials: cloth, herbs, ore, leather and more'],
      ['vendor', 'Vendor flips', 'Listings priced under what a vendor pays for them']];
    var when = D.flipsUpdated ? '<span class="ahb-muted">Flips from the price update ' + esc(ago(D.flipsUpdated)) + '.</span>' : '';
    els.ahbFlipBar.innerHTML = flipHow() +
      '<div class="ahb-chips" role="group" aria-label="Quick filters">' + chips.map(function (c) {
        return '<button type="button" class="ahb-chip' + (S.chip === c[0] ? ' active' : '') + '" data-chip="' + c[0] + '" title="' + esc(c[2]) + '">' + c[1] + '</button>';
      }).join('') + when + '</div>';
  }
  function renderFlips(cands) {
    if (D.flipState === 'loading' && !D.flips) {
      els.ahbTable.innerHTML = '';
      els.ahbSummary.innerHTML = '';
      emptyAndMore(0, 0, 'Loading flips...');
      return;
    }
    if (!D.flips) {
      els.ahbTable.innerHTML = '';
      els.ahbSummary.innerHTML = '';
      emptyAndMore(0, 0, 'Flip Finder starts with the next price update. The price job checks the auction house every 30 minutes; come back after the next one.');
      return;
    }
    var rows = cands.filter(function (x) { return passCat(x.cat); });
    sortRows(rows, S.sortF, fVal, 'profit');
    var shown = rows.slice(0, S.limit), html = thead(FLIP_COLS, S.sortF) + '<tbody>';
    shown.forEach(function (x) { html += flipRow(x); });
    html += '</tbody>';
    els.ahbTable.className = 'ahb-table ahb-flips';
    els.ahbTable.innerHTML = rows.length ? html : '';
    /* No grand total: most flips carry a warning, and gold added up from
       risky flips reads like gold you can expect. Say how many have none. */
    var clean = rows.filter(function (x) { return !x.risk.length; }).length;
    els.ahbSummary.innerHTML = '<span><b>' + count(rows.length) + '</b> ' + (rows.length === 1 ? 'flip' : 'flips') +
      (filterText() ? ' for ' + esc(filterText()) : '') + '</span>' +
      (rows.length ? '<span class="ahb-muted">' + (clean === rows.length ? 'none has a warning'
        : clean ? count(clean) + ' with no warnings' : 'every one has a warning') + '</span>' : '');
    var noDeals = S.chip === 'deals' && !D.flips.some(function (x) { return x.kind === 'deal'; });
    emptyAndMore(rows.length, shown.length, !D.flips.length
      ? 'No flips in the latest price update. The list refreshes with every update.'
      : noDeals
        ? 'No deals in the latest price update. One shows up here when a green, blue or epic item is listed well under ' +
          'its usual price. The list refreshes with every update.'
        : 'No flips match these filters. Try All flips, or Clear filters.');
  }
  function flipRow(x) {
    var tags = '';
    if (x.isVersion) {
      tags += '<a class="ahb-wh" href="' + esc(wowheadUrl(x.base, x.variant)) + '" target="_blank" rel="noopener" ' +
        'title="See this exact version, with its stats, on Wowhead">Wowhead</a>';
    }
    var f = x.f, bits = [];
    if (x.sold != null) bits.push('about ' + count(x.sold) + ' sold a day');
    if (f.listed != null) bits.push(count(f.listed) + ' listed');
    if (f.next > 0) bits.push('next listing above ' + price(f.next));
    if (f.market > 0) bits.push('market ' + moneyLong(f.market));
    /* Hovering the row says what to do first, then the numbers behind it. */
    var facts = bits.join(', ');
    var rowTitle = flipAction(x) + (facts ? '. ' + facts.charAt(0).toUpperCase() + facts.slice(1) : '');
    /* A deal says how far under its usual price the buy is, under its kind. */
    var note = x.kind === 'deal' ? dealNote(x) : '';
    var kind = kindBadge(x) + (note ? '<span class="ahb-kind-note">' + esc(note) + '</span>' : '');
    /* On a phone the table keeps Item, Buy up to and Profit; what to do next
       (relist price or vendor, how many) and the warnings go under the name.
       A deal's discount comes first, so it is not read as being about the
       relist price. */
    var mline = '<span class="ahb-fl-m">' +
      (x.kind === 'deal' ? '<span class="ahb-under">' + esc(note || 'under its usual price') + '</span>' : '') +
      (x.kind === 'vendor' ? '<span>Sell to vendor</span>' : '<span>Relist at <b class="ahb-sell">' + price(x.relist) + '</b></span>') +
      '<span class="ahb-muted">' + count(x.units) + ' for ' + money(x.cost) + '</span>' + riskBadges(x.risk) + '</span>';
    return '<tr data-id="' + esc(x.id) + '" tabindex="0" class="ahb-row" title="' + esc(rowTitle) + '">' +
      '<td class="ahb-c-item"><div class="ahb-item">' + icon(x.id) +
        '<div class="ahb-nmwrap"><span class="ahb-nm ' + qClass(x.q) + '">' + esc(x.name) + '</span>' +
        (tags ? '<span class="ahb-tags">' + tags + '</span>' : '') +
        (x.type ? '<span class="ahb-sub">' + esc(x.type) + (x.req ? ', level ' + x.req : '') + '</span>' : '') + mline + '</div></div></td>' +
      '<td class="ahb-c-kind">' + kind + '</td>' +
      '<td class="num ahb-buy">' + price(x.buyMax) + '</td>' +
      '<td class="num ahb-c-units">' + count(x.units) + '</td>' +
      '<td class="num ahb-c-cost" title="' + esc(moneyLong(x.cost)) + '">' + money(x.cost) + '</td>' +
      '<td class="num ahb-sell ahb-c-relist">' + (x.kind === 'vendor' ? '<span class="ahb-muted">vendor</span>' : price(x.relist)) + '</td>' +
      '<td class="num ahb-profit" title="' + esc(moneyLong(x.profit)) + '">' + money(x.profit, true) + '</td>' +
      '<td class="num ahb-c-roi">' + pct(x.roi) + '</td>' +
      '<td class="ahb-c-risk">' + (riskBadges(x.risk) || '<span class="ahb-muted">none</span>') + '</td>' +
      '</tr>';
  }
  function emptyAndMore(total, shown, emptyText) {
    els.ahbEmpty.hidden = total > 0;
    if (!total) els.ahbEmpty.textContent = emptyText;
    els.ahbMore.hidden = !(total > shown);
    if (total > shown) els.ahbMore.textContent = 'Show more (' + count(total - shown) + ' left)';
  }

  function render(keepScroll) {
    if (!root || root.hidden) return;
    renderTabs();
    if (!D.ready) {
      els.ahbTable.innerHTML = ''; els.ahbSummary.innerHTML = ''; els.ahbFlipBar.innerHTML = '';
      els.ahbTree.innerHTML = '';
      emptyAndMore(0, 0, D.failed ? 'Prices could not be loaded. Try reloading the page.' : 'Loading prices...');
      return;
    }
    var top = els.ahbWrap.scrollTop;
    var cands = S.tab === 'flips' ? flipCandidates() : browseCandidates();
    renderFilters(cands);
    renderFlipBar();
    if (S.tab === 'flips') renderFlips(cands); else renderBrowse(cands);
    els.ahbWrap.scrollTop = keepScroll ? top : 0;
  }

  /* ---------- data readiness ---------- */
  /* app.js fires "wow:data" once its files are in (and sets
     window.__wowDataReady); polling is the fallback for an older app.js. */
  var waiting = false, building = null;
  function dataAvailable() {
    return !!(window.__wowDataReady || (appMapping().length && typeof latest !== 'undefined' && latest));
  }
  function startIndex() {
    if (building) return building;
    building = loadSalesScale().then(function () {
      buildIndex();
      building = null;
      if (root && !root.hidden) render(true);
    });
    return building;
  }
  function waitForData() {
    if (waiting || D.ready) return;
    waiting = true;
    var tries = 0;
    (function poll() {
      if (D.ready || building) { waiting = false; return; }
      if (dataAvailable()) { waiting = false; startIndex(); return; }
      if (++tries > 120) { D.failed = true; waiting = false; render(); return; }
      setTimeout(poll, 500);
    })();
  }
  document.addEventListener('wow:data', function () {
    var again = function () { building = null; startIndex(); };
    if (building) building.then(again); else again();
  });
  /* Fresh prices every few minutes: re-read flips.json too, once the window
     has been used. */
  document.addEventListener('wow:latest', function () {
    if (!D.ready || !D.flipsWanted) return;
    loadFlips().then(function () { if (root && !root.hidden) render(true); });
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && root && !root.hidden) close();
  });

  /* ---------- entry points ---------- */
  function addButtons() {
    var sg = document.getElementById('searchGroup');
    if (sg && !document.getElementById('ahbOpen')) {
      var b = document.createElement('button');
      b.type = 'button';
      b.id = 'ahbOpen';
      b.className = 'ahb-open';
      b.title = 'Browse the auction house by category, and find flips';
      b.setAttribute('aria-label', 'Browse the auction house and find flips');
      b.innerHTML = ICON_BROWSE + '<span>Browse</span>';
      b.addEventListener('click', function () { open(); });
      sg.appendChild(b);
    }
    var menu = document.getElementById('moreActions');
    if (menu && !document.getElementById('ahbDrawerBrowse')) {
      var sep = menu.querySelector('.drawer-sep');
      var frag = document.createElement('div');
      frag.className = 'ahb-drawer';
      frag.innerHTML = '<div class="drawer-sep"></div><div class="drawer-label">Auction house</div>' +
        '<button type="button" class="drawer-link ahb-dlink" id="ahbDrawerBrowse">Browse all items</button>' +
        '<button type="button" class="drawer-link ahb-dlink" id="ahbDrawerFlips">Flip Finder</button>';
      if (sep) menu.insertBefore(frag, sep); else menu.appendChild(frag);
      document.getElementById('ahbDrawerBrowse').addEventListener('click', function () { open('browse'); });
      document.getElementById('ahbDrawerFlips').addEventListener('click', function () { open('flips'); });
    }
  }
  addButtons();
  /* #browse or #flips in the address opens the window straight away. */
  var h = (location.hash || '').toLowerCase();
  if (h === '#browse' || h === '#flips') open(h === '#flips' ? 'flips' : 'browse');
  else waitForData();

  window.ForeverBrowse = { open: open, close: close };
})();
