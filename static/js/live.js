// Live payments: "just paid" alerts on every landlord page, and a dashboard that updates itself.
// Polls /live/ (which doesn't count as activity, so idle logout still works).
(function () {
  var box = document.getElementById('live');
  if (!box || !window.fetch) return;
  var url = box.dataset.url;
  var cursor = Number(box.dataset.cursor) || 0;
  var onDashboard = box.dataset.dashboard === '1';
  var icon = box.dataset.icon;
  var baseTitle = document.title;
  var unseen = 0;
  var polls = 0;
  var stopped = false;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text) node.textContent = text;
    return node;
  }

  function toast(p, summary) {
    var list = document.getElementById('toasts');
    var t = el('a', 'toast');
    t.href = p.url;
    t.setAttribute('role', 'status');
    t.appendChild(el('strong', '', summary ? p.amount : p.amount + ' received'));
    t.appendChild(el('span', '', summary ? p.tenant : p.tenant + ' · ' + p.unit + ' · ' + p.method));
    list.appendChild(t);
    setTimeout(function () { t.classList.add('toast-out'); }, 9000);
    setTimeout(function () { t.remove(); }, 9600);
  }

  function notify(p) {
    if (!('Notification' in window) || Notification.permission !== 'granted' || !document.hidden) return;
    var n = new Notification('Payment received: ' + p.amount, {
      body: p.tenant + ' (' + p.unit + ') via ' + p.method, icon: icon, tag: 'payment-' + p.id,
    });
    n.onclick = function () { window.focus(); window.location.href = p.url; };
  }

  function applyFragments(fragments) {
    Object.keys(fragments).forEach(function (name) {
      var target = document.getElementById('live-' + name);
      if (!target || target.innerHTML === fragments[name]) return;
      target.innerHTML = fragments[name];
      target.classList.remove('live-updated');
      void target.offsetWidth;  // restart the highlight animation
      target.classList.add('live-updated');
    });
    document.dispatchEvent(new Event('DOMContentLoaded'));  // re-label new tables for phones
  }

  function request(withFragments) {
    var query = '?since=' + cursor + (withFragments ? '&fragments=1' : '');
    return fetch(url + query, {credentials: 'same-origin', redirect: 'manual', headers: {Accept: 'application/json'}})
      .then(function (r) {
        var json = (r.headers.get('content-type') || '').indexOf('json') !== -1;
        if (r.type === 'opaqueredirect' || r.status !== 200 || !json) { stopped = true; return null; }
        return r.json();
      });
  }

  function poll() {
    polls += 1;
    var refreshAnyway = onDashboard && polls % 4 === 0;  // about once a minute: charges, repairs…
    request(refreshAnyway).then(function (data) {
      if (!data) return;
      cursor = data.cursor;
      if (data.fragments) applyFragments(data.fragments);
      if (data.payments.length) {
        // A burst (e.g. after the laptop slept): two alerts and a summary, not a flood.
        data.payments.slice(0, 2).forEach(function (p) { toast(p); notify(p); });
        if (data.payments.length > 2) {
          var more = data.payments.length - 2;
          toast({url: box.dataset.paymentsUrl, amount: '+' + more + ' more payment' + (more > 1 ? 's' : ''),
                 tenant: 'See all payments', unit: '', method: ''}, true);
        }
        if (document.hidden) {
          unseen += data.payments.length;
          document.title = '(' + unseen + ') ' + baseTitle;
        }
        if (onDashboard && !data.fragments) {
          return request(true).then(function (fresh) { if (fresh && fresh.fragments) applyFragments(fresh.fragments); });
        }
      }
    }).catch(function () { /* offline for a moment: try again next time */ })
      .then(schedule);
  }

  function schedule() {
    if (stopped) return;
    setTimeout(poll, document.hidden ? 60000 : 15000);
  }

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) { unseen = 0; document.title = baseTitle; }
  });

  // "Turn on payment alerts": system notifications need permission, asked on a click.
  var enable = document.querySelector('[data-enable-alerts]');
  if (enable && 'Notification' in window && Notification.permission === 'default') {
    enable.hidden = false;
    enable.addEventListener('click', function () {
      Notification.requestPermission().then(function () { enable.hidden = true; });
    });
  }

  schedule();
})();
