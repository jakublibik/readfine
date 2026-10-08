// Catch me up page: the configuration form, saved configurations and the briefing
// modal. Loaded once per full page load (the page is never boosted), so the document
// listeners below are added once.
(function () {

  var PERIOD_DESCS = JSON.parse(document.getElementById('period-descs-data').textContent);

  function updatePeriodDesc() {
    var sel = document.querySelector('input[name="period"]:checked');
    var el = document.getElementById('period-desc');
    if (el) el.textContent = sel ? (PERIOD_DESCS[sel.value] || '') : '';
  }

  // ── Settings panel toggle ────────────────────────────────────────────────
  var settingsToggle = document.getElementById('settings-toggle');
  var settingsPanel = document.getElementById('settings-panel');
  var settingsArrow = document.getElementById('settings-toggle-arrow');
  if (settingsToggle) {
    settingsToggle.addEventListener('click', function () {
      var open = !settingsPanel.classList.contains('hidden');
      settingsPanel.classList.toggle('hidden', open);
      settingsArrow.classList.toggle('rotate-90', !open);
      if (!open) { triggerEstimateRefresh(); }
    });
  }

  // ── Score filter (optional min relevance score) ───────────────────────────
  // Independent of the label selector. The number input carries its submit name
  // only when the checkbox is on, so an unchecked box sends no filter_score_min.
  function applyScoreFilter() {
    var enabled = document.getElementById('score-filter-enabled');
    var scoreRow = document.getElementById('score-value-row');
    var scoreInput = document.getElementById('score-filter-value');
    var on = !!(enabled && enabled.checked);
    if (scoreRow) scoreRow.classList.toggle('hidden', !on);
    if (scoreInput) {
      if (on) scoreInput.setAttribute('name', 'filter_score_min');
      else scoreInput.removeAttribute('name');
    }
  }

  var scoreEnabled = document.getElementById('score-filter-enabled');
  if (scoreEnabled) {
    scoreEnabled.addEventListener('change', function () {
      applyScoreFilter();
      triggerEstimateRefresh();
    });
  }
  applyScoreFilter();

  // ── Custom prompt toggle ──────────────────────────────────────────────────
  var promptToggle = document.getElementById('prompt-toggle');
  var promptPanel = document.getElementById('prompt-panel');
  var promptArrow = document.getElementById('prompt-toggle-arrow');
  if (promptToggle) {
    promptToggle.addEventListener('click', function () {
      var open = !promptPanel.classList.contains('hidden');
      promptPanel.classList.toggle('hidden', open);
      promptArrow.classList.toggle('rotate-90', !open);
    });
  }

  // ── Count + cost refresh ──────────────────────────────────────────────────
  var estimateTimer = null;

  function buildQueryString() {
    var form = document.getElementById('catchup-form');
    if (!form) return '';
    var fd = new FormData(form);
    var params = new URLSearchParams();
    fd.forEach(function (val, key) {
      if (key !== 'custom_prompt') params.append(key, val);
    });
    return params.toString();
  }

  function triggerEstimateRefresh() {
    clearTimeout(estimateTimer);
    estimateTimer = setTimeout(function () {
      htmx.ajax('GET', '/htmx/catch-me-up/estimate?' + buildQueryString(), {
        target: '#catchup-estimate', swap: 'innerHTML'
      });
    }, 300);
  }

  // Attach listeners to form inputs
  var form = document.getElementById('catchup-form');
  if (form) {
    form.addEventListener('change', function (e) {
      if (e.target.name === 'period') {
        updatePeriodDesc();
      }
      triggerEstimateRefresh();
    });
    document.getElementById('article-limit').addEventListener('input', triggerEstimateRefresh);
    // Initial load
    updatePeriodDesc();
    triggerEstimateRefresh();
  }

  // ── Load saved config into form ───────────────────────────────────────────
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-action="load-catchup-config"], [data-action="generate-catchup-config"]');
    if (!btn) return;
    var raw = btn.getAttribute('data-config');
    if (!raw) return;
    var cfg;
    try { cfg = JSON.parse(raw); } catch (err) { return; }
    var isGenerate = btn.getAttribute('data-action') === 'generate-catchup-config';
    _applyConfig(cfg, !isGenerate);
    if (isGenerate) {
      var form = document.getElementById('catchup-form');
      if (form) {
        form.requestSubmit();
        var resultArea = document.getElementById('catchup-result');
        if (resultArea) resultArea.closest('.relative').scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    }
  });

  function _applyConfig(cfg, expandPanel) {
    var form = document.getElementById('catchup-form');
    if (!form) return;

    // Expand settings panel if collapsed and requested
    if (expandPanel && settingsPanel && settingsPanel.classList.contains('hidden')) {
      settingsPanel.classList.remove('hidden');
      if (settingsArrow) settingsArrow.classList.add('rotate-90');
    }

    // Period
    var periodRadio = form.querySelector('input[name="period"][value="' + cfg.period + '"]');
    if (periodRadio) periodRadio.checked = true;

    // Filter status
    var statusRadio = form.querySelector('input[name="filter_status"][value="' + cfg.filter_status + '"]');
    if (statusRadio) statusRadio.checked = true;

    // Score filter (independent toggle; only present when scoring is available)
    var hasScore = cfg.filter_score_min !== null && cfg.filter_score_min !== undefined;
    var scoreEnabledCb = document.getElementById('score-filter-enabled');
    var scoreInput = document.getElementById('score-filter-value');
    if (scoreInput && hasScore) {
      scoreInput.value = Math.round(cfg.filter_score_min * 100);
    }
    if (scoreEnabledCb) {
      scoreEnabledCb.checked = hasScore;
      applyScoreFilter();
    }

    // Article limit
    var limitInput = document.getElementById('article-limit');
    if (limitInput) limitInput.value = cfg.article_limit;

    // Include snippet
    var snippetCb = form.querySelector('input[name="include_snippet"]');
    if (snippetCb) snippetCb.checked = !!cfg.include_snippet;

    // Custom prompt
    var promptTextarea = document.getElementById('custom-prompt');
    var promptPanel = document.getElementById('prompt-panel');
    var promptArrow = document.getElementById('prompt-toggle-arrow');
    if (promptTextarea) {
      promptTextarea.value = cfg.custom_prompt || '';
      if (cfg.custom_prompt && promptPanel) {
        promptPanel.classList.remove('hidden');
        if (promptArrow) promptArrow.classList.add('rotate-90');
      }
      if (window._refreshInsertDefaultLabels) window._refreshInsertDefaultLabels();
    }

    // Scope — update hidden input and re-init checkboxes
    var scopeHidden = document.getElementById('scope-value');
    if (scopeHidden) {
      scopeHidden.value = cfg.scope_include || '';
      // Re-init checkboxes
      var allCb = document.getElementById('scope-all');
      var items = document.querySelectorAll('input.scope-item-cb[data-selector="scope"]');
      items.forEach(function (cb) { cb.checked = false; cb.style.opacity = ''; });
      if (cfg.scope_include) {
        try {
          var selected = JSON.parse(cfg.scope_include);
          selected.forEach(function (val) {
            var cb = document.querySelector('input.scope-item-cb[data-selector="scope"][value="' + val + '"]');
            if (cb) cb.checked = true;
          });
          if (allCb) allCb.checked = false;
        } catch (e) {}
      } else {
        if (allCb) allCb.checked = true;
        items.forEach(function (cb) { cb.style.opacity = '0.35'; });
      }
    }

    // Labels — update hidden input and re-init checkboxes (mirrors scope)
    var labelHidden = document.getElementById('catchup-label-value');
    if (labelHidden) {
      labelHidden.value = cfg.label_filter || '';
      var labelAny = document.getElementById('catchup-label-any');
      var labelItems = document.querySelectorAll('input.label-item-cb[data-selector="catchup-label"]');
      if (labelAny) labelAny.checked = false;
      labelItems.forEach(function (cb) { cb.checked = false; cb.style.opacity = ''; });
      if (cfg.label_filter) {
        try {
          var selectedLabels = JSON.parse(cfg.label_filter);
          if (selectedLabels.indexOf('any') !== -1) {
            if (labelAny) labelAny.checked = true;
            labelItems.forEach(function (cb) { cb.style.opacity = '0.35'; });
          } else {
            selectedLabels.forEach(function (val) {
              var cb = document.querySelector('input.label-item-cb[data-selector="catchup-label"][value="' + val + '"]');
              if (cb) cb.checked = true;
            });
          }
        } catch (e) {}
      }
    }

    // Fill name input in save form
    var nameInput = document.querySelector('#save-config-form [name=name]');
    if (nameInput) nameInput.value = cfg.name;

    updatePeriodDesc();
    triggerEstimateRefresh();
  }

  // ── Copy button ──────────────────────────────────────────────────────────
  var copyBtn = document.getElementById('catchup-copy-btn');
  var copyIcon = document.getElementById('catchup-copy-icon');
  var resultDiv = document.getElementById('catchup-result');

  var _copyIconDefault = copyIcon ? copyIcon.innerHTML : '';
  var _copyIconCheck = '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/>';

  // Show copy button after HTMX swaps result
  document.body.addEventListener('htmx:afterSwap', function (e) {
    if (e.detail.target && e.detail.target.id === 'catchup-result') {
      var hasContent = resultDiv.querySelector('.prose') !== null;
      if (copyBtn) copyBtn.classList.toggle('hidden', !hasContent);
    }
  });

  if (copyBtn) {
    copyBtn.addEventListener('click', function () {
      var text = resultDiv.innerText.trim();
      if (!text) return;
      navigator.clipboard.writeText(text).then(function () {
        copyIcon.innerHTML = _copyIconCheck;
        copyBtn.classList.add('text-green-500');
        setTimeout(function () {
          copyIcon.innerHTML = _copyIconDefault;
          copyBtn.classList.remove('text-green-500');
        }, 2000);
      });
    });
  }

  // Update config via PUT when editing a saved config
  // (re-use save form — set hidden config_id before submit if editing)
  // Currently loading config fills the main form; saving creates a new config.
  // Full PUT editing can be added later if needed.

})();

