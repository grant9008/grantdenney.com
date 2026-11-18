/* Footer lines: when the hourly snapshot last ran, so stale data is obvious,
   and how far back price history goes, so a near-empty chart is explained. */
(function () {
  var el = document.getElementById('wowDataStatus');
  var hEl = document.getElementById('wowHistoryStatus');
  if (!el || typeof loadStatus !== 'function') return;
  var DOT = ' \u00b7 ';
  /* Items listed every hour, so their history file starts when history did:
     Linen Cloth and Copper Ore. */
  var STAPLES = [2589, 2770];
  var since = null;   // epoch seconds of the first price point we know about

  function earliest(f) {
    var h = (f && f.h) || [], d = (f && f.d) || [];
    var t = h.length ? h[0][0] : null;
    /* Daily points are stamped at midnight, so only trust one when it is a
       full day older than the first hourly point (old hourly points get
       trimmed after a couple of weeks). */
    if (d.length && (t == null || d[0][0] + 86400 <= t)) t = d[0][0];
    return t;
  }
  function findSince(st) {
    if (since != null) return Promise.resolve(since);
    if (st.firstSnapshot && st.updated) { since = st.updated; return Promise.resolve(since); }
    if (typeof loadHistFile !== 'function') return Promise.resolve(null);
    var ids = STAPLES.slice();
    try { if (typeof selected !== 'undefined' && selected && selected.id != null) ids.push(selected.id); } catch (e) {}
    return Promise.all(ids.map(function (id) {
      return Promise.resolve(loadHistFile(id)).catch(function () { return null; });
    })).then(function (files) {
      var t = null;
      files.forEach(function (f) { var e = earliest(f); if (e != null && (t == null || e < t)) t = e; });
      if (t != null) since = t;
      return t;
    });
  }
  function paintHistory(st) {
    if (!hEl) return;
    findSince(st).then(function (t) {
      if (t == null) { hEl.hidden = true; return; }
      var when = new Date(t * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
      var young = Math.floor(Date.now() / 1000) - t < 86400;
      hEl.textContent = 'History since ' + when + (young ? DOT + 'Charts fill in hourly' : '');
      hEl.hidden = false;
    }).catch(function () { hEl.hidden = true; });
  }
  function paint() {
    loadStatus().then(function (st) {
      var age = Math.floor(Date.now() / 1000) - (st.updated || 0);
      var mins = Math.round(age / 60);
      var when = mins < 2 ? 'just now' : mins < 90 ? mins + ' min ago' : Math.round(mins / 60) + ' h ago';
      /* salesHours (newer status files): how many of the last 24 hours have a
         measured sold estimate. The site scales sold numbers up to a day. */
      var sh = typeof st.salesHours === 'number' ? Math.floor(st.salesHours) : null;
      var sales = '';
      if (st.firstSnapshot) sales = DOT + 'first snapshot, sales estimates start next hour';
      else if (sh != null && sh <= 0) sales = DOT + 'sales estimates resume with the next snapshot';
      else if (sh != null && sh < 24) sales = DOT + 'sales measured for ' + sh + ' of the last 24 hours so far (early estimate)';
      el.textContent = 'Snapshot ' + when + DOT + (st.auctions || 0).toLocaleString() + ' auctions' + DOT +
        (st.items || 0).toLocaleString() + ' items' + sales;
      el.classList.toggle('stale', age > 3 * 3600);
      paintHistory(st);
    }).catch(function () {
      el.textContent = 'No price data yet. The hourly job has not published a snapshot.';
      el.classList.add('stale');
      if (hEl) hEl.hidden = true;
    });
  }
  paint();
  setInterval(paint, 5 * 60 * 1000);
})();
