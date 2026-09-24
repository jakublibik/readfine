(function () {
  var cfgEl = document.getElementById('filter-edit-cfg');
  if (!cfgEl) return;
  var cfg = JSON.parse(cfgEl.textContent);
  var FIELDS = cfg.fields;
  var OPERATORS = cfg.operators;
  var SCORE_OPERATORS = cfg.scoreOperators;
  var SCORE_SOURCES = cfg.scoreSources;
  var ACTION_TYPES = cfg.actionTypes;
  var LABELS = cfg.labels;

  var FIELD_OPERATORS = {
    'title_or_content': ['contains', 'not_contains', 'regex'],
    'title':            ['contains', 'not_contains', 'equals', 'regex'],
    'content':          ['contains', 'not_contains', 'regex'],
    'author':           ['contains', 'not_contains', 'equals', 'regex'],
    'url':              ['contains', 'not_contains', 'equals', 'regex'],
    'published_at':     ['equals', 'gt', 'lt'],
    'score':            SCORE_OPERATORS,
  };

  // The "score" field reads one of three scorers. Order = default preference.
  var SOURCE_LABELS = { ai: 'AI', basic: 'Basic', relevance: 'AI, else basic' };
  var SOURCE_HINTS = {
    ai: 'Only articles a label filter sent to AI scoring have an AI score.',
    basic: 'Every article has a basic score, including ones the AI scored: ' +
           'basic below 30 → mark read also catches an article the AI gave 90. ' +
           'A threshold of 70 lets through about one article in ten.',
    relevance: 'AI, else basic uses the AI score where an article has one and ' +
               'the basic score otherwise, the same number the article list shows.',
  };
  var SOURCE_OFF_HINTS = {
    ai: 'AI scoring is off, so this condition never matches until you turn it back on.',
    basic: 'Basic relevance is off or has no terms, so this condition never matches. ' +
           'Both are on the Relevance page.',
    relevance: 'Neither scorer is on, so this condition never matches.',
  };

  function getOperatorsForField(field) {
    return FIELD_OPERATORS[field] || OPERATORS;
  }

  function getPlaceholderForField(field) {
    if (field === 'score') return '0–100';
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
    updateSourceUI(row);
    updateScoreFilterUI();
    updateRegexHintUI();
  }

  // Fill the row's source select: the sources this reader has running, plus the
  // saved one if its scorer is off, so an existing condition is shown as it is
  // rather than silently turned into another field on the next save.
  function initSourceSelect(row) {
    var srcSel = row.querySelector('.cond-source-select');
    var hidden = row.querySelector('[name="cond_source"]');
    var saved = hidden.getAttribute('data-saved') || '';
    var sources = SCORE_SOURCES.slice();
    if (saved && sources.indexOf(saved) === -1) sources.push(saved);
    var order = Object.keys(SOURCE_LABELS);
    sources.sort(function (a, b) { return order.indexOf(a) - order.indexOf(b); });
    srcSel.innerHTML = sources.map(function (src) {
      var off = SCORE_SOURCES.indexOf(src) === -1;
      return '<option value="' + src + '"' + (src === saved ? ' selected' : '') + '>' +
        SOURCE_LABELS[src] + (off ? ' (off)' : '') + '</option>';
    }).join('');
    if (!saved && SCORE_SOURCES.length) srcSel.value = SCORE_SOURCES[0];
    srcSel.addEventListener('change', function () { updateSourceUI(row); updateScoreFilterUI(); });
  }

  function updateSourceUI(row) {
    var fieldSel = row.querySelector('[name="cond_field"]');
    var srcSel = row.querySelector('.cond-source-select');
    var hidden = row.querySelector('[name="cond_source"]');
    var hint = row.querySelector('.cond-hint');
    var isScore = fieldSel.value === 'score';
    srcSel.classList.toggle('hidden', !isScore);
    hidden.value = isScore ? srcSel.value : '';
    if (!hint) return;
    if (isScore && srcSel.value) {
      var off = SCORE_SOURCES.indexOf(srcSel.value) === -1;
      hint.textContent = off ? SOURCE_OFF_HINTS[srcSel.value] : SOURCE_HINTS[srcSel.value];
      hint.className = 'cond-hint basis-full text-xs ' + (off ? 'text-amber-700' : 'text-gray-500');
    } else {
      hint.className = 'cond-hint hidden basis-full text-xs text-gray-500';
    }
  }

  // When the filter runs follows the latest number it reads (filter_service.filter_phase).
  function currentPhase() {
    var sources = {};
    document.querySelectorAll('.condition-row').forEach(function (r) {
      var fs = r.querySelector('[name="cond_field"]');
      var src = r.querySelector('[name="cond_source"]');
      if (fs && fs.value === 'score' && src && src.value) sources[src.value] = true;
    });
    if (sources.ai) return 'ai';
    if (sources.relevance) return 'relevance';
    if (sources.basic) return 'fetch';
    return null;
  }

  function updateScoreFilterUI() {
    var phase = currentPhase();
    var badge = document.getElementById('filter-type-badge');
    var notice = document.getElementById('score-filter-notice');
    if (badge) {
      if (phase) {
        badge.textContent = 'Score filter';
        badge.className = 'text-xs px-2 py-0.5 rounded font-medium bg-purple-100 text-purple-700';
      } else {
        // Without a scorer there is only one kind of filter, so nothing to label.
        badge.textContent = 'Regular filter';
        badge.className = 'text-xs px-2 py-0.5 rounded font-medium bg-gray-100 text-gray-500' +
          (SCORE_SOURCES.length ? '' : ' hidden');
      }
    }
    if (notice) {
      notice.classList.toggle('hidden', !phase);
      notice.querySelectorAll('[data-phase]').forEach(function (p) {
        p.classList.toggle('hidden', p.getAttribute('data-phase') !== phase);
      });
    }
  }

  function updateRegexHintUI() {
    var hint = document.getElementById('regex-hint');
    if (!hint) return;
    var hasRegex = false;
    document.querySelectorAll('.condition-row [name="cond_operator"]').forEach(function (op) {
      if (op.value === 'regex') hasRegex = true;
    });
    hint.classList.toggle('hidden', !hasRegex);
  }

  function wireOperatorHint(row) {
    var op = row.querySelector('[name="cond_operator"]');
    if (op) op.addEventListener('change', updateRegexHintUI);
  }

  document.getElementById('add-condition').addEventListener('click', function () {
    var row = document.createElement('div');
    row.className = 'flex flex-wrap items-center gap-2 condition-row';
    row.innerHTML =
      '<div class="flex items-center gap-2 shrink-0">' +
        '<select name="cond_field" class="border border-gray-300 rounded px-2 py-1.5 text-sm cond-field-select">' +
          FIELDS.map(function (f) { return '<option value="' + f + '">' + f.replace(/_/g, ' ') + '</option>'; }).join('') +
        '</select>' +
        '<select class="border border-gray-300 rounded px-2 py-1.5 text-sm cond-source-select hidden" aria-label="Score source"></select>' +
        '<input type="hidden" name="cond_source" value="" data-saved="">' +
        '<select name="cond_operator" class="border border-gray-300 rounded px-2 py-1.5 text-sm">' +
          OPERATORS.map(function (o) { return '<option value="' + o + '">' + o + '</option>'; }).join('') +
        '</select>' +
        '<input type="hidden" name="cond_position" value="0">' +
      '</div>' +
      '<input type="text" name="cond_value" required class="flex-1 min-w-32 border border-gray-300 rounded px-2 py-1.5 text-sm" placeholder="value">' +
      '<button type="button" class="text-red-400 hover:text-red-600 text-sm remove-row shrink-0">✕</button>' +
      '<p class="cond-hint hidden basis-full text-xs text-gray-500"></p>';
    row.querySelector('.remove-row').addEventListener('click', function () { row.remove(); updateScoreFilterUI(); updateRegexHintUI(); });
    initSourceSelect(row);
    var fieldSel = row.querySelector('[name="cond_field"]');
    fieldSel.addEventListener('change', function () { updateConditionRow(row); });
    wireOperatorHint(row);
    document.getElementById('conditions').appendChild(row);
    // Filter the operator list to the default field's allowed operators
    // (also refreshes the AI-filter and regex-hint UI).
    updateConditionRow(row);
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
    btn.addEventListener('click', function () { btn.closest('.condition-row').remove(); updateScoreFilterUI(); updateRegexHintUI(); });
  });
  document.querySelectorAll('.action-row .remove-row').forEach(function (btn) {
    btn.addEventListener('click', function () { btn.closest('.action-row').remove(); });
  });

  // Wire change handlers + initial state for pre-rendered condition rows
  document.querySelectorAll('.condition-row').forEach(function (row) {
    var fieldSel = row.querySelector('[name="cond_field"]');
    if (fieldSel) {
      initSourceSelect(row);
      fieldSel.addEventListener('change', function () { updateConditionRow(row); });
      updateConditionRow(row);
    }
    wireOperatorHint(row);
  });
  updateScoreFilterUI();
  updateRegexHintUI();

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
