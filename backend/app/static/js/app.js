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
      html.dataset.sidebarMode = mode || 'collapsible';
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
  if (menu) menu.classList.add('hidden');
});

// Nav active state — persists across sidebarRefresh swaps
var _activeNavGet = null;

document.addEventListener('click', function (e) {
  var navItem = e.target.closest('.nav-item');
  if (!navItem) return;
  document.querySelectorAll('.nav-item').forEach(function (i) { i.classList.remove('active'); });
  navItem.classList.add('active');
  _activeNavGet = navItem.getAttribute('hx-get');
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

// Article list: IntersectionObserver for mark-as-read on scroll
document.body.addEventListener('htmx:afterSettle', function (evt) {
  if (evt.detail.target.id !== 'article-list') return;

  var cfgEl = document.getElementById('article-list-cfg');
  if (!cfgEl) return;
  var cfg = JSON.parse(cfgEl.textContent);
  if (!cfg.markReadOnScroll) return;

  var list = document.getElementById('article-list');
  if (!list) return;
  var seen = new Set();

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
        el.classList.add('opacity-60');
        var dot = el.querySelector('.unread-dot');
        if (dot) dot.remove();
        htmx.ajax('POST', '/htmx/articles/' + id + '/read', { swap: 'none' });
      }
    });
  }, { root: list, threshold: 0.1 });

  list.querySelectorAll('.article-row').forEach(function (el) {
    observer.observe(el);
  });
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
    sidebar.classList.toggle('w-56', next);
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
  var row = document.getElementById('article-row-' + detail.id);
  if (!row) return;
  var isRead = detail.isRead;
  row.classList.toggle('opacity-60', isRead);
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

document.addEventListener('articleArchiveChanged', function (e) {
  var detail = e.detail;
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

  // Position picker near trigger button before HTMX request fires
  document.body.addEventListener('htmx:beforeRequest', function (e) {
    if (!e.target.hasAttribute('data-label-trigger')) return;
    var rect = e.target.getBoundingClientRect();
    var p = document.getElementById('label-picker');
    if (!p) return;
    var top = rect.bottom + 2;
    var left = rect.left;
    // Clamp to viewport right edge
    if (left + 220 > window.innerWidth) left = window.innerWidth - 224;
    p.style.top = top + 'px';
    p.style.left = left + 'px';
    p.classList.remove('hidden');
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
  var STORAGE_KEY = 'article-list-width';
  var MIN_WIDTH = 200;
  var MAX_WIDTH = 900;

  function applyWidth(px) {
    var el = document.getElementById('article-list');
    if (el) el.style.width = px + 'px';
  }

  document.addEventListener('DOMContentLoaded', function () {
    var saved = parseInt(localStorage.getItem(STORAGE_KEY), 10);
    if (saved && saved >= MIN_WIDTH && saved <= MAX_WIDTH) applyWidth(saved);

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
        var w = Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, startWidth + e.clientX - startX));
        applyWidth(w);
      }

      function onUp() {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        resizer.classList.remove('bg-blue-500');
        var list = document.getElementById('article-list');
        if (list) localStorage.setItem(STORAGE_KEY, list.offsetWidth);
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
      if (row.dataset.density !== 'compact') {
        e.stopImmediatePropagation(); // comfortable: block HTMX, let <a> open new tab (row stays expanded)
      }
      // compact: do nothing — <a> opens link naturally, HTMX bubbles up and closeInline fires
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

    // Scroll row into view
    setTimeout(function () {
      row.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 50);
  });

  // Close inline when article list reloads (nav change)
  document.body.addEventListener('htmx:beforeSwap', function (e) {
    if (e.detail.target.id === 'article-list') closeInline();
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

document.body.addEventListener('click', function (e) {
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
});

// afterSwap fires before the browser paints — prevents collapsed-section flash on load.
document.body.addEventListener('htmx:afterSwap', function (e) {
  if (e.detail.target && e.detail.target.id === 'sidebar') restoreSidebarCollapse(false);
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

  function openSidebarOverlay() {
    document.documentElement.classList.add('mobile-sidebar-open');
    history.pushState({ mobileSidebarOpen: true }, '');
    // Collapsible: reload sidebar with full (pinned) template
    if (isCollapsible()) htmx.trigger(document.body, 'sidebarRefresh');
  }

  function closeSidebarOverlay() {
    document.documentElement.classList.remove('mobile-sidebar-open');
    // Collapsible: reload sidebar back to rail template
    if (isCollapsible()) htmx.trigger(document.body, 'sidebarRefresh');
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
    if (e.target.closest('#mobile-strip-open-btn') || e.target.closest('#mobile-titlebar-open-btn')) {
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
    var titleEl = item.querySelector('span.flex-1');
    var title = (titleEl ? titleEl.textContent : (item.getAttribute('title') || '')).trim().slice(0, 40);
    var titleText = document.getElementById('mobile-title-text');
    if (titleText) titleText.textContent = title;
    if (document.documentElement.classList.contains('mobile-sidebar-open')) {
      closeSidebarOverlay();
    }
  });

  // Detail back button: close fullscreen detail
  document.addEventListener('click', function (e) {
    if (!e.target.closest('#mobile-detail-back-btn')) return;
    document.documentElement.classList.remove('mobile-detail-open');
    history.back();
  });

  // After article loads into #article-detail: open fullscreen if detail_mode_small=fullscreen
  document.body.addEventListener('htmx:afterSettle', function (e) {
    if (!isMobile() || e.detail.target.id !== 'article-detail') return;
    try { if (localStorage.getItem('detail_mode_small') !== 'fullscreen') return; } catch (err) { return; }
    document.documentElement.classList.add('mobile-detail-open');
    history.pushState({ mobileDetailOpen: true }, '');
  });

  // Browser back: sync classes with history state
  window.addEventListener('popstate', function (e) {
    if (!isMobile()) return;
    var state = e.state || {};
    document.documentElement.classList.toggle('mobile-sidebar-open', !!state.mobileSidebarOpen);
    document.documentElement.classList.toggle('mobile-detail-open', !!state.mobileDetailOpen);
  });
})();

