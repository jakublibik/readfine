// ── Generic: clear named input after HTMX swap (data-clear-on-swap="fieldname") ──
document.addEventListener('htmx:afterSwap', function (e) {
  var form = e.detail && e.detail.elt;
  if (!form || !form.dataset || !form.dataset.clearOnSwap) return;
  var inp = form.querySelector('[name="' + form.dataset.clearOnSwap + '"]');
  if (inp) inp.value = '';
});

// ── Generic: reset form after HTMX request (data-reset-on-request) ───────
document.addEventListener('htmx:afterRequest', function (e) {
  var el = e.detail && e.detail.elt;
  if (!el || !el.dataset || !('resetOnRequest' in el.dataset)) return;
  el.reset();
});

// ── Mobile side-nav: scroll the active tab into view on load ──────────────
(function () {
  function scrollActiveNavIntoView() {
    var active = document.querySelector('[data-mobile-nav] [data-mobile-nav-active]');
    if (!active) return;
    var bar = active.closest('[data-mobile-nav]');
    if (!bar || bar.offsetParent === null) return; // hidden (desktop): skip
    // Horizontally center the active tab without scrolling the page vertically.
    bar.scrollLeft = active.offsetLeft - (bar.clientWidth - active.offsetWidth) / 2;
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', scrollActiveNavIntoView);
  } else {
    scrollActiveNavIntoView();
  }
})();

// ── Sidebar: remove touch-active class after feed refresh ─────────────────
document.addEventListener('htmx:afterRequest', function (e) {
  var btn = e.detail && e.detail.elt;
  if (!btn || btn.dataset.action !== 'feed-refresh') return;
  var row = btn.closest('.feed-nav-row');
  if (row) row.classList.remove('touch-active');
  btn.blur();
});

// ── Dark mode: system preference live listener ────────────────────────────
(function () {
  var mq = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)');
  if (!mq) return;
  mq.addEventListener('change', function () {
    var cs;
    try { cs = localStorage.getItem('colorScheme'); } catch(e) {}
    if ((cs || 'system') === 'system') {
      document.documentElement.classList.toggle('dark', mq.matches);
    }
  });
})();

// ── HTMX configRequest: inject dynamic params without eval ────────────────
document.body.addEventListener('htmx:configRequest', function (e) {
  var elt = e.detail.elt;
  // Sidebar pinned state
  if (elt.id === 'sidebar') {
    var bucket = document.documentElement.dataset.bucket;
    if (bucket === 'small') {
      var sidebarMode = document.documentElement.dataset.sidebarMode;
      if (sidebarMode === 'collapsible') {
        // Rail unless overlay is open
        e.detail.parameters['pinned'] = document.documentElement.classList.contains('mobile-sidebar-open') ? 'true' : 'false';
      } else {
        // Hideable: always full sidebar
        e.detail.parameters['pinned'] = 'true';
      }
    } else {
      e.detail.parameters['pinned'] = window._sidebarPinned ? 'true' : 'false';
    }
  }
  // Mark-read before timestamp
  if (elt.dataset && elt.dataset.action === 'mark-read') {
    e.detail.parameters['before'] = new Date().toISOString();
  }
});

// ── Layout bucket system ──────────────────────────────────────────────────
(function () {
  var LAYOUT_DEFAULTS = { small: '1', medium: '2', large: '3' };

  function getBuckets() {
    var m = document.querySelector('meta[name="app-buckets"]');
    if (m) return { smallMax: parseInt(m.dataset.small), mediumMax: parseInt(m.dataset.medium) };
    return { smallMax: 640, mediumMax: 1100 };
  }

  function getCurrentBucket() {
    var b = getBuckets();
    var w = window.innerWidth;
    if (w <= b.smallMax) return 'small';
    if (w <= b.mediumMax) return 'medium';
    return 'large';
  }

  function getLayout(bucket) {
    try {
      return localStorage.getItem('layout_' + bucket) || LAYOUT_DEFAULTS[bucket];
    } catch (e) {
      return LAYOUT_DEFAULTS[bucket];
    }
  }

  function applyBucket() {
    var bucket = getCurrentBucket();
    var layout = getLayout(bucket);
    var html = document.documentElement;
    html.dataset.bucket = bucket;
    html.dataset.layout = layout;
    if (bucket === 'small') {
      var mode;
      try { mode = localStorage.getItem('sidebar_mode_small'); } catch (e) {}
      mode = mode || 'hideable-up';
      if (mode === 'hideable') {
        mode = 'hideable-up';
        try { localStorage.setItem('sidebar_mode_small', 'hideable-up'); } catch (e) {}
      }
      html.dataset.sidebarMode = mode;
    } else {
      html.classList.remove('mobile-sidebar-open', 'mobile-detail-open');
    }
  }

  window._getLayout = getLayout;
  window._getCurrentBucket = getCurrentBucket;
  window._applyBucket = applyBucket;

  document.addEventListener('DOMContentLoaded', applyBucket);
  window.addEventListener('resize', applyBucket);
})();

// Returns true when article clicks should expand inline in the list
function _shouldUseInline() {
  var bucket = document.documentElement.dataset.bucket;
  if (bucket === 'small') {
    try { return (localStorage.getItem('detail_mode_small') || 'inline') === 'inline'; } catch (e) { return true; }
  }
  return document.documentElement.dataset.layout === '2';
}

// ── Open article content links in a new tab
function openProseLinksInNewTab(root) {
  (root || document).querySelectorAll('.prose a[href]').forEach(function (a) {
    a.setAttribute('target', '_blank');
    a.setAttribute('rel', 'noopener noreferrer');
  });
}
document.addEventListener('DOMContentLoaded', function () { openProseLinksInNewTab(); });
document.body.addEventListener('htmx:afterSettle', function () { openProseLinksInNewTab(); });

