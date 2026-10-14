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

  function headline(p) {
    return '\uD83D\uDC9A ' + p.tenant.split(' ')[0] + ' just paid ' + p.amount;  // 💚 Wanjiku just paid KES 18,000
  }

  function toast(p, summary) {
    var list = document.getElementById('toasts');
    var t = el('a', 'toast');
    t.href = p.url;
    t.setAttribute('role', 'status');
    t.appendChild(el('strong', '', summary ? p.amount : headline(p)));
    t.appendChild(el('span', '', summary ? p.tenant : p.unit + ' · ' + p.method));
    list.appendChild(t);
    setTimeout(function () { t.classList.add('toast-out'); }, 9000);
    setTimeout(function () { t.remove(); }, 9600);
  }

  function notify(p) {
    if (!('Notification' in window) || Notification.permission !== 'granted' || !document.hidden) return;
    var n = new Notification(headline(p), {
      body: p.tenant + ' · ' + p.unit + ' · ' + p.method, icon: icon, tag: 'payment-' + p.id,
    });
    n.onclick = function () { window.focus(); window.location.href = p.url; };
  }

  // A soft two-note chime, made in the browser (no sound file). Browsers only allow sound after the
  // person has clicked or typed on the page, so the audio is unlocked on their first interaction.
  var audio = null;
  function soundOn() {
    try { return localStorage.getItem('payment-chime') !== 'off'; } catch (e) { return true; }
  }
  function unlockAudio() {
    var Ctx = window.AudioContext || window.webkitAudioContext;
    if (!audio && Ctx) audio = new Ctx();
    if (audio && audio.state === 'suspended') audio.resume();
  }
  document.addEventListener('pointerdown', unlockAudio, {once: true});
  document.addEventListener('keydown', unlockAudio, {once: true});
  function chime() {
    if (!audio || audio.state !== 'running' || !soundOn()) return;
    [[880, 0], [1318.5, 0.13]].forEach(function (note) {
      var start = audio.currentTime + note[1];
      var osc = audio.createOscillator();
      var gain = audio.createGain();
      osc.type = 'sine';
      osc.frequency.value = note[0];
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(0.07, start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.45);
      osc.connect(gain).connect(audio.destination);
      osc.start(start);
      osc.stop(start + 0.5);
    });
  }
  var soundToggle = document.querySelector('[data-chime-toggle]');
  function showSound() {
    if (!soundToggle) return;
    soundToggle.setAttribute('aria-checked', soundOn() ? 'true' : 'false');
    soundToggle.querySelector('[data-chime-label]').textContent = soundOn() ? 'On' : 'Off';
  }
  if (soundToggle) {
    soundToggle.addEventListener('click', function () {
      try { localStorage.setItem('payment-chime', soundOn() ? 'off' : 'on'); } catch (e) {}
      showSound();
      unlockAudio();
      chime();  // a preview, so they know what it sounds like
    });
    showSound();
  }

  // The rent-day ring glides to its new value instead of jumping.
  function animateRing(ring, from) {
    if (!ring) return;
    ring.style.strokeDashoffset = from;
    void ring.getBoundingClientRect();
    requestAnimationFrame(function () { ring.style.strokeDashoffset = ''; });
  }
  animateRing(document.querySelector('.ring-fill'), 100);

  function applyFragments(fragments) {
    Object.keys(fragments).forEach(function (name) {
      var target = document.getElementById('live-' + name);
      if (!target || target.innerHTML === fragments[name]) return;
      var oldRing = target.querySelector('.ring-fill');
      var from = oldRing ? getComputedStyle(oldRing).strokeDashoffset : null;
      target.innerHTML = fragments[name];
      if (from !== null) animateRing(target.querySelector('.ring-fill'), from);
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
        chime();
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

  // Settings → Payment alerts: system notifications need the browser's permission, asked on a click.
  var enable = document.querySelector('[data-enable-alerts]');
  var alertsStatus = document.querySelector('[data-alerts-status]');
  function showAlerts() {
    if (!alertsStatus) return;
    var state = 'Notification' in window ? Notification.permission : 'unsupported';
    alertsStatus.textContent = {
      granted: 'On. You\u2019ll be alerted even when the app is in the background.',
      denied: 'Blocked in this browser. Allow notifications for this site in your browser settings.',
      unsupported: 'This browser doesn\u2019t support notifications.',
    }[state] || 'Get alerts even when the app is in the background.';
    if (enable) enable.hidden = state !== 'default';
  }
  if (enable) {
    enable.addEventListener('click', function () {
      Notification.requestPermission().then(showAlerts);
    });
  }
  showAlerts();

  schedule();
})();
