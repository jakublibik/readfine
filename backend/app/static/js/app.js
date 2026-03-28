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