// Hide duplicate headings emitted by feed/readable content when they repeat the article title.
function normalizeArticleHeading(text) {
  var normalized = (text || '').replace(/\s+/g, ' ').trim().toLowerCase();
  if (normalized.normalize) {
    normalized = normalized.normalize('NFKD').replace(/[\u0300-\u036f]/g, '');
  }
  return normalized
    .replace(/["'`]/g, '')
    .replace(/[.,!?;:()[\]{}]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function articleHeadingMatchesTitle(headingText, titleText) {
  var heading = normalizeArticleHeading(headingText);
  var title = normalizeArticleHeading(titleText);
  if (!heading || !title) return false;
  if (heading === title) return true;
  if (heading.slice(0, 80) === title.slice(0, 80)) return true;

  // Readable extractors often return just the article headline while feeds include
  // a site suffix, for example "Headline - Publisher".
  if (heading.length >= 20 && title.indexOf(heading) === 0) return true;
  if (title.length >= 20 && heading.indexOf(title) === 0) return true;
  return false;
}

function hideDuplicateH1() {
  // The article title is always shown outside the body — in the article list beside
  // the content (inline view) or in the detail header (single-column + 3-panel) —
  // so a body heading repeating it is redundant in every layout. The content lives in
  // a different container depending on how the article was opened: the inline shell when
  // expanded in the list (medium 2-panel AND small/mobile inline mode), the right panel
  // otherwise. Detect by presence of the inline shell rather than by layout alone, since
  // the small bucket uses inline expansion driven by detail_mode_small, not layout==='2'.
  var container = document.getElementById('inline-article-detail-content')
    || document.getElementById('article-detail');
  if (!container) return;
  // data-title lives on the inner <article>, not on the outer [data-article-id] root.
  var articleEl = container.querySelector('[data-title]');
  if (!articleEl) return;
  var content = articleEl.querySelector('#article-content-' + articleEl.dataset.articleId);
  var prose = (content && content.querySelector('.prose')) || articleEl.querySelector('.prose');
  if (!prose) return;
  var headings = prose.querySelectorAll('h1, h2');
  for (var i = 0; i < headings.length && i < 3; i += 1) {
    if (articleHeadingMatchesTitle(headings[i].textContent, articleEl.dataset.title || '')) {
      headings[i].style.display = 'none';
      break;
    }
  }
}
document.addEventListener('DOMContentLoaded', hideDuplicateH1);
document.body.addEventListener('htmx:afterSettle', hideDuplicateH1);

// Dates are formatted server-side (Jinja `localtime`/`utctime` filters using the
// user's stored timezone), so no client-side localization is needed.

// Copy-to-clipboard for [data-copy] buttons
document.addEventListener('click', function (e) {
  var btn = e.target.closest('.copy-btn[data-copy]');
  if (!btn) return;
  navigator.clipboard.writeText(btn.dataset.copy).then(function () {
    var svg = btn.querySelector('svg');
    svg.style.display = 'none';
    btn.insertAdjacentText('beforeend', '✓');
    btn.classList.add('text-green-500');
    setTimeout(function () {
      btn.lastChild.remove();
      svg.style.display = '';
      btn.classList.remove('text-green-500');
    }, 1500);
  });
});

// Copy-from-element for [data-copy-from] buttons — reads text from element with given ID
document.addEventListener('click', function (e) {
  var btn = e.target.closest('.copy-from-btn[data-copy-from]');
  if (!btn) return;
  var el = document.getElementById(btn.dataset.copyFrom);
  if (!el) return;
  var text = el.value || el.textContent || '';
  navigator.clipboard.writeText(text).then(function () {
    var orig = btn.textContent;
    btn.textContent = 'Copied!';
    btn.classList.add('text-green-600');
    setTimeout(function () {
      btn.textContent = orig;
      btn.classList.remove('text-green-600');
    }, 2000);
  });
});

// AI CSS selector generation — fill selector input and trigger preview after generation
document.body.addEventListener('selectorGenerated', function (e) {
  var input = document.getElementById('selector-input');
  if (input) input.value = e.detail.selector || '';
  var previewBtn = document.getElementById('preview-btn');
  if (previewBtn) previewBtn.click();
});

// [data-menu-toggle] button opens/closes its next sibling [data-menu]; click outside closes all
// Menu uses position:fixed — immune to parent overflow clipping
document.addEventListener('click', function (e) {
  var toggle = e.target.closest('[data-menu-toggle]');
  if (toggle) {
    var menu = toggle.nextElementSibling;
    if (!menu) return;
    var isOpen = !menu.classList.contains('hidden');
    document.querySelectorAll('[data-menu]').forEach(function (m) { m.classList.add('hidden'); });
    if (!isOpen) {
      var rect = toggle.getBoundingClientRect();
      var menuMinW = 144; // min-w-36
      menu.style.top = (rect.bottom + 4) + 'px';
      if (rect.right >= menuMinW) {
        menu.style.right = (window.innerWidth - rect.right) + 'px';
        menu.style.left = '';
      } else {
        menu.style.left = Math.max(4, rect.left) + 'px';
        menu.style.right = '';
      }
      menu.classList.remove('hidden');
    }
    return;
  }
  if (!e.target.closest('[data-menu]')) {
    document.querySelectorAll('[data-menu]').forEach(function (m) { m.classList.add('hidden'); });
  }
});
document.body.addEventListener('htmx:afterRequest', function (e) {
  var menu = e.target.closest('[data-menu]');
  if (menu && !menu.hasAttribute('data-menu-persist')) menu.classList.add('hidden');
});

// Nav active state — persists across sidebarRefresh swaps
var _activeNavGet = null;
var _navSnapshot = null;

function _saveNavSnapshot() {
  var titleEl = document.getElementById('mobile-title-text');
  _navSnapshot = {
    url: _activeNavGet,
    title: titleEl ? titleEl.textContent : null,
  };
}

function _revertNavSnapshot() {
  if (!_navSnapshot) return;
  var snap = _navSnapshot;
  _navSnapshot = null;
  _activeNavGet = snap.url;
  try { if (snap.url) localStorage.setItem('lastNavItem', snap.url); } catch (e) {}
  // Restore the desktop sidebar active highlight to the previous nav item.
  if (snap.url) {
    document.querySelectorAll('.nav-item').forEach(function (i) { i.classList.remove('active'); });
    var prev = document.querySelector('.nav-item[hx-get="' + snap.url + '"]');
    if (prev) prev.classList.add('active');
  }
  var titleEl = document.getElementById('mobile-title-text');
  if (titleEl && snap.title !== null) {
    titleEl.textContent = snap.title;
    try { localStorage.setItem('mobile_title_text', snap.title); } catch (e) {}
  }
  _syncMobileQuicklink();
  if (snap.url) htmx.ajax('GET', snap.url, { target: '#article-list', swap: 'innerHTML' });
}

function showToast(msg, type) {
  var bg = type === 'error' ? '#b91c1c' : type === 'ok' ? '#15803d' : type === 'warning' ? '#b45309' : '#374151';
  var id = 'app-toast-' + Date.now();
  var toast = document.createElement('div');
  toast.id = id;
  toast.textContent = msg;
  toast.style.cssText = 'position:fixed;bottom:4rem;left:50%;transform:translateX(-50%);' +
    'background:' + bg + ';color:#fff;padding:0.5rem 1rem;border-radius:0.5rem;' +
    'font-size:0.8rem;z-index:9999;max-width:90vw;word-break:break-word;pointer-events:none;';
  document.body.appendChild(toast);
  setTimeout(function () { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 4000);
}

function _showNavErrorToast() {
  showToast('Connection error — restoring previous view', 'info');
}

document.body.addEventListener('showToast', function (e) {
  showToast(e.detail.msg, e.detail.type);
});

document.body.addEventListener('htmx:sendError', function (e) {
  if (!e.detail.target || e.detail.target.id !== 'article-list') return;
  if (!_navSnapshot) return;
  _showNavErrorToast();
  _revertNavSnapshot();
});

// Server returned 4xx/5xx for the article-list load — htmx leaves the old list in
// place, so revert the nav chrome to match instead of showing a stale list under a
// new title/active item.
document.body.addEventListener('htmx:responseError', function (e) {
  if (!e.detail.target || e.detail.target.id !== 'article-list') return;
  if (!_navSnapshot) return;
  _showNavErrorToast();
  _revertNavSnapshot();
});

// A successful list load commits the new nav state — drop the snapshot so a later
// unrelated #article-list error can't revert to a stale view.
document.body.addEventListener('htmx:afterSwap', function (e) {
  if (e.detail.target && e.detail.target.id === 'article-list') _navSnapshot = null;
});

// Dismissing the AI error (Settings → AI) fires this via HX-Trigger; clear any
// AI error dots still rendered on the page (e.g. the side-nav "AI" badge).
document.body.addEventListener('ai-error-dismissed', function () {
  document.querySelectorAll('.ai-error-badge').forEach(function (el) { el.remove(); });
});

document.addEventListener('click', function (e) {
  var navItem = e.target.closest('.nav-item');
  if (!navItem) return;
  _saveNavSnapshot(); // capture previous nav state so a failed list load can revert
  document.querySelectorAll('.nav-item').forEach(function (i) { i.classList.remove('active'); });
  navItem.classList.add('active');
  _activeNavGet = navItem.getAttribute('hx-get');
  try { if (_activeNavGet) localStorage.setItem('lastNavItem', _activeNavGet); } catch (err) {}
  if (_activeNavGet) htmx.trigger(document.body, 'sidebarRefresh');
});

document.body.addEventListener('htmx:beforeSwap', function (evt) {
  if (evt.detail.target.id !== 'sidebar') return;
  // Preserve scroll: the feed list scrolls inside #sidebar-scroll, which lives
  // inside the swapped innerHTML and is destroyed/recreated on every refresh —
  // resetting scrollTop to 0 and jumping the list up on background refreshes
  // (e.g. the mark-as-read sidebarRefresh after opening an article).
  var oldScroll = evt.detail.target.querySelector('#sidebar-scroll');
  window._sidebarScroll = oldScroll ? oldScroll.scrollTop : 0;
  var navGet = _activeNavGet || '/htmx/articles';
  var temp = document.createElement('div');
  temp.innerHTML = evt.detail.serverResponse;
  var match = temp.querySelector('.nav-item[hx-get="' + navGet + '"]');
  if (match) {
    temp.querySelectorAll('.nav-item').forEach(function (i) { i.classList.remove('active'); });
    match.classList.add('active');
  }
  evt.detail.serverResponse = temp.innerHTML;
});

function _syncMobileQuicklink() {
  var isLabels = _activeNavGet && _activeNavGet.indexOf('labeled_only=true') !== -1;
  var text = isLabels ? 'Starred →' : 'Labels →';
  var link = document.getElementById('mobile-title-quicklink');
  if (link) link.textContent = text;
  var bottomLink = document.getElementById('mobile-bottom-quicklink');
  if (bottomLink) bottomLink.textContent = text;
}

// Restore last-selected nav on page load; fall back to All Articles
function _autoLoadArticleList() {
  if (!document.getElementById('article-list')) return;
  var saved;
  try { saved = localStorage.getItem('lastNavItem'); } catch (e) {}
  var url = saved || '/htmx/articles';
  _activeNavGet = url;
  _syncMobileQuicklink();
  htmx.ajax('GET', url, { target: '#article-list', swap: 'innerHTML' });
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _autoLoadArticleList);
} else {
  _autoLoadArticleList();
}

// On page load: restore fullscreen article from URL hash (small bucket + fullscreen mode only).
// Converts the hash URL to a list URL first (replaceState), so Back returns to the list.
function _restoreArticleFromHash() {
  if (window._getCurrentBucket() !== 'small') return;
  try { if (localStorage.getItem('detail_mode_small') !== 'fullscreen') return; } catch (e) { return; }
  var match = window.location.hash.match(/^#article-(\d+)$/);
  if (!match) return;
  history.replaceState(null, '', window.location.pathname);
  htmx.ajax('GET', '/htmx/articles/' + match[1], { target: '#article-detail', swap: 'innerHTML' });
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _restoreArticleFromHash);
} else {
  _restoreArticleFromHash();
}

// On page load with a deep-link (?open_article_id=…), show the detail as a
// fullscreen overlay in non-large layouts where #article-detail is hidden.
// In the large/3-panel layout the panel is already visible — nothing to do.
function _initDeeplinkDetail() {
  // Read the URL param (not the loader element) — htmx may have already swapped it away.
  if (!/[?&]open_article_id=/.test(window.location.search)) return;
  if (window._getCurrentBucket() === 'large') return;
  document.documentElement.classList.add('deeplink-detail-open');
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _initDeeplinkDetail);
} else {
  _initDeeplinkDetail();
}

// ── Mobile title bar count badge ──────────────────────────────────────────
function _setTitleBarCount(count, type) {
  window._titleBarCountType = type || null;
  var badge = document.getElementById('mobile-title-count');
  if (!badge) return;
  if (count == null || count <= 0 || !type) {
    badge.className = 'hidden';
    return;
  }
  badge.textContent = count;
  if (type === 'starred') {
    badge.className = 'flex-shrink-0 text-[13px] font-medium text-gray-400 relative -top-[0.5px]';
  } else {
    badge.className = 'flex-shrink-0 text-xs font-medium bg-blue-100 text-blue-700 px-1.5 py-0.5 rounded-full';
  }
}

// Batched mark-as-read — collects IDs and sends one request per debounce window
var _pendingMarkRead = new Set();
var _markReadTimer = null;

function _flushMarkRead() {
  _markReadTimer = null;
  if (_pendingMarkRead.size === 0) return;
  var ids = Array.from(_pendingMarkRead);
  _pendingMarkRead.clear();
  var csrfToken = document.cookie.split('; ').find(function (r) { return r.startsWith('csrftoken='); });
  csrfToken = csrfToken ? csrfToken.split('=')[1] : '';
  fetch('/htmx/articles/set-read-batch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'x-csrftoken': csrfToken },
    body: JSON.stringify({ ids: ids }),
    credentials: 'same-origin',
  }).then(function (r) {
    if (r.ok) htmx.trigger(document.body, 'sidebarRefresh');
  }).catch(function (e) { console.warn('mark-read-batch failed:', e); });
}

function _queueMarkRead(id) {
  if (window._dwellSend) window._dwellSend();
  _pendingMarkRead.add(id);
  clearTimeout(_markReadTimer);
  _markReadTimer = setTimeout(_flushMarkRead, 500);
}

document.addEventListener('visibilitychange', function () {
  if (document.visibilityState === 'hidden') _flushMarkRead();
});
window.addEventListener('beforeunload', function () { _flushMarkRead(); });

// Article list: IntersectionObserver for mark-as-read on scroll
document.body.addEventListener('htmx:afterSettle', function (evt) {
  if (evt.detail.target.id !== 'article-list') return;

  var cfgEl = document.getElementById('article-list-cfg');
  if (!cfgEl) return;
  var cfg = JSON.parse(cfgEl.textContent);
  _setTitleBarCount(cfg.titleBarCount, cfg.titleBarCountType);

  var list = document.getElementById('article-list');
  if (!list) return;

  if (window._articleListMutationObserver) {
    window._articleListMutationObserver.disconnect();
    window._articleListMutationObserver = null;
  }
  window._articleReadObserver = null;

  if (!cfg.markReadOnScroll) return;

  var seen = new Set();

  var topPanel = document.getElementById('mobile-title-bar');
  var barVisible = topPanel && getComputedStyle(topPanel).display !== 'none';
  var barHeight = barVisible ? Math.round(topPanel.getBoundingClientRect().height) : 0;
  var topOffset = barVisible ? barHeight : 0;
  var bottomOffset = 0;

  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      var el = entry.target;
      var id = el.dataset.articleId;
      var isRead = el.dataset.isRead === 'true';

      if (entry.isIntersecting) {
        seen.add(id);
      } else if (!isRead && entry.boundingClientRect.top < 0) {
        seen.delete(id);
        el.dataset.isRead = 'true';
        el.classList.add('opacity-75');
        var titleEl = el.querySelector('[data-article-title]');
        if (titleEl) {
          titleEl.classList.remove('font-bold', 'text-gray-900');
          titleEl.classList.add('font-medium', 'text-gray-800');
        }
        _queueMarkRead(id);
        if (window._titleBarCountType === 'unread') {
          var badge = document.getElementById('mobile-title-count');
          if (badge && !badge.classList.contains('hidden')) {
            _setTitleBarCount(Math.max(0, parseInt(badge.textContent, 10) - 1), 'unread');
          }
        }
      }
    });
  }, { root: list, threshold: 0.1, rootMargin: '-' + topOffset + 'px 0px -' + bottomOffset + 'px 0px' });

  window._articleReadObserver = observer;

  list.querySelectorAll('.article-row').forEach(function (el) {
    observer.observe(el);
  });

  // Watch for article rows appended by infinite scroll sentinel swaps
  var mutObs = new MutationObserver(function (mutations) {
    mutations.forEach(function (mutation) {
      mutation.addedNodes.forEach(function (node) {
        if (node.nodeType !== 1) return;
        if (node.classList.contains('article-row')) {
          window._articleReadObserver.observe(node);
        }
      });
    });
  });
  mutObs.observe(list, { childList: true });
  window._articleListMutationObserver = mutObs;
});

// Sidebar pin toggle — localStorage + CSS class, no server state
document.body.addEventListener('click', function (e) {
  if (!e.target.closest('[data-action="toggle-sidebar-pin"]')) return;
  var next = !window._sidebarPinned;
  window._sidebarPinned = next;
  try { localStorage.setItem('sidebarPinned', next ? 'true' : 'false'); } catch (err) {}
  var html = document.documentElement;
  html.classList.toggle('sidebar-unpinned', !next);
  var sidebar = document.getElementById('sidebar');
  if (sidebar) {
    sidebar.classList.toggle('w-60', next);
    sidebar.classList.toggle('w-12', !next);
  }
  htmx.trigger(document.body, 'sidebarRefresh');
});

// Focus pw-error element when present (password change error in preferences)
function _focusPwError() {
  var el = document.getElementById('pw-error');
  if (el) el.focus();
}
document.addEventListener('DOMContentLoaded', _focusPwError);
document.body.addEventListener('htmx:afterSettle', _focusPwError);

// Article detail: auto mark-as-read timer
document.body.addEventListener('htmx:afterSettle', function (evt) {
  var targetId = evt.detail.target.id;
  var isDetailTarget = targetId === 'article-detail' || targetId === 'inline-article-detail-content';
  if (!isDetailTarget) return;

  var articleEl = evt.detail.target.querySelector('[data-article-id]');
  if (!articleEl) return;

  var articleId = articleEl.dataset.articleId;
  var isRead = articleEl.dataset.isRead === 'true';
  if (isRead) return;

  var timer = setTimeout(function () {
    var readBtn = evt.detail.target.querySelector('#read-btn-' + articleId);
    if (readBtn) {
      htmx.ajax('POST', '/htmx/articles/' + articleId + '/set-read?state=true', {
        target: '#read-btn-' + articleId,
        swap: 'innerHTML'
      });
    } else {
      // Article no longer in view — mark as read server-side only, skip UI swap
      var csrfToken = document.cookie.split('; ').find(function (r) { return r.startsWith('csrftoken='); });
      csrfToken = csrfToken ? csrfToken.split('=')[1] : '';
      fetch('/htmx/articles/' + articleId + '/set-read?state=true', {
        method: 'POST',
        headers: { 'x-csrftoken': csrfToken },
      }).then(function (r) {
        if (!r.ok) console.warn('mark-as-read fallback failed: ' + r.status);
      }).catch(function () {});
    }
  }, 500);

  evt.detail.target.addEventListener(
    'htmx:beforeRequest', function () { clearTimeout(timer); }, { once: true }
  );
});

// Reset scroll to top when a new article loads into #article-detail
document.body.addEventListener('htmx:afterSwap', function (e) {
  if (e.detail.target.id === 'article-detail') e.detail.target.scrollTop = 0;
});

// OPML import form: intercept submit to send CSRF header with multipart upload
document.addEventListener('DOMContentLoaded', function () {
  var form = document.getElementById('opml-import-form');
  if (!form) return;
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var btn = document.getElementById('opml-submit-btn');
    var busy = document.getElementById('opml-busy');
    btn.disabled = true;
    btn.classList.add('opacity-50', 'cursor-not-allowed');
    busy.classList.remove('hidden');
    busy.classList.add('inline-flex');
    var token = document.cookie.split('; ').find(function (r) { return r.startsWith('csrftoken='); });
    token = token ? token.split('=')[1] : '';
    fetch(form.action, {
      method: 'POST',
      headers: { 'x-csrftoken': token },
      body: new FormData(form),
    }).then(function (r) { return r.text(); }).then(function (html) {
      document.open(); document.write(html); document.close();
    }).catch(function () {
      btn.disabled = false;
      btn.classList.remove('opacity-50', 'cursor-not-allowed');
      busy.classList.add('hidden');
      busy.classList.remove('inline-flex');
    });
  });
});

// Feed subscribe form: auto-check "Private feed" when auth fields are filled
(function () {
  var cb = document.getElementById('feed-is-private');
  var authUser = document.getElementById('feed-auth-user');
  var authPass = document.getElementById('feed-auth-pass');
  if (!cb || !authUser || !authPass) return;
  function autoCheck() {
    if (authUser.value.trim() || authPass.value) cb.checked = true;
  }
  authUser.addEventListener('input', autoCheck);
  authPass.addEventListener('input', autoCheck);
})();

// ── User menu ──────────────────────────────────────────────────────────────
function toggleUserMenu() {
  var el = document.getElementById('full-menu-dropdown');
  if (!el) return;
  el.classList.toggle('hidden');
}

document.addEventListener('click', function (e) {
  if (!e.target.closest('#full-menu-container')) {
    var el = document.getElementById('full-menu-dropdown');
    if (el) el.classList.add('hidden');
  }
});

