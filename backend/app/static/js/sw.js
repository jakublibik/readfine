// Service worker. It exists for one reason: Chrome fires beforeinstallprompt only for
// a page whose worker has a fetch handler, and that event is what the Install button in
// Settings → Preferences hangs on. (Installing from the browser's own menu has not
// needed a worker since Chrome 108 on mobile / 112 on desktop.)
//
// It caches nothing, deliberately. A worker outlives logout and account switches on a
// shared browser, so anything it stored would sit outside the Cache-Control: no-store
// rule that keeps one account's rendered pages from reaching the next person at this
// browser (CWE-525). Adding a cache here means solving invalidation on logout first.

self.addEventListener('install', function () {
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  // Claim open pages right away. Without this the very first visit stays uncontrolled
  // until the next navigation, and the install prompt would only appear after a reload.
  event.waitUntil(self.clients.claim());
});

// Empty on purpose. A pass-through (`event.respondWith(fetch(event.request))`) looks
// equivalent but is worse on both counts: it defeats the browser's ability to skip a
// no-op fetch handler entirely, and routing every request through the worker can break
// range requests, which is how the video players in article bodies fetch.
self.addEventListener('fetch', function () {});
