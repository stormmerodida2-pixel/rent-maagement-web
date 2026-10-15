// SweetAlert2 pop-ups: confirmations for risky actions, and toasts for "Saved"-style messages.
// Without JavaScript everything still works: forms submit directly and messages show on the page.
//
// Confirmations: put data-confirm="Question?" on a <form> or a submit <button>/<a>. Optional:
//   data-confirm-title, data-confirm-ok (button text), data-confirm-danger (red button).
//   "{amount}" in the text is replaced by the form's "amount" field, so the pop-up shows real values.
(function () {
  if (!window.Swal) return;

  var Popup = Swal.mixin({
    customClass: {popup: 'swal-popup', confirmButton: 'btn btn-primary', cancelButton: 'btn', denyButton: 'btn'},
    buttonsStyling: false,
    reverseButtons: true,
    focusCancel: true,
  });

  function fill(text, form) {
    return text.replace(/\{(\w+)\}/g, function (all, name) {
      var field = form && form.elements[name];
      if (!field || !field.value) return all;
      var n = Number(field.value);
      return isNaN(n) ? field.value : n.toLocaleString('en-KE');
    });
  }

  function ask(el, form) {
    var danger = el.hasAttribute('data-confirm-danger');
    return Popup.fire({
      title: el.dataset.confirmTitle || 'Are you sure?',
      text: fill(el.dataset.confirm, form),
      icon: danger ? 'warning' : 'question',
      showCancelButton: true,
      confirmButtonText: el.dataset.confirmOk || 'Yes, continue',
      cancelButtonText: 'Cancel',
      customClass: {popup: 'swal-popup', confirmButton: danger ? 'btn btn-danger' : 'btn btn-primary', cancelButton: 'btn'},
    });
  }

  document.addEventListener('submit', function (event) {
    var form = event.target;
    var button = event.submitter;
    var el = button && button.hasAttribute('data-confirm') ? button : (form.hasAttribute('data-confirm') ? form : null);
    if (!el || form.dataset.confirmed === '1') return;
    event.preventDefault();
    ask(el, form).then(function (result) {
      if (!result.isConfirmed) return;
      form.dataset.confirmed = '1';
      if (form.requestSubmit) form.requestSubmit(button || undefined); else form.submit();
      setTimeout(function () { delete form.dataset.confirmed; }, 1000);
    });
  }, true);

  document.addEventListener('click', function (event) {
    var link = event.target.closest('a[data-confirm]');
    if (!link) return;
    event.preventDefault();
    ask(link, null).then(function (result) { if (result.isConfirmed) window.location.href = link.href; });
  });

  // "Saved." and friends become a small toast; errors and warnings stay on the page where they can't be missed.
  var Toast = Swal.mixin({
    toast: true, position: 'top-end', showConfirmButton: false, timer: 4000, timerProgressBar: true,
    customClass: {popup: 'swal-popup swal-toast'},
  });
  document.addEventListener('DOMContentLoaded', function () {
    var shown = document.querySelectorAll('.messages [data-toast]');
    shown.forEach(function (alert, i) {
      setTimeout(function () {
        Toast.fire({icon: alert.dataset.toast, title: alert.textContent.trim()});
      }, i * 400);
      alert.remove();
    });
    document.querySelectorAll('.messages').forEach(function (box) { if (!box.children.length) box.remove(); });
  });
})();
