// Settings → Preferences. The form is boosted, so after every save htmx swaps in a new
// body and this file runs again. Listeners on elements of the page are therefore added
// fresh each time (the old elements are gone with their listeners); the two on window
// are added once and look the elements up when they fire.
(function () {
  // ── Color scheme (per device, kept in localStorage) ─────────────────────────
  function applyColorScheme(scheme) {
    try { localStorage.setItem('colorScheme', scheme); } catch (e) {}
    if (scheme === 'dark') {
      document.documentElement.classList.add('dark');
    } else if (scheme === 'light') {
      document.documentElement.classList.remove('dark');
    } else {
      document.documentElement.classList.toggle('dark',
        !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches));
    }
    if (window.syncThemeColor) window.syncThemeColor();
  }

  var cs;
  try { cs = localStorage.getItem('colorScheme'); } catch (e) {}
  cs = cs || 'system';
  var radio = document.querySelector('input[name="color_scheme_device"][value="' + cs + '"]');
  if (radio) radio.checked = true;
  document.querySelectorAll('input[name="color_scheme_device"]').forEach(function (el) {
    el.addEventListener('change', function () { applyColorScheme(this.value); });
  });

  // ── Install as an app ───────────────────────────────────────────────────────
  // The event this hangs on is caught in init.js, which runs before any deferred
  // script and so cannot miss it; here we only decide what to show.
  function els() {
    var row = document.getElementById('install-app-row');
    var btn = document.getElementById('install-app-btn');
    var hint = document.getElementById('install-app-hint');
    var title = document.getElementById('install-app-title');
    return row && btn && hint && title ? { row: row, btn: btn, hint: hint, title: title } : null;
  }

  function isInstalled() {
    return (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches)
      || navigator.standalone === true;
  }

  // iPadOS 13+ calls itself a Mac, so the touch points are what tell the two apart.
  function isIos() {
    return /iPad|iPhone|iPod/.test(navigator.userAgent)
      || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  }

  function show(el, heading, text, withButton) {
    el.title.textContent = heading;
    el.hint.textContent = text;
    el.btn.hidden = !withButton;
    el.row.classList.remove('hidden');
  }

  var el = els();
  if (!el) return;

  // Inside the installed app there is nothing left to offer, so the card stays hidden
  // and neither listener below is wanted: an installed window is not asked to install
  // again. Deliberately not a confirmation line either, since the only true thing it
  // could say is that you are in the installed app, which the missing address bar
  // already says. The appinstalled message further down is a different case: that one
  // runs in the browser tab you pressed Install in, where naming where the app went is
  // the whole point.
  if (isInstalled()) return;

  if (isIos()) {
    // No prompt exists on iOS and installing cannot be triggered from a page, so the
    // steps are all we can offer. The warning about signing in is not padding: iOS
    // gives an installed app its own storage, so the session does not come across and
    // the first launch looks like a login that failed.
    show(el, 'Install Readfine',
         'Open the Share menu, then Add to Home Screen. You will be asked to sign in '
       + 'again there: iOS keeps an installed app’s data separate from the browser.', false);
  } else if (window._installPrompt) {
    el.row.classList.remove('hidden');
  }

  if (!window._prefsInstallListeners) {
    window._prefsInstallListeners = true;

    // Late arrival: the event can land after this page has rendered.
    window.addEventListener('beforeinstallprompt', function (e) {
      e.preventDefault();
      window._installPrompt = e;
      var cur = els();
      if (!cur) return;
      cur.btn.hidden = false;
      cur.row.classList.remove('hidden');
    });

    window.addEventListener('appinstalled', function () {
      window._installPrompt = null;
      var cur = els();
      if (cur) show(cur, 'Readfine is installed', 'Open it from your home screen or app list.', false);
    });
  }

  el.btn.addEventListener('click', function () {
    var evt = window._installPrompt;
    if (!evt) { el.row.classList.add('hidden'); return; }
    // Single use, whatever the answer: a spent event cannot be raised again.
    window._installPrompt = null;
    el.btn.disabled = true;
    evt.prompt();
    evt.userChoice.then(function (choice) {
      el.btn.disabled = false;
      // Accepted: appinstalled arrives and rewrites the row. Dismissed: the event is
      // gone, so take the button away rather than leave one that does nothing. Chrome
      // may offer another later, and the listener above puts it back.
      if (!choice || choice.outcome !== 'accepted') el.row.classList.add('hidden');
    }, function () {
      el.btn.disabled = false;
      el.row.classList.add('hidden');
    });
  });
})();
