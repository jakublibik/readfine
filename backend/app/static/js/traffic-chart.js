// Admin → Traffic: the daily chart's readout line, and the collapsed caveats box.
//
// The bars carry their own text in data-readout, rendered by the template, so this
// only moves a string into one element. Nothing here computes a number, and the
// figure still reads without the script: the line starts on the most recent bar and
// every bar keeps its title attribute.
(function () {
  var bars = document.getElementById('traffic-bars');
  var readout = document.getElementById('traffic-readout');

  if (bars && readout) {
    var columns = Array.prototype.slice.call(bars.querySelectorAll('button'));
    // Two states, because a pointer moving across the chart is asking a different
    // question from a click. Hovering is a look: it writes the line and takes it
    // back on the way out. Clicking (or tapping, where there is no hover at all)
    // picks a bar and keeps it, so the line returns to that one rather than to
    // whichever bar the pointer happened to leave by.
    var PIN = ['ring-1', 'ring-inset', 'ring-blue-300'];
    var fallback = columns[columns.length - 1];
    var pinned = null;

    var show = function (column) {
      // textContent, not innerHTML: the line is built from stored counts and dates,
      // but this is the habit that keeps it from ever mattering.
      if (column) readout.textContent = column.getAttribute('data-readout') || '';
    };

    var pin = function (column) {
      if (pinned) pinned.classList.remove.apply(pinned.classList, PIN);
      pinned = column;
      column.classList.add.apply(column.classList, PIN);
      show(column);
    };

    bars.addEventListener('pointerleave', function () { show(pinned || fallback); });

    columns.forEach(function (column, index) {
      column.addEventListener('pointerenter', function () { show(column); });
      // A tap has no hover, so the click is what carries a touch screen. Focus
      // covers the keyboard and follows the arrow keys below.
      column.addEventListener('click', function () { pin(column); });
      column.addEventListener('focus', function () { pin(column); });

      // Roving tabindex: the chart is one stop in the tab order and the arrows walk
      // it, instead of a year's worth of bars each being its own stop.
      column.addEventListener('keydown', function (event) {
        var step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
        if (!step) return;
        var next = columns[index + step];
        if (!next) return;
        event.preventDefault();
        column.setAttribute('tabindex', '-1');
        next.setAttribute('tabindex', '0');
        next.focus();
      });
    });
  }

  // The "why" links point at this box, which is closed by default. Browsers only
  // open a closed <details> by themselves when the fragment names something inside
  // it, and not all of them do even then, so open it here.
  var box = document.getElementById('how-to-read');
  if (box) {
    if (location.hash === '#how-to-read') box.open = true;
    document.querySelectorAll('a[href="#how-to-read"]').forEach(function (link) {
      link.addEventListener('click', function () { box.open = true; });
    });
  }
})();
