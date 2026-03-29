// Local time formatting for <time datetime="..."> elements
function _formatLocalTime(isoStr, format) {
  var dt = new Date(isoStr);
  if (isNaN(dt.getTime())) return null;
  if (format === 'long') {
    return dt.toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' });
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

// Nav active state — event delegation, survives HTMX sidebar swaps
document.addEventListener('click', function (e) {
  var navItem = e.target.closest('.nav-item');
  if (!navItem) return;
  document.querySelectorAll('.nav-item').forEach(function (i) { i.classList.remove('active'); });
  navItem.classList.add('active');
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
    htmx.ajax('POST', '/htmx/articles/' + articleId + '/set-read?state=true', {
      target: '#read-btn-' + articleId,
      swap: 'innerHTML'
    });
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

