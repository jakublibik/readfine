// Whether a story is unfolded on screen is decided here and nowhere else.
//
// The server closes a whole story when the reader marks a folded row read, and leaves
// the story alone when the row is one of an unfolded group. It cannot tell which it is
// looking at: folding is a thing the browser did. So every human mark-as-read carries
// the answer, and if the browser gets it wrong the reader loses the coverage they just
// asked to see, silently and with nothing in the list to say so.
//
// Both halves have gone wrong once already, which is why they are tested: a member's
// own id has nothing hanging under it, and the set-read requests all carry a query
// string that an end-anchored pattern would not match.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { browser, list, row, sendHtmx } from './harness.mjs';

test('a row with no group under it is folded', () => {
  const w = browser(list(row(1), row(2)));
  assert.equal(w._storyUnfolded(1), false);
});

test('the row a group hangs from counts as unfolded', () => {
  const w = browser(list(row(1), row(2, { parent: 1 }), row(3, { parent: 1 })));
  assert.equal(w._storyUnfolded(1), true);
});

test('a member of an unfolded group counts as unfolded too', () => {
  // The one that got away: reading a member used to look like reading a folded row,
  // and closed the very group the reader had just opened.
  const w = browser(list(row(1), row(2, { parent: 1 }), row(3, { parent: 1 })));
  assert.equal(w._storyUnfolded(2), true);
  assert.equal(w._storyUnfolded(3), true);
});

test('a row of another group is not dragged in', () => {
  const w = browser(list(row(1), row(2, { parent: 1 }), row(9), row(10, { parent: 9 })));
  assert.equal(w._storyUnfolded(9), true);
  assert.equal(w._storyUnfolded(3), false);
});

test('the read button says so when the story is unfolded', () => {
  const w = browser(list(row(1), row(2, { parent: 1 })));
  assert.equal(
    sendHtmx(w, 'post', '/htmx/articles/1/read').story_unfolded, 'true',
  );
});

test('set-read says so despite its query string', () => {
  // Every set-read call in app.js carries ?state=true. An end-anchored pattern matched
  // none of them, so the flag never left the browser and the auto mark-as-read closed
  // groups that were open on screen.
  const w = browser(list(row(1), row(2, { parent: 1 })));
  assert.equal(
    sendHtmx(w, 'post', '/htmx/articles/1/set-read?state=true').story_unfolded, 'true',
  );
});

test('a folded row sends nothing', () => {
  const w = browser(list(row(1), row(2)));
  assert.equal(sendHtmx(w, 'post', '/htmx/articles/1/read').story_unfolded, undefined);
  assert.equal(
    sendHtmx(w, 'post', '/htmx/articles/1/set-read?state=true').story_unfolded,
    undefined,
  );
});

test('another article id in the path is not mistaken for this one', () => {
  const w = browser(list(row(1), row(2, { parent: 1 }), row(7)));
  assert.equal(sendHtmx(w, 'post', '/htmx/articles/7/read').story_unfolded, undefined);
});

test('a request that is not a read is left alone', () => {
  const w = browser(list(row(1), row(2, { parent: 1 })));
  assert.equal(sendHtmx(w, 'post', '/htmx/articles/1/star').story_unfolded, undefined);
  assert.equal(
    sendHtmx(w, 'get', '/htmx/articles/1/story-rows?density=').story_unfolded, undefined,
  );
});

test('the scroll batch carries the unfolded rows with the ids', async () => {
  const w = browser(list(row(1), row(2, { parent: 1 }), row(5)));
  let sent = null;
  w.fetch = function (url, opts) {
    sent = { url: url, body: JSON.parse(opts.body) };
    return Promise.resolve({ ok: true });
  };
  w._queueMarkRead(2);
  w._queueMarkRead(5);
  w._flushMarkRead();

  assert.equal(sent.url, '/htmx/articles/set-read-batch');
  assert.deepEqual(sent.body.ids.sort(), [2, 5]);
  // 2 is a member of a group that is open, so it closes nothing; 5 is an ordinary row.
  assert.deepEqual(sent.body.unfolded, [2]);
});