// ── Dwell stop on bottom un-star ───────────────────────────────────────────
document.body.addEventListener('click', function (e) {
  var btn = e.target.closest('[data-bottom-star]');
  if (!btn) return;
  if (!btn._optimisticStarred) return;
  if (window._dwellSend) window._dwellSend();
});

// ── Link-opened tracking ───────────────────────────────────────────────────
document.body.addEventListener('click', function (e) {
  var link = e.target.closest('a[target="_blank"]');
  if (!link) return;
  var row = link.closest('[data-article-id]');
  if (!row) return;
  var articleId = row.dataset.articleId;
  if (!articleId) return;
  var csrf = (document.cookie.split('; ').find(function (r) { return r.startsWith('csrftoken='); }) || '').split('=')[1] || '';
  fetch('/htmx/articles/' + articleId + '/link-opened', {
    method: 'POST',
    keepalive: true,
    credentials: 'include',
    headers: { 'x-csrftoken': csrf },
  });
});

// ── Search modal ───────────────────────────────────────────────────────────
function openSearchModal() {
  var el = document.getElementById('full-menu-dropdown');
  if (el) el.classList.add('hidden');
  var overlay = document.getElementById('search-modal-overlay');
  if (overlay) overlay.classList.remove('hidden');
  htmx.ajax('GET', '/htmx/search-modal', { target: '#search-modal-content', swap: 'innerHTML' });
}

function closeSearchModal() {
  var overlay = document.getElementById('search-modal-overlay');
  if (overlay) overlay.classList.add('hidden');
}

function updateSearchScope(value) {
  var folderSel = document.getElementById('search-folder-select');
  var feedSel = document.getElementById('search-feed-select');
  if (folderSel) folderSel.disabled = value !== 'folder';
  if (feedSel) feedSel.disabled = value !== 'feed';
}

function submitSearch() {
  var input = document.getElementById('search-input');
  if (!input) return;
  var q = input.value.trim();
  if (!q) { input.focus(); return; }
  var scopeEl = document.querySelector('input[name="search-scope"]:checked');
  if (!scopeEl) return;
  var params = new URLSearchParams({ q: q });
  if (scopeEl.value === 'folder') {
    var sel = document.getElementById('search-folder-select');
    if (sel && sel.value) params.set('folder_id', sel.value);
  } else if (scopeEl.value === 'feed') {
    var sel = document.getElementById('search-feed-select');
    if (sel && sel.value) params.set('feed_id', sel.value);
  }
  htmx.ajax('GET', '/htmx/articles?' + params.toString(), { target: '#article-list', swap: 'innerHTML' });
  closeSearchModal();
}

// Focus search input when modal loads
document.body.addEventListener('htmx:afterSettle', function (evt) {
  if (evt.detail.target.id === 'search-modal-content') {
    var input = document.getElementById('search-input');
    if (input) input.focus();
  }
});

// ── Feedback modal ─────────────────────────────────────────────────────────
function openFeedbackModal() {
  var menu = document.getElementById('full-menu-dropdown');
  if (menu) menu.classList.add('hidden');
  var overlay = document.getElementById('feedback-modal-overlay');
  if (!overlay) return;
  var content = document.getElementById('feedback-modal-content');
  if (content) {
    content.innerHTML = '<div class="py-6 flex items-center justify-center gap-2 text-sm text-gray-400">' +
      '<svg class="animate-spin h-4 w-4 flex-shrink-0" fill="none" viewBox="0 0 24 24">' +
      '<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>' +
      '<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4l3-3-3-3v4a8 8 0 00-8 8z"></path>' +
      '</svg>Loading…</div>';
  }
  overlay.classList.remove('hidden');
  htmx.ajax('GET', '/htmx/feedback', { target: '#feedback-modal-content', swap: 'innerHTML' });
}

function closeFeedbackModal() {
  var overlay = document.getElementById('feedback-modal-overlay');
  if (overlay) overlay.classList.add('hidden');
}

// ── Keyboard shortcuts ─────────────────────────────────────────────────────
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') { closeSearchModal(); closeFeedbackModal(); return; }
  if (e.key === 'Enter' && e.target.id === 'search-input') { submitSearch(); return; }
  if (e.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) {
    e.preventDefault();
    openSearchModal();
  }
});

// ── Briefing modal close via HX-Trigger ───────────────────────────────────
document.addEventListener('closeBriefingModal', function () {
  if (typeof closeBriefingModal === 'function') closeBriefingModal();
});

// ── Config menu ────────────────────────────────────────────────────────────
function closeAllConfigMenus() {
  document.querySelectorAll('[id^="config-menu-"]:not([id*="container"])').forEach(function (m) {
    m.classList.add('hidden');
  });
}

document.addEventListener('click', function (e) {
  if (!e.target.closest('[id^="config-menu-container-"]')) {
    closeAllConfigMenus();
  }
});

function startConfigRename(configId, currentName) {
  closeAllConfigMenus();
  var li = document.getElementById('catchup-config-' + configId);
  if (!li) return;
  var nameBtn = li.querySelector('[data-action="load-catchup-config"]');
  if (!nameBtn) return;
  var originalHtml = nameBtn.outerHTML;

  var input = document.createElement('input');
  input.type = 'text';
  input.value = currentName;
  input.style.cssText = 'flex:1;min-width:0;font-size:0.875rem;border:1px solid #60a5fa;border-radius:0.25rem;padding:1px 0 1px 2px;outline:none;';
  input.id = 'rename-input-' + configId;

  var saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.innerHTML = '<svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7"/></svg>';
  saveBtn.style.cssText = 'flex-shrink:0;color:#2563eb;background:transparent;border:none;cursor:pointer;padding:0;line-height:0;';
  saveBtn.dataset.action = 'save-config-rename';
  saveBtn.dataset.configId = configId;
  saveBtn.title = 'Save';

  var wrapper = document.createElement('div');
  wrapper.style.cssText = 'display:flex;align-items:center;gap:2px;flex:1;min-width:0;';
  wrapper.id = 'rename-wrapper-' + configId;
  wrapper.appendChild(input);
  wrapper.appendChild(saveBtn);

  nameBtn.replaceWith(wrapper);
  input.focus();
  input.select();

  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); saveConfigRename(configId); }
    if (e.key === 'Escape') { cancelConfigRename(configId, originalHtml); }
  });
  input.addEventListener('blur', function (e) {
    setTimeout(function () {
      var w = document.getElementById('rename-wrapper-' + configId);
      if (w && !w.contains(document.activeElement)) {
        cancelConfigRename(configId, originalHtml);
      }
    }, 150);
  });
}

function cancelConfigRename(configId, originalHtml) {
  var wrapper = document.getElementById('rename-wrapper-' + configId);
  if (!wrapper) return;
  var tmp = document.createElement('div');
  tmp.innerHTML = originalHtml;
  wrapper.replaceWith(tmp.firstChild);
}

function saveConfigRename(configId) {
  var input = document.getElementById('rename-input-' + configId);
  if (!input) return;
  var newName = input.value.trim();
  if (!newName) { input.focus(); return; }

  var csrf = (document.cookie.split('; ').find(function (r) { return r.startsWith('csrftoken='); }) || '').split('=')[1] || '';
  var form = new FormData();
  form.append('name', newName);

  fetch('/htmx/catchup-configs/' + configId + '/rename', {
    method: 'PUT',
    credentials: 'include',
    headers: { 'x-csrftoken': csrf, 'HX-Request': 'true' },
    body: form,
  }).then(function (resp) {
    return resp.text();
  }).then(function (html) {
    var wrapper = document.getElementById('catchup-configs-list-wrapper');
    if (wrapper) { wrapper.innerHTML = html; htmx.process(wrapper); }
  });
}

// ── data-action delegation ─────────────────────────────────────────────────
document.addEventListener('click', function (e) {
  var el = e.target.closest('[data-action]');
  if (!el) return;
  var action = el.dataset.action;
  if (action === 'toggle-user-menu') { toggleUserMenu(); return; }
  if (action === 'open-search') { openSearchModal(); return; }
  if (action === 'close-search') { closeSearchModal(); return; }
  if (action === 'open-feedback-modal') { openFeedbackModal(); return; }
  if (action === 'close-feedback-modal') { closeFeedbackModal(); return; }
  if (action === 'submit-search') { submitSearch(); return; }
  if (action === 'select-all') { el.select(); return; }
  if (action === 'refresh-articles') {
    // Clearing search returns to the active nav category (where you were before
    // searching), so the list matches the still-highlighted nav item.
    htmx.ajax('GET', _activeNavGet || '/htmx/articles', { target: '#article-list', swap: 'innerHTML' });
    return;
  }
  if (action === 'toggle-config-menu') {
    var id = el.dataset.configId;
    var menu = document.getElementById('config-menu-' + id);
    if (!menu) return;
    var wasHidden = menu.classList.contains('hidden');
    closeAllConfigMenus();
    if (wasHidden) menu.classList.remove('hidden');
    return;
  }
  if (action === 'rename-config') {
    startConfigRename(el.dataset.configId, el.dataset.configName);
    return;
  }
  if (action === 'save-config-rename') {
    saveConfigRename(el.dataset.configId);
    return;
  }
  if (action === 'delete-config') {
    var id = el.dataset.configId;
    var csrf = (document.cookie.split('; ').find(function (r) { return r.startsWith('csrftoken='); }) || '').split('=')[1] || '';
    fetch('/htmx/catchup-configs/' + id, {
      method: 'DELETE',
      credentials: 'include',
      headers: { 'x-csrftoken': csrf, 'HX-Request': 'true' },
    }).then(function (resp) { return resp.text(); }).then(function (html) {
      var wrapper = document.getElementById('catchup-configs-list-wrapper');
      if (wrapper) { wrapper.innerHTML = html; htmx.process(wrapper); }
    });
    return;
  }
});

document.addEventListener('change', function (e) {
  if (e.target.name === 'search-scope') { updateSearchScope(e.target.value); }
});

// ── Article read state (class toggle, no DOM swap) ────────────────────────
document.addEventListener('articleReadChanged', function (e) {
  var detail = e.detail;
  var headerRead = document.querySelector('[data-header-read]');
  if (headerRead) {
    var hrSvg = headerRead.querySelector('svg');
    if (hrSvg) {
      if (detail.isRead) {
        hrSvg.classList.replace('text-gray-400', 'text-gray-600');
        hrSvg.innerHTML = '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 10.07V19a2 2 0 01-2 2H5a2 2 0 01-2-2v-8.93M21 10.07A2 2 0 0020.11 8.4l-7-4.667a2 2 0 00-2.22 0L3.9 8.4A2 2 0 003 10.07M21 10.07l-7.44 4.96a2 2 0 01-2.12 0L3 10.07"/>';
      } else {
        hrSvg.classList.replace('text-gray-600', 'text-gray-400');
        hrSvg.innerHTML = '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"/>';
      }
    }
    var hrLabel = headerRead.querySelector('[data-label]');
    if (hrLabel) hrLabel.textContent = detail.isRead ? 'Mark as unread' : 'Mark as read';
  }
  var row = document.getElementById('article-row-' + detail.id);
  if (!row) return;
  var isRead = detail.isRead;
  if (window._titleBarCountType === 'unread') {
    var badge = document.getElementById('mobile-title-count');
    if (badge && !badge.classList.contains('hidden')) {
      var wasRead = row.dataset.isRead === 'true';
      if (isRead && !wasRead) _setTitleBarCount(Math.max(0, parseInt(badge.textContent, 10) - 1), 'unread');
      else if (!isRead && wasRead) _setTitleBarCount(parseInt(badge.textContent, 10) + 1, 'unread');
    }
  }
  row.classList.toggle('opacity-75', isRead);
  row.dataset.isRead = isRead ? 'true' : 'false';
  var title = row.querySelector('p, [data-article-title]');
  if (title) {
    if (isRead) {
      title.classList.remove('font-bold', 'text-gray-900');
      title.classList.add('font-medium', 'text-gray-800');
    } else {
      title.classList.remove('font-medium', 'text-gray-800');
      title.classList.add('font-bold', 'text-gray-900');
    }
  }
});

document.addEventListener('articleStarChanged', function (e) {
  var detail = e.detail;
  var bottomBar = document.querySelector('.article-bottom-bar[data-article-id="' + detail.id + '"]');
  if (bottomBar && !bottomBar.classList.contains('hidden')) {
    var bsBtn = bottomBar.querySelector('[data-bottom-star]');
    if (bsBtn) {
      var bsSpan = bsBtn.querySelector('span');
      if (bsSpan) {
        bsSpan.textContent = detail.isStarred ? '★' : '☆';
        bsSpan.className = 'text-base leading-none ' + (detail.isStarred ? 'text-gray-600' : 'text-gray-300');
      }
      bsBtn.title = detail.isStarred ? 'Remove star' : 'Star';
    }
  }
  var headerStar = document.querySelector('[data-header-star]');
  if (headerStar) {
    var hsSvg = headerStar.querySelector('svg');
    if (hsSvg) hsSvg.setAttribute('fill', detail.isStarred ? 'currentColor' : 'none');
    var hsLabel = headerStar.querySelector('[data-label]');
    if (hsLabel) hsLabel.textContent = detail.isStarred ? 'Starred' : 'Star';
  }
  if (e.target === document && window._titleBarCountType === 'starred') {
    var badge = document.getElementById('mobile-title-count');
    if (badge && !badge.classList.contains('hidden')) {
      var cur = parseInt(badge.textContent, 10);
      _setTitleBarCount(Math.max(0, cur + (detail.isStarred ? 1 : -1)), 'starred');
    }
  }
  var row = document.getElementById('article-row-' + detail.id);
  if (!row) return;
  var btn = row.querySelector('[data-star-btn]');
  if (!btn) return;
  var isStarred = detail.isStarred;
  var isRead = row.dataset.isRead === 'true';
  var span = btn.querySelector('span');
  if (span) {
    var sizeClass = (span.className.match(/text-(?:xs|sm|base|lg|xl)/) || ['text-base'])[0];
    if (isStarred) {
      span.textContent = '★';
      span.className = sizeClass + ' ' + (isRead ? 'text-gray-700' : 'text-gray-900');
    } else {
      span.textContent = '☆';
      span.className = sizeClass + ' text-gray-300 hover:text-gray-500';
    }
  }
  btn.title = isStarred ? 'Remove star' : 'Star article';
});

