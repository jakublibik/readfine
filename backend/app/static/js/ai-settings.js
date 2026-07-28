// Show loading text in a target element before an HTMX request fires
document.addEventListener('htmx:beforeRequest', function (e) {
  var btn = e.detail.elt;
  var targetId = btn.dataset && btn.dataset.loadingTarget;
  if (!targetId) return;
  var el = document.getElementById(targetId);
  if (el) el.innerHTML = '<span class="text-gray-400 text-sm">' + (btn.dataset.loadingText || 'Loading…') + '</span>';
});

// Scoring checkbox → enable/disable everything that only applies to scoring
// (score in list, interest profile, generate, auto-update, revert), the moment
// it is clicked. Delegated, because saving the form swaps the page in via
// hx-boost and any handler bound to the old elements would be gone.
document.addEventListener('change', function (e) {
  if (!e.target || e.target.id !== 'ai_scoring_enabled_default') return;
  var dependent = document.getElementById('scoring-dependent');
  if (!dependent) return;
  var off = !e.target.checked;
  dependent.classList.toggle('opacity-50', off);
  dependent.querySelectorAll('input, textarea, select, button').forEach(function (el) {
    el.disabled = off;
  });
});

document.addEventListener('DOMContentLoaded', function () {
  // Provider select → update "Available models" link
  document.querySelectorAll('select[data-slot]').forEach(function (sel) {
    var slot = sel.dataset.slot;
    var link = document.getElementById('models-link-' + slot);
    if (!link) return;
    sel.addEventListener('change', function () {
      var v = sel.value;
      if (v && link.dataset[v]) {
        link.href = link.dataset[v];
        link.classList.remove('hidden');
      } else {
        link.classList.add('hidden');
      }
    });
  });

  // Remove API key button → clear password input before submit
  document.querySelectorAll('button[data-clear-key]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var inp = btn.closest('form').querySelector('[name=api_key]');
      if (inp) inp.value = '';
    });
  });

  // Content limit input — format with space as thousands separator
  var limitInput = document.getElementById('ai_content_limit');
  if (limitInput) {
    function fmtLimit(digits) {
      return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
    }
    limitInput.addEventListener('input', function () {
      var pos = limitInput.selectionStart;
      var digitsBeforeCursor = limitInput.value.slice(0, pos).replace(/[^\d]/g, '').length;
      var digits = limitInput.value.replace(/[^\d]/g, '');
      limitInput.value = fmtLimit(digits);
      var newPos = 0, count = 0;
      for (var i = 0; i < limitInput.value.length; i++) {
        if (limitInput.value[i] !== ' ') count++;
        if (count === digitsBeforeCursor) { newPos = i + 1; break; }
      }
      limitInput.setSelectionRange(newPos, newPos);
    });
    limitInput.closest('form').addEventListener('submit', function () {
      limitInput.value = limitInput.value.replace(/[^\d]/g, '');
    });
  }

  // Preference text character counter. Delegated, because generating or
  // reverting the profile swaps the textarea node out from under us.
  document.addEventListener('input', function (e) {
    if (!e.target || e.target.id !== 'ai_preference_text') return;
    var counter = document.getElementById('pref-char-count');
    if (!counter) return;
    var n = e.target.value.length;
    counter.textContent = n;
    counter.parentElement.classList.toggle('text-red-500', n > 5000);
    counter.parentElement.classList.toggle('text-gray-400', n <= 5000);
  });
});
