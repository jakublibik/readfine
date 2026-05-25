document.addEventListener('DOMContentLoaded', function () {
  var tabFeeds = document.getElementById('tab-feeds');
  var tabStats = document.getElementById('tab-stats');
  if (!tabFeeds || !tabStats) return;

  var activeClasses = ['bg-white', 'shadow-sm', 'text-gray-900'];
  var inactiveClasses = ['text-gray-500'];

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
