// Scope selector — initialises all .scope-selector elements on the page.
// Replaces the inline <script> that was previously embedded in scope_selector.html.
(function () {
  function initScopeSelector(container) {
    var sid = container.dataset.selectorId;
    if (!sid) return;

    var hiddenInput = document.getElementById(sid + '-value');
    var allCb = document.getElementById(sid + '-all');
    if (!hiddenInput || !allCb) return;

    function getSelected() {
      return Array.from(document.querySelectorAll(
        'input.scope-item-cb[data-selector="' + sid + '"]:checked'
      )).map(function (cb) { return cb.value; });
    }

    function updateHidden() {
      var selected = getSelected();
      hiddenInput.value = selected.length ? JSON.stringify(selected) : '';
    }

    function setItemsDimmed(dimmed) {
      document.querySelectorAll('input.scope-item-cb[data-selector="' + sid + '"]')
        .forEach(function (cb) { cb.style.opacity = dimmed ? '0.35' : ''; });
    }

    function syncAllCb() {
      var items = document.querySelectorAll('input.scope-item-cb[data-selector="' + sid + '"]');
      var anyChecked = Array.from(items).some(function (cb) { return cb.checked; });
      allCb.checked = !anyChecked;
      setItemsDimmed(!anyChecked);
    }

    // Init from current hidden value
    var currentValue = hiddenInput.value;
    if (currentValue) {
      try {
        var initial = JSON.parse(currentValue);
        initial.forEach(function (val) {
          var cb = document.querySelector(
            'input.scope-item-cb[data-selector="' + sid + '"][value="' + val + '"]'
          );
          if (cb) cb.checked = true;
        });
      } catch (e) {}
      allCb.checked = false;
      setItemsDimmed(false);
    } else {
      allCb.checked = true;
      setItemsDimmed(true);
    }

    allCb.addEventListener('change', function () {
      if (allCb.checked) {
        document.querySelectorAll('input.scope-item-cb[data-selector="' + sid + '"]')
          .forEach(function (cb) { cb.checked = false; });
        hiddenInput.value = '';
        setItemsDimmed(true);
      } else {
        setItemsDimmed(false);
      }
      hiddenInput.dispatchEvent(new Event('change', { bubbles: true }));
    });

    document.querySelectorAll('input.scope-item-cb[data-selector="' + sid + '"]')
      .forEach(function (cb) {
        cb.addEventListener('change', function () {
          if (cb.checked) allCb.checked = false;
          updateHidden();
          syncAllCb();
          hiddenInput.dispatchEvent(new Event('change', { bubbles: true }));
        });
      });
  }

  document.querySelectorAll('.scope-selector').forEach(initScopeSelector);
})();
