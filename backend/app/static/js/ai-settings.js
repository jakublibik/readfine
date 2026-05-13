// Show loading text in a target element before an HTMX request fires
document.addEventListener('htmx:beforeRequest', function (e) {
  var btn = e.detail.elt;
  var targetId = btn.dataset && btn.dataset.loadingTarget;
  if (!targetId) return;
  var el = document.getElementById(targetId);
  if (el) el.innerHTML = '<span class="text-gray-400 text-sm">' + (btn.dataset.loadingText || 'Loading…') + '</span>';
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

  // Scoring enabled checkbox → toggle preference section
  var chk = document.getElementById('chk-scoring-enabled');
  if (chk) {
    chk.addEventListener('change', function () {
      var on = chk.checked;
      var ta = document.getElementById('ai_preference_text');
      var btn = document.getElementById('btn-generate-preference');
      var sec = document.getElementById('preference-section');
      if (ta) ta.disabled = !on;
      if (btn) btn.disabled = !on;
      if (sec) sec.classList.toggle('opacity-50', !on);
    });
  }

  // Remove API key button → clear password input before submit
  document.querySelectorAll('button[data-clear-key]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var inp = btn.closest('form').querySelector('[name=api_key]');
      if (inp) inp.value = '';
    });
  });

  // Preference text character counter
  var ta = document.getElementById('ai_preference_text');
  var counter = document.getElementById('pref-char-count');
  if (ta && counter) {
    ta.addEventListener('input', function () {
      var n = ta.value.length;
      counter.textContent = n;
      counter.parentElement.classList.toggle('text-red-500', n > 5000);
      counter.parentElement.classList.toggle('text-gray-400', n <= 5000);
    });
  }
});
