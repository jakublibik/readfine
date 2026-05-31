(function () {
  var cfgEl = document.getElementById('filter-edit-cfg');
  if (!cfgEl) return;
  var cfg = JSON.parse(cfgEl.textContent);
  var FIELDS = cfg.fields;
  var OPERATORS = cfg.operators;
  var AI_SCORE_OPERATORS = cfg.aiScoreOperators;
  var AI_SCORE_AVAILABLE = cfg.aiScoreAvailable;
  var ACTION_TYPES = cfg.actionTypes;
  var LABELS = cfg.labels;

  var FIELD_OPERATORS = {
    'title_or_content': ['contains', 'not_contains', 'regex'],
    'title':            ['contains', 'not_contains', 'equals', 'regex'],
    'content':          ['contains', 'not_contains', 'regex'],
    'author':           ['contains', 'not_contains', 'equals', 'regex'],
    'url':              ['contains', 'not_contains', 'equals', 'regex'],
    'published_at':     ['equals', 'gt', 'lt'],
    'ai_score':         AI_SCORE_OPERATORS,
  };

  function getOperatorsForField(field) {
    return FIELD_OPERATORS[field] || OPERATORS;
  }

  function getPlaceholderForField(field) {
    if (field === 'ai_score') return '0–100';
    if (field === 'published_at') return 'YYYY-MM-DD';
    return 'value';
  }

  function updateConditionRow(row) {
    var fieldSel = row.querySelector('[name="cond_field"]');
    var opSel = row.querySelector('[name="cond_operator"]');
    var valInput = row.querySelector('[name="cond_value"]');
    if (!fieldSel || !opSel) return;
    var field = fieldSel.value;
    var ops = getOperatorsForField(field);
    var currentOp = opSel.value;
    opSel.innerHTML = ops.map(function (o) {
      return '<option value="' + o + '"' + (o === currentOp ? ' selected' : '') + '>' + o + '</option>';
    }).join('');
    // If current operator not in allowed list, reset to first
    if (ops.indexOf(currentOp) === -1) opSel.value = ops[0];
    if (valInput) valInput.placeholder = getPlaceholderForField(field);
    updateAiFilterUI();
  }

  function updateAiFilterUI() {
    if (!AI_SCORE_AVAILABLE) return;
    var rows = document.querySelectorAll('.condition-row');
    var hasAi = false;
    rows.forEach(function (r) {
      var fs = r.querySelector('[name="cond_field"]');
      if (fs && fs.value === 'ai_score') hasAi = true;
    });
    var badge = document.getElementById('filter-type-badge');
    var notice = document.getElementById('ai-filter-notice');
    if (badge) {
      if (hasAi) {
        badge.textContent = 'Score filter';
        badge.className = 'text-xs px-2 py-0.5 rounded font-medium bg-purple-100 text-purple-700';
        badge.classList.remove('hidden');
      } else {
        badge.textContent = 'Regular filter';
        badge.className = 'text-xs px-2 py-0.5 rounded font-medium bg-gray-100 text-gray-500';
        badge.classList.remove('hidden');
      }
    }
    if (notice) notice.classList.toggle('hidden', !hasAi);
  }

  document.getElementById('add-condition').addEventListener('click', function () {
    var row = document.createElement('div');
    row.className = 'flex flex-wrap items-center gap-2 condition-row';
    row.innerHTML =
      '<div class="flex items-center gap-2 shrink-0">' +
        '<select name="cond_field" class="border border-gray-300 rounded px-2 py-1.5 text-sm cond-field-select">' +
          FIELDS.map(function (f) { return '<option value="' + f + '">' + f.replace(/_/g, ' ') + '</option>'; }).join('') +
        '</select>' +
        '<select name="cond_operator" class="border border-gray-300 rounded px-2 py-1.5 text-sm">' +
          OPERATORS.map(function (o) { return '<option value="' + o + '">' + o + '</option>'; }).join('') +
        '</select>' +
        '<input type="hidden" name="cond_position" value="0">' +
      '</div>' +
      '<input type="text" name="cond_value" required class="flex-1 min-w-32 border border-gray-300 rounded px-2 py-1.5 text-sm" placeholder="value">' +
      '<button type="button" class="text-red-400 hover:text-red-600 text-sm remove-row shrink-0">✕</button>';
    row.querySelector('.remove-row').addEventListener('click', function () { row.remove(); updateAiFilterUI(); });
    var fieldSel = row.querySelector('[name="cond_field"]');
    fieldSel.addEventListener('change', function () { updateConditionRow(row); });
    document.getElementById('conditions').appendChild(row);
    updateAiFilterUI();
  });

  document.getElementById('add-action').addEventListener('click', function () {
    var labelOptions = LABELS.map(function (l) { return '<option value="' + l.id + '">' + l.name + '</option>'; }).join('');
    var row = document.createElement('div');
    row.className = 'flex flex-wrap items-center gap-2 action-row';
    row.innerHTML =
      '<select name="action_type" class="border border-gray-300 rounded px-2 py-1.5 text-sm action-type-select">' +
        ACTION_TYPES.map(function (t) { return '<option value="' + t + '">' + t + '</option>'; }).join('') +
      '</select>' +
      '<select name="action_value" class="border border-gray-300 rounded px-2 py-1.5 text-sm hidden label-select">' +
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
  document.querySelectorAll('.condition-row .remove-row').forEach(function (btn) {
    btn.addEventListener('click', function () { btn.closest('.condition-row').remove(); updateAiFilterUI(); });
  });
  document.querySelectorAll('.action-row .remove-row').forEach(function (btn) {
    btn.addEventListener('click', function () { btn.closest('.action-row').remove(); });
  });

  // Wire change handlers + initial state for pre-rendered condition rows
  document.querySelectorAll('.condition-row').forEach(function (row) {
    var fieldSel = row.querySelector('[name="cond_field"]');
    if (fieldSel) {
      fieldSel.addEventListener('change', function () { updateConditionRow(row); });
      updateConditionRow(row);
    }
  });
  updateAiFilterUI();

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
  // Clicking a specific feed directly overrides "All feeds" (same UX as catch me up
  // scope selector): items are dimmed when "All" is active but remain clickable.
  var scopeAll = document.getElementById('scope-all');
  if (scopeAll) {
    var scopeItems = document.querySelectorAll('#scope-include-list input[name="scope_include"]');

    function setScopeAllMode(allChecked) {
      scopeItems.forEach(function (cb) {
        cb.style.opacity = allChecked ? '0.35' : '';
        if (allChecked) cb.checked = false;
      });
    }

    // Apply initial dimming state from server-rendered checked state.
    setScopeAllMode(scopeAll.checked);

    scopeAll.addEventListener('change', function () {
      setScopeAllMode(this.checked);
    });

    scopeItems.forEach(function (cb) {
      cb.addEventListener('change', function () {
        if (this.checked) {
          // Clicking a feed directly overrides "All feeds"
          scopeAll.checked = false;
          scopeItems.forEach(function (c) { c.style.opacity = ''; });
        } else {
          // Last item unchecked → revert to "All feeds"
          var anyChecked = Array.from(scopeItems).some(function (c) { return c.checked; });
          if (!anyChecked) {
            scopeAll.checked = true;
            setScopeAllMode(true);
          }
        }
      });
    });
  }

  // ── scroll to test result if present ────────────────────────────────────
  var testResult = document.getElementById('test-result');
  if (testResult && testResult.children.length > 0) {
    testResult.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
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
