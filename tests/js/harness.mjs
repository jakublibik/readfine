// A browser to run app.js in.
//
// app.js is a plain script, not a module: it declares its functions at the top level
// and wires its listeners as it loads. Evaluated inside a jsdom window that makes it
// exactly what the page gets, and every top-level name becomes a property of that
// window, so a test can call the real function rather than a copy of it.
//
// htmx is stubbed rather than loaded. The tests here are about what app.js decides,
// not about what htmx does with it, and a stub is also the only way to see the request
// htmx would have sent: sendHtmx below fires the configRequest event the same way and
// hands back the parameters app.js put on it.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const APP_JS = path.join(ROOT, 'backend', 'app', 'static', 'js', 'app.js');

export function browser(bodyHtml = '') {
  const dom = new JSDOM(
    '<!doctype html><html><body>' + bodyHtml + '</body></html>',
    // pretendToBeVisual gives requestAnimationFrame, which app.js uses as it loads.
    { runScripts: 'outside-only', pretendToBeVisual: true, url: 'https://readfine.test/' },
  );
  const w = dom.window;
  w.htmx = {
    onLoad() {}, process() {}, trigger() {}, config: {},
    ajax() { return Promise.resolve(); },
  };
  w.fetch = function () { return Promise.resolve({ ok: true }); };
  // jsdom lays nothing out, so it ships neither of these; app.js calls them on paths a
  // test can reach, some of them from a timer where the throw lands outside the test.
  w.Element.prototype.scrollIntoView = function () {};
  w.Element.prototype.scrollTo = function () {};
  w.eval(fs.readFileSync(APP_JS, 'utf8'));
  return w;
}

// One list row, as article_row.html draws it. `parent` is the id of the row a story
// member was unfolded from, and is what marks this row as part of a group.
export function row(id, { parent = null, scope = null, toggle = false } = {}) {
  return (
    '<div id="article-row-' + id + '" class="article-row" data-article-id="' + id + '"'
    + (parent === null ? '' : ' data-story-parent="' + parent + '"')
    // The list's filters, which article_row.html puts on the row so that unfolding
    // asks for the members this list folded rather than the whole group.
    + (scope === null ? '' : ' data-story-scope="' + scope + '"')
    + '>'
    + (toggle ? '<button data-story-toggle="' + id + '"></button>' : '')
    + '</div>'
  );
}

export function list(...rows) {
  return '<div id="article-list">' + rows.join('') + '</div>';
}

// The right panel, which every layout has in the DOM even where CSS hides it. `article`
// is the id of the article sitting in it, or null for the empty panel of a layout that
// reads inline.
export function detail(article = null) {
  return (
    '<main id="article-detail">'
    + (article === null ? '' : '<div id="article-detail-root" data-article-id="' + article
       + '"><article class="reading-area" data-article-id="' + article + '"></article></div>')
    + '</main>'
  );
}

// A title in the story footer, as story_members.html draws it.
export function memberLink(id) {
  return '<a href="#" data-open-story-member="' + id + '">covered elsewhere</a>';
}

// Records what app.js asked htmx to load, instead of loading it.
export function captureAjax(w) {
  const calls = [];
  w.htmx.ajax = function (verb, path, opts) {
    calls.push({ verb: verb, path: path, target: opts && opts.target });
    return Promise.resolve();
  };
  return calls;
}

// The request htmx would have made: fire configRequest the way htmx does and report
// what app.js added to it.
export function sendHtmx(w, verb, pathWithQuery, elt = null) {
  // `elt` is the element htmx issued the request from. Every listener on this event
  // reads it, so it has to be there even for the ones that do not care which it is.
  const detail = {
    verb: verb, path: pathWithQuery, parameters: {}, headers: {},
    elt: elt || w.document.body,
  };
  w.document.body.dispatchEvent(
    new w.CustomEvent('htmx:configRequest', { detail: detail, bubbles: true }),
  );
  return detail.parameters;
}
