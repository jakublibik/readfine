document.addEventListener('DOMContentLoaded', function () {
  var tabFeeds = document.getElementById('tab-feeds');
  var tabStats = document.getElementById('tab-stats');
  if (!tabFeeds || !tabStats) return;

  var activeClasses = ['bg-white', 'dark:bg-gray-700', 'shadow-sm', 'text-gray-900', 'dark:text-gray-100'];
  var inactiveClasses = ['text-gray-500', 'dark:text-gray-400', 'hover:bg-white', 'dark:hover:bg-gray-700', 'hover:shadow-sm'];

  function activate(which) {
    var on = which === 'stats' ? tabStats : tabFeeds;
    var off = which === 'stats' ? tabFeeds : tabStats;
    on.classList.add(...activeClasses);
    on.classList.remove(...inactiveClasses);
    off.classList.remove(...activeClasses);
    off.classList.add(...inactiveClasses);
  }

  tabFeeds.addEventListener('click', function () { activate('feeds'); });
  tabStats.addEventListener('click', function () { activate('stats'); });

  // Creating a folder re-renders the Feeds list into #feeds-list. If the Stats tab was
  // active, sync the highlight back to Feeds so it can't show "Stats" over a feeds list.
  document.body.addEventListener('htmx:afterSwap', function (evt) {
    var cfg = evt.detail.requestConfig;
    if (cfg && cfg.path && cfg.path.indexOf('/settings/folders') !== -1) {
      activate('feeds');
    }
  });
});