(function () {
  function openBriefingModal(configId, configName) {
    var overlay = document.getElementById('briefing-modal-overlay');
    var title = document.getElementById('briefing-modal-title');
    var content = document.getElementById('briefing-modal-content');
    if (title) title.textContent = 'Briefing - ' + configName;
    // Show a loading shell so the modal isn't empty during load (and to clear any
    // stale content from a previous open) — visible on slower connections.
    if (content) {
      content.innerHTML = '<div class="py-6 flex items-center justify-center gap-2 text-sm text-gray-400">' +
        '<svg class="animate-spin h-4 w-4 flex-shrink-0" fill="none" viewBox="0 0 24 24">' +
        '<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>' +
        '<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4l3-3-3-3v4a8 8 0 00-8 8z"></path>' +
        '</svg>Loading…</div>';
    }
    if (overlay) overlay.classList.remove('hidden');
    htmx.ajax('GET', '/htmx/catchup-configs/' + configId + '/briefing', {
      target: '#briefing-modal-content',
      swap: 'innerHTML'
    });
  }
  function closeBriefingModal() {
    var overlay = document.getElementById('briefing-modal-overlay');
    if (overlay) overlay.classList.add('hidden');
  }
  window.openBriefingModal = openBriefingModal;
  window.closeBriefingModal = closeBriefingModal;

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeBriefingModal();
  });
  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-action="close-briefing-modal"]')) {
      closeBriefingModal();
      return;
    }
    var opener = e.target.closest('[data-action="open-briefing-modal"]');
    if (opener) {
      openBriefingModal(opener.dataset.configId, opener.dataset.configName);
    }
  });

  // Event delegation for briefing modal interactive fields (loaded via HTMX)
  document.addEventListener('change', function (e) {
    var el = e.target;
    if (el.dataset.action === 'toggle-briefing-fields') {
      var fields = document.getElementById('briefing-fields');
      if (fields) fields.classList.toggle('hidden', !el.checked);
    } else if (el.dataset.action === 'toggle-briefing-day') {
      var dayRow = document.getElementById('briefing-day-row');
      if (dayRow) dayRow.classList.toggle('hidden', el.value !== 'weekly');
    }
  });
})();

