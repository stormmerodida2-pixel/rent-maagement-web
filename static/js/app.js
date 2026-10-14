// Small page behaviours, wired with data- attributes so pages need no inline JavaScript
// (the Content-Security-Policy blocks inline handlers like onclick="…").
(function () {
  document.addEventListener('click', function (event) {
    if (event.target.closest('[data-print]')) {
      window.print();
    }
    var back = event.target.closest('[data-history-back]');
    if (back && window.history.length > 1) {
      event.preventDefault();  // otherwise the link's own href is the fallback
      window.history.back();
    }
  });

  // Copy boxes: select everything on click so it's easy to copy.
  document.addEventListener('click', function (event) {
    var input = event.target.closest('[data-select-on-click]');
    if (input) input.select();
  });

  // Filters that apply as soon as they change.
  document.addEventListener('change', function (event) {
    if (event.target.matches('[data-autosubmit]') && event.target.form) event.target.form.submit();
  });

  // data-show-when="field_id=value": show an element only while another field has that value.
  function syncConditional() {
    document.querySelectorAll('[data-show-when]').forEach(function (el) {
      var parts = el.getAttribute('data-show-when').split('=');
      var field = document.getElementById(parts[0]);
      if (field) el.style.display = field.value === parts[1] ? '' : 'none';
    });
  }
  document.addEventListener('change', syncConditional);
  document.addEventListener('DOMContentLoaded', syncConditional);
})();
