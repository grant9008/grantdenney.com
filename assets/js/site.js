/* grantdenney.com — no dependencies, no build step. */
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
})();
