/**
 * Procedure carousel controller — prev/next, dots, keyboard arrows,
 * autoplay with pause on hover/focus/interaction. Pure vanilla, no deps.
 *
 * The track is a CSS scroll-snap container, so without this script the user
 * can still horizontally swipe / scroll through slides. This script just adds
 * affordances (arrows, dots, autoplay) for mouse + keyboard users.
 */
(function () {
  "use strict";

  /** @param {HTMLElement} root */
  function init(root) {
    if (root.dataset.carouselInit === "1") return;
    root.dataset.carouselInit = "1";

    var track = root.querySelector("[data-carousel-track]");
    var slides = track ? track.querySelectorAll("[data-slide-index]") : [];
    var prevBtn = root.querySelector("[data-carousel-prev]");
    var nextBtn = root.querySelector("[data-carousel-next]");
    var dots = root.querySelectorAll("[data-carousel-dot]");
    var n = slides.length;
    if (!track || n < 2) return;

    var current = 0;
    var AUTOPLAY_MS = 7000;
    var autoplayTimer = null;
    var autoplayPaused = false;

    function setActive(idx) {
      idx = ((idx % n) + n) % n;
      current = idx;
      for (var i = 0; i < dots.length; i++) {
        var dot = dots[i];
        if (i === idx) {
          dot.className =
            "h-1.5 rounded-full transition-all duration-300 cursor-pointer w-6 bg-emerald-400";
          dot.setAttribute("aria-selected", "true");
        } else {
          dot.className =
            "h-1.5 rounded-full transition-all duration-300 cursor-pointer w-1.5 bg-zinc-700 hover:bg-zinc-500";
          dot.setAttribute("aria-selected", "false");
        }
      }
    }

    function scrollTo(idx, behavior) {
      idx = ((idx % n) + n) % n;
      var slide = slides[idx];
      if (!slide) return;
      track.scrollTo({
        left: slide.offsetLeft - track.offsetLeft,
        behavior: behavior || "smooth",
      });
      setActive(idx);
    }

    function next() {
      scrollTo(current + 1);
    }
    function prev() {
      scrollTo(current - 1);
    }

    if (nextBtn) nextBtn.addEventListener("click", function (e) {
      e.preventDefault();
      pauseAutoplayUntilInteractionEnds();
      next();
    });
    if (prevBtn) prevBtn.addEventListener("click", function (e) {
      e.preventDefault();
      pauseAutoplayUntilInteractionEnds();
      prev();
    });

    for (var i = 0; i < dots.length; i++) {
      (function (idx) {
        dots[idx].addEventListener("click", function (e) {
          e.preventDefault();
          pauseAutoplayUntilInteractionEnds();
          scrollTo(idx);
        });
      })(i);
    }

    // Keep dots in sync when the user swipes / scrolls manually.
    var scrollTimer = null;
    track.addEventListener(
      "scroll",
      function () {
        if (scrollTimer) clearTimeout(scrollTimer);
        scrollTimer = setTimeout(function () {
          var slideWidth = slides[0].offsetWidth;
          if (!slideWidth) return;
          var idx = Math.round(track.scrollLeft / slideWidth);
          if (idx !== current) setActive(idx);
        }, 80);
      },
      { passive: true }
    );

    // Keyboard arrows when the carousel has focus inside it.
    root.addEventListener("keydown", function (e) {
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        pauseAutoplayUntilInteractionEnds();
        prev();
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        pauseAutoplayUntilInteractionEnds();
        next();
      }
    });

    // Autoplay with pause on hover, focus-within, and tab visibility change.
    function startAutoplay() {
      stopAutoplay();
      if (autoplayPaused) return;
      autoplayTimer = setInterval(function () {
        if (document.hidden) return;
        next();
      }, AUTOPLAY_MS);
    }
    function stopAutoplay() {
      if (autoplayTimer) {
        clearInterval(autoplayTimer);
        autoplayTimer = null;
      }
    }
    function pauseAutoplayUntilInteractionEnds() {
      autoplayPaused = true;
      stopAutoplay();
      // Resume after the user has been idle for a beat — keeps interactions feeling responsive
      // but doesn't strand the carousel paused forever if focus is left in it.
      if (pauseAutoplayUntilInteractionEnds._t) clearTimeout(pauseAutoplayUntilInteractionEnds._t);
      pauseAutoplayUntilInteractionEnds._t = setTimeout(function () {
        autoplayPaused = false;
        startAutoplay();
      }, 12000);
    }

    root.addEventListener("mouseenter", function () {
      autoplayPaused = true;
      stopAutoplay();
    });
    root.addEventListener("mouseleave", function () {
      autoplayPaused = false;
      startAutoplay();
    });
    root.addEventListener("focusin", function () {
      autoplayPaused = true;
      stopAutoplay();
    });
    root.addEventListener("focusout", function () {
      // Wait a tick — a click on a button moves focus around briefly.
      setTimeout(function () {
        if (!root.contains(document.activeElement)) {
          autoplayPaused = false;
          startAutoplay();
        }
      }, 50);
    });

    // Respect reduced motion — no autoplay.
    var reduceMotion =
      window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!reduceMotion) startAutoplay();

    // Make sure the first slide is positioned correctly even if the browser
    // restored a scroll position.
    requestAnimationFrame(function () {
      scrollTo(0, "auto");
    });
  }

  function boot() {
    var roots = document.querySelectorAll("[data-carousel]");
    for (var i = 0; i < roots.length; i++) init(roots[i]);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
