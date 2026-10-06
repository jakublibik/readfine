// Admin → Settings. The form is boosted, so this runs again after every save, on the
// freshly swapped elements.
(function () {
  var registration = document.querySelector('[name="registration_enabled"]');
  if (registration) {
    registration.addEventListener('change', function () {
      var enabled = this.checked;
      document.getElementById('legal-section').classList.toggle('hidden', !enabled);
      var w = document.getElementById('legal-warning');
      if (w) w.classList.toggle('hidden', !enabled);
      document.getElementById('traffic-closed-note').classList.toggle('hidden', enabled);
    });
  }
  var dormantWarning = document.querySelector('[name="dormant_warning_enabled"]');
  var dormantUrlNote = document.getElementById('dormant-url-note');
  if (dormantWarning && dormantUrlNote) {
    dormantWarning.addEventListener('change', function () {
      dormantUrlNote.classList.toggle('hidden', !this.checked);
    });
  }
})();
