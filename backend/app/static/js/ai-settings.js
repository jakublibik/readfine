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

// Auto-summarize checkbox → the minimum length below it only governs those runs,
// so it dims and locks with the checkbox. Same delegation reason as above.
document.addEventListener('change', function (e) {
  if (!e.target || e.target.id !== 'ai_summary_enabled_default') return;
  var dependent = document.getElementById('auto-summary-dependent');
  if (!dependent) return;
  var off = !e.target.checked;
  dependent.classList.toggle('opacity-50', off);
  dependent.querySelectorAll('input, textarea, select, button').forEach(function (el) {
    el.disabled = off;
  });
});

// The endpoint field only means anything while a slot is on the custom provider.
// Asks every slot rather than the one that changed, so switching one away leaves
// the field up while the other still needs it.
function syncCustomEndpointRow() {
  var row = document.getElementById('custom-endpoint-row');
  if (!row) return;
  var anyCustom = Array.prototype.some.call(
    document.querySelectorAll('select[data-slot]'),
    function (s) { return s.value === 'custom'; }
  );
  row.classList.toggle('hidden', !anyCustom);
}

// Provider select → show or hide the endpoint field and update the docs link.
// Custom points at our own help page rather than a provider's model list, so it
// also changes the wording and stops opening in a new tab.
//
// Delegated for the same reason as the scoring checkbox above: a save swaps the
// whole page in through hx-boost, and handlers bound to the elements that were
// replaced would be gone — which is exactly the state a rejected save leaves the
// form in, right when the endpoint field has to appear again.
document.addEventListener('change', function (e) {
  var sel = e.target;
  if (!sel || !sel.matches || !sel.matches('select[data-slot]')) return;

  syncCustomEndpointRow();

  var link = document.getElementById('models-link-' + sel.dataset.slot);
  if (!link) return;
  var v = sel.value;
  if (v && link.dataset[v]) {
    link.href = link.dataset[v];
    var custom = v === 'custom';
    link.textContent = custom ? link.dataset.labelCustom : link.dataset.labelDefault;
    if (custom) {
      link.removeAttribute('target');
      link.removeAttribute('rel');
    } else {
      link.target = '_blank';
      link.rel = 'noopener';
    }
    link.classList.remove('hidden');
  } else {
    link.classList.add('hidden');
  }
});

// Thousands grouping for the numeric limit inputs (Limits section). Delegated
// for the same reason as the provider select above: saving swaps the whole page
// in through hx-boost, and a listener bound to the input itself would be gone
// the moment the settings are saved once.
function fmtThousands(digits) {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
}

document.addEventListener('input', function (e) {
  var inp = e.target;
  if (!inp || !inp.matches || !inp.matches('[data-thousands]')) return;
  var pos = inp.selectionStart;
  var digitsBeforeCursor = inp.value.slice(0, pos).replace(/[^\d]/g, '').length;
  inp.value = fmtThousands(inp.value.replace(/[^\d]/g, ''));
  var newPos = 0, count = 0;
  for (var i = 0; i < inp.value.length; i++) {
    if (inp.value[i] !== ' ') count++;
    if (count === digitsBeforeCursor) { newPos = i + 1; break; }
  }
  inp.setSelectionRange(newPos, newPos);
});

// Strip the separators before the request is built. Capture phase, not bubbling:
// hx-boost binds its own submit handler on the form, and a listener on document
// would only see the event on the way back up, after htmx already read the
// values. (The server strips whitespace too, so this is belt and braces.)
document.addEventListener('submit', function (e) {
  var form = e.target;
  if (!form || !form.querySelectorAll) return;
  form.querySelectorAll('[data-thousands]').forEach(function (inp) {
    inp.value = inp.value.replace(/[^\d]/g, '');
  });
}, true);

document.addEventListener('DOMContentLoaded', function () {
  // Remove API key button → clear password input before submit
  document.querySelectorAll('button[data-clear-key]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var inp = btn.closest('form').querySelector('[name=api_key]');
      if (inp) inp.value = '';
    });
  });

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
