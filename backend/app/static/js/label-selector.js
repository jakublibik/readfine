// Label filter selector — initialises all .label-selector elements.
// Mirrors scope-selector.js: a catch-all "Any label" checkbox that is mutually
// exclusive with the individual label checkboxes. Empty selection = no filter.
// The block is collapsed by default and auto-expands when a value is present.
(function () {
  function initLabelSelector(container) {
    if (container._labelInited) return;
    container._labelInited = true;
    var sid = container.dataset.selectorId;
    if (!sid) return;

    var hiddenInput = document.getElementById(sid + '-value');
    var anyCb = document.getElementById(sid + '-any');
    var panel = document.getElementById(sid + '-panel');
    var arrow = document.getElementById(sid + '-arrow');
    var summary = document.getElementById(sid + '-summary');
    var toggle = document.getElementById(sid + '-toggle');
    if (!hiddenInput || !anyCb) return;

    function items() {
      return Array.from(document.querySelectorAll(
        'input.label-item-cb[data-selector="' + sid + '"]'
      ));
    }

    function setItemsDimmed(dimmed) {
      items().forEach(function (cb) { cb.style.opacity = dimmed ? '0.35' : ''; });
    }

    function updateSummary() {
      if (!summary) return;
      if (anyCb.checked) { summary.textContent = '· Any'; return; }
      var n = items().filter(function (cb) { return cb.checked; }).length;
      summary.textContent = n ? '· ' + n : '';
    }

    function updateHidden() {
      var selected;
      if (anyCb.checked) {
        selected = ['any'];
      } else {
        selected = items().filter(function (cb) { return cb.checked; })
          .map(function (cb) { return cb.value; });
      }
      hiddenInput.value = selected.length ? JSON.stringify(selected) : '';
      updateSummary();
      hiddenInput.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function expand() {
      if (panel) panel.classList.remove('hidden');
      if (arrow) arrow.classList.add('rotate-90');
    }

    // Init from current hidden value
    var hasValue = false;
    if (hiddenInput.value) {
      try {
        var initial = JSON.parse(hiddenInput.value);
        if (initial.indexOf('any') !== -1) {
          anyCb.checked = true;
          setItemsDimmed(true);
        } else {
          initial.forEach(function (val) {
            var cb = document.querySelector(
              'input.label-item-cb[data-selector="' + sid + '"][value="' + val + '"]'
            );
            if (cb) cb.checked = true;
          });
        }
        hasValue = initial.length > 0;
      } catch (e) {}
    }
    updateSummary();
    if (hasValue) expand();  // auto-open when prefilled

    if (toggle) {
      toggle.addEventListener('click', function () {
        if (!panel) return;
        var open = !panel.classList.contains('hidden');
        panel.classList.toggle('hidden', open);
        if (arrow) arrow.classList.toggle('rotate-90', !open);
      });
    }

    anyCb.addEventListener('change', function () {
      if (anyCb.checked) {
        items().forEach(function (cb) { cb.checked = false; });
        setItemsDimmed(true);
      } else {
        setItemsDimmed(false);
      }
      updateHidden();
    });

    items().forEach(function (cb) {
      cb.addEventListener('change', function () {
        if (cb.checked) { anyCb.checked = false; setItemsDimmed(false); }
        updateHidden();
      });
    });
  }

  function initAll(root) {
    (root || document).querySelectorAll('.label-selector').forEach(initLabelSelector);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { initAll(document); });
  } else {
    initAll(document);
  }

  // Selectors loaded dynamically (e.g. the search modal swapped in via HTMX).
  document.body.addEventListener('htmx:afterSettle', function (e) {
    initAll(e.detail.target);
  });
})();
