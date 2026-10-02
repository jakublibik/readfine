// Keyboard shortcuts in the reader.
//
// Every shortcut clicks the control the mouse would use, so what is worth pinning down
// is which control that is: the next row after the article that is open (in whichever
// layout it is open), the ··· menu of that article and not of another one in the page,
// and nothing at all while the reader is typing or another window is in front.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { browser, list, row } from './harness.mjs';

function reader(w, { layout = '3', bucket = 'large' } = {}) {
  w.document.documentElement.dataset.layout = layout;
  w.document.documentElement.dataset.bucket = bucket;
  return w;
}

// An article as article_detail.html draws it, with the ··· menu controls the keys use.
function article(id) {
  return (
    '<div id="article-detail-root" data-article-id="' + id + '">'
    + '<div class="article-detail-title-row"><a href="https://example.com/' + id + '" data-external-link>t</a></div>'
    + '<button data-header-star></button><button data-header-read></button><button data-header-archive></button>'
    + '</div>'
  );
}

function panel(id = null) {
  return '<main id="article-detail">' + (id === null ? '' : article(id)) + '</main>';
}

function inline(id) {
  return '<div id="inline-article-detail"><div id="inline-article-detail-content">' + article(id) + '</div></div>';
}

function press(w, key, opts = {}, target = null) {
  const e = new w.KeyboardEvent('keydown', Object.assign({ key: key, bubbles: true, cancelable: true }, opts));
  (target || w.document.body).dispatchEvent(e);
  return e;
}

// What got clicked, as a short name: "row 2", "star 1", "nav /htmx/articles".
function clicks(w) {
  const seen = [];
  w.document.addEventListener('click', function (e) {
    const t = e.target;
    if (t.classList.contains('article-row')) seen.push('row ' + t.dataset.articleId);
    else if (t.closest('[data-article-id]')) {
      const id = t.closest('[data-article-id]').dataset.articleId;
      if (t.hasAttribute('data-header-star')) seen.push('star ' + id);
      if (t.hasAttribute('data-header-read')) seen.push('read ' + id);
      if (t.hasAttribute('data-header-archive')) seen.push('archive ' + id);
    }
    if (t.classList.contains('nav-item')) seen.push('nav ' + t.getAttribute('hx-get'));
    if (t.dataset.action === 'mark-read') seen.push('mark-read');
  }, true);
  return seen;
}

test('j and k move from the article open in the panel', () => {
  const w = reader(browser(list(row(1), row(2), row(3)) + panel(2)));
  const seen = clicks(w);
  press(w, 'j');
  press(w, 'k');
  assert.deepEqual(seen, ['row 3', 'row 1']);
});

test('the TT-RSS keys n and p do the same', () => {
  const w = reader(browser(list(row(1), row(2), row(3)) + panel(2)));
  const seen = clicks(w);
  press(w, 'n');
  press(w, 'p');
  assert.deepEqual(seen, ['row 3', 'row 1']);
});

test('with nothing open, j starts at the top', () => {
  const w = reader(browser(list(row(1), row(2)) + panel()));
  const seen = clicks(w);
  press(w, 'j');
  assert.deepEqual(seen, ['row 1']);
});

test('at the end of the list j stays put', () => {
  const w = reader(browser(list(row(1), row(2)) + panel(2)));
  const seen = clicks(w);
  press(w, 'j');
  assert.deepEqual(seen, []);
});

test('reading inline, the open article is the one in the list, not the panel', () => {
  // The panel keeps whatever it last held in the 2-panel layout; moving from it would
  // jump to wherever that article happens to be.
  const w = reader(browser(list(row(1), row(2), row(3)) + inline(1) + panel(2)), { layout: '2', bucket: 'medium' });
  const seen = clicks(w);
  press(w, 'j');
  press(w, 's');
  assert.deepEqual(seen, ['row 2', 'star 1']);
});

test('after closing the inline article, j carries on from it', () => {
  const w = reader(browser(list(row(1), row(2), row(3)) + panel()), { layout: '2', bucket: 'medium' });
  w.document.getElementById('article-row-2').click();
  const seen = clicks(w);
  press(w, 'j');
  assert.deepEqual(seen, ['row 3']);
});

test('a new list starts the cursor over', () => {
  const w = reader(browser(list(row(1), row(2), row(3)) + panel()), { layout: '2', bucket: 'medium' });
  w.document.getElementById('article-row-2').click();
  w.document.body.dispatchEvent(new w.CustomEvent('htmx:afterSwap', {
    bubbles: true, detail: { target: w.document.getElementById('article-list') },
  }));
  const seen = clicks(w);
  press(w, 'j');
  assert.deepEqual(seen, ['row 1']);
});

