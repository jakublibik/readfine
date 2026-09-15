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
  w.eval(fs.readFileSync(APP_JS, 'utf8'));
  return w;
}

// One list row, as article_row.html draws it. `parent` is the id of the row a story
// member was unfolded from, and is what marks this row as part of a group.
export function row(id, { parent = null } = {}) {
  return (
    '<div id="article-row-' + id + '" class="article-row" data-article-id="' + id + '"'
    + (parent === null ? '' : ' data-story-parent="' + parent + '"')
    + '></div>'
  );
}

export function list(...rows) {
  return '<div id="article-list">' + rows.join('') + '</div>';
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
