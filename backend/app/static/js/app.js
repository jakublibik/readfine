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

// Hide duplicate h1 if it matches the article title (first 50 chars)
function hideDuplicateH1() {
  var articleEl = (document.querySelector('#article-detail [data-article-id]') ||
                   document.querySelector('#inline-article-detail-content [data-article-id]'));
  if (!articleEl) return;
  var prose = articleEl.querySelector('.prose');
  if (!prose) return;
  var h1 = prose.querySelector(':scope > h1:first-child');
  if (!h1) return;
  var normalize = function (s) { return s.trim().toLowerCase().slice(0, 50); };
  if (normalize(h1.textContent) === normalize(articleEl.dataset.title || '')) {
    h1.style.display = 'none';
  }
}
document.addEventListener('DOMContentLoaded', hideDuplicateH1);
document.body.addEventListener('htmx:afterSettle', hideDuplicateH1);

// Local time formatting for <time datetime="..."> elements
function _formatLocalTime(isoStr, format) {
  var dt = new Date(isoStr);
  if (isNaN(dt.getTime())) return null;
  if (format === 'long') {
    var time = dt.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    return dt.toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' }) + ' ' + time;
  }
  if (format === 'date') {
    return dt.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  }
  // short: today → HH:MM, this year → "Mon DD HH:MM", older → "Mon DD, YYYY HH:MM"
  var now = new Date();
  var isToday = dt.toDateString() === now.toDateString();
  if (isToday) {
    return dt.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  }
  var isThisYear = dt.getFullYear() === now.getFullYear();
  var time = dt.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  if (isThisYear) {
    return dt.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ' ' + time;
  }
  return dt.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) + ' ' + time;
}

function localizeAllTimes() {
  document.querySelectorAll('time[datetime]').forEach(function (el) {
    var localized = _formatLocalTime(el.getAttribute('datetime'), el.dataset.format || 'short');
    if (localized) el.textContent = localized;
  });
}

document.addEventListener('DOMContentLoaded', localizeAllTimes);
document.body.addEventListener('htmx:afterSettle', localizeAllTimes);

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
      menu.style.top = (rect.bottom + 4) + 'px';
      menu.style.right = (window.innerWidth - rect.right) + 'px';
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
  var titleEl = document.getElementById('mobile-title-text');
  if (titleEl && snap.title !== null) {
    titleEl.textContent = snap.title;
    try { localStorage.setItem('mobile_title_text', snap.title); } catch (e) {}
  }
  _syncMobileQuicklink();
  if (snap.url) htmx.ajax('GET', snap.url, { target: '#article-list', swap: 'innerHTML' });
}

