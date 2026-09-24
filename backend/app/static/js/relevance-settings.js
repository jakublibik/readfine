// "Unsaved changes" under the term list while it differs from the saved one
// (the saved text rides on the text area as data-saved). Only typing needs this:
// a click on a suggestion saves, and the server sends the lines back itself.
// Delegated, because saving the form swaps the page in via hx-boost.
document.addEventListener('input', function (e) {
  var area = e.target;
  if (!area || area.id !== 'relevance_terms') return;
  var hint = document.getElementById('relevance-terms-unsaved');
  var status = document.getElementById('relevance-terms-status');
  if (hint) hint.classList.toggle('hidden', area.value.trim() === (area.dataset.saved || '').trim());
  if (status) status.classList.add('hidden');
});
