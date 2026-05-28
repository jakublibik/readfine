document.addEventListener('DOMContentLoaded', function () {
  var tabFeeds = document.getElementById('tab-feeds');
  var tabStats = document.getElementById('tab-stats');
  if (!tabFeeds || !tabStats) return;

  var activeClasses = ['bg-white', 'dark:bg-gray-700', 'shadow-sm', 'text-gray-900', 'dark:text-gray-100'];
  var inactiveClasses = ['text-gray-500', 'dark:text-gray-400', 'hover:bg-white', 'dark:hover:bg-gray-700', 'hover:shadow-sm'];

  tabFeeds.addEventListener('click', function () {
    tabFeeds.classList.add(...activeClasses);
    tabFeeds.classList.remove(...inactiveClasses);
    tabStats.classList.remove(...activeClasses);
    tabStats.classList.add(...inactiveClasses);
  });

  tabStats.addEventListener('click', function () {
    tabStats.classList.add(...activeClasses);
    tabStats.classList.remove(...inactiveClasses);
    tabFeeds.classList.remove(...activeClasses);
    tabFeeds.classList.add(...inactiveClasses);
  });
});