// Request hooks for the save-config form, the generate form and the test briefing
// button. These used to be hx-on attributes; they live here so htmx can run with
// allowEval off. Matched on the element that issued the request, not on bubbling, so a
// request from anything nested inside a form does not count as the form's own.
(function () {
  function scrollToResult() {
    var result = document.getElementById('catchup-result');
    if (result) result.closest('.relative').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function setSpinner(visible) {
    var spinner = document.getElementById('catchup-spinner');
    if (spinner) spinner.classList.toggle('hidden', !visible);
  }

  document.body.addEventListener('htmx:beforeRequest', function (e) {
    var elt = e.detail.elt;
    if (!elt) return;
    if (elt.id === 'save-config-form') {
      var name = elt.querySelector('[name=name]');
      if (name && !name.value.trim()) {
        showToast('Enter a configuration name.', 'warning');
        e.preventDefault();
      }
    } else if (elt.id === 'catchup-form') {
      setSpinner(true);
      scrollToResult();
    }
  });

  document.body.addEventListener('htmx:afterRequest', function (e) {
    var elt = e.detail.elt;
    if (!elt) return;
    if (elt.id === 'save-config-form') {
      var name = elt.querySelector('[name=name]');
      if (e.detail.successful && name) name.value = '';
    } else if (elt.id === 'catchup-form') {
      setSpinner(false);
    }
  });

  // A sent test says so next to the button for a few seconds.
  document.body.addEventListener('htmx:afterSwap', function (e) {
    var target = e.detail.target;
    if (!target || target.id !== 'briefing-test-result') return;
    var ok = target.querySelector('[data-briefing-test-ok]');
    if (ok) setTimeout(function () { if (ok.parentNode) ok.remove(); }, 5000);
  });

  // Said in place next to the button, so the generic toast in app.js stays out. On
  // responseError rather than afterRequest: htmx hands each event its own detail
  // object, and the fallback checks the one it was given.
  document.body.addEventListener('htmx:responseError', function (e) {
    var elt = e.detail.elt;
    if (!elt || !elt.hasAttribute('data-briefing-test') || e.detail.xhr.status !== 429) return;
    e.detail._rfHandled = true;
    var result = document.getElementById('briefing-test-result');
    if (!result) return;
    var p = document.createElement('p');
    p.className = 'text-yellow-600 text-sm';
    p.textContent = 'Wait a moment before sending another test.';
    result.replaceChildren(p);
    setTimeout(function () { if (p.parentNode) p.remove(); }, 5000);
  });
})();