// Optimistic star toggle: fire articleStarChanged immediately on click, revert on error
document.addEventListener('click', function (e) {
  var btn = e.target.closest('[data-star-btn]');
  if (!btn) return;
  var row = btn.closest('.article-row');
  if (!row || !row.dataset.articleId) return;
  var span = btn.querySelector('span');
  var wasStarred = span && span.textContent.trim() === '★';
  btn._optimisticStarred = wasStarred;
  document.dispatchEvent(new CustomEvent('articleStarChanged', {
    detail: { id: parseInt(row.dataset.articleId, 10), isStarred: !wasStarred }
  }));
}, true);

function _revertOptimisticStar(elt) {
  var btn = elt && elt.closest ? elt.closest('[data-star-btn]') : null;
  if (!btn || typeof btn._optimisticStarred === 'undefined') return;
  var row = btn.closest('.article-row');
  if (!row || !row.dataset.articleId) return;
  document.dispatchEvent(new CustomEvent('articleStarChanged', {
    detail: { id: parseInt(row.dataset.articleId, 10), isStarred: btn._optimisticStarred }
  }));
  delete btn._optimisticStarred;
}

document.body.addEventListener('htmx:sendError', function (e) { _revertOptimisticStar(e.detail.elt); });
document.body.addEventListener('htmx:responseError', function (e) { _revertOptimisticStar(e.detail.elt); });
document.body.addEventListener('htmx:afterRequest', function (e) {
  var btn = e.detail.elt && e.detail.elt.closest ? e.detail.elt.closest('[data-star-btn]') : null;
  if (btn) delete btn._optimisticStarred;
});

// Optimistic star toggle for detail bottom-bar and header-menu star buttons
document.addEventListener('click', function (e) {
  var btn = e.target.closest('[data-bottom-star], [data-header-star]');
  if (!btn) return;
  var articleId, wasStarred;
  if (btn.hasAttribute('data-bottom-star')) {
    var bar = btn.closest('.article-bottom-bar');
    if (!bar || !bar.dataset.articleId) return;
    articleId = parseInt(bar.dataset.articleId, 10);
    var span = btn.querySelector('span');
    wasStarred = !!(span && span.textContent.trim() === '★');
  } else {
    var articleEl = document.querySelector('#article-detail [data-article-id], #inline-article-detail-content [data-article-id]');
    if (!articleEl) return;
    articleId = parseInt(articleEl.dataset.articleId, 10);
    var svg = btn.querySelector('svg');
    wasStarred = !!(svg && svg.getAttribute('fill') === 'currentColor');
  }
  btn._optimisticStarred = wasStarred;
  document.dispatchEvent(new CustomEvent('articleStarChanged', {
    detail: { id: articleId, isStarred: !wasStarred }
  }));
}, true);

function _revertOptimisticDetailStar(elt) {
  var btn = elt && elt.closest ? (elt.closest('[data-bottom-star]') || elt.closest('[data-header-star]')) : null;
  if (!btn || typeof btn._optimisticStarred === 'undefined') return;
  var articleId;
  if (btn.hasAttribute('data-bottom-star')) {
    var bar = btn.closest('.article-bottom-bar');
    if (!bar || !bar.dataset.articleId) return;
    articleId = parseInt(bar.dataset.articleId, 10);
  } else {
    var articleEl = document.querySelector('#article-detail [data-article-id], #inline-article-detail-content [data-article-id]');
    if (!articleEl) return;
    articleId = parseInt(articleEl.dataset.articleId, 10);
  }
  document.dispatchEvent(new CustomEvent('articleStarChanged', {
    detail: { id: articleId, isStarred: btn._optimisticStarred }
  }));
  delete btn._optimisticStarred;
}
document.body.addEventListener('htmx:sendError', function (e) { _revertOptimisticDetailStar(e.detail.elt); });
document.body.addEventListener('htmx:responseError', function (e) { _revertOptimisticDetailStar(e.detail.elt); });
document.body.addEventListener('htmx:afterRequest', function (e) {
  var btn = e.detail.elt && e.detail.elt.closest ? (e.detail.elt.closest('[data-bottom-star]') || e.detail.elt.closest('[data-header-star]')) : null;
  if (btn) delete btn._optimisticStarred;
});

document.addEventListener('articleArchiveChanged', function (e) {
  var detail = e.detail;
  var bottomBar = document.querySelector('.article-bottom-bar[data-article-id="' + detail.id + '"]');
  if (bottomBar && !bottomBar.classList.contains('hidden')) {
    var baBtn = bottomBar.querySelector('[data-bottom-archive]');
    if (baBtn) {
      var baSvg = baBtn.querySelector('svg');
      if (baSvg) {
        baSvg.classList.toggle('text-gray-700', detail.isArchived);
        baSvg.classList.toggle('dark:text-gray-200', detail.isArchived);
        baSvg.classList.toggle('text-gray-400', !detail.isArchived);
        baSvg.classList.toggle('dark:text-gray-500', !detail.isArchived);
      }
      baBtn.title = detail.isArchived ? 'Unarchive' : 'Archive';
    }
  }
  var headerArchive = document.querySelector('[data-header-archive]');
  if (headerArchive) {
    var haSvg = headerArchive.querySelector('svg');
    if (haSvg) {
      haSvg.classList.toggle('text-gray-700', detail.isArchived);
      haSvg.classList.toggle('text-gray-400', !detail.isArchived);
    }
    var haLabel = headerArchive.querySelector('[data-label]');
    if (haLabel) haLabel.textContent = detail.isArchived ? 'Archived' : 'Archive';
  }
  var row = document.getElementById('article-row-' + detail.id);
  if (!row) return;
  var indicator = row.querySelector('[data-archived-indicator]');
  if (indicator) indicator.classList.toggle('hidden', !detail.isArchived);
});

document.body.addEventListener('htmx:afterSettle', function (e) {
  var id = e.detail.target.id || '';
  if (!id.startsWith('archive-btn-')) return;
  var archiveBtn = e.detail.target.querySelector('button');
  var isArchived = !!(archiveBtn && archiveBtn.classList.contains('bg-gray-100'));
  var articleId = id.replace('archive-btn-', '');
  var bottomBar = document.querySelector('.article-bottom-bar[data-article-id="' + articleId + '"]');
  if (!bottomBar) return;
  var baBtn = bottomBar.querySelector('[data-bottom-archive]');
  if (!baBtn) return;
  var baSvg = baBtn.querySelector('svg');
  if (baSvg) {
    baSvg.classList.toggle('text-gray-700', isArchived);
    baSvg.classList.toggle('dark:text-gray-200', isArchived);
    baSvg.classList.toggle('text-gray-400', !isArchived);
    baSvg.classList.toggle('dark:text-gray-500', !isArchived);
  }
  baBtn.title = isArchived ? 'Unarchive' : 'Archive';
});

// ── Label picker ──────────────────────────────────────────────────────────
(function () {
  function closePicker() {
    var p = document.getElementById('label-picker');
    if (p) p.classList.add('hidden');
  }

  var _pickerTriggerRect = null;

  function _vh() { return window.visualViewport ? window.visualViewport.height : window.innerHeight; }

  // Position picker near trigger; open above if not enough space below
  document.body.addEventListener('htmx:beforeRequest', function (e) {
    if (!e.target.hasAttribute('data-label-trigger')) return;
    _pickerTriggerRect = e.target.getBoundingClientRect();
    var p = document.getElementById('label-picker');
    if (!p) return;
    var left = _pickerTriggerRect.left;
    if (left + 220 > window.innerWidth) left = window.innerWidth - 224;
    var spaceBelow = _vh() - _pickerTriggerRect.bottom;
    var top = spaceBelow < 300
      ? Math.max(4, _pickerTriggerRect.top - 300 - 2)
      : _pickerTriggerRect.bottom + 2;
    p.style.top = top + 'px';
    p.style.left = left + 'px';
    p.classList.remove('hidden');
  });

  // After content loads, fine-tune with actual picker height
  document.body.addEventListener('htmx:afterSettle', function (e) {
    if (!_pickerTriggerRect) return;
    var p = document.getElementById('label-picker');
    if (!p || p.classList.contains('hidden')) return;
    var rect = _pickerTriggerRect;
    _pickerTriggerRect = null;
    requestAnimationFrame(function () {
      var h = p.offsetHeight;
      var spaceBelow = _vh() - rect.bottom;
      p.style.top = (spaceBelow < h + 8
        ? Math.max(4, rect.top - h - 2)
        : rect.bottom + 2) + 'px';
    });
  });

  // Close on outside click
  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-close-picker]')) { closePicker(); return; }
    if (!e.target.closest('#label-picker') && !e.target.closest('[data-label-trigger]')) {
      closePicker();
    }
  });

  // Close on Escape
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closePicker();
  });
})();

