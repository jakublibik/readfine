(function () {
  var cfgEl = document.getElementById('filter-edit-cfg');
  if (!cfgEl) return;
  var cfg = JSON.parse(cfgEl.textContent);
  var FIELDS = cfg.fields;
  var OPERATORS = cfg.operators;
  var ACTION_TYPES = cfg.actionTypes;
  var LABELS = cfg.labels;

  document.getElementById('add-condition').addEventListener('click', function () {
    var row = document.createElement('div');
    row.className = 'flex items-center gap-2 condition-row';
    row.innerHTML =
      '<select name="cond_field" class="border border-gray-300 rounded px-2 py-1 text-sm">' +
        FIELDS.map(function (f) { return '<option value="' + f + '">' + f.replace(/_/g, ' ') + '</option>'; }).join('') +
      '</select>' +
      '<select name="cond_operator" class="border border-gray-300 rounded px-2 py-1 text-sm">' +
        OPERATORS.map(function (o) { return '<option value="' + o + '">' + o + '</option>'; }).join('') +
      '</select>' +
      '<input type="text" name="cond_value" required class="flex-1 border border-gray-300 rounded px-2 py-1 text-sm" placeholder="value">' +
      '<input type="hidden" name="cond_position" value="0">' +
      '<button type="button" class="text-red-400 hover:text-red-600 text-sm remove-row">✕</button>';
    row.querySelector('.remove-row').addEventListener('click', function () { row.remove(); });
    document.getElementById('conditions').appendChild(row);
  });

  document.getElementById('add-action').addEventListener('click', function () {
    var labelOptions = LABELS.map(function (l) { return '<option value="' + l.id + '">' + l.name + '</option>'; }).join('');
    var row = document.createElement('div');
    row.className = 'flex items-center gap-2 action-row';
    row.innerHTML =
      '<select name="action_type" class="border border-gray-300 rounded px-2 py-1 text-sm action-type-select">' +
        ACTION_TYPES.map(function (t) { return '<option value="' + t + '">' + t + '</option>'; }).join('') +
      '</select>' +
      '<select name="action_value" class="border border-gray-300 rounded px-2 py-1 text-sm hidden label-select">' +
        '<option value="">-- select label --</option>' + labelOptions +
      '</select>' +
      '<button type="button" class="text-red-400 hover:text-red-600 text-sm remove-row">✕</button>';
    row.querySelector('.remove-row').addEventListener('click', function () { row.remove(); });
    var typeSelect = row.querySelector('.action-type-select');
    typeSelect.addEventListener('change', function () { toggleActionValue(typeSelect); });
    document.getElementById('actions').appendChild(row);
    toggleActionValue(typeSelect);
  });

  // Attach remove handlers to pre-rendered rows (from server)
  document.querySelectorAll('.condition-row .remove-row, .action-row .remove-row').forEach(function (btn) {
    btn.addEventListener('click', function () { btn.closest('.condition-row, .action-row').remove(); });
  });

  window.toggleActionValue = function (select) {
    var labelSelect = select.closest('.action-row').querySelector('.label-select');
    if (select.value === 'label') {
      labelSelect.classList.remove('hidden');
    } else {
      labelSelect.classList.add('hidden');
      labelSelect.value = '';
    }
  };

  // Action-type selects for pre-rendered rows
  document.querySelectorAll('.action-type-select').forEach(function (sel) {
    sel.addEventListener('change', function () { window.toggleActionValue(sel); });
    window.toggleActionValue(sel);
  });

  // ── scope_include: "All feeds" mutual exclusivity ────────────────────────────
  var scopeAll = document.getElementById('scope-all');
  if (scopeAll) {
    var scopeItems = document.querySelectorAll('#scope-include-list input[name="scope_include"]');

    function setScopeAllMode(allChecked) {
      scopeItems.forEach(function (cb) {
        cb.disabled = allChecked;
        if (allChecked) cb.checked = false;
      });
    }

    // Initial state is already set by server-rendered HTML (disabled attr).
    // Wire up interactions:
    scopeAll.addEventListener('change', function () {
      if (this.checked) {
        setScopeAllMode(true);
      } else {
        // User unchecked "All" — enable items but don't select any
        scopeItems.forEach(function (cb) { cb.disabled = false; });
      }
    });

    scopeItems.forEach(function (cb) {
      cb.addEventListener('change', function () {
        if (this.checked) {
          // Deselect "All feeds" when any specific item is picked
          scopeAll.checked = false;
          scopeItems.forEach(function (c) { c.disabled = false; });
        } else {
          // If nothing else is checked, revert to "All feeds"
          var anyChecked = Array.from(scopeItems).some(function (c) { return c.checked; });
          if (!anyChecked) {
            scopeAll.checked = true;
            setScopeAllMode(true);
          }
        }
      });
    });
  }

  // ── scope_except toggle ───────────────────────────────────────────────────
  var exceptToggle = document.getElementById('except-toggle');
  if (exceptToggle) {
    exceptToggle.addEventListener('change', function () {
      var panel = document.getElementById('except-panel');
      panel.classList.toggle('hidden', !this.checked);
      if (!this.checked) {
        panel.querySelectorAll('input[name="scope_except"]').forEach(function (cb) {
          cb.checked = false;
        });
      }
    });
  }
})();