test('article keys act on the open article, aliases included', () => {
  const w = reader(browser(list(row(1)) + panel(1)));
  const seen = clicks(w);
  for (const key of ['s', 'f', 'm', 'u', 'e']) press(w, key);
  assert.deepEqual(seen, ['star 1', 'star 1', 'read 1', 'read 1', 'archive 1']);
});

test('a phone reading full-screen has no open article once it went back to the list', () => {
  const w = reader(browser(list(row(1)) + panel(1)), { layout: '3', bucket: 'small' });
  w.localStorage.setItem('detail_mode_small', 'fullscreen');
  const seen = clicks(w);
  press(w, 's');
  assert.deepEqual(seen, []);
});

test('over a story, the keys star that article and leave the list alone', () => {
  const w = reader(browser(list(row(1), row(2)) + inline(1) + panel(7)), { layout: '2', bucket: 'medium' });
  w.document.documentElement.classList.add('story-detail-open');
  const seen = clicks(w);
  press(w, 'j');
  press(w, 's');
  assert.deepEqual(seen, ['star 7']);
});

test('typing in a field is typing', () => {
  const w = reader(browser(list(row(1), row(2)) + panel(1) + '<input id="x"><textarea id="y"></textarea>'));
  const seen = clicks(w);
  press(w, 'j', {}, w.document.getElementById('x'));
  press(w, 's', {}, w.document.getElementById('y'));
  assert.deepEqual(seen, []);
});

test('Ctrl and Alt combinations stay with the browser', () => {
  const w = reader(browser(list(row(1), row(2)) + panel(1)));
  const seen = clicks(w);
  press(w, 'j', { ctrlKey: true });
  press(w, 's', { altKey: true });
  press(w, 'j', { metaKey: true });
  assert.deepEqual(seen, []);
});

test('nothing moves while a window is in front of the list', () => {
  const w = reader(browser(list(row(1), row(2)) + panel(1)
    + '<div id="general-chat-modal"></div>'));
  const seen = clicks(w);
  press(w, 'j');
  assert.deepEqual(seen, []);
});

test('g then a goes to All articles, g then s to Starred', () => {
  const w = reader(browser(list(row(1)) + panel()
    + '<div id="sidebar-full"><a class="nav-item" hx-get="/htmx/articles"></a>'
    + '<a class="nav-item" hx-get="/htmx/articles?starred_only=true"></a></div>'));
  const seen = clicks(w);
  press(w, 'g'); press(w, 'a');
  press(w, 'g'); press(w, 's');
  assert.deepEqual(seen, ['nav /htmx/articles', 'nav /htmx/articles?starred_only=true']);
});

test('g followed by anything else lets that key through', () => {
  const w = reader(browser(list(row(1), row(2)) + panel(1)));
  const seen = clicks(w);
  press(w, 'g'); press(w, 'j');
  assert.deepEqual(seen, ['row 2']);
});

function sidebarRow(active) {
  return '<div class="mark-read-row"><a class="nav-item' + (active ? ' active' : '') + '" hx-get="/htmx/articles?feed_id=1"></a>'
    + '<button data-action="mark-read"></button></div>';
}

test('Shift+A marks the open view read once confirmed', () => {
  const w = reader(browser(list(row(1)) + panel() + sidebarRow(true)));
  let asked = 0;
  w.confirm = function () { asked++; return true; };
  const seen = clicks(w);
  press(w, 'A', { shiftKey: true });
  assert.equal(asked, 1);
  assert.deepEqual(seen, ['mark-read']);
});

test('Shift+A does nothing when the question is turned down', () => {
  const w = reader(browser(list(row(1)) + panel() + sidebarRow(true)));
  w.confirm = function () { return false; };
  const seen = clicks(w);
  press(w, 'A', { shiftKey: true });
  assert.deepEqual(seen, []);
});

test('Shift+A leaves search results alone', () => {
  // The sidebar still highlights the view the search was started from, and marking
  // that read is not what the reader looking at results asked for.
  const w = reader(browser(
    '<div id="article-list"><div data-save-search-area></div>' + row(1) + '</div>'
    + panel() + sidebarRow(true)));
  let asked = 0;
  w.confirm = function () { asked++; return true; };
  const seen = clicks(w);
  press(w, 'A', { shiftKey: true });
  assert.equal(asked, 0);
  assert.deepEqual(seen, []);
});

