// Theme switch: auto (follow the device) | light | dark.
// Stored in localStorage "theme", the same key Django's admin uses, so both stay in sync.
(function () {
  var KEY = 'theme';
  var root = document.documentElement;

  function save(choice) {
    try { localStorage.setItem(KEY, choice); } catch (e) { /* private mode: still applies for this page */ }
  }

  function apply(choice) {
    root.setAttribute('data-theme', choice);
    document.querySelectorAll('[data-theme-choice]').forEach(function (btn) {
      btn.setAttribute('aria-pressed', String(btn.getAttribute('data-theme-choice') === choice));
    });
  }

  document.addEventListener('click', function (event) {
    var btn = event.target.closest('[data-theme-choice]');
    if (!btn) return;
    var choice = btn.getAttribute('data-theme-choice');
    save(choice);
    apply(choice);
  });

  // Another tab (or the admin) changed the theme.
  window.addEventListener('storage', function (event) {
    if (event.key === KEY) apply(event.newValue || 'auto');
  });

  apply(root.getAttribute('data-theme') || 'auto');
})();
