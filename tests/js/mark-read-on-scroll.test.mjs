// Who is watching the article list decides whose articles lose their unread state.
//
// The list says in its own config whether scrolling past a row marks it read, and
// search says no: looking something up is not reading it. Honouring that is not just a
// matter of not starting an observer, though, because the previous list's one is still
// running. The mutation observer that catches rows arriving by infinite scroll hands
// them to it, and it runs as a microtask, so on a swap it has already fed the new list
// to the old observer before htmx settles and app.js gets to decide anything. Dropping
// the reference there left that observer watching a search it had been told to leave
// alone, and the reader lost the state of everything they scrolled past.
//
// So the invariant is about the observers, not the config: after a swap, exactly one
// observer is live, it is the one made for the list now on screen, and where the list
// said not to mark read there is none at all.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { browser, row } from './harness.mjs';

// jsdom ships no IntersectionObserver. This one records what each instance ends up
// watching, which is the thing under test, and a disconnected instance says so rather
// than disappearing, so a test can tell "stopped" from "never started".
function watchObservers(w) {
  const instances = [];
  w.IntersectionObserver = function (callback, options) {
    const inst = {
      callback: callback,
      options: options,
      watching: new Set(),
      live: true,
      observe(el) { inst.watching.add(el); },
      unobserve(el) { inst.watching.delete(el); },
      disconnect() { inst.watching.clear(); inst.live = false; },
    };
    instances.push(inst);
    return inst;
  };
  return instances;
}

function cfg(markReadOnScroll) {
  return (
    '<script type="application/json" id="article-list-cfg">'
    + JSON.stringify({
      markReadOnScroll: markReadOnScroll,
      density: 'comfortable',
      labelDisplay: 'indicator',
      titleBarCount: null,
      titleBarCountType: null,
    })
    + '<\/script>'
  );
}

function listPanel(markReadOnScroll, ...rows) {
  return '<div id="article-list">' + rows.join('') + cfg(markReadOnScroll) + '</div>';
}

// htmx's innerHTML swap, and the gap that follows it: settle is a timeout away, so the
// mutation observer's microtask has long since run by the time app.js is called.
async function swapList(w, markReadOnScroll, ...rows) {
  w.document.getElementById('article-list').innerHTML = rows.join('') + cfg(markReadOnScroll);
  await new Promise(function (resolve) { setTimeout(resolve, 0); });
}

function settle(w) {
  const target = w.document.getElementById('article-list');
  target.dispatchEvent(
    new w.CustomEvent('htmx:afterSettle', { detail: { target: target }, bubbles: true }),
  );
}

function rowsOf(w) {
  return Array.from(w.document.querySelectorAll('#article-list .article-row'));
}

function liveWatchers(observers) {
  return observers.filter(function (o) { return o.live; });
}

test('a list that says not to mark read is left unwatched', async () => {
  const w = browser(listPanel(true, row(1), row(2)));
  const observers = watchObservers(w);
  settle(w);
  assert.equal(liveWatchers(observers).length, 1);

  await swapList(w, false, row(10), row(11));
  settle(w);

  const watched = liveWatchers(observers).flatMap(function (o) { return [...o.watching]; });
  assert.deepEqual(rowsOf(w).filter(function (r) { return watched.includes(r); }), []);
});

test('an ordinary swap is watched, by one observer and not two', async () => {
  const w = browser(listPanel(true, row(1), row(2)));
  const observers = watchObservers(w);
  settle(w);

  await swapList(w, true, row(10), row(11));
  settle(w);

  const live = liveWatchers(observers);
  assert.equal(live.length, 1);
  assert.deepEqual(rowsOf(w).filter(function (r) { return !live[0].watching.has(r); }), []);
});

test('rows arriving after the swap are watched as well', async () => {
  // Infinite scroll appends into the settled list. That is what the mutation observer
  // is for, and it is the reason the swap case is delicate, so both halves are held
  // here: what it appends has to be watched, and it has to be watched by the observer
  // belonging to the list it was appended to.
  const w = browser(listPanel(true, row(1)));
  const observers = watchObservers(w);
  settle(w);

  w.document.getElementById('article-list').insertAdjacentHTML('beforeend', row(2));
  await new Promise(function (resolve) { setTimeout(resolve, 0); });

  const live = liveWatchers(observers);
  assert.equal(live.length, 1);
  assert.deepEqual(rowsOf(w).filter(function (r) { return !live[0].watching.has(r); }), []);
});