// ── Article list resizer ───────────────────────────────────────────────────
(function () {
  var KEY_3 = 'article-list-width';
  var KEY_2 = 'article-list-width-2';
  var MIN_WIDTH = 200;
  var MAX_WIDTH = 1600;

  function is2panel() { return document.documentElement.dataset.layout === '2'; }
  function storageKey() { return is2panel() ? KEY_2 : KEY_3; }

  function defaultWidth2panel() {
    // article max-w-[52rem] + px-6 both sides = 832 + 48 = 880px, capped at available space
    var sidebar = document.getElementById('sidebar');
    var available = window.innerWidth - (sidebar ? sidebar.offsetWidth : 224) - 4;
    return Math.min(880, Math.max(MIN_WIDTH, available));
  }

  function applyWidth(px) {
    var el = document.getElementById('article-list');
    if (el) el.style.width = px + 'px';
  }

  document.addEventListener('DOMContentLoaded', function () {
    var key = storageKey();
    var saved = parseInt(localStorage.getItem(key), 10);
    if (saved && saved >= MIN_WIDTH && saved <= MAX_WIDTH) {
      applyWidth(saved);
    } else if (is2panel()) {
      applyWidth(defaultWidth2panel());
    }

    var resizer = document.getElementById('article-list-resizer');
    if (!resizer) return;

    resizer.addEventListener('mousedown', function (e) {
      e.preventDefault();
      var list = document.getElementById('article-list');
      var startX = e.clientX;
      var startWidth = list.offsetWidth;

      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
      resizer.classList.add('bg-blue-500');

      function onMove(e) {
        var sidebar = document.getElementById('sidebar');
        var maxAvail = window.innerWidth - (sidebar ? sidebar.offsetWidth : 0) - 4;
        var w = Math.min(maxAvail, Math.max(MIN_WIDTH, startWidth + e.clientX - startX));
        applyWidth(w);
      }

      function onUp() {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        resizer.classList.remove('bg-blue-500');
        var list = document.getElementById('article-list');
        if (list) localStorage.setItem(storageKey(), list.offsetWidth);
      }

      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  });
})();

// ── 2-panel: inline article detail ────────────────────────────────────────
(function () {
  var INLINE_ID = 'inline-article-detail';
  var CONTENT_ID = INLINE_ID + '-content';

  function openExternal(url) {
    // Do not pass a features string — window.open with 'noopener' intentionally returns null
    // even on success, making the blocked-popup check unreliable. Modern browsers apply
    // noopener by default for cross-origin _blank. Fall back to same-tab only when truly blocked.
    var w = window.open(url, '_blank');
    if (!w) window.location.href = url;
  }

  // Title <a> click handling: prevent native navigation except when row is expanded in 2-panel
  document.addEventListener('click', function (e) {
    var a = e.target.closest('[data-article-title]');
    if (!a || !a.href) return;
    var row = a.closest('.article-row');
    if (!row) return;
    var isExpanded = _shouldUseInline() && row.classList.contains('inline-expanded');
    if (isExpanded) {
      e.stopImmediatePropagation(); // block HTMX
      e.preventDefault();
      if (row.dataset.density === 'compact') {
        closeInline();
      }
      openExternal(a.href);
    } else {
      e.preventDefault(); // block link navigation, let HTMX load detail
    }
  }, true);

  // External links in article detail panel: use openExternal for popup-blocked fallback
  document.addEventListener('click', function (e) {
    var a = e.target.closest('[data-external-link]');
    if (!a || !a.href) return;
    e.preventDefault();
    openExternal(a.href);
  });

  function closeInline() {
    var el = document.getElementById(INLINE_ID);
    if (el) el.remove();
    var expandedRow = document.querySelector('.article-row.inline-expanded');
    if (expandedRow) expandedRow.classList.remove('inline-expanded');
  }

  window._closeInlineDetail = closeInline;

  // Load article content into the inline shell with one-shot error recovery.
  function _loadInlineContent(articleId) {
    function isContentTarget(ev) {
      return ev.detail && ev.detail.target && ev.detail.target.id === CONTENT_ID;
    }
    function cleanup() {
      document.body.removeEventListener('htmx:sendError', onError);
      document.body.removeEventListener('htmx:responseError', onError);
      document.body.removeEventListener('htmx:afterSettle', onSettle);
    }
    function onSettle(ev) { if (isContentTarget(ev)) cleanup(); }
    function onError(ev) {
      if (!isContentTarget(ev)) return;
      cleanup();
      var content = document.getElementById(CONTENT_ID);
      if (!content) return;
      content.innerHTML = '<div class="px-6 py-6 text-sm text-gray-400">' +
        'Couldn’t load this article. ' +
        '<button type="button" data-inline-retry class="text-blue-600 underline">Retry</button></div>';
      content.querySelector('[data-inline-retry]').addEventListener('click', function () {
        content.innerHTML = '<div class="px-6 py-6 flex items-center gap-2 text-sm text-gray-400">' +
          '<svg class="animate-spin h-4 w-4 flex-shrink-0" fill="none" viewBox="0 0 24 24">' +
          '<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>' +
          '<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4l3-3-3-3v4a8 8 0 00-8 8z"></path>' +
          '</svg>Loading…</div>';
        _loadInlineContent(articleId);
      });
    }
    document.body.addEventListener('htmx:sendError', onError);
    document.body.addEventListener('htmx:responseError', onError);
    document.body.addEventListener('htmx:afterSettle', onSettle);
    htmx.ajax('GET', '/htmx/articles/' + articleId, {
      target: '#' + CONTENT_ID,
      swap: 'innerHTML'
    });
  }

  // Intercept HTMX requests targeting #article-detail for inline expand
  document.body.addEventListener('htmx:beforeRequest', function (e) {
    if (!_shouldUseInline()) return;
    if (!e.detail.target || e.detail.target.id !== 'article-detail') return;
    // Deep-link (?open_article_id=…) loads the detail panel directly (handled as a
    // fullscreen overlay), bypassing inline expansion which needs an .article-row.
    if (e.detail.elt && e.detail.elt.hasAttribute && e.detail.elt.hasAttribute('data-deeplink-open')) return;
    // Skip action buttons (star, etc.) — hx-target="#article-detail" is inherited from parent row
    if (e.detail.elt && e.detail.elt.closest('[data-stop-propagation]')) return;

    e.preventDefault();

    var row = e.detail.elt.closest('.article-row') || e.detail.elt;
    if (!row || !row.dataset.articleId) return;

    var articleId = row.dataset.articleId;

    if (row.classList.contains('inline-expanded')) {
      // Title clicks are intercepted by capture listener before HTMX sees them.
      // Any click reaching here (▲, feed name, date, padding…) collapses the row.
      closeInline();
      return;
    }

    // Different article: close previous, open new
    closeInline();

    // Build inline container
    var container = document.createElement('div');
    container.id = INLINE_ID;
    container.dataset.articleId = articleId;
    container.className = 'border-b border-gray-200 bg-white';
    container.style.boxShadow = 'inset 0 -6px 8px -6px rgba(0,0,0,0.06)';

    var content = document.createElement('div');
    content.id = CONTENT_ID;
    content.innerHTML = '<div class="px-6 py-6 flex items-center gap-2 text-sm text-gray-400">' +
      '<svg class="animate-spin h-4 w-4 flex-shrink-0" fill="none" viewBox="0 0 24 24">' +
      '<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>' +
      '<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4l3-3-3-3v4a8 8 0 00-8 8z"></path>' +
      '</svg>Loading…</div>';
    container.appendChild(content);

    row.insertAdjacentElement('afterend', container);
    row.classList.add('inline-expanded');

    // Load article content, with one-shot error recovery so a failed load doesn't
    // leave the "Loading…" shell spinning forever.
    _loadInlineContent(articleId);

    // Scroll row into view, accounting for mobile top panel if visible
    setTimeout(function () {
      var topPanel = document.getElementById('mobile-title-bar');
      var barVisible = topPanel && getComputedStyle(topPanel).display !== 'none';
      var topOffset = barVisible ? topPanel.getBoundingClientRect().height : 0;
      if (topOffset > 0) {
        var list = document.getElementById('article-list');
        if (list) {
          var scrollTarget = list.scrollTop + row.getBoundingClientRect().top
            - list.getBoundingClientRect().top - topOffset;
          list.scrollTo({ top: scrollTarget, behavior: 'smooth' });
        }
      } else {
        row.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    }, 50);
  });

  // Close inline when article list reloads (nav change)
  document.body.addEventListener('htmx:beforeSwap', function (e) {
    if (e.detail.target.id !== 'article-list') return;
    closeInline();
    e.detail.target.scrollTop = 0;
  });
})();

// ── Sidebar collapsible sections ──────────────────────────────────────────
function restoreSidebarCollapse(animate) {
  document.querySelectorAll('.collapse-toggle[data-collapse]').forEach(function (btn) {
    var key = btn.dataset.collapse;
    try {
      if (localStorage.getItem('sidebar_col_' + key) === '1') {
        var content = document.getElementById('collapse-' + key);
        if (content) {
          if (!animate) content.style.transition = 'none';
          content.classList.add('collapsed');
          if (!animate) requestAnimationFrame(function () {
            requestAnimationFrame(function () { content.style.transition = ''; });
          });
        }
        btn.classList.add('is-collapsed');
      }
    } catch (err) {}
  });
}

document.addEventListener('click', function (e) {
  var btn = e.target.closest('.collapse-toggle[data-collapse]');
  if (!btn) return;
  e.stopPropagation();
  e.preventDefault();
  var key = btn.dataset.collapse;
  var content = document.getElementById('collapse-' + key);
  if (!content) return;
  var collapsed = content.classList.toggle('collapsed');
  btn.classList.toggle('is-collapsed', collapsed);
  try {
    if (collapsed) localStorage.setItem('sidebar_col_' + key, '1');
    else localStorage.removeItem('sidebar_col_' + key);
  } catch (err) {}
}, true);

// afterSwap fires before the browser paints — prevents collapsed-section flash on load.
document.body.addEventListener('htmx:afterSwap', function (e) {
  if (!(e.detail.target && e.detail.target.id === 'sidebar')) return;
  restoreSidebarCollapse(false);
  // Restore here (before paint) to avoid a visible scroll-to-top flash;
  // afterSettle restores again in case settle re-applies server classes.
  if (window._sidebarScroll) {
    var ns = e.detail.target.querySelector('#sidebar-scroll');
    if (ns) ns.scrollTop = window._sidebarScroll;
  }
});

// In collapsible mode the sidebar is dropped to opacity:0 during a refresh to hide
// the rail↔overlay content swap, then restored once the refresh settles. That restore
// MUST be guaranteed: the sidebar holds its own (only) toggle button, so if opacity
// stays 0 the whole sidebar — and the way out of it — becomes invisible, locking the
// user out until a full page reload. Normal restore is on afterSettle; the error
// handlers below cover a failed request, and the watchdog covers the case where no
// request settles or even fires at all.
var _sidebarOpacityTimer = null;

function _restoreSidebarOpacity(sb) {
  sb = sb || document.getElementById('sidebar');
  if (_sidebarOpacityTimer) { clearTimeout(_sidebarOpacityTimer); _sidebarOpacityTimer = null; }
  if (!sb || sb.style.opacity !== '0') return;
  sb.style.transition = 'opacity 150ms ease';
  sb.style.opacity = '1';
  setTimeout(function () { sb.style.transition = ''; sb.style.opacity = ''; }, 160);
}

function _hideSidebarForRefresh(sb) {
  if (!sb) return;
  sb.style.transition = 'none';
  sb.style.opacity = '0';
  if (_sidebarOpacityTimer) clearTimeout(_sidebarOpacityTimer);
  // Backstop: force the sidebar visible again even if the refresh never settles.
  _sidebarOpacityTimer = setTimeout(function () { _restoreSidebarOpacity(sb); }, 1500);
}

// Sidebar refresh failed (network/server error) — restore visibility immediately
// instead of waiting for the watchdog, so the rail never lingers invisible.
document.body.addEventListener('htmx:sendError', function (e) {
  if (e.detail.target && e.detail.target.id === 'sidebar') _restoreSidebarOpacity(e.detail.target);
});
document.body.addEventListener('htmx:responseError', function (e) {
  if (e.detail.target && e.detail.target.id === 'sidebar') _restoreSidebarOpacity(e.detail.target);
});

// HTMX settle can re-apply server classes and drop client-added "collapsed".
// Re-apply once more after settle to keep sections collapsed after sidebar refresh.
document.body.addEventListener('htmx:afterSettle', function (e) {
  if (!(e.detail.target && e.detail.target.id === 'sidebar')) return;
  restoreSidebarCollapse(false);
  var sb = e.detail.target;
  if (window._sidebarScroll) {
    var newScroll = sb.querySelector('#sidebar-scroll');
    if (newScroll) newScroll.scrollTop = window._sidebarScroll;
  }
  _restoreSidebarOpacity(sb);
});

restoreSidebarCollapse(false);

// ── Sidebar mark-all-as-read: refresh sidebar + article list after action ──
document.body.addEventListener('htmx:afterRequest', function (e) {
  if (!e.detail.elt || e.detail.elt.dataset.action !== 'mark-read') return;
  document.querySelectorAll('.mark-read-row.touch-active').forEach(function (r) {
    r.classList.remove('touch-active');
  });
  if (!e.detail.successful) return;
  e.detail.elt.style.display = 'none';
  // Replace badge with server-returned total badge HTML
  var row = e.detail.elt.closest('.mark-read-row');
  if (row && e.detail.xhr && e.detail.xhr.responseText) {
    var badge = row.querySelector('.mark-read-badge');
    if (badge) badge.outerHTML = e.detail.xhr.responseText;
  }
  var active = document.querySelector('.nav-item.active[hx-get]');
  var url = active ? active.getAttribute('hx-get') : '/htmx/articles';
  htmx.ajax('GET', url, { target: '#article-list', swap: 'innerHTML' });
});

// ── Sidebar mark-read button: long-press on touch devices ─────────────────
(function () {
  var _lpTimer = null;
  var _lpRow = null;
  var _lpHideTimer = null;
  var _lpX = 0, _lpY = 0;

  function hideActive() {
    if (_lpHideTimer) { clearTimeout(_lpHideTimer); _lpHideTimer = null; }
    document.querySelectorAll('.mark-read-row.touch-active').forEach(function (r) {
      r.classList.remove('touch-active');
    });
    _lpRow = null;
  }

  function clearLongPress() {
    if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; }
    hideActive();
  }

  document.body.addEventListener('pointerdown', function (e) {
    if (e.pointerType !== 'touch') return;
    var row = e.target.closest('.mark-read-row');
    if (!row) { hideActive(); return; }
    // New row tapped — hide any currently active button
    hideActive();
    _lpRow = row;
    _lpX = e.clientX;
    _lpY = e.clientY;
    _lpTimer = setTimeout(function () {
      row.classList.add('touch-active');
      _lpTimer = null;
      // Auto-hide after 3s if no action taken
      _lpHideTimer = setTimeout(hideActive, 3000);
    }, 600);
  });

  document.body.addEventListener('pointerup', function (e) {
    if (e.pointerType !== 'touch') return;
    if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; }
  });

  document.body.addEventListener('pointermove', function (e) {
    if (e.pointerType !== 'touch' || !_lpTimer) return;
    var dx = e.clientX - _lpX;
    var dy = e.clientY - _lpY;
    if (Math.sqrt(dx * dx + dy * dy) > 10) { clearTimeout(_lpTimer); _lpTimer = null; }
  });

  // Prevent browser context menu (Firefox long-press) on sidebar rows
  document.body.addEventListener('contextmenu', function (e) {
    if (e.target.closest('.mark-read-row')) e.preventDefault();
  });
})();

// ── Settings mobile nav: preserve horizontal scroll across navigation ─────
document.addEventListener('click', function (e) {
  var link = e.target.closest('a[href]');
  if (!link) return;
  var href = link.getAttribute('href') || '';
  if (!href.startsWith('/settings') && !href.startsWith('/admin')) return;
  var nav = document.querySelector('.sm\\:hidden .overflow-x-auto');
  if (nav) {
    try { sessionStorage.setItem('_snav_scroll', nav.scrollLeft); } catch (e) {}
  }
});

function _restoreSettingsNavScroll() {
  var nav = document.querySelector('.sm\\:hidden .overflow-x-auto');
  if (!nav) return;
  try {
    var saved = sessionStorage.getItem('_snav_scroll');
    if (saved !== null) nav.scrollLeft = parseInt(saved, 10);
  } catch (e) {}
}
document.addEventListener('DOMContentLoaded', _restoreSettingsNavScroll);
document.body.addEventListener('htmx:afterSettle', _restoreSettingsNavScroll);

// ── Label edit panel expand/collapse ──────────────────────────────────────
document.addEventListener('click', function (e) {
  var toggle = e.target.closest('.label-edit-toggle');
  if (toggle) {
    var li = toggle.closest('li');
    var panel = li.querySelector('.label-edit-panel');
    var isHidden = panel.classList.toggle('hidden');
    toggle.textContent = isHidden ? 'Edit' : 'Close';
    return;
  }
  var cancel = e.target.closest('.label-edit-cancel');
  if (cancel) {
    var li = cancel.closest('li');
    li.querySelector('.label-edit-panel').classList.add('hidden');
    li.querySelector('.label-edit-toggle').textContent = 'Edit';
  }
});

// ── Swap confirm: two-click destructive action without layout shift ───────
// First click arms the button (changes text + style); second click fires.
// Disarm on outside click or 3.5s timeout. Works with htmx by stopping the
// first click in capture phase, so htmx's bubble listener never sees it.
document.addEventListener('click', function (e) {
  var btn = e.target.closest('.swap-confirm');
  if (!btn) return;
  if (btn.dataset.armed === '1') return;
  e.stopPropagation();
  e.preventDefault();
  btn.dataset.armed = '1';
  btn.dataset.origText = btn.textContent;
  btn.dataset.origClass = btn.className;
  btn.textContent = btn.dataset.confirmText || 'Confirm';
  if (btn.dataset.confirmClass) {
    btn.className = btn.dataset.confirmClass.trim() + ' swap-confirm';
  }

  var timer;
  var disarm = function () {
    if (btn.dataset.armed !== '1') return;
    btn.dataset.armed = '';
    btn.textContent = btn.dataset.origText;
    btn.className = btn.dataset.origClass;
    clearTimeout(timer);
    document.removeEventListener('click', outside, true);
  };
  var outside = function (ev) {
    if (!btn.contains(ev.target)) disarm();
  };
  timer = setTimeout(disarm, 3500);
  setTimeout(function () { document.addEventListener('click', outside, true); }, 0);
}, true);

// ── Dismiss apply-preview: remove the inline preview block ─────────────────
document.addEventListener('click', function (e) {
  var btn = e.target.closest('.apply-cancel');
  if (!btn) return;
  var preview = btn.closest('.apply-preview');
  if (preview) preview.remove();
});

// ── Color swatches (label color picker) ───────────────────────────────────
document.addEventListener('click', function (e) {
  var swatch = e.target.closest('.color-swatch');
  if (!swatch) return;
  e.preventDefault();
  var group = swatch.closest('.color-swatch-group');
  group.querySelectorAll('.color-swatch').forEach(function (s) {
    s.classList.remove('ring-2', 'ring-gray-700', 'ring-offset-1');
  });
  swatch.classList.add('ring-2', 'ring-gray-700', 'ring-offset-1');
  var form = swatch.closest('form');
  if (!form) return;
  var input = form.querySelector('input[type="hidden"][name="color"]');
  if (input) input.value = swatch.dataset.color;
  var customInput = form.querySelector('.color-custom-input');
  if (customInput) customInput.value = '';
  var preview = form.querySelector('.color-custom-preview');
  if (preview) preview.style.backgroundColor = '';
});

document.addEventListener('input', function (e) {
  var customInput = e.target.closest('.color-custom-input');
  if (!customInput) return;
  var form = customInput.closest('form');
  if (!form) return;
  var val = customInput.value.trim();
  if (val && !val.startsWith('#')) val = '#' + val;
  var isValid = /^#[0-9a-fA-F]{6}$/.test(val);
  var preview = form.querySelector('.color-custom-preview');
  var hiddenInput = form.querySelector('input[type="hidden"][name="color"]');
  if (isValid) {
    form.querySelectorAll('.color-swatch').forEach(function (s) {
      s.classList.remove('ring-2', 'ring-gray-700', 'ring-offset-1');
    });
    if (preview) preview.style.backgroundColor = val;
    if (hiddenInput) hiddenInput.value = val;
  } else {
    if (preview) preview.style.backgroundColor = '';
  }
});

// ── Preferences: layout bucket selects ────────────────────────────────────
(function () {
  var defaults = { small: '1', medium: '2', large: '3' };
  var minimums = { small: '1', medium: '2', large: '2' };
  document.querySelectorAll('[data-layout-bucket]').forEach(function (sel) {
    var bucket = sel.dataset.layoutBucket;
    var val;
    try { val = localStorage.getItem('layout_' + bucket); } catch (e) {}
    if (val && parseInt(val) < parseInt(minimums[bucket])) val = minimums[bucket];
    sel.value = val || defaults[bucket];
    sel.addEventListener('change', function () {
      try { localStorage.setItem('layout_' + bucket, sel.value); } catch (e) {}
      if (window._applyBucket) window._applyBucket();
    });
  });
})();

// ── stopPropagation for action buttons inside article rows ─────────────────
// Attached via htmx:afterSettle so each button gets its own listener —
// bubbling phase only, so hx-post on the button fires first.
document.body.addEventListener('htmx:afterSettle', function (evt) {
  if (evt.detail.target.id !== 'article-list') return;
  evt.detail.target.querySelectorAll('[data-stop-propagation]').forEach(function (el) {
    el.addEventListener('click', function (e) { e.stopPropagation(); });
  });
});

// ── Mobile navigation (small bucket) ──────────────────────────────────────
(function () {
  function isMobile() { return document.documentElement.dataset.bucket === 'small'; }
  function isCollapsible() { return document.documentElement.dataset.sidebarMode === 'collapsible'; }

  // Restore title bar text after page refresh
  document.addEventListener('DOMContentLoaded', function () {
    try {
      var saved = localStorage.getItem('mobile_title_text');
      var titleText = document.getElementById('mobile-title-text');
      if (saved && titleText) titleText.textContent = saved;
    } catch (err) {}
    _syncMobileQuicklink();
  });

  function openSidebarOverlay() {
    if (isCollapsible()) _hideSidebarForRefresh(document.getElementById('sidebar'));
    document.documentElement.classList.add('mobile-sidebar-open');
    history.pushState({ mobileSidebarOpen: true }, '');
    if (isCollapsible()) { htmx.trigger(document.body, 'sidebarRefresh'); }
  }

  function closeSidebarOverlay() {
    if (isCollapsible()) _hideSidebarForRefresh(document.getElementById('sidebar'));
    document.documentElement.classList.remove('mobile-sidebar-open');
    if (isCollapsible()) { htmx.trigger(document.body, 'sidebarRefresh'); }
  }

  // Intercept toggle-sidebar-pin on mobile: open/close overlay instead of pinning
  document.addEventListener('click', function (e) {
    if (!isMobile()) return;
    if (!e.target.closest('[data-action="toggle-sidebar-pin"]')) return;
    e.stopImmediatePropagation();
    if (document.documentElement.classList.contains('mobile-sidebar-open')) {
      closeSidebarOverlay();
      history.back();
    } else {
      openSidebarOverlay();
    }
  }, true);

  // Title bar hamburger (hideable) and bottom bar hamburger
  document.addEventListener('click', function (e) {
    if (!isMobile()) return;
    if (e.target.closest('#mobile-titlebar-open-btn') || e.target.closest('#mobile-bottombar-open-btn')) {
      openSidebarOverlay();
    }
  });

  // Backdrop: close overlay
  document.addEventListener('click', function (e) {
    if (!e.target.closest('#mobile-sidebar-backdrop')) return;
    closeSidebarOverlay();
    history.back();
  });

  // Sidebar item click: close overlay + update title bar text
  document.addEventListener('click', function (e) {
    if (!isMobile()) return;
    var item = e.target.closest('#sidebar [hx-target="#article-list"]');
    if (!item) return;
    _saveNavSnapshot();
    var titleEl = item.querySelector('span.flex-1');
    var title = (titleEl ? titleEl.textContent : (item.getAttribute('title') || '')).trim().slice(0, 40);
    var titleText = document.getElementById('mobile-title-text');
    if (titleText) titleText.textContent = title;
    try { localStorage.setItem('mobile_title_text', title); } catch (err) {}
    _syncMobileQuicklink();
    if (document.documentElement.classList.contains('mobile-sidebar-open')) {
      closeSidebarOverlay();
      history.back();
    }
  });

  // Bottom bar refresh button
  document.addEventListener('click', function (e) {
    if (!e.target.closest('#mobile-bottom-refresh-btn')) return;
    var url = _activeNavGet || '/htmx/articles';
    htmx.ajax('GET', url, { target: '#article-list', swap: 'innerHTML' });
  });

  // Quicklink click: navigate to Labels or Starred
  document.addEventListener('click', function (e) {
    if (!isMobile()) return;
    if (!e.target.closest('#mobile-title-quicklink') && !e.target.closest('#mobile-bottom-quicklink')) return;
    _saveNavSnapshot();
    var isLabels = _activeNavGet && _activeNavGet.indexOf('labeled_only=true') !== -1;
    var targetUrl = isLabels ? '/htmx/articles?starred_only=true' : '/htmx/articles?labeled_only=true';
    var targetTitle = isLabels ? 'Starred' : 'Labels';
    _activeNavGet = targetUrl;
    try { localStorage.setItem('lastNavItem', targetUrl); } catch (err) {}
    var titleText = document.getElementById('mobile-title-text');
    if (titleText) titleText.textContent = targetTitle;
    try { localStorage.setItem('mobile_title_text', targetTitle); } catch (err) {}
    _syncMobileQuicklink();
    htmx.ajax('GET', targetUrl, { target: '#article-list', swap: 'innerHTML' });
    htmx.trigger(document.body, 'sidebarRefresh');
  });

  // Detail back button: close fullscreen detail
  document.addEventListener('click', function (e) {
    if (!e.target.closest('#mobile-detail-back-btn')) return;
    // Flush dwell + stop the clock, else list-browsing time gets attributed to this article.
    if (window._dwellSend) window._dwellSend();
    document.documentElement.classList.remove('mobile-detail-open', 'deeplink-detail-open');
    history.back();
  });

  // Sync detail topbar star/archive indicators from article body buttons
  function syncDetailTopbar() {
    var starContainer = document.querySelector('#article-detail [id^="star-btn-"]');
    var topStar = document.getElementById('detail-topbar-star');
    if (starContainer && topStar) {
      var isStarred = !!starContainer.querySelector('span.text-gray-900');
      topStar.querySelector('svg').setAttribute('fill', isStarred ? 'currentColor' : 'none');
      topStar.classList.toggle('text-gray-900', isStarred);
      topStar.classList.toggle('text-gray-400', !isStarred);
    }
    var archiveContainer = document.querySelector('#article-detail [id^="archive-btn-"]');
    var topArchive = document.getElementById('detail-topbar-archive');
    if (archiveContainer && topArchive) {
      var archiveBtn = archiveContainer.querySelector('button');
      var isArchived = !!(archiveBtn && archiveBtn.classList.contains('bg-gray-100'));
      topArchive.classList.toggle('text-gray-700', isArchived);
      topArchive.classList.toggle('text-gray-400', !isArchived);
    }
  }

  // After article loads into #article-detail: sync topbar + open fullscreen if needed
  document.body.addEventListener('htmx:afterSettle', function (e) {
    if (!isMobile() || e.detail.target.id !== 'article-detail') return;
    syncDetailTopbar();
    try { if (localStorage.getItem('detail_mode_small') !== 'fullscreen') return; } catch (err) { return; }
    var articleEl = e.detail.target.querySelector('[data-article-id]');
    var articleId = articleEl ? articleEl.dataset.articleId : null;
    var articleHash = articleId ? '#article-' + articleId : '';
    if (document.documentElement.classList.contains('mobile-detail-open')) {
      history.replaceState({ mobileDetailOpen: true, articleId: articleId }, '', articleHash || window.location.pathname);
    } else {
      document.documentElement.classList.add('mobile-detail-open');
      history.pushState({ mobileDetailOpen: true, articleId: articleId }, '', articleHash || window.location.pathname);
    }
    e.detail.target.scrollTop = 0;
  });

  // After star/archive HTMX swap: re-sync topbar indicators
  document.body.addEventListener('htmx:afterSettle', function (e) {
    var id = e.detail.target.id || '';
    if (id.startsWith('star-btn-') || id.startsWith('archive-btn-')) syncDetailTopbar();
  });

  // Topbar star: delegate to body star button
  document.addEventListener('click', function (e) {
    if (!e.target.closest('#detail-topbar-star')) return;
    var btn = document.querySelector('#article-detail [id^="star-btn-"] button');
    if (btn) btn.click();
  });

  // Topbar archive: delegate to body archive button
  document.addEventListener('click', function (e) {
    if (!e.target.closest('#detail-topbar-archive')) return;
    var btn = document.querySelector('#article-detail [id^="archive-btn-"] button');
    if (btn) btn.click();
  });

  // Topbar share: show picker
  var _pendingShareTitle = null;

  function _showShareToast(msg) {
    var t = document.createElement('div');
    t.textContent = msg;
    t.style.cssText = 'position:fixed;bottom:1.5rem;left:50%;transform:translateX(-50%);background:#1f2937;color:#fff;padding:.5rem 1rem;border-radius:.375rem;font-size:.875rem;z-index:9999;pointer-events:none';
    document.body.appendChild(t);
    setTimeout(function () { t.remove(); }, 2000);
  }

  function _execCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
    document.body.appendChild(ta);
    ta.focus(); ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) {}
    ta.remove();
    return ok;
  }

  function _copyWithFeedback(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text)
        .then(function () { _showShareToast('Link copied'); })
        .catch(function () { if (_execCopy(text)) _showShareToast('Link copied'); });
    } else if (_execCopy(text)) {
      _showShareToast('Link copied');
    }
  }

  function doShare(title, url) {
    if (navigator.share) {
      navigator.share({ title: title, url: url }).catch(function (err) {
        if (err && err.name === 'AbortError') return;
        _copyWithFeedback(url);
      });
    } else {
      _copyWithFeedback(url);
    }
  }

  // Place the picker so its bottom sits at the click point (grows upward from where
  // the user tapped), aligning its right edge to the anchor and clamping into the
  // viewport. Drops below only when there isn't room above.
  function positionSharePicker(picker, rect) {
    picker.style.zIndex = '60';
    picker.style.right = '';
    picker.style.bottom = '';
    picker.style.maxWidth = '';

    var margin = 8;
    var pickerH = picker.offsetHeight;
    var pickerW = picker.offsetWidth;
    var aboveTop = rect.bottom - pickerH;
    var top = aboveTop >= margin ? aboveTop : rect.bottom + 4;
    var left = Math.min(rect.right - pickerW, window.innerWidth - pickerW - margin);
    left = Math.max(margin, left);
    picker.style.top = top + 'px';
    picker.style.left = left + 'px';
  }

  document.addEventListener('click', function (e) {
    var trigger = e.target.closest('#detail-topbar-share') || e.target.closest('[data-bottom-share]');
    if (!trigger) return;
    var picker = document.getElementById('detail-share-picker');
    if (!picker) return;
    var isOpen = !picker.classList.contains('hidden');
    if (isOpen) { picker.classList.add('hidden'); e.stopPropagation(); return; }
    // Measure the actual clicked button before the menu (and this trigger) is hidden,
    // so the picker appears right where the user tapped.
    var anchorRect = trigger.getBoundingClientRect();
    document.querySelectorAll('[data-menu]').forEach(function (m) { m.classList.add('hidden'); });
    picker.classList.remove('hidden');
    positionSharePicker(picker, anchorRect);
    e.stopPropagation();
  });

  document.addEventListener('click', function (e) {
    var picker = document.getElementById('detail-share-picker');
    if (picker && !e.target.closest('#detail-share-picker') && !e.target.closest('#detail-topbar-share') && !e.target.closest('[data-bottom-share]')) {
      picker.classList.add('hidden');
    }
  });

  document.addEventListener('click', function (e) {
    if (!e.target.closest('#detail-share-pick-original')) return;
    document.getElementById('detail-share-picker').classList.add('hidden');
    var articleEl = (document.querySelector('#article-detail [data-article-id]') ||
                     document.querySelector('#inline-article-detail-content [data-article-id]'));
    if (!articleEl) return;
    doShare(articleEl.dataset.title || '', articleEl.dataset.url || window.location.href);
  });

  document.addEventListener('click', function (e) {
    if (!e.target.closest('#detail-share-pick-readfine')) return;
    document.getElementById('detail-share-picker').classList.add('hidden');
    var articleEl = (document.querySelector('#article-detail [data-article-id]') ||
                     document.querySelector('#inline-article-detail-content [data-article-id]'));
    if (!articleEl) return;
    var id = articleEl.dataset.articleId;
    var title = articleEl.dataset.title || '';
    var shareInput = (document.querySelector('#article-detail [id^="share-btn-"] input[type="text"]') ||
                      document.querySelector('#inline-article-detail-content [id^="share-btn-"] input[type="text"]'));
    if (shareInput && shareInput.value) {
      doShare(title, shareInput.value);
    } else {
      _pendingShareTitle = title;
      htmx.ajax('POST', '/htmx/articles/' + id + '/share', { target: '#share-btn-' + id, swap: 'outerHTML' });
    }
  });

  // After share token generated: complete pending share
  document.body.addEventListener('htmx:afterSettle', function (e) {
    if (_pendingShareTitle === null) return;
    var targetId = (e.detail.target && e.detail.target.id) || '';
    if (!targetId.startsWith('share-btn-')) return;
    var input = document.getElementById(targetId) && document.getElementById(targetId).querySelector('input[type="text"]');
    if (input) doShare(_pendingShareTitle, input.value);
    _pendingShareTitle = null;
  });

  // Topbar/bottom next: mark current as read, load next article from list
  document.addEventListener('click', function (e) {
    if (!isMobile() || (!e.target.closest('#detail-topbar-next') && !e.target.closest('[data-bottom-next]'))) return;
    var detailArticle = document.querySelector('#article-detail [data-article-id]');
    if (!detailArticle) return;
    var currentId = detailArticle.dataset.articleId;
    htmx.ajax('POST', '/htmx/articles/' + currentId + '/set-read?state=true', { swap: 'none' });
    detailArticle.dataset.isRead = 'true';
    var currentRow = document.querySelector('#article-list [data-article-id="' + currentId + '"]');
    if (!currentRow) return;
    var next = currentRow.nextElementSibling;
    while (next && !next.dataset.articleId) next = next.nextElementSibling;
    if (!next) return;
    htmx.ajax('GET', '/htmx/articles/' + next.dataset.articleId, { target: '#article-detail', swap: 'innerHTML' });
  });

  // Browser back: sync classes with history state
  window.addEventListener('popstate', function (e) {
    if (!isMobile()) return;
    var state = e.state || {};
    document.documentElement.classList.toggle('mobile-sidebar-open', !!state.mobileSidebarOpen);
    document.documentElement.classList.toggle('mobile-detail-open', !!state.mobileDetailOpen);
  });

  // Bottom action bar: always visible when article is loaded
  function _syncBottomBar() {
    var inInline = _shouldUseInline();
    var containerId = inInline ? 'inline-article-detail-content' : 'article-detail';
    var container = document.getElementById(containerId);
    if (!container) return;
    var bar = container.querySelector('.article-bottom-bar');
    if (!bar) return;
    bar.classList.remove('hidden');
    var nextBtn = bar.querySelector('[data-bottom-next]');
    if (nextBtn) nextBtn.classList.toggle('hidden', !(isMobile() && !inInline));
  }
  document.body.addEventListener('htmx:afterSettle', function (e) {
    var id = e.detail.target.id;
    if (id === 'article-detail' || id === 'inline-article-detail-content' || (id && id.startsWith('article-content-'))) _syncBottomBar();
  });

  // Bottom bar: show on scroll-up, hide on scroll-down / at top
  (function () {
    var lastScrollTop = 0;
    var ticking = false;

    function updateBottomBar(scrollTop, list) {
      var bar = document.getElementById('mobile-bottom-bar');
      if (!bar) return;
      var atBottom = list && (scrollTop + list.clientHeight >= list.scrollHeight - 10);
      if (atBottom || scrollTop < lastScrollTop) {
        bar.classList.add('bottom-bar-visible');
      } else if (scrollTop > lastScrollTop) {
        bar.classList.remove('bottom-bar-visible');
      }
      lastScrollTop = scrollTop;
    }

    function attachScrollListener() {
      var list = document.getElementById('article-list');
      if (!list || list._bottomBarBound) return;
      list._bottomBarBound = true;
      list.addEventListener('scroll', function () {
        if (ticking) return;
        ticking = true;
        requestAnimationFrame(function () {
          updateBottomBar(list.scrollTop, list);
          ticking = false;
        });
      }, { passive: true });
    }

    document.addEventListener('DOMContentLoaded', attachScrollListener);
    document.body.addEventListener('htmx:afterSettle', function (e) {
      if (e.detail.target.id === 'article-list') attachScrollListener();
    });

    // Reset bar when article list navigates to a new feed
    document.body.addEventListener('htmx:beforeSwap', function (e) {
      if (e.detail.target.id !== 'article-list') return;
      lastScrollTop = 0;
      var bar = document.getElementById('mobile-bottom-bar');
      if (bar) bar.classList.remove('bottom-bar-visible');
    });
  })();

  // Preferences page: sync small-bucket selects with localStorage
  var prefPairs = [
    { id: 'sidebar-mode-small', key: 'sidebar_mode_small', def: 'hideable-up' },
    { id: 'detail-mode-small',  key: 'detail_mode_small',  def: 'inline' }
  ];
  prefPairs.forEach(function (p) {
    var sel = document.getElementById(p.id);
    if (!sel) return;
    try { sel.value = localStorage.getItem(p.key) || p.def; } catch (e) {}
    sel.addEventListener('change', function () {
      try { localStorage.setItem(p.key, sel.value); } catch (e) {}
      if (p.key === 'sidebar_mode_small' && window._applyBucket) window._applyBucket();
    });
  });
})();

