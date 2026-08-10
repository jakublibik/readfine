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

  // --- Subscribe form: show the feed's own title before you commit to it -------------
  // The name a feed will show up under is otherwise a surprise until after subscribing,
  // which makes "Custom title" impossible to decide on. Two places reveal it without
  // costing a request of their own: the Test the user asked for, and the detected-feed
  // list, which reads each candidate's name out of the parse the detection already did.
  // Both land in the title's placeholder (blank = keep the feed's own title, the same
  // convention as the feed edit page).
  //
  // Deliberately not triggered on leaving the URL field. Testing on the user's behalf
  // costs a slot of the endpoint's 10/minute, fires before the auth fields below it are
  // filled in (so an authenticated feed answers 401 and reads as broken), and races the
  // request of whichever button the user was on their way to click.
  var urlInput = document.getElementById('feed-url-input');
  var titleInput = document.getElementById('feed-custom-title');

  function resetTitlePlaceholder() {
    if (titleInput) titleInput.placeholder = titleInput.dataset.defaultPlaceholder || '';
  }

  function setTitlePlaceholder(title) {
    if (titleInput && title) titleInput.placeholder = title;
  }

  if (urlInput) {
    // A placeholder naming the feed that was tested a moment ago would misdescribe the
    // address now in the field, so drop it as soon as that address moves.
    urlInput.addEventListener('input', resetTitlePlaceholder);
  }

  document.body.addEventListener('htmx:configRequest', function (evt) {
    // "Feed X added successfully" is server-rendered and would otherwise sit there for the
    // rest of the visit, still naming the previous feed while you work on the next one.
    if (evt.detail.path === '/settings/feeds/test' || evt.detail.path === '/settings/feeds') {
      var banner = document.getElementById('feed-added-banner');
      if (banner) banner.remove();
    }
  });

  document.body.addEventListener('htmx:afterSwap', function (evt) {
    if (!evt.detail.target || evt.detail.target.id !== 'feed-test-result') return;
    var box = evt.detail.target.querySelector('[data-feed-title]');
    if (box) setTitlePlaceholder(box.dataset.feedTitle);
    else resetTitlePlaceholder();
  });

  // htmx leaves a non-2xx response unswapped, and there is no app-wide handler to fall
  // back on (the htmx:responseError listeners in app.js are each scoped to the article
  // list, the star revert or the chat). So a Test that tripped this endpoint's
  // 10/minute did nothing whatsoever: the spinner stopped, the box stayed empty and the
  // user had no way to know a limit existed, let alone that waiting would fix it.
  document.body.addEventListener('htmx:responseError', function (evt) {
    var cfg = evt.detail.requestConfig;
    var path = cfg && cfg.path;
    if (path !== '/settings/feeds/test' && path !== '/settings/feeds') return;
    var box = document.getElementById('feed-test-result');
    if (!box) return;
    var status = evt.detail.xhr ? evt.detail.xhr.status : 0;
    var line = document.createElement('p');
    line.className = 'text-sm text-red-600';
    line.textContent = status === 429
      ? 'Too many feed tests in a row. Wait a minute and try again.'
      : 'The request failed (HTTP ' + status + '). Please try again.';
    box.replaceChildren(line);
    // Nothing came back to name, and the previous feed's name must not stand for the
    // address now in the field.
    resetTitlePlaceholder();
  });

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
    // The detected list already knows the feed's name, so show it with no request of its
    // own. Reset first: a candidate the detection could not name (no data-title) would
    // otherwise leave the previously tested feed's name standing.
    resetTitlePlaceholder();
    setTitlePlaceholder(btn.dataset.title);
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
