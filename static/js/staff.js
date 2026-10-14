// Staff area: the M-Pesa prompt page checks every few seconds until the payment arrives or fails.
(function () {
  var card = document.querySelector('[data-prompt-poll]');
  if (!card || !window.fetch) return;
  var url = card.dataset.promptPoll;
  var tries = 0;

  function show(state) {
    card.className = card.className.replace(/prompt-(waiting|paid|failed)/, 'prompt-' + state.status);
    card.querySelector('[data-prompt-title]').textContent = state.title;
    card.querySelector('[data-prompt-detail]').textContent = state.detail;
    var check = document.querySelector('[data-prompt-check]');
    if (check) check.hidden = state.status !== 'waiting';
  }

  function poll() {
    tries += 1;
    fetch(url, {credentials: 'same-origin', headers: {Accept: 'application/json'}})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (state) {
        if (!state) return;
        show(state);
        if (state.status === 'waiting' && tries < 60) setTimeout(poll, 4000);  // about four minutes
      })
      .catch(function () { if (tries < 60) setTimeout(poll, 6000); });
  }
  if (card.className.indexOf('prompt-waiting') !== -1) setTimeout(poll, 3000);
})();
