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
  // Without this the name a feed will show up under is a surprise until after subscribing,
  // which makes "Custom title" impossible to decide on. Testing the URL reveals it, so run
  // the test on our own once the URL is settled and put the result in the title's
  // placeholder (blank = keep the feed's title, as on the feed edit page).
  var urlInput = document.getElementById('feed-url-input');
  var titleInput = document.getElementById('feed-custom-title');

  // The last URL a test actually ran against. Guards the auto-test: leaving the URL field
  // again (e.g. tabbing on to the title) must not re-test what was just tested.
  var lastTestedUrl = null;

  function resetTitlePlaceholder() {
    if (titleInput) titleInput.placeholder = titleInput.dataset.defaultPlaceholder || '';
  }

  function setTitlePlaceholder(title) {
    if (titleInput && title) titleInput.placeholder = title;
  }

  // Clicking Test or Subscribe blurs the URL field, and that blur would fire the auto-test
  // alongside the click's own request — two responses racing for #feed-test-result. The
  // mousedown lands before the blur, so it can wave the auto-test off.
  var skipAutoTest = false;

  // `force` is for picking a detected feed: that button lives inside the form, so its own
  // mousedown would otherwise wave off the very test it wants.
  function autoTest(force) {
    if (!urlInput || (skipAutoTest && !force)) return;
    var url = urlInput.value.trim();
    // Needs a host with a dot to be worth a request — no probing half-typed addresses.
    if (!url || url === lastTestedUrl || !/^(https?:\/\/)?[^\s/]+\.[^\s/]{2,}/.test(url)) return;
    htmx.trigger(urlInput, 'feedAutoTest');
  }

  var subscribeForm = document.getElementById('feed-subscribe-form');
  if (subscribeForm) {
    subscribeForm.addEventListener('mousedown', function (evt) {
      if (!evt.target.closest('button')) return;
      skipAutoTest = true;
      setTimeout(function () { skipAutoTest = false; }, 0);
    });
  }

  if (urlInput) {
    // A stale placeholder would name the previous feed, so drop it as soon as the URL moves.
    urlInput.addEventListener('input', function () {
      if (urlInput.value.trim() !== lastTestedUrl) resetTitlePlaceholder();
    });
    // 'change' fires on blur (and on Enter), but only when the value changed since focus —
    // lastTestedUrl then rules out the case where that change was already tested.
    urlInput.addEventListener('change', function () { autoTest(); });
  }

  // Every test (manual, Enter, or auto) records the URL it ran with, so all three paths
  // keep the guard honest.
  document.body.addEventListener('htmx:configRequest', function (evt) {
    if (evt.detail.path === '/settings/feeds/test') {
      lastTestedUrl = (evt.detail.parameters.url || '').trim();
    }
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
    // The detected list already knows the feed's title — show it right away, then test the
    // picked URL so the count and the "Feed OK" confirmation follow (and so the subscribe
    // reuses that parse from the preview cache instead of fetching again).
    setTitlePlaceholder(btn.dataset.title);
    autoTest(true);
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
