// Critical init: runs synchronously before first paint to prevent layout flash.
(function () {
  var pinned = true;
  try { pinned = localStorage.getItem('sidebarPinned') !== 'false'; } catch (e) {}
  window._sidebarPinned = pinned;
  if (!pinned) document.documentElement.classList.add('sidebar-unpinned');

  var LAYOUT_DEFAULTS = { small: '1', medium: '2', large: '3' };
  var w = window.innerWidth;
  // Read the user's custom breakpoints from server-rendered <html> data attributes so
  // the first-paint bucket matches what app.js computes later (no layout flash on reload).
  var smallMax = parseInt(document.documentElement.dataset.bucketSmallMax, 10) || 640;
  var mediumMax = parseInt(document.documentElement.dataset.bucketMediumMax, 10) || 1100;
  var bucket = w <= smallMax ? 'small' : w <= mediumMax ? 'medium' : 'large';
  var layout;
  try { layout = localStorage.getItem('layout_' + bucket); } catch (e) {}
  document.documentElement.dataset.bucket = bucket;
  document.documentElement.dataset.layout = layout || LAYOUT_DEFAULTS[bucket];

  if (bucket === 'small') {
    var sidebarMode;
    try { sidebarMode = localStorage.getItem('sidebar_mode_small'); } catch (e) {}
    var resolvedMode = sidebarMode || 'hideable-up';
    if (resolvedMode === 'hideable') resolvedMode = 'hideable-up';
    document.documentElement.dataset.sidebarMode = resolvedMode;
  }

  // Dark mode: apply before first paint
  var _cs;
  try { _cs = localStorage.getItem('colorScheme'); } catch (e) {}
  _cs = _cs || 'system';
  if (_cs === 'dark' || (_cs === 'system' && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
    document.documentElement.classList.add('dark');
  }

  // Browser/OS chrome around the page (address bar, status bar) follows the shell
  // background. Shared with the other two places that toggle .dark — app.js's
  // system-preference listener and the scheme picker in settings — so the bar
  // never keeps the colour of the scheme the user just left.
  window.syncThemeColor = function () {
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) {
      meta.setAttribute(
        'content',
        document.documentElement.classList.contains('dark') ? '#141414' : '#f9fafb'
      );
    }
  };
  window.syncThemeColor();
})();
