// ── CSRF token from the double-submit cookie, for hand-rolled fetch() calls.
// (HTMX requests get it automatically via csrf.js.) ──
function getCsrfToken() {
  var m = document.cookie.split('; ').find(function (r) { return r.startsWith('csrftoken='); });
  return m ? m.split('=')[1] : '';
}

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
// The strip is a horizontal scroll container and browsers restore its offset
// when you navigate (e.g. Admin → Settings), which lands *after* the first
// pass here and leaves the active tab off-screen. So run several passes, each
// a no-op once the tab is visible, and stop as soon as the user scrolls the
// strip by hand.
(function () {
  var userScrolled = false;

  function scrollActiveNavIntoView() {
    if (userScrolled) return;
    var active = document.querySelector('[data-mobile-nav] [data-mobile-nav-active]');
    if (!active) return;
    var bar = active.closest('[data-mobile-nav]');
    if (!bar || bar.offsetParent === null) return; // hidden (desktop): skip
    // offsetLeft is relative to the nearest positioned ancestor, not to the
    // strip, so measure against the strip itself.
    var left = active.getBoundingClientRect().left - bar.getBoundingClientRect().left;
    if (left >= 0 && left + active.offsetWidth <= bar.clientWidth) return; // already visible
    // Horizontally center the active tab without scrolling the page vertically.
    bar.scrollLeft += left - (bar.clientWidth - active.offsetWidth) / 2;
  }

  function markUserScroll(e) {
    if (e.target.closest && e.target.closest('[data-mobile-nav]')) userScrolled = true;
  }
  var listenOpts = { capture: true, passive: true };
  document.addEventListener('pointerdown', markUserScroll, listenOpts);
  document.addEventListener('touchstart', markUserScroll, listenOpts);
  document.addEventListener('wheel', markUserScroll, listenOpts);

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', scrollActiveNavIntoView);
  } else {
    scrollActiveNavIntoView();
  }
  requestAnimationFrame(scrollActiveNavIntoView);
  window.addEventListener('load', function () {
    scrollActiveNavIntoView();
    setTimeout(scrollActiveNavIntoView, 150);
  });
  window.addEventListener('pageshow', scrollActiveNavIntoView);
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
      if (window.syncThemeColor) window.syncThemeColor();
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

// ── Videos in article content: play them here instead of leaving for the site
//
// Stored content carries only a thumbnail and a link (see app/utils/video.py, video_figure),
// and it stays that way until the reader clicks. No player is loaded before then, so no
// video service can set a cookie on an article you only scrolled past.
//
// The thumbnail's src is our own /img/video-thumb endpoint, not the video host, so
// opening an article no longer hands YouTube or Vimeo the reader's IP and the video id
// (see app/utils/video.py and video_thumb_service). That is a server-side matter and
// nothing here needs to touch it — including the old dance of upgrading the src to a
// sharper image and reverting on error, which the server now resolves once.
//
// The click is what loads the player, and the player is built here from the ids on the
// figure, never from markup a feed supplied. The id is checked against the shape it must
// have, because it goes into a URL and the figure may well have arrived in a feed's own
// HTML.
var VIDEO_PROVIDERS = {
  youtube: {
    id: /^[A-Za-z0-9_-]{6,20}$/,
    label: 'YouTube',
    // youtube-nocookie: no cookies until playback actually starts.
    src: function (id, start) {
      return 'https://www.youtube-nocookie.com/embed/' + id + '?autoplay=1&rel=0' +
        (start ? '&start=' + start : '');
    },
  },
  vimeo: {
    id: /^\d{5,15}$/,
    label: 'Vimeo',
    src: function (id, start) {
      return 'https://player.vimeo.com/video/' + id + '?autoplay=1' + (start ? '#t=' + start + 's' : '');
    },
  },
};

function _videoProvider(fig) {
  var spec = VIDEO_PROVIDERS[fig.getAttribute('data-video-provider')];
  return spec && spec.id.test(fig.getAttribute('data-video-id') || '') ? spec : null;
}

// Label every figure the handler below can act on. The caption states what the thing
// is and what playing it costs; it is deliberately not phrased as an instruction,
// since the badge on the thumbnail is what asks to be clicked and a caption reading
// "Play video" competes with it for the same click. The invitation lives on the
// thumbnail, in the badge and in its tooltip.
//
// Done in script rather than in the stored markup so articles saved before this
// existed are described the same way.
function markVideoFacades(root) {
  (root || document).querySelectorAll('.prose figure[data-video-id]').forEach(function (fig) {
    var spec = _videoProvider(fig);
    // data-video-playing: a figure whose player is already running must not be
    // described as a facade again by a swap somewhere else on the page.
    if (!spec || fig.hasAttribute('data-video-ready') || fig.hasAttribute('data-video-playing')) return;
    fig.setAttribute('data-video-ready', '');
    var link = fig.querySelector('a[href]');
    if (link) link.setAttribute('title', 'Play here (loads the player from ' + spec.label + ')');
    var caption = fig.querySelector('figcaption');
    if (caption) caption.textContent = spec.label + ' video, loaded only when you play it';
  });
}
document.addEventListener('DOMContentLoaded', function () { markVideoFacades(); });
document.body.addEventListener('htmx:afterSettle', function () { markVideoFacades(); });

// Start the player in *fig*, at *start* seconds when given. A player already running
// is re-pointed rather than rebuilt: reloading the frame is a second of black, but it
// needs no player API, which would mean loading a script from the video site and
// widening script-src for the sake of a smoother seek.
function playVideo(fig, spec, start) {
  var running = fig.querySelector('iframe.video-embed');
  if (running) {
    running.src = spec.src(fig.getAttribute('data-video-id'), start);
    return;
  }
  var frame = document.createElement('iframe');
  frame.src = spec.src(fig.getAttribute('data-video-id'), start);
  frame.className = 'video-embed';
  frame.title = spec.label + ' video player';
  frame.setAttribute('allow', 'autoplay; encrypted-media; picture-in-picture; fullscreen');
  frame.setAttribute('allowfullscreen', '');
  frame.setAttribute('referrerpolicy', 'strict-origin-when-cross-origin');

  // The player goes over the thumbnail, not in its place: the picture holds the box
  // while the frame loads, so there is no moment with the page background showing
  // between two dark things (see input.css). Nothing below the video moves either,
  // since the thumbnail keeps taking exactly the room it took, and the caption keeps
  // its line. What the caption says changes, though: the way out to the site is the
  // useful thing to offer once the video is already playing here.
  var link = fig.querySelector('a[href]');
  var href = link ? link.getAttribute('href') : null;
  if (link) {
    // One box around both, so the player takes the thumbnail's box exactly instead of
    // computing a near-identical one of its own (see input.css).
    var box = document.createElement('div');
    box.className = 'video-box';
    link.replaceWith(box);
    box.appendChild(link);
    box.appendChild(frame);
    // Hide the thumbnail once the frame has painted: it is there to cover the load and
    // nothing more. Hidden on the element itself rather than by a selector, so the
    // caption's link out to the site is not caught by the same rule. If the event
    // never comes, the picture stays behind the player, which is where it started.
    frame.addEventListener('load', function () { link.style.visibility = 'hidden'; }, { once: true });
  } else {
    fig.replaceChildren(frame);
  }

  var caption = fig.querySelector('figcaption');
  if (caption && href) {
    var out = document.createElement('a');
    out.href = href;
    out.target = '_blank';
    out.rel = 'noopener noreferrer';
    out.textContent = 'Watch on ' + spec.label;
    caption.replaceChildren(out);
  } else if (caption) {
    caption.textContent = spec.label;
  }

  // Off with the facade marks: the badge belongs to a thumbnail, and leaving the
  // figure clickable would have the play handler swallow the click on the link the
  // caption now holds.
  fig.removeAttribute('data-video-ready');
  fig.setAttribute('data-video-playing', '');
}

// Capture, and stopped there: the thumbnail is wrapped in an anchor to the video
// site, openProseLinksInNewTab has since marked it target="_blank", and the
// link-opened tracker counts any such click inside an article as the reader leaving
// for the source. That signal feeds retention and scoring, and pressing play here is
// not leaving. Bubbling would reach the tracker before this handler, so the click has
// to be taken on the way down.
document.addEventListener('click', function (e) {
  if (!e.target || !e.target.closest) return;
  var fig = e.target.closest('.prose figure[data-video-ready]');
  if (!fig) return;
  var spec = _videoProvider(fig);
  // Without a provider we never reach here, but if we did, the anchor to the site is
  // the right thing to leave alone.
  if (!spec) return;
  e.preventDefault();
  e.stopPropagation();
  playVideo(fig, spec);
}, true);

// The video a chapter mark belongs to: the last one above it. A description sits
// under its own video, and an article holding several videos each with their own
// description would otherwise send every mark to the first player on the page.
function _videoForSeek(el) {
  var scope = el.closest('.prose');
  if (!scope) return null;
  var found = null;
  scope.querySelectorAll('figure[data-video-id]').forEach(function (fig) {
    if (fig.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) found = fig;
  });
  return found;
}

// Chapter marks in a description seek the player here instead of opening the video
// site at that point, which is what their href does for anyone this script never
// reaches. Same capture treatment as above, and for the same reason: the mark is an
// anchor out to the site, so a bubbling click would be filed as the reader leaving.
document.addEventListener('click', function (e) {
  if (!e.target || !e.target.closest) return;
  var mark = e.target.closest('.prose a[data-seek]');
  if (!mark) return;
  var seconds = parseInt(mark.getAttribute('data-seek'), 10);
  // Stored marks are written by _link_timestamps, but the same attribute in a feed's
  // own markup would arrive here too, so the value is read as a number or not at all.
  if (!isFinite(seconds) || seconds < 0) return;
  var fig = _videoForSeek(mark);
  if (!fig) return;
  var spec = _videoProvider(fig);
  if (!spec) return;
  e.preventDefault();
  e.stopPropagation();
  playVideo(fig, spec, seconds);
  // A long description puts the player off the top of the screen; block: 'nearest'
  // leaves it alone when it is already visible.
  fig.scrollIntoView({ block: 'nearest' });
}, true);

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
  // Anchor with both left+right so the box stretches edge-to-edge (minus a small
  // gutter) on narrow screens; max-width + margin:auto caps and centers it on wider
  // ones. Without this a fixed block shrinks to its text and looks half-width on mobile.
  toast.style.cssText = 'position:fixed;bottom:4rem;left:0.75rem;right:0.75rem;margin-inline:auto;' +
    'max-width:24rem;background:' + bg + ';color:#fff;padding:0.5rem 1rem;border-radius:0.5rem;' +
    'font-size:0.8rem;z-index:9999;word-break:break-word;pointer-events:none;text-align:center;';
  document.body.appendChild(toast);
  setTimeout(function () { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 4000);
}

function _showNavErrorToast() {
  showToast('Connection error — restoring previous view', 'info');
}

document.body.addEventListener('showToast', function (e) {
  showToast(e.detail.msg, e.detail.type);
});

// An article was removed from Saved. It no longer exists in the current view, so
// drop its row and clear the detail panel. No two-way state sync is needed here
// (unlike star/archive): nothing survives to keep in sync.
document.body.addEventListener('savedArticleRemoved', function (e) {
  var id = e.detail && e.detail.id;
  if (!id) return;
  // Scoped to a row in the list, because data-article-id is on five different things:
  // the row, the detail's outer div, the <article> inside it, the bottom bar, and the
  // inline container built below. An unscoped query takes the first in document order,
  // and the list comes before the detail. With the article open but its row not in the
  // list (opened from Saved, then another feed picked in the sidebar), that used to
  // remove the whole detail pane and then find nothing left to put the empty state in.
  var row = document.querySelector('#article-list .article-row[data-article-id="' + id + '"]');
  if (row) row.remove();
  // The 2-panel/mobile body is the row's sibling, not its child, so removing the row
  // leaves it behind: an expanded article with no row above it.
  var inline = document.getElementById('inline-article-detail');
  if (inline && inline.dataset.articleId === String(id)) inline.remove();
  var detail = document.getElementById('article-detail');
  // Exact, not a prefix: the id carries no suffix, and "article-content-1" is a prefix
  // of "article-content-12", so removing article 1 cleared the pane on article 12.
  var openDetail = detail && detail.querySelector('#article-content-' + id);
  if (openDetail) {
    // The empty state main.html renders, minus its icon: there is no round trip
    // here to render the real one, and this is the only place that needs it
    // client-side. Keep the wording in step with that template.
    detail.innerHTML =
      '<div class="flex items-center justify-center h-full text-gray-400">' +
      '<div class="text-center"><p class="text-sm">Select an article to read</p></div></div>';
  }
  showToast('Removed from Saved', 'ok');
});

// Height of a sticky list header (the Saved URL box, the search-results strip), which
// stays put while the list scrolls. A row aligned to the list's own top would slide
// underneath it. The mobile title bar is not counted: the shell reserves its height
// (see base.html), so the list already starts below it.
function listStickyOffset() {
  var listHeader = document.querySelector('#article-list [data-list-header]');
  return listHeader ? listHeader.getBoundingClientRect().height : 0;
}

// An article was added to Saved. The list is ordered by publication date, so the row
// rarely lands on top — a video from a feed you follow carries the date it was
// published and can sit far down. Bring it into view and flash it, so saving doesn't
// look like nothing happened.
//
// The event arrives before the list is swapped in (the save form sits inside
// #article-list, so the swap removes the very element an after-settle trigger would
// fire on), hence the two steps: remember the id, act once the new list has settled.
var _pendingSavedRowId = null;

document.body.addEventListener('savedArticleAdded', function (e) {
  _pendingSavedRowId = (e.detail && e.detail.id) || null;
});

document.body.addEventListener('htmx:afterSettle', function (e) {
  if (!_pendingSavedRowId) return;
  if (!e.detail.target || e.detail.target.id !== 'article-list') return;
  var id = _pendingSavedRowId;
  _pendingSavedRowId = null;
  var list = e.detail.target;
  var row = list && list.querySelector('[data-article-id="' + id + '"]');
  if (!row) {
    // Older than everything on the first page, so there is no row to point at. Stay
    // quiet if the server already sent a toast ("Already saved…"): two of them land
    // on the same spot and the reader gets neither.
    if (!document.querySelector('[id^="app-toast-"]')) {
      showToast('Saved, further down the list', 'info');
    }
    return;
  }
  var offset = listStickyOffset();
  var rowRect = row.getBoundingClientRect();
  var listRect = list.getBoundingClientRect();
  // Move as little as possible: a visible row makes the list stay put, one below the
  // fold comes up to just above the bottom edge (so the articles you were looking at
  // keep their place), and one above the fold aligns under the sticky header.
  var target = null;
  if (rowRect.top < listRect.top + offset) {
    target = list.scrollTop + rowRect.top - listRect.top - offset;
  } else if (rowRect.bottom > listRect.bottom) {
    target = list.scrollTop + rowRect.bottom - listRect.bottom + 12;
  }
  if (target !== null) {
    // Instant, and with mark-as-read held off: the row can be hundreds of articles
    // down, and gliding there would run every article in between past the top edge,
    // which the read-on-scroll observer counts as read. The flash is what points the
    // row out, so nothing is lost by jumping straight to it.
    window._suppressMarkRead = true;
    list.scrollTo({ top: Math.max(0, target), behavior: 'instant' });
    setTimeout(function () { window._suppressMarkRead = false; }, 300);
  }
  row.classList.add('row-just-saved');
  setTimeout(function () { row.classList.remove('row-just-saved'); }, 2000);
});

// A manual feed refresh finished — if that feed is the one currently displayed,
// reload the article list so newly fetched items appear without re-clicking it.
document.body.addEventListener('feedRefreshed', function (e) {
  var feedId = e.detail && e.detail.feed_id;
  if (!feedId || !_activeNavGet) return;
  // Anchor to a full param value so feed_id=5 doesn't match feed_id=50.
  var re = new RegExp('[?&]feed_id=' + feedId + '(?:&|$)');
  if (re.test(_activeNavGet)) {
    htmx.ajax('GET', _activeNavGet, { target: '#article-list', swap: 'innerHTML' });
  }
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

// ── Article-list loading overlay ──────────────────────────────────────────
// Switching nav (Starred ⇄ Labeled, folders, feeds…) swaps #article-list
// asynchronously, so the nav highlight changed instantly while the list still
// showed the previous section. Show a loading overlay over the list for the
// duration of the request so the two don't visibly desync. The pagination
// "load-more" sentinel targets itself (not #article-list), so it's excluded by
// the target check and doesn't trigger the overlay.
// Delay showing the overlay so quick (local/cached) loads don't flash a spinner.
// Only requests that outlast the threshold get the overlay; faster ones swap in
// before it ever appears.
var _LIST_LOADING_DELAY_MS = 150;
var _listLoadingTimer = null;
function _showListLoading() {
  if (_listLoadingTimer) return;
  _listLoadingTimer = setTimeout(function () {
    _listLoadingTimer = null;
    var list = document.getElementById('article-list');
    if (!list || list.querySelector('.article-list-loading')) return;
    var ov = document.createElement('div');
    ov.className = 'article-list-loading absolute inset-0 z-20 bg-white/80';
    ov.innerHTML =
      '<div class="sticky top-0 flex items-center justify-center" style="height:55vh">' +
        '<svg class="w-6 h-6 animate-spin text-blue-500" fill="none" viewBox="0 0 24 24">' +
          '<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>' +
          '<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path>' +
        '</svg>' +
      '</div>';
    list.appendChild(ov);
  }, _LIST_LOADING_DELAY_MS);
}
function _hideListLoading() {
  if (_listLoadingTimer) { clearTimeout(_listLoadingTimer); _listLoadingTimer = null; }
  var list = document.getElementById('article-list');
  if (!list) return;
  var ov = list.querySelector('.article-list-loading');
  if (ov) ov.remove();
}
// On success the innerHTML swap removes the overlay for us; afterRequest is the
// catch-all that also clears it after a 4xx/5xx (which leaves the old list in
// place with no swap).
document.body.addEventListener('htmx:beforeRequest', function (e) {
  if (e.detail.target && e.detail.target.id === 'article-list') _showListLoading();
});
document.body.addEventListener('htmx:afterRequest', function (e) {
  if (e.detail.target && e.detail.target.id === 'article-list') _hideListLoading();
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
  // On the initial `load` swap _activeNavGet may not be set yet (it's assigned by
  // _autoLoadArticleList, which races this fetch) — fall back to the saved nav so
  // the active category is highlighted from the first render.
  var navGet = _activeNavGet;
  if (!navGet) { try { navGet = localStorage.getItem('lastNavItem'); } catch (e) {} }
  navGet = navGet || '/htmx/articles';
  var temp = document.createElement('div');
  temp.innerHTML = evt.detail.serverResponse;
  // Each nav target is rendered twice (collapsed rail + full sidebar); highlight
  // every match so whichever one is visible shows the active state.
  var matches = temp.querySelectorAll('.nav-item[hx-get="' + navGet + '"]');
  if (matches.length) {
    temp.querySelectorAll('.nav-item').forEach(function (i) { i.classList.remove('active'); });
    matches.forEach(function (m) { m.classList.add('active'); });
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

// Restore last-selected nav on page load; fall back to All Articles.
// A ?view=starred|labeled deep-link (e.g. from the Stats page) overrides the
// saved nav and is consumed from the URL, like ?open_article_id.
function _autoLoadArticleList() {
  if (!document.getElementById('article-list')) return;
  var url;
  var view = window.location.search.match(/[?&]view=(starred|labeled|saved)(?:&|$)/);
  if (view) {
    url = '/htmx/articles?' + view[1] + '_only=true';
    try { localStorage.setItem('lastNavItem', url); } catch (e) {}
    history.replaceState(null, '', window.location.pathname);
  } else {
    var saved;
    try { saved = localStorage.getItem('lastNavItem'); } catch (e) {}
    url = saved || '/htmx/articles';
  }
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
  var csrfToken = getCsrfToken();
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

  // No inset needed for the mobile title bar: the shell reserves its height (see
  // base.html), so the list's own top edge already sits below it.
  var topOffset = 0;
  var bottomOffset = 0;

  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      var el = entry.target;
      var id = el.dataset.articleId;
      var isRead = el.dataset.isRead === 'true';

      if (entry.isIntersecting) {
        seen.add(id);
      } else if (!isRead && entry.boundingClientRect.top < 0) {
        // A jump the app made on the reader's behalf (pointing at a freshly saved
        // row) is not reading: whatever it flew past stays unread.
        if (window._suppressMarkRead) return;
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

// Sidebar pin toggle — fully client-side. Both the rail and the full sidebar are
// always in the DOM, so this just flips the state class + width and CSS swaps which
// block is shown. No server round-trip → instant collapse/expand.
document.body.addEventListener('click', function (e) {
  if (!e.target.closest('[data-action="toggle-sidebar-pin"]')) return;
  var next = !window._sidebarPinned;
  window._sidebarPinned = next;
  try { localStorage.setItem('sidebarPinned', next ? 'true' : 'false'); } catch (err) {}
  document.documentElement.classList.toggle('sidebar-unpinned', !next);
  var sidebar = document.getElementById('sidebar');
  if (sidebar) {
    sidebar.classList.toggle('w-60', next);
    sidebar.classList.toggle('w-12', !next);
  }
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
      var csrfToken = getCsrfToken();
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

// ── The row whose article is open in the detail pane ──────────────────────────
// Read off the detail rather than set where the click happens. Every way an article
// reaches the pane ends in a swap into #article-detail — a row click, the
// ?open_article_id deep link, the Next button — so one place covers all of them, and
// a row the server re-rendered (the row-poll on a saved article, an out-of-band swap,
// infinite scroll) gets its mark back at the next settle instead of losing it.
//
// Which layouts show it is left to CSS: the class is kept up to date everywhere and
// only the 3-panel layout draws it, so switching layout cannot strand a stale mark.
(function () {
  var ACTIVE = 'article-active';

  function syncActiveRow() {
    var list = document.getElementById('article-list');
    if (!list) return;
    var open = document.querySelector('#article-detail [data-article-id]');
    var row = open
      ? list.querySelector('.article-row[data-article-id="' + open.dataset.articleId + '"]')
      : null;
    list.querySelectorAll('.' + ACTIVE).forEach(function (el) {
      if (el !== row) el.classList.remove(ACTIVE);
    });
    if (row) row.classList.add(ACTIVE);
  }

  document.body.addEventListener('htmx:afterSettle', syncActiveRow);
  document.addEventListener('DOMContentLoaded', syncActiveRow);
})();

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
    var token = getCsrfToken();
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
// Top-level so the auto-open handler below can report a programmatic window.open,
// which never trips the delegated anchor listener.
function recordLinkOpened(articleId) {
  if (!articleId) return;
  fetch('/htmx/articles/' + articleId + '/link-opened', {
    method: 'POST',
    keepalive: true,
    credentials: 'include',
    headers: { 'x-csrftoken': getCsrfToken() },
  });
}

document.body.addEventListener('click', function (e) {
  var link = e.target.closest('a[target="_blank"]');
  if (!link) return;
  var row = link.closest('[data-article-id]');
  if (!row) return;
  recordLinkOpened(row.dataset.articleId);
});

// ── Search modal ───────────────────────────────────────────────────────────
function openSearchModal(prefill) {
  var el = document.getElementById('full-menu-dropdown');
  if (el) el.classList.add('hidden');
  var overlay = document.getElementById('search-modal-overlay');
  if (overlay) overlay.classList.remove('hidden');
  // Only restore the previous query/scope when reopening from the results header
  // (prefill); a fresh search from the menu or the "/" shortcut starts empty.
  window._searchPrefill = !!prefill;
  var url = '/htmx/search-modal';
  if (prefill) {
    var qs = [];
    if (window._lastSearchScope) qs.push('scope=' + encodeURIComponent(window._lastSearchScope));
    if (window._lastSearchSort) qs.push('sort=' + encodeURIComponent(window._lastSearchSort));
    if (window._lastSearchStatus) qs.push('status=' + encodeURIComponent(window._lastSearchStatus));
    if (window._lastSearchLabels) qs.push('labels=' + encodeURIComponent(window._lastSearchLabels));
    if (qs.length) url += '?' + qs.join('&');
  }
  htmx.ajax('GET', url, { target: '#search-modal-content', swap: 'innerHTML' });
}

function closeSearchModal() {
  var overlay = document.getElementById('search-modal-overlay');
  if (overlay) overlay.classList.add('hidden');
}

function submitSearch() {
  var input = document.getElementById('search-input');
  if (!input) return;
  var q = input.value.trim();
  // Multi-select scope: hidden input holds a JSON array like ["feed:1","folder:2"].
  var scopeEl = document.getElementById('search-scope-value');
  var scopeVal = scopeEl ? scopeEl.value.trim() : '';
  // Sort: relevance (default) | newest | oldest.
  var sortEl = document.getElementById('search-sort');
  var sortVal = sortEl ? sortEl.value : 'relevance';
  // Status: all (default, no filter) | unread | read.
  var statusEl = document.getElementById('search-status');
  var statusVal = statusEl ? statusEl.value : 'all';
  // Labels: JSON array like ["any"] or ["label:3"]. Empty = no label filter.
  var labelsEl = document.getElementById('search-labels-value');
  var labelsVal = labelsEl ? labelsEl.value.trim() : '';

  // Empty text is allowed as a pure filter view, but only when at least one
  // filter is set — otherwise it's just "all articles", so nudge for input.
  var hasFilter = !!scopeVal || !!labelsVal || (statusVal && statusVal !== 'all');
  if (!q && !hasFilter) { input.focus(); return; }

  window._lastSearchQuery = q;
  window._lastSearchScope = scopeVal;
  window._lastSearchSort = sortVal;
  window._lastSearchStatus = statusVal;
  window._lastSearchLabels = labelsVal;

  var params = new URLSearchParams();
  if (q) params.set('q', q);
  if (scopeVal) params.set('scope_include', scopeVal);
  params.set('sort', sortVal);
  if (statusVal && statusVal !== 'all') params.set('read_status', statusVal);
  if (labelsVal) params.set('label_filter', labelsVal);
  htmx.ajax('GET', '/htmx/articles?' + params.toString(), { target: '#article-list', swap: 'innerHTML' });
  closeSearchModal();
  // On mobile the search modal is opened from inside the sidebar overlay; close
  // it so the results are immediately visible instead of hidden behind it.
  if (window._closeMobileSidebarOverlay) window._closeMobileSidebarOverlay();
}

// Focus search input when modal loads
document.body.addEventListener('htmx:afterSettle', function (evt) {
  if (evt.detail.target.id === 'search-modal-content') {
    var input = document.getElementById('search-input');
    if (input) {
      // Pre-fill with the previous query only when reopening from the results
      // header, so "search again" is one keystroke away.
      if (window._searchPrefill && window._lastSearchQuery) input.value = window._lastSearchQuery;
      input.focus();
      input.select();
    }
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

  var csrf = getCsrfToken();
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
  if (action === 'open-search') { openSearchModal(false); return; }
  if (action === 'open-search-again') { openSearchModal(true); return; }
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
    var csrf = getCsrfToken();
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

// Starring queues a summary that runs in the background. If the article is open,
// pull in the polling spinner so the reader can see one is being generated.
document.addEventListener('summaryStarted', function (e) {
  var id = e.detail && e.detail.id;
  var block = id ? document.getElementById('ai-summary-' + id) : null;
  if (!block) return;
  htmx.ajax('GET', '/htmx/articles/' + id + '/ai-summary/poll', { target: block, swap: 'outerHTML' });
});

// Optimistic star toggle: fire articleStarChanged immediately on click, revert on error.
// One path for list rows ([data-star-btn]), the detail bottom bar ([data-bottom-star])
// and the detail header menu ([data-header-star]).
var _STAR_SELECTOR = '[data-star-btn], [data-bottom-star], [data-header-star]';

function _starArticleId(btn) {
  var row = btn.closest('.article-row');
  if (row) return row.dataset.articleId ? parseInt(row.dataset.articleId, 10) : NaN;
  var bar = btn.closest('.article-bottom-bar');
  if (bar) return bar.dataset.articleId ? parseInt(bar.dataset.articleId, 10) : NaN;
  // header-menu star: the id lives on the visible detail element
  var el = document.querySelector('#article-detail [data-article-id], #inline-article-detail-content [data-article-id]');
  return el ? parseInt(el.dataset.articleId, 10) : NaN;
}

function _starIsStarred(btn) {
  var svg = btn.querySelector('svg');           // detail header star
  if (svg) return svg.getAttribute('fill') === 'currentColor';
  var span = btn.querySelector('span');         // list rows + bottom bar use a ★ glyph
  return !!(span && span.textContent.trim() === '\u2605');
}

document.addEventListener('click', function (e) {
  var btn = e.target.closest(_STAR_SELECTOR);
  if (!btn) return;
  var articleId = _starArticleId(btn);
  if (isNaN(articleId)) return;
  var wasStarred = _starIsStarred(btn);
  btn._optimisticStarred = wasStarred;
  document.dispatchEvent(new CustomEvent('articleStarChanged', {
    detail: { id: articleId, isStarred: !wasStarred }
  }));
}, true);

function _revertOptimisticStar(elt) {
  var btn = elt && elt.closest ? elt.closest(_STAR_SELECTOR) : null;
  if (!btn || typeof btn._optimisticStarred === 'undefined') return;
  var articleId = _starArticleId(btn);
  if (isNaN(articleId)) return;
  document.dispatchEvent(new CustomEvent('articleStarChanged', {
    detail: { id: articleId, isStarred: btn._optimisticStarred }
  }));
  delete btn._optimisticStarred;
}

document.body.addEventListener('htmx:sendError', function (e) { _revertOptimisticStar(e.detail.elt); });
document.body.addEventListener('htmx:responseError', function (e) { _revertOptimisticStar(e.detail.elt); });
document.body.addEventListener('htmx:afterRequest', function (e) {
  var btn = e.detail.elt && e.detail.elt.closest ? e.detail.elt.closest(_STAR_SELECTOR) : null;
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

  // sameTabFallback defaults to true: when the user clicked a link, navigating this
  // tab is the honest outcome of a blocked popup. Pass false for opens the user did
  // not ask for — throwing the reader out of the app on a plain row click would lose
  // the list position and scroll. Returns whether a tab actually opened.
  function openExternal(url, sameTabFallback) {
    // Do not pass a features string — window.open with 'noopener' intentionally returns null
    // even on success, making the blocked-popup check unreliable. Modern browsers apply
    // noopener by default for cross-origin _blank. Fall back to same-tab only when truly blocked.
    var w = window.open(url, '_blank');
    if (!w && sameTabFallback !== false) window.location.href = url;
    return !!w;
  }

  function currentDetailArticleId() {
    var el = document.querySelector('#article-detail [data-article-id]');
    return el ? el.dataset.articleId : null;
  }

  // Articles with nothing to show: open the source straight from the click, while the
  // user gesture is still live. Doing it after the HTMX response would be a bare
  // window.open and the popup blocker would eat it.
  document.addEventListener('click', function (e) {
    if (document.documentElement.dataset.openOriginalEmpty !== '1') return;
    var row = e.target.closest('.article-row');
    if (!row || !row.dataset.noBody || !row.dataset.url) return;
    // Star and label buttons, excluded from the row's own hx-trigger the same way.
    if (e.target.closest('[data-stop-propagation]')) return;
    // An expanded row is handled by the collapse path and by the title handler above,
    // either of which would otherwise produce a second tab.
    if (_shouldUseInline() && row.classList.contains('inline-expanded')) return;
    // 3-panel has no expanded class, so without this a repeat click on the article
    // already in the detail pane opens another tab.
    if (currentDetailArticleId() === row.dataset.articleId) return;
    if (!openExternal(row.dataset.url, false)) return;
    // The delegated tracker already covers a real anchor click; avoid a duplicate POST.
    if (!e.target.closest('a[target="_blank"]')) recordLinkOpened(row.dataset.articleId);
    if (window._trackExternalVisit) window._trackExternalVisit(row.dataset.articleId);
    // No preventDefault: HTMX still loads the detail behind the new tab.
  }, true);

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

    // Scroll the row into view, clearing anything pinned above it: the mobile top
    // panel sits outside the list, and a sticky list header (the Saved URL box, the
    // search-results strip) sits inside it and stays put while the list scrolls, so
    // a row aligned to the list's top would slide underneath it.
    setTimeout(function () {
      var topOffset = listStickyOffset();
      if (topOffset > 0) {
        var list = document.getElementById('article-list');
        if (list) {
          var scrollTarget = list.scrollTop + row.getBoundingClientRect().top
            - list.getBoundingClientRect().top - topOffset;
          list.scrollTo({ top: Math.max(0, scrollTarget), behavior: 'smooth' });
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
});

restoreSidebarCollapse(false);

// ── Auto-advance: after marking a feed/folder/label read, open the next one
// with unread (opt-in via the sidebar's data-auto-advance flag). ────────────
function _autoAdvanceParam(hx) {
  if (/[?&]feed_id=/.test(hx)) return 'feed_id';
  if (/[?&]folder_id=/.test(hx)) return 'folder_id';
  if (/[?&]label_id=/.test(hx)) return 'label_id';
  return null; // special rows (All / Starred / Archived / Labels header)
}

// Un-collapse a section so the target row is visible and stays open after the
// sidebarRefresh re-render (which restores collapse state from localStorage).
function _expandCollapsible(key) {
  var content = document.getElementById('collapse-' + key);
  var toggle = document.querySelector('.collapse-toggle[data-collapse="' + key + '"]');
  if (content) content.classList.remove('collapsed');
  if (toggle) toggle.classList.remove('is-collapsed');
  try { localStorage.removeItem('sidebar_col_' + key); } catch (err) {}
}

// Returns true if it navigated to a next scope; false if none applied.
function _markReadAutoAdvance(clickedRow) {
  var full = document.getElementById('sidebar-full');
  if (!full || !full.hasAttribute('data-auto-advance') || !clickedRow) return false;
  var clickedA = clickedRow.querySelector('.nav-item[hx-get]');
  if (!clickedA) return false;
  var param = _autoAdvanceParam(clickedA.getAttribute('hx-get') || '');
  if (!param) return false;

  // Same-kind rows in visual order (feeds cross folder boundaries).
  var rows = Array.from(full.querySelectorAll('.mark-read-row')).filter(function (r) {
    var a = r.querySelector('.nav-item[hx-get]');
    return a && new RegExp('[?&]' + param + '=').test(a.getAttribute('hx-get') || '');
  });
  var start = rows.indexOf(clickedRow);
  if (start === -1) return false;

  var nextRow = null;
  for (var i = start + 1; i < rows.length; i++) {
    // Unread badge is the pill variant (.rounded-full); total badge is not.
    if (rows[i].querySelector('.mark-read-badge.rounded-full')) { nextRow = rows[i]; break; }
  }
  if (!nextRow) return false;

  var nextA = nextRow.querySelector('.nav-item[hx-get]');
  var url = nextA.getAttribute('hx-get');

  // Expand any collapsed ancestor section (feed inside a folder, label list)…
  var anc = nextRow.parentElement ? nextRow.parentElement.closest('.collapsible') : null;
  while (anc) {
    if (anc.id && anc.id.indexOf('collapse-') === 0) _expandCollapsible(anc.id.slice(9));
    anc = anc.parentElement ? anc.parentElement.closest('.collapsible') : null;
  }
  // …and, when advancing to a folder, open the folder itself.
  if (param === 'folder_id') {
    var m = url.match(/[?&]folder_id=(\d+)/);
    if (m) _expandCollapsible('folder-' + m[1]);
  }

  _saveNavSnapshot();
  document.querySelectorAll('.nav-item').forEach(function (i) { i.classList.remove('active'); });
  nextA.classList.add('active');
  _activeNavGet = url;
  try { localStorage.setItem('lastNavItem', url); } catch (err) {}
  _syncMobileQuicklink();
  nextRow.scrollIntoView({ block: 'nearest' });
  htmx.ajax('GET', url, { target: '#article-list', swap: 'innerHTML' });
  return true;
}

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
  // Marking a feed/folder read also changes sibling counts (labels, other feeds
  // sharing an article). The endpoint sends HX-Trigger: sidebarRefresh, but that
  // event races the POST inside #sidebar's hx-sync="this:abort" context and gets
  // dropped, leaving those counts stale. Re-trigger once the request has fully
  // settled so the whole sidebar recomputes.
  setTimeout(function () { htmx.trigger(document.body, 'sidebarRefresh'); }, 0);
  // Opt-in: jump to the next unread feed/folder/label instead of just refreshing.
  if (_markReadAutoAdvance(row)) return;
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
    // Collapsible mode swaps rail↔full purely by toggling this class now (both are
    // always rendered and CSS picks one), so no sidebarRefresh/opacity dance is needed.
    document.documentElement.classList.add('mobile-sidebar-open');
    history.pushState({ mobileSidebarOpen: true }, '');
  }

  function closeSidebarOverlay() {
    document.documentElement.classList.remove('mobile-sidebar-open');
  }

  // Exposed so global handlers (e.g. search submit) can dismiss the mobile
  // sidebar overlay that the search modal was opened from.
  window._closeMobileSidebarOverlay = function () {
    if (!isMobile()) return;
    if (document.documentElement.classList.contains('mobile-sidebar-open')) {
      closeSidebarOverlay();
      history.back();
    }
  };

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

  // The article's url/title live on the <article> element, not on the wrapper div
  // that also carries data-article-id. Matching on the tag keeps a bare
  // '[data-article-id]' query from picking up the wrapper and losing the source URL.
  function currentDetailArticleEl() {
    return (document.querySelector('#article-detail article[data-article-id]') ||
            document.querySelector('#inline-article-detail-content article[data-article-id]'));
  }

  document.addEventListener('click', function (e) {
    if (!e.target.closest('#detail-share-pick-original')) return;
    document.getElementById('detail-share-picker').classList.add('hidden');
    var articleEl = currentDetailArticleEl();
    if (!articleEl) return;
    doShare(articleEl.dataset.title || '', articleEl.dataset.url || window.location.href);
  });

  document.addEventListener('click', function (e) {
    if (!e.target.closest('#detail-share-pick-readfine')) return;
    document.getElementById('detail-share-picker').classList.add('hidden');
    var articleEl = currentDetailArticleEl();
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

  // Copy article: title + source + body as rich HTML with a plain-text fallback.
  // Writes both text/html and text/plain to the clipboard so rich editors (Docs,
  // Word, email) paste formatting + images, while plain fields get clean text.
  function _htmlEscape(s) {
    return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // Rewrite relative img/href URLs to absolute (against the article's own URL) so
  // images and links still resolve once pasted outside the app.
  function _absolutizeUrls(el, base) {
    if (!base) return;
    el.querySelectorAll('img[src]').forEach(function (img) {
      var raw = img.getAttribute('src');
      try { img.setAttribute('src', new URL(raw, base).href); } catch (e) {}
    });
    el.querySelectorAll('a[href]').forEach(function (a) {
      var raw = a.getAttribute('href');
      try { a.setAttribute('href', new URL(raw, base).href); } catch (e) {}
    });
  }

  // Mirror the stylesheet's table handling in the copied markup: layout tables
  // (no <th> — e.g. Reddit's image+meta wrappers) are rendered as stacked blocks
  // via CSS, so unwrap their cells into <div>s here too; genuine data tables
  // (with <th>) are kept intact.
  function _unwrapLayoutTables(root) {
    root.querySelectorAll('table').forEach(function (table) {
      if (!table.parentNode || table.querySelector('th')) return;
      var frag = document.createDocumentFragment();
      table.querySelectorAll('td').forEach(function (td) {
        var div = document.createElement('div');
        while (td.firstChild) div.appendChild(td.firstChild);
        frag.appendChild(div);
      });
      table.replaceWith(frag);
    });
  }

  function _copyPlainFallback(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text)
        .then(function () { _showShareToast('Article copied'); })
        .catch(function () { if (_execCopy(text)) _showShareToast('Article copied'); });
    } else if (_execCopy(text)) {
      _showShareToast('Article copied');
    }
  }

  function _copyArticle(html, text) {
    if (window.ClipboardItem && navigator.clipboard && navigator.clipboard.write) {
      try {
        var item = new ClipboardItem({
          'text/html': new Blob([html], { type: 'text/html' }),
          'text/plain': new Blob([text], { type: 'text/plain' })
        });
        navigator.clipboard.write([item])
          .then(function () { _showShareToast('Article copied'); })
          .catch(function () { _copyPlainFallback(text); });
        return;
      } catch (e) { /* fall through to plain */ }
    }
    _copyPlainFallback(text);
  }

  document.addEventListener('click', function (e) {
    var trigger = e.target.closest('[data-copy-article]');
    if (!trigger) return;
    var root = trigger.closest('article[data-article-id]');
    if (!root) return;
    var body = root.querySelector('.article-body');
    if (!body) return;

    var title = root.dataset.title || '';
    var url = root.dataset.url || '';
    var feed = root.dataset.feedTitle || '';

    var clone = body.cloneNode(true);
    _unwrapLayoutTables(clone);
    _absolutizeUrls(clone, url);

    var header = '';
    if (title) {
      header += url ? '<h1><a href="' + _htmlEscape(url) + '">' + _htmlEscape(title) + '</a></h1>'
                    : '<h1>' + _htmlEscape(title) + '</h1>';
    }
    var srcBits = [];
    if (feed) srcBits.push(_htmlEscape(feed));
    if (url) srcBits.push('<a href="' + _htmlEscape(url) + '">' + _htmlEscape(url) + '</a>');
    if (srcBits.length) header += '<p>' + srcBits.join(' &middot; ') + '</p>';
    var html = header + clone.innerHTML;

    var textLines = [];
    if (title) textLines.push(title);
    var srcLine = [feed, url].filter(Boolean).join(' · ');
    if (srcLine) textLines.push(srcLine);
    textLines.push('');
    textLines.push(body.innerText || body.textContent || '');
    _copyArticle(html, textLines.join('\n'));
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
    var detailArticle = currentDetailArticleEl();
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
    ['sidebar-chat-btn', 'mobile-bottom-chat-btn'].forEach(function (id) {
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


// ── Admin feeds: remember A–Z / By-host grouping (per device, localStorage) ──
// The toggle sits next to the page heading (outside the swapped table partial),
// so its active state is managed here rather than re-rendered on swap.
(function () {
  var ACTIVE = ['bg-blue-50', 'border-blue-300', 'text-blue-700'];
  var INACTIVE = ['border-gray-200', 'text-gray-600', 'hover:border-gray-300'];
  function readMode() {
    try { return localStorage.getItem('admin_feeds_group'); } catch (e) { return null; }
  }
  function setToggleActive(mode) {
    document.querySelectorAll('[data-feeds-group]').forEach(function (b) {
      var on = b.dataset.feedsGroup === mode;
      ACTIVE.forEach(function (c) { b.classList.toggle(c, on); });
      INACTIVE.forEach(function (c) { b.classList.toggle(c, !on); });
    });
  }
  // Persist the choice + reflect it on the toggle when clicked (htmx does the swap).
  document.addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest('[data-feeds-group]');
    if (!btn) return;
    var mode = btn.dataset.feedsGroup;
    try { localStorage.setItem('admin_feeds_group', mode); } catch (err) {}
    setToggleActive(mode);
  });
  // Apply the remembered grouping on load (the server renders A–Z by default
  // because it can't read localStorage on a full page navigation).
  function applyRemembered() {
    var el = document.getElementById('feeds-table');
    if (!el || !window.htmx) return;
    if (readMode() === 'host' && el.dataset.groupMode !== 'host') {
      setToggleActive('host');
      window.htmx.ajax('GET', '/admin/feeds/table?group=host',
                       { target: '#feeds-table', swap: 'outerHTML' });
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', applyRemembered);
  } else {
    applyRemembered();
  }
})();
