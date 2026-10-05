/* grantdenney.com, no dependencies, no build step. */
(function () {
  'use strict';

  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------- footer year ------------------------------------------------ */

  var year = document.querySelector('[data-year]');
  if (year) year.textContent = new Date().getFullYear();

  /* ---------- nav shadow once you leave the top -------------------------- */

  var nav = document.getElementById('nav');
  if (nav) {
    var onScroll = function () {
      nav.dataset.stuck = window.scrollY > 12 ? 'true' : 'false';
    };
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
  }

  /* ---------- reveal on scroll ------------------------------------------- */

  var revealables = document.querySelectorAll('[data-reveal]');
  if (!('IntersectionObserver' in window) || reduceMotion) {
    revealables.forEach(function (el) { el.classList.add('is-in'); });
  } else {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.classList.add('is-in');
        io.unobserve(entry.target);
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.05 });
    revealables.forEach(function (el) { io.observe(el); });
  }

  /* ---------- slider ------------------------------------------------------ */

  document.querySelectorAll('[data-slider]').forEach(function (root) {
    var track = root.querySelector('.slider__track');
    var slides = Array.prototype.slice.call(root.querySelectorAll('.slide'));
    var prev = root.querySelector('[data-prev]');
    var next = root.querySelector('[data-next]');
    var bar = root.querySelector('[data-bar]');
    var count = root.querySelector('[data-count]');
    if (!track || !slides.length) return;

    var pad = function (n) { return String(n).padStart(2, '0'); };

    // Index of the slide nearest the left edge of the viewport.
    function currentIndex() {
      var x = track.scrollLeft;
      var best = 0;
      var bestGap = Infinity;
      slides.forEach(function (slide, i) {
        var gap = Math.abs(slide.offsetLeft - track.offsetLeft - x);
        if (gap < bestGap) { bestGap = gap; best = i; }
      });
      return best;
    }

    function sync() {
      var i = currentIndex();
      // Track can't scroll past its own width, so the last slide never sits at 0 gap.
      var maxScroll = track.scrollWidth - track.clientWidth;
      var atEnd = track.scrollLeft >= maxScroll - 2;
      var shown = atEnd ? slides.length - 1 : i;

      if (count) count.textContent = pad(shown + 1) + ' / ' + pad(slides.length);
      if (bar) {
        var pct = maxScroll > 0 ? track.scrollLeft / maxScroll : 0;
        // Bar width reflects how much of the strip is on screen.
        var visible = track.clientWidth / track.scrollWidth;
        bar.style.width = Math.max(visible * 100, 8) + '%';
        bar.style.transform = 'translateX(' + (pct * (100 / Math.max(visible, 0.08) - 100)) + '%)';
      }
      if (prev) prev.disabled = track.scrollLeft <= 2;
      if (next) next.disabled = atEnd;
    }

    function step(dir) {
      var i = currentIndex();
      var target = slides[Math.min(Math.max(i + dir, 0), slides.length - 1)];
      if (!target) return;
      track.scrollTo({
        left: target.offsetLeft - track.offsetLeft,
        behavior: reduceMotion ? 'auto' : 'smooth'
      });
    }

    if (prev) prev.addEventListener('click', function () { step(-1); });
    if (next) next.addEventListener('click', function () { step(1); });

    var raf = null;
    track.addEventListener('scroll', function () {
      if (raf) return;
      raf = requestAnimationFrame(function () { raf = null; sync(); });
    }, { passive: true });

    window.addEventListener('resize', sync);

    // Arrow keys when the strip has focus.
    track.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowRight') { e.preventDefault(); step(1); }
      if (e.key === 'ArrowLeft') { e.preventDefault(); step(-1); }
    });

    sync();
  });

  /* ---------- lightbox ---------------------------------------------------- */

  var lb = document.querySelector('[data-lightbox]');
  if (lb) {
    var lbImg = lb.querySelector('[data-lb-img]');
    var lbCap = lb.querySelector('[data-lb-cap]');
    // Anything with data-full opens the viewer. data-gallery keeps prev/next
    // inside its own set, so the car photos don't page into the Showrun slider.
    var openers = Array.prototype.slice.call(document.querySelectorAll('[data-full]'));
    var group = [];
    var at = 0;
    var lastFocus = null;

    function galleryOf(el) {
      return el.dataset.gallery || (el.classList.contains('slide') ? 'showrun' : 'default');
    }

    function show(i) {
      at = (i + group.length) % group.length;
      var el = group[at];
      lbImg.src = el.dataset.full;
      lbImg.alt = el.dataset.cap || '';
      lbCap.textContent = el.dataset.cap || '';
    }

    function open(i) {
      lastFocus = document.activeElement;
      var name = galleryOf(openers[i]);
      group = openers.filter(function (o) { return galleryOf(o) === name; });
      show(group.indexOf(openers[i]));
      lb.dataset.open = 'true';
      lb.setAttribute('aria-hidden', 'false');
      document.body.dataset.lbOpen = 'true';
      lb.querySelector('[data-lb-close]').focus();
    }

    function close() {
      lb.dataset.open = 'false';
      lb.setAttribute('aria-hidden', 'true');
      delete document.body.dataset.lbOpen;
      if (lastFocus && lastFocus.focus) lastFocus.focus();
    }

    openers.forEach(function (el, i) {
      el.addEventListener('click', function () { open(i); });
      el.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(i); }
      });
    });

    lb.querySelector('[data-lb-close]').addEventListener('click', close);
    lb.querySelector('[data-lb-prev]').addEventListener('click', function () { show(at - 1); });
    lb.querySelector('[data-lb-next]').addEventListener('click', function () { show(at + 1); });

    // Click the backdrop (not the image or the buttons) to dismiss.
    lb.addEventListener('click', function (e) {
      if (e.target === lb || e.target.classList.contains('lb__stage')) close();
    });

    document.addEventListener('keydown', function (e) {
      if (lb.dataset.open !== 'true') return;
      if (e.key === 'Escape') close();
      if (e.key === 'ArrowRight') show(at + 1);
      if (e.key === 'ArrowLeft') show(at - 1);
    });
  }

  /* ---------- live plugin stats ------------------------------------------
     The numbers in the markup are the last known good values, so the block
     is correct with JS off or the API down. A successful fetch replaces them
     and turns the indicator green; a failure leaves the baked values alone.
     ---------------------------------------------------------------------- */

  document.querySelectorAll('[data-live]').forEach(function (box) {
    var plugin = box.dataset.livePlugin;
    var repo = box.dataset.liveRepo;
    if (!plugin) return;

    var usersEl = box.querySelector('[data-live-users]');
    var updEl = box.querySelector('[data-live-updated]');
    var noteEl = box.querySelector('[data-live-note]');

    function setValue(el, main) {
      if (!el) return;
      var small = el.querySelector('small');
      el.textContent = main;
      if (small) el.appendChild(small);
    }

    function fmtDate(iso) {
      var d = new Date(iso);
      if (isNaN(d)) return null;
      return d.getDate() + ' ' +
        ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][d.getMonth()] +
        ' ' + d.getFullYear();
    }

    // RuneLite keys its counts by the client version, so ask which one is current.
    fetch('https://static.runelite.net/bootstrap.json')
      .then(function (r) { return r.json(); })
      .then(function (b) {
        if (!b || !b.version) throw new Error('no version');
        return fetch('https://api.runelite.net/runelite-' + b.version + '/pluginhub');
      })
      .then(function (r) { return r.json(); })
      .then(function (counts) {
        var n = counts && counts[plugin];
        if (typeof n !== 'number') throw new Error('no count');
        setValue(usersEl, n.toLocaleString());
        box.dataset.liveOk = 'true';
        if (noteEl) noteEl.textContent = '· checked just now';
      })
      .catch(function () { /* keep the baked-in number */ });

    if (repo && updEl) {
      fetch('https://api.github.com/repos/' + repo)
        .then(function (r) { return r.json(); })
        .then(function (j) {
          var d = j && j.pushed_at && fmtDate(j.pushed_at);
          if (d) setValue(updEl, d);
        })
        .catch(function () { /* keep the baked-in date */ });
    }
  });

  /* ---------- shot stacks that overflow their card ------------------------
     The photo column is clipped to the body's height. When the stack is
     taller than that, it becomes a slider: arrows step one figure at a time
     and a counter says where you are. When everything fits, no controls
     appear at all.
     ---------------------------------------------------------------------- */

  var ARROW = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 15l6-6 6 6"/></svg>';

  document.querySelectorAll('.card__shot').forEach(function (panel) {
    var stack = panel.querySelector('.shotstack');
    if (!stack) return;
    var figures = Array.prototype.slice.call(stack.querySelectorAll('figure'));
    if (figures.length < 2) return;

    var nav = document.createElement('div');
    nav.className = 'shotnav';
    nav.innerHTML =
      '<button type="button" data-up aria-label="Previous photo">' + ARROW + '</button>' +
      '<span data-count></span>' +
      '<button type="button" data-down aria-label="Next photo" style="transform:rotate(180deg)">' + ARROW + '</button>';
    panel.appendChild(nav);

    var up = nav.querySelector('[data-up]');
    var down = nav.querySelector('[data-down]');
    var count = nav.querySelector('[data-count]');

    function current() {
      var top = stack.scrollTop;
      var best = 0, bestGap = Infinity;
      figures.forEach(function (fig, i) {
        var gap = Math.abs(fig.offsetTop - figures[0].offsetTop - top);
        if (gap < bestGap) { bestGap = gap; best = i; }
      });
      return best;
    }

    function sync() {
      // Only a slider when it actually overflows, a resize can change that.
      var overflowing = stack.scrollHeight > stack.clientHeight + 4;
      panel.dataset.overflowing = overflowing ? 'true' : 'false';
      if (!overflowing) return;
      var max = stack.scrollHeight - stack.clientHeight;
      var atEnd = stack.scrollTop >= max - 2;
      var i = atEnd ? figures.length - 1 : current();
      count.textContent = (i + 1) + ' / ' + figures.length;
      up.disabled = stack.scrollTop <= 2;
      down.disabled = atEnd;
    }

    function step(dir) {
      var target = figures[Math.min(Math.max(current() + dir, 0), figures.length - 1)];
      if (target) stack.scrollTop = target.offsetTop - figures[0].offsetTop;
    }

    up.addEventListener('click', function () { step(-1); });
    down.addEventListener('click', function () { step(1); });

    var raf = null;
    stack.addEventListener('scroll', function () {
      if (raf) return;
      raf = requestAnimationFrame(function () { raf = null; sync(); });
    }, { passive: true });

    window.addEventListener('resize', sync);
    // images settle late; re-check as they land
    stack.querySelectorAll('img').forEach(function (img) {
      if (!img.complete) img.addEventListener('load', sync, { once: true });
    });
    sync();
    setTimeout(sync, 600);
  });

  /* ---------- live hero chart ---------------------------------------------
     Draws the real install history from data/installs.json behind the name.
     The file is appended to once a day by a GitHub Action, because RuneLite's
     API reports a current count and keeps no history. If the fetch fails, or
     there are too few points to be a line, nothing is drawn at all.
     ---------------------------------------------------------------------- */

  var plot = document.querySelector('[data-heroplot]');
  if (plot) {
    fetch('data/installs.json')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var pts = (data && data.points) || [];
        if (pts.length < 2) return;            // not a chart yet

        var slugs = Object.keys(data.plugins || {});
        var totals = pts.map(function (p) {
          return slugs.reduce(function (sum, k) { return sum + (p[k] || 0); }, 0);
        });

        var W = 520, H = 200, padL = 10, padR = 58, padT = 26, padB = 30;
        var lo = Math.min.apply(null, totals);
        var hi = Math.max.apply(null, totals);
        // Give a flat-ish series some room so it isn't a dead horizontal line.
        var span = Math.max(hi - lo, Math.max(hi * 0.08, 1));
        var base = Math.max(lo - span * 0.35, 0);
        var top = hi + span * 0.3;

        function x(i) { return padL + (i / (pts.length - 1)) * (W - padL - padR); }
        function y(v) { return padT + (1 - (v - base) / (top - base)) * (H - padT - padB); }

        var line = totals.map(function (v, i) { return (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(v).toFixed(1); }).join(' ');
        var area = line + ' L' + x(pts.length - 1).toFixed(1) + ' ' + (H - padB) + ' L' + x(0).toFixed(1) + ' ' + (H - padB) + ' Z';

        var parts = ['<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">'];
        for (var g = 0; g <= 3; g++) {
          var gy = padT + (g / 3) * (H - padT - padB);
          parts.push('<line class="hp-grid" x1="' + padL + '" y1="' + gy.toFixed(1) + '" x2="' + (W - padR) + '" y2="' + gy.toFixed(1) + '"/>');
        }
        parts.push('<path class="hp-area" d="' + area + '"/>');
        parts.push('<path class="hp-line" d="' + line + '"/>');
        parts.push('<circle class="hp-dot" cx="' + x(pts.length - 1).toFixed(1) + '" cy="' + y(totals[totals.length - 1]).toFixed(1) + '" r="4"/>');

        // only the first and last date, so it stays quiet
        function nice(iso) {
          var d = new Date(iso + 'T00:00:00');
          if (isNaN(d)) return iso;
          return d.getDate() + ' ' + ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][d.getMonth()];
        }
        parts.push('<text class="hp-tick" x="' + padL + '" y="' + (H - padB + 20) + '">' + nice(pts[0].date) + '</text>');
        parts.push('<text class="hp-tick" text-anchor="end" x="' + (W - padR) + '" y="' + (H - padB + 20) + '">' + nice(pts[pts.length - 1].date) + '</text>');
        parts.push('<text class="hp-tick" text-anchor="end" x="' + (W - padR) + '" y="' + (y(totals[totals.length - 1]) - 12).toFixed(1) + '">' + totals[totals.length - 1].toLocaleString() + '</text>');
        parts.push('</svg>');

        // The chart is the recorded daily history. The hero sentence quotes a live
        // figure below, so seed it from the file and let the API overwrite it.
        var heroNum = document.querySelector('[data-hero-installs]');
        if (heroNum && !heroNum.dataset.live) {
          heroNum.textContent = totals[totals.length - 1].toLocaleString();
        }

        plot.innerHTML = parts.join('');
        plot.dataset.ready = 'true';

        var label = document.querySelector('[data-heroplot-label]');
        if (label) {
          label.innerHTML = '<i></i> Active installs, recorded daily since ' + nice(pts[0].date);
          label.hidden = false;
        }
      })
      .catch(function () { /* no chart rather than a wrong one */ });
  }

  /* ---------- live total in the hero sentence -----------------------------
     Same source as the per-plugin counters, so the headline number and the
     cards can never disagree. Falls back to whatever the chart seeded.
     ---------------------------------------------------------------------- */

  var heroInstalls = document.querySelector('[data-hero-installs]');
  if (heroInstalls) {
    fetch('https://static.runelite.net/bootstrap.json')
      .then(function (r) { return r.json(); })
      .then(function (b) {
        if (!b || !b.version) throw new Error('no version');
        return fetch('https://api.runelite.net/runelite-' + b.version + '/pluginhub');
      })
      .then(function (r) { return r.json(); })
      .then(function (counts) {
        var total = ['personal-space', 'pocketge-flip-tracker'].reduce(function (sum, k) {
          var n = counts && counts[k];
          return sum + (typeof n === 'number' ? n : 0);
        }, 0);
        if (total > 0) {
          heroInstalls.textContent = total.toLocaleString();
          heroInstalls.dataset.live = 'true';
        }
      })
      .catch(function () { /* keep the seeded number */ });
  }
})();
