// Failure handling for the share target page.
//
// app.js has htmx error handlers, but each one is scoped to a part of the reading view
// (the article list, the chat panel), so a failed save here was reported by nobody.
// htmx does not swap a 4xx or 5xx response, which is what keeps the rate limiter's
// error page out of the card, and it also means the page is left exactly as it was:
// with auto-save that is the word "Saving…" and a hidden form, so the share sat there
// unfinished with nothing to press. Reveal the form and say what happened.
(function () {
  var body = document.getElementById('share-target-body');
  if (!body) return;

  function recover(status) {
    var saving = document.getElementById('share-target-saving');
    if (saving) saving.remove();
    var form = body.querySelector('form');
    if (form) form.classList.remove('hidden');
    // 429 is the one worth naming: it says to wait rather than to try again, and it is
    // reachable by sharing a handful of links in a row rather than by anything broken.
    showToast(status === 429
      ? 'Too many saves in a row. Wait a minute, then press Save.'
      : 'The save did not go through. Press Save to try again.', 'error');
  }

  document.body.addEventListener('htmx:responseError', function (e) {
    recover(e.detail && e.detail.xhr ? e.detail.xhr.status : 0);
  });
  // No response at all: offline, or the connection dropped mid-share.
  document.body.addEventListener('htmx:sendError', function () { recover(0); });
})();
