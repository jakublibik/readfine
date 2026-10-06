// Admin → Feeds: the feed edit modal and the learned rate limits modal.
(function () {
  var feedOverlay = document.getElementById('feed-edit-overlay');
  var limitsOverlay = document.getElementById('rate-limits-overlay');

  function openFeedEdit() {
    feedOverlay.classList.remove('hidden');
    document.body.classList.add('overflow-hidden');
  }
  function closeFeedEdit() {
    feedOverlay.classList.add('hidden');
    document.body.classList.remove('overflow-hidden');
  }

  if (feedOverlay) {
    document.addEventListener('click', function (e) {
      if (e.target.closest('[data-action="open-feed-edit"]')) {
        openFeedEdit();
      } else if (e.target.closest('[data-action="close-feed-edit"]')) {
        closeFeedEdit();
      }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeFeedEdit();
    });
    document.body.addEventListener('feedEditDone', closeFeedEdit);
  }

  if (limitsOverlay) {
    document.addEventListener('click', function (e) {
      if (e.target.closest('[data-action="open-rate-limits"]')) {
        limitsOverlay.classList.remove('hidden');
      } else if (e.target.closest('[data-action="close-rate-limits"]')) {
        limitsOverlay.classList.add('hidden');
      }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') limitsOverlay.classList.add('hidden');
    });
  }
})();
