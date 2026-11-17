/* Footer line: when the hourly snapshot last ran, so stale data is obvious. */
(function () {
  var el = document.getElementById('wowDataStatus');
  if (!el || typeof loadStatus !== 'function') return;
  function paint() {
    loadStatus().then(function (st) {
      var age = Math.floor(Date.now() / 1000) - (st.updated || 0);
      var mins = Math.round(age / 60);
      var when = mins < 2 ? 'just now' : mins < 90 ? mins + ' min ago' : Math.round(mins / 60) + ' h ago';
      el.textContent = 'Snapshot ' + when + ' · ' + (st.auctions || 0).toLocaleString() + ' auctions · ' +
        (st.items || 0).toLocaleString() + ' items' + (st.firstSnapshot ? ' · first snapshot, sales estimates start next hour' : '');
      el.classList.toggle('stale', age > 3 * 3600);
    }).catch(function () {
      el.textContent = 'No price data yet. The hourly job has not published a snapshot.';
      el.classList.add('stale');
    });
  }
  paint();
  setInterval(paint, 5 * 60 * 1000);
})();