test('? opens the list and Escape closes it', () => {
  const w = reader(browser(list(row(1)) + panel()
    + '<div id="shortcuts-modal-overlay" class="hidden"></div>'));
  const overlay = w.document.getElementById('shortcuts-modal-overlay');
  press(w, '?', { shiftKey: true });
  assert.equal(overlay.classList.contains('hidden'), false);
  const seen = clicks(w);
  press(w, 'j');
  assert.deepEqual(seen, [], 'the list is in front, so j does nothing');
  press(w, 'Escape');
  assert.equal(overlay.classList.contains('hidden'), true);
});

// The sidebar as sidebar.html draws it: one active entry, and a folder whose feeds sit
// in a section that can be collapsed.
function sidebar({ active = null, collapsed = false } = {}) {
  const item = (url) => '<a class="nav-item' + (url === active ? ' active' : '') + '" hx-get="' + url + '"></a>';
  return '<div id="sidebar-full">'
    + item('/htmx/articles') + item('/htmx/articles?starred_only=true')
    + item('/htmx/articles?folder_id=1')
    + '<div class="collapsible' + (collapsed ? ' collapsed' : '') + '" id="collapse-folder-1">'
    + item('/htmx/articles?feed_id=1') + item('/htmx/articles?feed_id=2') + '</div>'
    + item('/htmx/articles?feed_id=3')
    + '</div>';
}

test('Shift+J and Shift+K walk the sidebar from the open entry', () => {
  const w = reader(browser(list(row(1)) + panel() + sidebar({ active: '/htmx/articles?folder_id=1' })));
  const seen = clicks(w);
  // The highlight follows each click, so K comes back to where J started.
  press(w, 'J', { shiftKey: true });
  press(w, 'K', { shiftKey: true });
  press(w, 'K', { shiftKey: true });
  assert.deepEqual(seen, [
    'nav /htmx/articles?feed_id=1', 'nav /htmx/articles?folder_id=1', 'nav /htmx/articles?starred_only=true',
  ]);
});

test('a collapsed folder is stepped over, not into', () => {
  const w = reader(browser(list(row(1)) + panel()
    + sidebar({ active: '/htmx/articles?folder_id=1', collapsed: true })));
  const seen = clicks(w);
  press(w, 'J', { shiftKey: true });
  assert.deepEqual(seen, ['nav /htmx/articles?feed_id=3']);
});

test('the sidebar walk stops at the end instead of starting over', () => {
  const w = reader(browser(list(row(1)) + panel() + sidebar({ active: '/htmx/articles?feed_id=3' })));
  const seen = clicks(w);
  press(w, 'J', { shiftKey: true });
  assert.deepEqual(seen, []);
});

test('t opens the labels of the open article', () => {
  const w = reader(browser(list(row(1)) + '<main id="article-detail"><div id="article-detail-root" data-article-id="1">'
    + '<button data-label-trigger></button></div></main>'));
  let opened = 0;
  w.document.querySelector('[data-label-trigger]').addEventListener('click', () => opened++);
  press(w, 't');
  press(w, 'l');
  assert.equal(opened, 2);
});

function scrollable(el, { height, client, top }) {
  Object.defineProperty(el, 'scrollHeight', { value: height });
  Object.defineProperty(el, 'clientHeight', { value: client });
  el.scrollTop = top;
  const scrolled = [];
  el.scrollBy = (opts) => scrolled.push(opts.top);
  return scrolled;
}

test('Space pages down the article while there is more of it', () => {
  const w = reader(browser(list(row(1), row(2)) + panel(1)));
  const scrolled = scrollable(w.document.getElementById('article-detail'), { height: 1000, client: 400, top: 0 });
  const seen = clicks(w);
  press(w, ' ');
  assert.deepEqual(seen, []);
  assert.equal(scrolled.length, 1);
  assert.ok(scrolled[0] > 0);
});

test('Space at the end of the article opens the next one', () => {
  const w = reader(browser(list(row(1), row(2)) + panel(1)));
  scrollable(w.document.getElementById('article-detail'), { height: 1000, client: 400, top: 600 });
  const seen = clicks(w);
  press(w, ' ');
  assert.deepEqual(seen, ['row 2']);
});

test('Shift+Space only goes back up', () => {
  const w = reader(browser(list(row(1), row(2)) + panel(2)));
  const scrolled = scrollable(w.document.getElementById('article-detail'), { height: 1000, client: 400, top: 600 });
  const seen = clicks(w);
  press(w, ' ', { shiftKey: true });
  assert.deepEqual(seen, []);
  assert.ok(scrolled[0] < 0);
});

test('Space on a focused button presses the button', () => {
  const w = reader(browser(list(row(1), row(2)) + panel(1) + '<button id="b"></button>'));
  const seen = clicks(w);
  const e = press(w, ' ', {}, w.document.getElementById('b'));
  assert.deepEqual(seen, []);
  assert.equal(e.defaultPrevented, false);
});