// AI context panel: [data-show-context-panel] closes ··· dropdown and reveals the panel
(function () {
  function closeAllMenus() {
    document.querySelectorAll('[data-menu]').forEach(function (m) { m.classList.add('hidden'); });
  }

  function attachContextPanel(root) {
    (root || document).querySelectorAll('[data-show-context-panel]').forEach(function (btn) {
      if (btn._aiContextAttached) return;
      btn._aiContextAttached = true;
      btn.addEventListener('click', function () {
        var articleId = btn.getAttribute('data-show-context-panel');
        closeAllMenus();
        var panel = document.getElementById('context-panel-' + articleId);
        if (panel) {
          panel.classList.remove('hidden');
          var textarea = document.getElementById('context-focus-' + articleId);
          if (textarea) {
            textarea.scrollIntoView({ behavior: 'smooth', block: 'center' });
            textarea.focus();
          }
        }
      });
    });
  }

  // [data-close-menu] buttons close the dropdown on click
  function attachCloseMenu(root) {
    (root || document).querySelectorAll('[data-close-menu]').forEach(function (btn) {
      if (btn._aiCloseMenuAttached) return;
      btn._aiCloseMenuAttached = true;
      btn.addEventListener('click', function () { closeAllMenus(); });
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    attachContextPanel();
    attachCloseMenu();
  });
  document.body.addEventListener('htmx:afterSettle', function () {
    attachContextPanel();
    attachCloseMenu();
  });
})();

// AI chat modal
(function () {
  var _chatPending = {};

  function _escHtml(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
  function _userBubble(msg) {
    var d = document.createElement('div');
    d.className = 'flex justify-end';
    d.innerHTML = '<div class="max-w-[85%] bg-blue-50 dark:bg-blue-900/30 border border-blue-100 dark:border-blue-800 rounded-lg px-3 py-2 text-sm text-gray-800 dark:text-gray-200">' + _escHtml(msg) + '</div>';
    return d;
  }
  function _typingIndicator(id) {
    var d = document.createElement('div');
    d.id = id;
    d.className = 'flex justify-start';
    d.innerHTML = '<div class="bg-gray-50 dark:bg-gray-800 border border-gray-100 dark:border-gray-700 rounded-lg px-3 py-3 flex gap-1 items-center">'
      + '<span class="w-1 h-1 bg-gray-400 dark:bg-gray-500 rounded-full animate-chat-bounce" style="animation-delay:0ms"></span>'
      + '<span class="w-1 h-1 bg-gray-400 dark:bg-gray-500 rounded-full animate-chat-bounce" style="animation-delay:150ms"></span>'
      + '<span class="w-1 h-1 bg-gray-400 dark:bg-gray-500 rounded-full animate-chat-bounce" style="animation-delay:300ms"></span>'
      + '</div>';
    return d;
  }
  function _inlineChatError(area, msg) {
    var err = document.createElement('p');
    err.className = 'text-xs text-red-500 py-1';
    err.textContent = msg;
    var inputDiv = area.querySelector('.flex-shrink-0.pt-2');
    if (inputDiv) area.insertBefore(err, inputDiv); else area.appendChild(err);
  }

  function applyVisualViewport(modal) {
    if (!window.visualViewport) return;
    var vv = window.visualViewport;
    modal.style.top = vv.offsetTop + 'px';
    modal.style.height = vv.height + 'px';
    modal.style.bottom = 'auto';
  }

  function openChatModal(articleId) {
    var modal = document.getElementById('chat-modal-' + articleId);
    if (!modal) return;
    modal.classList.remove('hidden');
    if (window.visualViewport) {
      applyVisualViewport(modal);
      var listener = function () { applyVisualViewport(modal); };
      modal._vvpListener = listener;
      window.visualViewport.addEventListener('resize', listener);
      window.visualViewport.addEventListener('scroll', listener);
    }
    scrollChatToBottom(modal);
    var input = document.getElementById('chat-input-' + articleId);
    if (input) input.focus();
  }

  function updateChatIndicator(articleId, hasMessages) {
    var el = document.getElementById('chat-indicator-' + articleId);
    if (el) el.classList.toggle('hidden', !hasMessages);
  }

  function closeChatModal(articleId) {
    var modal = document.getElementById('chat-modal-' + articleId);
    if (!modal) return;
    modal.classList.add('hidden');
    modal.style.top = '';
    modal.style.height = '';
    modal.style.bottom = '';
    if (modal._vvpListener && window.visualViewport) {
      window.visualViewport.removeEventListener('resize', modal._vvpListener);
      window.visualViewport.removeEventListener('scroll', modal._vvpListener);
      modal._vvpListener = null;
    }
    var msgs = document.getElementById('chat-messages-' + articleId);
    updateChatIndicator(articleId, msgs && msgs.children.length > 0);
  }

  function attachChatModal(root) {
    (root || document).querySelectorAll('[data-show-chat-modal]').forEach(function (btn) {
      if (btn._aiChatAttached) return;
      btn._aiChatAttached = true;
      btn.addEventListener('click', function () {
        var articleId = btn.getAttribute('data-show-chat-modal');
        document.querySelectorAll('[data-menu]').forEach(function (m) { m.classList.add('hidden'); });
        openChatModal(articleId);
      });
    });
    (root || document).querySelectorAll('[data-close-chat-modal]').forEach(function (el) {
      if (el._aiChatCloseAttached) return;
      el._aiChatCloseAttached = true;
      el.addEventListener('click', function () {
        closeChatModal(el.getAttribute('data-close-chat-modal'));
      });
    });
  }

  function scrollChatToBottom(root) {
    (root || document).querySelectorAll('[id^="chat-messages-"]').forEach(function (el) {
      requestAnimationFrame(function () {
        var last = el.lastElementChild;
        if (last) {
          last.scrollIntoView({ block: 'start', behavior: 'instant' });
        } else {
          el.scrollTop = el.scrollHeight;
        }
      });
    });
  }

  function confirmLongMessage(textarea) {
    if (textarea.value.length <= 2000) return true;
    return confirm(textarea.value.length + ' characters — may use significant AI tokens. Send anyway?');
  }

  // Enter submits, Shift+Enter inserts newline

  function attachChatAttachBtn(root) {
    (root || document).querySelectorAll('[id^="chat-attach-btn-"]').forEach(function (btn) {
      if (btn._attachBtnAttached) return;
      btn._attachBtnAttached = true;
      var articleId = btn.id.replace('chat-attach-btn-', '');
      var chk = document.getElementById('chat-article-' + articleId);
      var textarea = document.getElementById('chat-input-' + articleId);
      if (!chk) return;
      btn.addEventListener('click', function () {
        chk.checked = !chk.checked;
        chk.dispatchEvent(new Event('change'));
        if (textarea) textarea.focus();
      });
    });
  }

  function attachArticlePlaceholder(root) {
    (root || document).querySelectorAll('[id^="chat-article-"]').forEach(function (chk) {
      if (chk._placeholderAttached) return;
      chk._placeholderAttached = true;
      var articleId = chk.id.replace('chat-article-', '');
      var input = document.getElementById('chat-input-' + articleId);
      if (!input) return;
      function update() {
        input.placeholder = chk.checked
          ? 'Ask a question about this article…'
          : 'Ask a question…';
        input.focus();
        var attachBtn = document.getElementById('chat-attach-btn-' + articleId);
        if (attachBtn) {
          attachBtn.classList.toggle('text-blue-500', chk.checked);
          attachBtn.classList.toggle('text-gray-400', !chk.checked);
        }
      }
      chk.addEventListener('change', update);
    });
  }

  function attachChatKeydown(root) {
    (root || document).querySelectorAll('[data-chat-input-id]').forEach(function (el) {
      if (el._chatKeyAttached) return;
      el._chatKeyAttached = true;
      el.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter' || e.shiftKey) return;
        e.preventDefault();
        var area = el.closest('[id^="chat-area-"]');
        var sendBtn = area && area.querySelector('button[hx-post*="/ai-chat"]');
        if (sendBtn) {
          if (!confirmLongMessage(el)) return;
          htmx.trigger(sendBtn, 'click');
        }
      });
    });
  }

  // Capture-phase click intercept for long message warning on Send button
  document.body.addEventListener('click', function (e) {
    var btn = e.target.closest('button[hx-post*="/ai-chat"]');
    if (!btn) return;
    var match = (btn.getAttribute('hx-post') || '').match(/articles\/(\d+)\/ai-chat/);
    if (!match) return;
    var input = document.getElementById('chat-input-' + match[1]);
    if (input && !confirmLongMessage(input)) {
      e.stopImmediatePropagation();
    }
  }, true);

  // Clear textarea + scroll + re-attach keydown after HTMX swap of #chat-area-*
  document.body.addEventListener('htmx:afterSwap', function (e) {
    var target = e.detail.target;
    if (!target || !target.id || !target.id.startsWith('chat-area-')) return;
    var articleId = target.id.replace('chat-area-', '');
    var newArea = document.getElementById('chat-area-' + articleId);
    if (!newArea) return;
    var input = document.getElementById('chat-input-' + articleId);
    if (input) input.value = '';
    scrollChatToBottom(newArea);
    attachChatKeydown(newArea);

    attachChatAttachBtn(newArea);
    attachArticlePlaceholder(newArea);
    var msgs = document.getElementById('chat-messages-' + articleId);
    updateChatIndicator(articleId, msgs && msgs.children.length > 0);
    if (input) input.focus();
    var pending = _chatPending[articleId];
    if (pending) {
      if (newArea.querySelector('.text-red-500') && input) { input.value = pending; input.focus(); }
      delete _chatPending[articleId];
    }
  });

  // General (non-article) chat modal
  function syncGeneralChatContext() {
    var root = document.getElementById('article-detail-root');
    var artId = root ? (root.getAttribute('data-article-id') || '') : '';
    var artInput = document.getElementById('general-chat-article-id');
    if (artInput) artInput.value = artId;
    var attachBtn = document.getElementById('general-chat-attach-btn');
    var titleSpan = document.getElementById('general-chat-attach-title');
    var short = '';
    if (artId) {
      var artElem = document.querySelector('article[data-article-id="' + artId + '"]');
      var title = artElem ? (artElem.getAttribute('data-title') || '') : '';
      short = title.length > 25 ? title.slice(0, 25) + '…' : title;
    }
    if (attachBtn) attachBtn.classList.toggle('hidden', !artId);
    if (titleSpan) {
      titleSpan.classList.toggle('hidden', !artId);
      titleSpan.textContent = short;
    }
  }

  function openGeneralChat() {
    var modal = document.getElementById('general-chat-modal');
    if (!modal) return;
    modal.classList.remove('hidden');
    syncGeneralChatContext();
    if (window.visualViewport) {
      applyVisualViewport(modal);
      var listener = function () { applyVisualViewport(modal); };
      modal._vvpListener = listener;
      window.visualViewport.addEventListener('resize', listener);
      window.visualViewport.addEventListener('scroll', listener);
    }
    var input = document.getElementById('general-chat-input');
    if (input) input.focus();
  }

  function closeGeneralChat() {
    var modal = document.getElementById('general-chat-modal');
    if (!modal) return;
    modal.classList.add('hidden');
    modal.style.top = '';
    modal.style.height = '';
    modal.style.bottom = '';
    if (modal._vvpListener && window.visualViewport) {
      window.visualViewport.removeEventListener('resize', modal._vvpListener);
      window.visualViewport.removeEventListener('scroll', modal._vvpListener);
      modal._vvpListener = null;
    }
  }

  function attachGeneralChat() {
    ['sidebar-chat-btn', 'sidebar-rail-chat-btn', 'mobile-bottom-chat-btn'].forEach(function (id) {
      var btn = document.getElementById(id);
      if (btn && !btn._generalChatAttached) {
        btn._generalChatAttached = true;
        btn.addEventListener('click', openGeneralChat);
      }
    });
    var genAttachBtn = document.getElementById('general-chat-attach-btn');
    if (genAttachBtn && !genAttachBtn._attachBtnAttached) {
      genAttachBtn._attachBtnAttached = true;
      var genChk = document.getElementById('general-chat-include-article');
      genAttachBtn.addEventListener('click', function () {
        if (genChk) {
          genChk.checked = !genChk.checked;
          genAttachBtn.classList.toggle('text-blue-500', genChk.checked);
          genAttachBtn.classList.toggle('text-gray-400', !genChk.checked);
        }
        var inp = document.getElementById('general-chat-input');
        if (inp) {
          inp.placeholder = (genChk && genChk.checked)
            ? 'Ask a question about this article…'
            : 'Ask a question…';
          inp.focus();
        }
      });
    }
    var closeBtn = document.getElementById('general-chat-close-btn');
    if (closeBtn && !closeBtn._generalChatCloseAttached) {
      closeBtn._generalChatCloseAttached = true;
      closeBtn.addEventListener('click', closeGeneralChat);
    }
    var backdrop = document.getElementById('general-chat-backdrop');
    if (backdrop && !backdrop._generalChatCloseAttached) {
      backdrop._generalChatCloseAttached = true;
      backdrop.addEventListener('click', closeGeneralChat);
    }
  }

  function attachGeneralChatKeydown() {
    var input = document.getElementById('general-chat-input');
    if (input && !input._generalChatKeyAttached) {
      input._generalChatKeyAttached = true;
      input.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter' || e.shiftKey) return;
        e.preventDefault();
        var sendBtn = document.getElementById('general-chat-submit');
        if (sendBtn) {
          if (input.value.length > 2000 && !confirm(input.value.length + ' characters — may use significant AI tokens. Send anyway?')) return;
          htmx.trigger(sendBtn, 'click');
        }
      });
    }
  }

  // Esc closes open chat modals
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    document.querySelectorAll('[id^="chat-modal-"]').forEach(function (modal) {
      if (!modal.classList.contains('hidden')) {
        closeChatModal(modal.getAttribute('data-chat-modal-id'));
      }
    });
    closeGeneralChat();
  });

  // After HTMX swap of #general-chat-area: clear textarea, scroll, re-attach keydown
  document.body.addEventListener('htmx:afterSwap', function (e) {
    var target = e.detail && e.detail.target;
    if (!target || target.id !== 'general-chat-area') return;
    var newArea = document.getElementById('general-chat-area');
    if (!newArea) return;
    var input = document.getElementById('general-chat-input');
    if (input) input.value = '';
    var msgs = document.getElementById('general-chat-messages');
    if (msgs) {
      requestAnimationFrame(function () {
        var last = msgs.lastElementChild;
        if (last) last.scrollIntoView({ block: 'start', behavior: 'instant' });
        else msgs.scrollTop = msgs.scrollHeight;
      });
    }
    attachGeneralChatKeydown();
    syncGeneralChatContext();
    if (input) input.focus();
    var pending = _chatPending['general'];
    if (pending) {
      if (newArea.querySelector('.text-red-500') && input) { input.value = pending; input.focus(); }
      delete _chatPending['general'];
    }
    var hist = document.getElementById('general-chat-history');
    var msgsEl = document.getElementById('general-chat-messages');
    if (hist && hist.value === '[]') {
      try { sessionStorage.removeItem('_gchat_history'); sessionStorage.removeItem('_gchat_html'); } catch (e) {}
    } else if (hist && msgsEl) {
      try {
        sessionStorage.setItem('_gchat_history', hist.value);
        sessionStorage.setItem('_gchat_html', msgsEl.innerHTML);
      } catch (e) {}
    }
  });

  // Optimistic UI: show user message + typing indicator before server responds
  document.body.addEventListener('htmx:beforeRequest', function (e) {
    var postUrl = (e.detail.elt.getAttribute('hx-post') || '');
    var artMatch = postUrl.match(/articles\/(\d+)\/ai-chat/);
    var isGeneral = postUrl === '/htmx/ai-chat';
    if (!artMatch && !isGeneral) return;
    var key     = artMatch ? artMatch[1] : 'general';
    var inputId = artMatch ? 'chat-input-' + artMatch[1] : 'general-chat-input';
    var msgsId  = artMatch ? 'chat-messages-' + artMatch[1] : 'general-chat-messages';
    var typingId = artMatch ? 'chat-typing-' + artMatch[1] : 'general-chat-typing';
    var input = document.getElementById(inputId);
    if (!input || !input.value.trim()) return;
    var msg = input.value.trim();
    _chatPending[key] = msg;
    input.value = '';
    var msgs = document.getElementById(msgsId);
    if (msgs) {
      msgs.appendChild(_userBubble(msg));
      msgs.appendChild(_typingIndicator(typingId));
      requestAnimationFrame(function () {
        var last = msgs.lastElementChild;
        if (last) last.scrollIntoView({ block: 'start', behavior: 'instant' });
      });
    }
  });

  function _handleChatCommError(elt, errMsg) {
    var postUrl = (elt.getAttribute('hx-post') || '');
    var artMatch = postUrl.match(/articles\/(\d+)\/ai-chat/);
    var isGeneral = postUrl === '/htmx/ai-chat';
    if (!artMatch && !isGeneral) return;
    var key     = artMatch ? artMatch[1] : 'general';
    var inputId = artMatch ? 'chat-input-' + artMatch[1] : 'general-chat-input';
    var msgsId  = artMatch ? 'chat-messages-' + artMatch[1] : 'general-chat-messages';
    var areaId  = artMatch ? 'chat-area-' + artMatch[1] : 'general-chat-area';
    var msgs = document.getElementById(msgsId);
    if (msgs) {
      if (msgs.lastChild) msgs.removeChild(msgs.lastChild); // typing indicator
      if (msgs.lastChild) msgs.removeChild(msgs.lastChild); // user bubble
    }
    var area = document.getElementById(areaId);
    if (area) _inlineChatError(area, errMsg);
    var pending = _chatPending[key];
    if (pending) {
      var input = document.getElementById(inputId);
      if (input) { input.value = pending; input.focus(); }
      delete _chatPending[key];
    }
  }

  document.body.addEventListener('htmx:responseError', function (e) {
    _handleChatCommError(e.detail.elt, 'Request failed — please try again.');
  });
  document.body.addEventListener('htmx:sendError', function (e) {
    _handleChatCommError(e.detail.elt, 'Network error — please try again.');
  });

  document.addEventListener('DOMContentLoaded', function () {
    attachChatModal();
    attachChatKeydown();

    attachChatAttachBtn();
    attachArticlePlaceholder();
    scrollChatToBottom();
    attachGeneralChat();
    attachGeneralChatKeydown();
  });
  document.body.addEventListener('htmx:afterSettle', function () {
    attachChatModal();
    attachChatKeydown();

    attachChatAttachBtn();
    attachArticlePlaceholder();
    attachGeneralChat();
    syncGeneralChatContext();
  });

  (function restoreGeneralChatSession() {
    try {
      var html = sessionStorage.getItem('_gchat_html');
      var history = sessionStorage.getItem('_gchat_history');
      if (!html || !history || history === '[]') return;
      var msgs = document.getElementById('general-chat-messages');
      var hist = document.getElementById('general-chat-history');
      if (msgs) msgs.innerHTML = html;
      if (hist) hist.value = history;
    } catch (e) {}
  })();
})();

