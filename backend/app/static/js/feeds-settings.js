document.addEventListener('DOMContentLoaded', function () {
  // The success banner is driven by the ?added= param the subscribe POST redirects
  // to. Strip it from the address bar so a manual page refresh doesn't re-show a
  // stale "added successfully" banner — this load already rendered it server-side.
  (function () {
    var params = new URLSearchParams(window.location.search);
    if (params.has('added')) {
      params.delete('added');
      var qs = params.toString();
      history.replaceState(null, '', window.location.pathname + (qs ? '?' + qs : '') + window.location.hash);
    }
  })();

  var tabFeeds = document.getElementById('tab-feeds');
  var tabStats = document.getElementById('tab-stats');
  if (!tabFeeds || !tabStats) return;

  var activeClasses = ['bg-white', 'dark:bg-gray-700', 'shadow-sm', 'text-gray-900', 'dark:text-gray-100'];
  var inactiveClasses = ['text-gray-500', 'dark:text-gray-400', 'hover:bg-white', 'dark:hover:bg-gray-700', 'hover:shadow-sm'];

  function activate(which) {
    var on = which === 'stats' ? tabStats : tabFeeds;
    var off = which === 'stats' ? tabFeeds : tabStats;
    on.classList.add(...activeClasses);
    on.classList.remove(...inactiveClasses);
    off.classList.remove(...activeClasses);
    off.classList.add(...inactiveClasses);
  }

  tabFeeds.addEventListener('click', function () { activate('feeds'); });
  tabStats.addEventListener('click', function () { activate('stats'); });

  // Detected-feed "Select RSS" buttons are injected via HTMX into the feed test
  // result. Delegated click so it works on swapped-in content (and because the
  // nonce-based CSP blocks the inline onclick these buttons used to carry).
  document.body.addEventListener('click', function (evt) {
    var btn = evt.target.closest('[data-select-detected-feed]');
    if (!btn) return;
    var input = document.getElementById('feed-url-input');
    if (!input) return;
    input.value = btn.dataset.url || '';
    input.focus();
  });

  // Creating a folder re-renders the Feeds list into #feeds-list. If the Stats tab was
  // active, sync the highlight back to Feeds so it can't show "Stats" over a feeds list.
  document.body.addEventListener('htmx:afterSwap', function (evt) {
    var cfg = evt.detail.requestConfig;
    if (cfg && cfg.path && cfg.path.indexOf('/settings/folders') !== -1) {
      activate('feeds');
    }
  });

  // Folder create/rename/delete also re-renders the subscribe form's folder dropdown
  // out-of-band. That replaces the <select> node, so the user's current choice would be
  // lost — capture it before the swap and restore it after (unless that folder is gone).
  var savedFolderValue = null;
  document.body.addEventListener('htmx:beforeSwap', function (evt) {
    var cfg = evt.detail.requestConfig;
    if (cfg && cfg.path && cfg.path.indexOf('/settings/folders') !== -1) {
      var sel = document.getElementById('subscribe-folder-select');
      savedFolderValue = sel ? sel.value : null;
    }
  });
  document.body.addEventListener('htmx:afterSettle', function (evt) {
    var cfg = evt.detail.requestConfig;
    if (cfg && cfg.path && cfg.path.indexOf('/settings/folders') !== -1 && savedFolderValue !== null) {
      var sel = document.getElementById('subscribe-folder-select');
      if (sel && Array.prototype.some.call(sel.options, function (o) { return o.value === savedFolderValue; })) {
        sel.value = savedFolderValue;
      }
      savedFolderValue = null;
    }
  });
});
