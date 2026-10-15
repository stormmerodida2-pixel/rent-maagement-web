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

  // Phones: the hamburger slides the menu in. It closes with the X, a tap on the dimmed page, Escape,
  // or choosing a link; focus moves into the menu and back to the hamburger.
  function setNav(open) {
    var sidebar = document.querySelector('.sidebar');
    var toggle = document.querySelector('[data-nav-toggle]');
    if (!sidebar || !toggle || sidebar.classList.contains('nav-open') === open) return;
    sidebar.classList.toggle('nav-open', open);
    document.body.classList.toggle('nav-locked', open);
    toggle.setAttribute('aria-expanded', String(open));
    if (open) {
      var close = sidebar.querySelector('.nav-close');
      if (close) close.focus();
    } else {
      toggle.focus();
    }
  }
  document.addEventListener('click', function (event) {
    if (event.target.closest('[data-nav-toggle]')) { setNav(true); return; }
    if (event.target.closest('[data-nav-close]')) { setNav(false); return; }
    if (event.target.closest('.sidebar.nav-open .nav a')) setNav(false);
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') setNav(false);
  });
  window.addEventListener('resize', function () {
    if (window.innerWidth > 900) setNav(false);  // e.g. a tablet turned sideways
  });
  window.addEventListener('pageshow', function () { setNav(false); });  // back button: arrive with it closed

  // Phones: label every table cell with its column heading, so tables can stack into cards.
  function labelTables() {
    document.querySelectorAll('table.table').forEach(function (table) {
      var head = table.tHead && table.tHead.rows[0];
      if (!head) return;
      var labels = Array.prototype.map.call(head.cells, function (th) { return th.textContent.trim(); });
      Array.prototype.forEach.call(table.querySelectorAll('tbody tr, tfoot tr'), function (row) {
        Array.prototype.forEach.call(row.cells, function (cell, i) {
          if (cell.hasAttribute('data-label')) return;
          cell.setAttribute('data-label', labels[i] || '');
          if (!cell.textContent.trim() && !cell.querySelector('input, button, a, svg')) cell.classList.add('blank');
          // Keep a cell's contents together (e.g. "name · unit") beside its label, instead of spread apart.
          if (i > 0 && cell.childNodes.length > 1) {
            var wrap = document.createElement('span');
            wrap.className = 'cell-value';
            while (cell.firstChild) wrap.appendChild(cell.firstChild);
            cell.appendChild(wrap);
          }
        });
      });
      table.classList.add('stack');
    });
  }
  document.addEventListener('DOMContentLoaded', labelTables);

  document.addEventListener('click', function (event) {
    if (event.target.closest('[data-reload]')) window.location.reload();
  });

  // Installable app: register the service worker, and show "Install app" where the phone allows it.
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () { navigator.serviceWorker.register('/sw.js').catch(function () {}); });
  }
  var installed = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone;
  var isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
  var deferredPrompt = null;
  function showInstallButtons() {
    document.querySelectorAll('[data-install]').forEach(function (b) { b.hidden = false; });
  }
  window.addEventListener('beforeinstallprompt', function (event) {
    event.preventDefault();  // show our own button instead of the browser's mini-bar
    deferredPrompt = event;
    if (!installed) showInstallButtons();
  });
  if (!installed && isIOS) document.addEventListener('DOMContentLoaded', showInstallButtons);
  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-install]');
    if (!button) return;
    if (deferredPrompt) {
      deferredPrompt.prompt();
      deferredPrompt.userChoice.then(function () { deferredPrompt = null; });
    } else {
      // iPhone/iPad have no install prompt: show how to add it from the Share menu.
      document.querySelectorAll('[data-install-ios]').forEach(function (hint) { hint.hidden = !hint.hidden; });
    }
  });
  // "Remind everyone who owes": keep the count, SMS cost and button in step with the ticks.
  function syncRemind(form) {
    var boxes = form.querySelectorAll('input[name="lease"]');
    var count = 0, parts = 0;
    boxes.forEach(function (b) { if (b.checked) { count += 1; parts += Number(b.dataset.parts) || 1; } });
    form.querySelector('[data-remind-count]').textContent = count;
    form.querySelector('[data-remind-cost]').textContent = (parts * Number(form.dataset.costPerPart)).toFixed(2);
    form.querySelector('[data-remind-send]').disabled = count === 0;
  }
  document.addEventListener('change', function (event) {
    var form = event.target.closest('[data-remind-form]');
    if (form) syncRemind(form);
  });
  document.addEventListener('click', function (event) {
    var toggle = event.target.closest('[data-remind-all]');
    if (!toggle) return;
    var form = toggle.closest('[data-remind-form]');
    form.querySelectorAll('input[name="lease"]').forEach(function (b) { b.checked = toggle.dataset.remindAll === '1'; });
    syncRemind(form);
  });
  // Copy buttons (e.g. a listing's share link): data-copy="#id-of-input".
  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-copy]');
    if (!button) return;
    var field = document.querySelector(button.dataset.copy);
    if (!field) return;
    var done = function () {
      var label = button.querySelector('[data-copy-label]');
      if (label) { label.textContent = 'Copied'; setTimeout(function () { label.textContent = 'Copy'; }, 2000); }
    };
    if (navigator.clipboard) {
      navigator.clipboard.writeText(field.value).then(done, function () { field.select(); });
    } else {
      field.select();
      try { document.execCommand('copy'); done(); } catch (e) { /* the text stays selected to copy by hand */ }
    }
  });
  window.addEventListener('appinstalled', function () {
    document.querySelectorAll('[data-install], [data-install-ios]').forEach(function (b) { b.hidden = true; });
  });
})();