function _showNavErrorToast() {
  var existing = document.getElementById('nav-error-toast');
  if (existing) return;
  var toast = document.createElement('div');
  toast.id = 'nav-error-toast';
  toast.textContent = 'Connection error — restoring previous view';
  toast.style.cssText = 'position:fixed;bottom:4rem;left:50%;transform:translateX(-50%);' +
    'background:#374151;color:#fff;padding:0.5rem 1rem;border-radius:0.5rem;' +
    'font-size:0.8rem;z-index:9999;white-space:nowrap;pointer-events:none;';
  document.body.appendChild(toast);
  setTimeout(function () { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 3000);
}

document.body.addEventListener('htmx:sendError', function (e) {
  if (!e.detail.target || e.detail.target.id !== 'article-list') return;
  if (!_navSnapshot) return;
  _showNavErrorToast();
  _revertNavSnapshot();
});

document.addEventListener('click', function (e) {
  var navItem = e.target.closest('.nav-item');
  if (!navItem) return;
  document.querySelectorAll('.nav-item').forEach(function (i) { i.classList.remove('active'); });
  navItem.classList.add('active');
  _activeNavGet = navItem.getAttribute('hx-get');
  try { if (_activeNavGet) localStorage.setItem('lastNavItem', _activeNavGet); } catch (err) {}
  if (_activeNavGet) htmx.trigger(document.body, 'sidebarRefresh');
});

document.body.addEventListener('htmx:beforeSwap', function (evt) {
  if (evt.detail.target.id !== 'sidebar') return;
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
      } else if (seen.has(id) && !isRead && entry.boundingClientRect.top < 0) {
        seen.delete(id);
        el.dataset.isRead = 'true';
        el.classList.add('opacity-75');
        var titleEl = el.querySelector('[data-article-title]');
        if (titleEl) {
          titleEl.classList.remove('font-bold', 'text-gray-900');
          titleEl.classList.add('font-medium', 'text-gray-800');
        }
        htmx.ajax('POST', '/htmx/articles/' + id + '/set-read?state=true', { swap: 'none' });
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

// ── Keyboard shortcuts ─────────────────────────────────────────────────────
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') { closeSearchModal(); return; }
  if (e.key === 'Enter' && e.target.id === 'search-input') { submitSearch(); return; }
  if (e.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) {
    e.preventDefault();
    openSearchModal();
  }
});

// ── data-action delegation ─────────────────────────────────────────────────
document.addEventListener('click', function (e) {
  var el = e.target.closest('[data-action]');
  if (!el) return;
  var action = el.dataset.action;
  if (action === 'toggle-user-menu') { toggleUserMenu(); return; }
  if (action === 'open-search') { openSearchModal(); return; }
  if (action === 'close-search') { closeSearchModal(); return; }
  if (action === 'submit-search') { submitSearch(); return; }
  if (action === 'select-all') { el.select(); return; }
  if (action === 'refresh-articles') {
    htmx.ajax('GET', '/htmx/articles', { target: '#article-list', swap: 'innerHTML' });
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
        bsSpan.className = 'text-base leading-none ' + (detail.isStarred ? 'text-gray-900' : 'text-gray-300');
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
  var row = document.getElementById('article-row-' + detail.id);
  if (!row) return;
  var btn = row.querySelector('[data-star-btn]');
  if (!btn) return;
  var isStarred = detail.isStarred;
  var isRead = row.dataset.isRead === 'true';
  var span = btn.querySelector('span');
  if (span) {
    if (isStarred) {
      span.textContent = '★';
      span.className = span.className
        .replace('text-gray-300', isRead ? 'text-gray-800' : 'text-gray-900')
        .replace(' hover:text-gray-800', '');
    } else {
      span.textContent = '☆';
      span.className = span.className
        .replace('text-gray-900', 'text-gray-300')
        .replace('text-gray-800', 'text-gray-300');
      if (!span.className.includes('hover:text-gray-800')) span.className += ' hover:text-gray-800';
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

document.addEventListener('articleArchiveChanged', function (e) {
  var detail = e.detail;
  var bottomBar = document.querySelector('.article-bottom-bar[data-article-id="' + detail.id + '"]');
  if (bottomBar && !bottomBar.classList.contains('hidden')) {
    var baBtn = bottomBar.querySelector('[data-bottom-archive]');
    if (baBtn) {
      baBtn.classList.toggle('text-gray-700', detail.isArchived);
      baBtn.classList.toggle('text-gray-400', !detail.isArchived);
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

  // Title <a> click handling: prevent native navigation except when row is expanded in 2-panel
  document.addEventListener('click', function (e) {
    var a = e.target.closest('[data-article-title]');
    if (!a || !a.href) return;
    var row = a.closest('.article-row');
    if (!row) return;
    var isExpanded = _shouldUseInline() && row.classList.contains('inline-expanded');
    if (isExpanded) {
      e.stopImmediatePropagation(); // block HTMX; let native <a target="_blank"> handle navigation
      if (row.dataset.density === 'compact') {
        closeInline(); // HTMX won't fire (propagation stopped), close manually
      }
      // comfortable: row stays expanded
    } else {
      e.preventDefault(); // block link navigation, let HTMX load detail
    }
  }, true);

  function closeInline() {
    var el = document.getElementById(INLINE_ID);
    if (el) el.remove();
    var expandedRow = document.querySelector('.article-row.inline-expanded');
    if (expandedRow) expandedRow.classList.remove('inline-expanded');
  }

  window._closeInlineDetail = closeInline;

  // Intercept HTMX requests targeting #article-detail for inline expand
  document.body.addEventListener('htmx:beforeRequest', function (e) {
    if (!_shouldUseInline()) return;
    if (!e.detail.target || e.detail.target.id !== 'article-detail') return;
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
    container.appendChild(content);

    row.insertAdjacentElement('afterend', container);
    row.classList.add('inline-expanded');

    // Load article content
    htmx.ajax('GET', '/htmx/articles/' + articleId, {
      target: '#' + CONTENT_ID,
      swap: 'innerHTML'
    });

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
  if (e.detail.target && e.detail.target.id === 'sidebar') restoreSidebarCollapse(false);
});

// HTMX settle can re-apply server classes and drop client-added "collapsed".
// Re-apply once more after settle to keep sections collapsed after sidebar refresh.
document.body.addEventListener('htmx:afterSettle', function (e) {
  if (!(e.detail.target && e.detail.target.id === 'sidebar')) return;
  restoreSidebarCollapse(false);
  var sb = e.detail.target;
  if (sb.style.opacity === '0') {
    sb.style.transition = 'opacity 150ms ease';
    sb.style.opacity = '1';
    setTimeout(function () { sb.style.transition = ''; sb.style.opacity = ''; }, 160);
  }
});

restoreSidebarCollapse(false);

// ── Sidebar mark-all-as-read: refresh sidebar + article list after action ──
document.body.addEventListener('htmx:afterRequest', function (e) {
  if (!e.detail.elt || e.detail.elt.dataset.action !== 'mark-read') return;
  document.querySelectorAll('.mark-read-row.touch-active').forEach(function (r) {
    r.classList.remove('touch-active');
  });
  if (!e.detail.successful) return;
  htmx.trigger(document.body, 'sidebarRefresh');
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

// ── Inline confirm (replaces native confirm() dialogs) ────────────────────
document.addEventListener('click', function (e) {
  var trigger = e.target.closest('.inline-confirm-trigger');
  if (trigger) {
    var wrap = trigger.closest('.inline-confirm');
    trigger.classList.add('hidden');
    wrap.querySelector('.inline-confirm-ask').classList.remove('hidden');
    return;
  }
  var cancel = e.target.closest('.inline-confirm-cancel');
  if (cancel) {
    var wrap = cancel.closest('.inline-confirm');
    wrap.querySelector('.inline-confirm-ask').classList.add('hidden');
    wrap.querySelector('.inline-confirm-trigger').classList.remove('hidden');
  }
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

  function _collapsibleSidebarFadeOut() {
    var sb = document.getElementById('sidebar');
    if (sb) { sb.style.transition = 'opacity 100ms ease'; sb.style.opacity = '0'; }
  }

  function openSidebarOverlay() {
    document.documentElement.classList.add('mobile-sidebar-open');
    history.pushState({ mobileSidebarOpen: true }, '');
    if (isCollapsible()) { _collapsibleSidebarFadeOut(); htmx.trigger(document.body, 'sidebarRefresh'); }
  }

  function closeSidebarOverlay() {
    document.documentElement.classList.remove('mobile-sidebar-open');
    if (isCollapsible()) { _collapsibleSidebarFadeOut(); htmx.trigger(document.body, 'sidebarRefresh'); }
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

  // Strip hamburger (minimizable) and title bar hamburger (hideable)
  document.addEventListener('click', function (e) {
    if (!isMobile()) return;
    if (e.target.closest('#mobile-strip-open-btn') || e.target.closest('#mobile-titlebar-open-btn') || e.target.closest('#mobile-bottombar-open-btn')) {
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
    document.documentElement.classList.remove('mobile-detail-open');
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

  document.addEventListener('click', function (e) {
    var trigger = e.target.closest('#detail-topbar-share') || e.target.closest('[data-bottom-share]');
    if (!trigger) return;
    var picker = document.getElementById('detail-share-picker');
    if (!picker) return;
    var isOpen = !picker.classList.contains('hidden');
    if (isOpen) { picker.classList.add('hidden'); e.stopPropagation(); return; }
    picker.classList.remove('hidden');
    var rect = trigger.getBoundingClientRect();
    var pickerH = picker.offsetHeight;
    var pickerW = picker.offsetWidth;
    var spaceBelow = window.innerHeight - rect.bottom;
    var top = spaceBelow >= pickerH + 8 ? rect.bottom + 4 : Math.max(4, rect.top - pickerH - 4);
    picker.style.top = top + 'px';
    picker.style.left = Math.max(4, rect.left - pickerW + rect.width) + 'px';
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
      if (scrollTop < 5) {
        bar.classList.remove('bottom-bar-visible');
      } else if (atBottom || scrollTop < lastScrollTop) {
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

