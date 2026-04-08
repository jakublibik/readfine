// Open article content links in a new tab
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
  var articleEl = document.querySelector('#article-detail [data-article-id]');
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

// Sidebar pin toggle — width class swap on sidebarPinChanged event
document.body.addEventListener('sidebarPinChanged', function (e) {
  var sidebar = document.getElementById('sidebar');
  if (!sidebar) return;
  sidebar.classList.toggle('w-64', e.detail.pinned);
  sidebar.classList.toggle('w-12', !e.detail.pinned);
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
  if (evt.detail.target.id !== 'article-detail') return;

  var articleEl = document.querySelector('#article-detail [data-article-id]');
  if (!articleEl) return;

  var articleId = articleEl.dataset.articleId;
  var isRead = articleEl.dataset.isRead === 'true';
  if (isRead) return;

  var timer = setTimeout(function () {
    var target = document.getElementById('read-btn-' + articleId);
    if (target) {
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
  }, 1500);

  document.getElementById('article-detail').addEventListener(
    'htmx:beforeRequest', function () { clearTimeout(timer); }, { once: true }
  );
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

// ── stopPropagation for action buttons inside article rows ─────────────────
// Attached via htmx:afterSettle so each button gets its own listener —
// bubbling phase only, so hx-post on the button fires first.
document.body.addEventListener('htmx:afterSettle', function (evt) {
  if (evt.detail.target.id !== 'article-list') return;
  evt.detail.target.querySelectorAll('[data-stop-propagation]').forEach(function (el) {
    el.addEventListener('click', function (e) { e.stopPropagation(); });
  });
});