// ── "insert default" links for prompt fields ──────────────────────────────
// The default prompt is rendered into each textarea's placeholder. The link
// copies it into the value so it can be used as an editable starting point.
// Label is contextual: "insert default" when empty, "reset to default" when filled.
(function () {
  function refresh() {
    document.querySelectorAll('[data-insert-default]').forEach(function (btn) {
      var ta = document.querySelector(btn.getAttribute('data-insert-default'));
      if (ta) btn.textContent = ta.value.trim() ? 'reset to default' : 'insert default';
    });
  }
  window._refreshInsertDefaultLabels = refresh;

  function disarmInsertBtn(btn) {
    if (btn.dataset.armed !== '1') return;
    btn.dataset.armed = '';
    btn.className = btn.dataset.origClass || btn.className;
    clearTimeout(Number(btn.dataset.armTimer));
    if (window._refreshInsertDefaultLabels) window._refreshInsertDefaultLabels();
    document.removeEventListener('click', btn._insertOutside, true);
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-insert-default]');
    if (!btn) return;
    var ta = document.querySelector(btn.getAttribute('data-insert-default'));
    if (!ta) return;

    if (btn.dataset.armed === '1') {
      // Second click: clear field and disarm
      disarmInsertBtn(btn);
      ta.value = '';
      ta.dispatchEvent(new Event('input', { bubbles: true }));
      ta.focus();
      return;
    }

    if (ta.value.trim()) {
      // Arm for confirmation
      btn.dataset.armed = '1';
      btn.dataset.origText = btn.textContent;
      btn.dataset.origClass = btn.className;
      btn.textContent = 'Confirm reset';
      btn.className = 'text-xs text-red-600 font-medium hover:text-red-800 bg-transparent border-0 p-0 cursor-pointer';
      btn._insertOutside = function (ev) { if (!btn.contains(ev.target)) disarmInsertBtn(btn); };
      btn.dataset.armTimer = setTimeout(function () { disarmInsertBtn(btn); }, 3500);
      setTimeout(function () { document.addEventListener('click', btn._insertOutside, true); }, 0);
    } else {
      // Empty field: insert editable copy of the default immediately
      ta.value = ta.placeholder;
      ta.dispatchEvent(new Event('input', { bubbles: true }));
      ta.focus();
    }
  });

  document.addEventListener('input', function (e) {
    if (e.target && e.target.matches && e.target.matches('textarea[id]')) {
      var btn = document.querySelector('[data-insert-default="#' + e.target.id + '"]');
      if (btn) btn.textContent = e.target.value.trim() ? 'reset to default' : 'insert default';
    }
  });

  document.addEventListener('DOMContentLoaded', refresh);
  refresh();
})();

