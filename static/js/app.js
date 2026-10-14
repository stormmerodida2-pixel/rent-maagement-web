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

  // Phones: the Menu button opens and closes the main navigation.
  document.addEventListener('click', function (event) {
    var toggle = event.target.closest('[data-nav-toggle]');
    if (!toggle) return;
    var open = toggle.closest('.sidebar').classList.toggle('nav-open');
    toggle.setAttribute('aria-expanded', String(open));
  });

  // Phones: label every table cell with its column heading, so tables can stack into cards.
  function labelTables() {
    document.querySelectorAll('table.table').forEach(function (table) {
      var head = table.tHead && table.tHead.rows[0];
      if (!head) return;
      var labels = Array.prototype.map.call(head.cells, function (th) { return th.textContent.trim(); });
      Array.prototype.forEach.call(table.querySelectorAll('tbody tr, tfoot tr'), function (row) {
        Array.prototype.forEach.call(row.cells, function (cell, i) {
          if (!cell.hasAttribute('data-label')) cell.setAttribute('data-label', labels[i] || '');
        });
      });
      table.classList.add('stack');
    });
  }
  document.addEventListener('DOMContentLoaded', labelTables);
})();
