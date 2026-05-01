// Critical init: runs synchronously before first paint to prevent layout flash.
(function () {
  var pinned = true;
  try { pinned = localStorage.getItem('sidebarPinned') !== 'false'; } catch (e) {}
  window._sidebarPinned = pinned;
  if (!pinned) document.documentElement.classList.add('sidebar-unpinned');

  var LAYOUT_DEFAULTS = { small: '1', medium: '2', large: '3' };
  var w = window.innerWidth;
  var bucket = w <= 640 ? 'small' : w <= 1100 ? 'medium' : 'large';
  var layout;
  try { layout = localStorage.getItem('layout_' + bucket); } catch (e) {}
  document.documentElement.dataset.bucket = bucket;
  document.documentElement.dataset.layout = layout || LAYOUT_DEFAULTS[bucket];

  if (bucket === 'small') {
    var sidebarMode;
    try { sidebarMode = localStorage.getItem('sidebar_mode_small'); } catch (e) {}
    var resolvedMode = sidebarMode || 'collapsible';
    if (resolvedMode === 'hideable') resolvedMode = 'hideable-up';
    document.documentElement.dataset.sidebarMode = resolvedMode;
  }
})();
