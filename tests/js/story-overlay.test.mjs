// Opening another source's article from the story footer.
//
// Where the reader is in the list, the article has to get a window of its own: swapping
// the inline shell left the row above it describing one article and the content below it
// another. Where they already have a surface to themselves it must not, or every article
// looked at would cost a press of Back on the way out.
//
// The panel is also emptied on the way out, and that is the half with teeth:
// #article-detail is the first place currentDetailArticleEl and the share lookups look,
// ahead of the inline shell, so an article left behind in a hidden panel would answer
// for the one the reader is actually on.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { browser, captureAjax, detail, list, memberLink, row } from './harness.mjs';

// `layout` is 2 or 3, `bucket` is what the viewport bucketed to. Their defaults are the
// case this feature was built for: two panels, reading inline.
function reader(w, { layout = '2', bucket = 'medium' } = {}) {
  w.document.documentElement.dataset.layout = layout;
  w.document.documentElement.dataset.bucket = bucket;
  return w;
}

function open(w, id) {
  w.document.querySelector('[data-open-story-member="' + id + '"]')
    .dispatchEvent(new w.MouseEvent('click', { bubbles: true, cancelable: true }));
}

function raised(w) {
  return w.document.documentElement.classList.contains('story-detail-open');
}

test('reading inline, a member is raised over the list', () => {
  const w = reader(browser(list(row(1)) + detail() + memberLink(7)));
  const calls = captureAjax(w);
  open(w, 7);
  assert.equal(raised(w), true);
  assert.deepEqual(calls, [{ verb: 'GET', path: '/htmx/articles/7', target: '#article-detail' }]);
});

test('the three panel layout has the panel open already', () => {
  const w = reader(browser(list(row(1)) + detail(1) + memberLink(7)), { layout: '3', bucket: 'large' });
  const calls = captureAjax(w);
  open(w, 7);
  assert.equal(raised(w), false);
  assert.equal(calls[0].target, '#article-detail');
});

test('a phone reading fullscreen is on a surface of its own', () => {
  const w = reader(browser(list(row(1)) + detail(1) + memberLink(7)), { bucket: 'small' });
  w.localStorage.setItem('detail_mode_small', 'fullscreen');
  w.document.documentElement.classList.add('mobile-detail-open');
  open(w, 7);
  assert.equal(raised(w), false);
});

test('a member of the member costs one press of Back, not two', () => {
  const w = reader(browser(list(row(1)) + detail() + memberLink(7) + memberLink(8)));
  const before = w.history.length;
  open(w, 7);
  open(w, 8);
  assert.equal(raised(w), true);
  assert.equal(w.history.length, before + 1);
});

test('Back lowers the window and empties the panel', () => {
  const w = reader(browser(list(row(1)) + detail() + memberLink(7)));
  open(w, 7);
  w.document.getElementById('article-detail').innerHTML = detail(7).replace(/<\/?main[^>]*>/g, '');
  w.dispatchEvent(new w.PopStateEvent('popstate', { state: null }));
  assert.equal(raised(w), false);
  assert.equal(w.document.getElementById('article-detail').innerHTML, '');
});

test('the back button in the bar lowers it too', () => {
  const w = reader(browser(
    list(row(1)) + '<main id="article-detail"><button id="mobile-detail-back-btn"></button></main>'
    + memberLink(7),
  ));
  open(w, 7);
  w.document.getElementById('mobile-detail-back-btn')
    .dispatchEvent(new w.MouseEvent('click', { bubbles: true, cancelable: true }));
  assert.equal(raised(w), false);
});

test('Escape lowers it as well', () => {
  const w = reader(browser(list(row(1)) + detail() + memberLink(7)));
  open(w, 7);
  w.document.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(raised(w), false);
});

test('the request is not taken for a row expansion', () => {
  // What it cost to find out: an inline layout turns every request aimed at the panel
  // into a row expansion, so this one was cancelled and the window came up on the
  // panel's "Select an article to read".
  const w = reader(browser(list(row(1)) + detail() + memberLink(7)));
  const link = w.document.querySelector('[data-open-story-member="7"]');
  const request = new w.CustomEvent('htmx:beforeRequest', {
    detail: { target: w.document.getElementById('article-detail'), elt: link },
    bubbles: true, cancelable: true,
  });
  w.document.body.dispatchEvent(request);
  assert.equal(request.defaultPrevented, false);
});

test('a row of the list still expands inline', () => {
  const w = reader(browser(list(row(1)) + detail()));
  const request = new w.CustomEvent('htmx:beforeRequest', {
    detail: { target: w.document.getElementById('article-detail'),
              elt: w.document.getElementById('article-row-1') },
    bubbles: true, cancelable: true,
  });
  w.document.body.dispatchEvent(request);
  assert.equal(request.defaultPrevented, true);
});

// The ··· menu of a detail. Its star is updated by an event carrying an article id,
// and both containers have one, so the id has to decide which.
function menu(id) {
  return (
    '<div id="article-detail-root" data-article-id="' + id + '">'
    + '<button data-header-star><svg fill="none"></svg><span data-label>Star</span></button>'
    + '</div>'
  );
}

test('the star in the menu follows the article it belongs to', () => {
  const w = reader(browser(
    '<div id="article-list"><div id="inline-article-detail-content">' + menu(3)
    + '</div></div><main id="article-detail">' + menu(7) + '</main>',
  ));
  w.document.dispatchEvent(new w.CustomEvent('articleStarChanged', {
    detail: { id: 7, isStarred: true },
  }));
  const [shell, panel] = [...w.document.querySelectorAll('[data-header-star] svg')];
  assert.equal(shell.getAttribute('fill'), 'none');
  assert.equal(panel.getAttribute('fill'), 'currentColor');
});

test('Escape with nothing raised is left to the search modal', () => {
  const w = reader(browser(list(row(1)) + detail()));
  assert.equal(w._closeStoryOverlay(), false);
});
