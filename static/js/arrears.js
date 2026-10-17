// Month-by-month arrears for a tenant who was already living there: keep the "Owes KES …" total up to date
// as months are marked paid, not paid or part paid, and let "All paid" / "None paid" set every month.
(function () {
  var form = document.querySelector('[data-arrears]');
  if (!form) return;
  var total = form.querySelector('[data-owed-total]');

  function owed() {
    var sum = 0;
    form.querySelectorAll('[data-month]').forEach(function (row) {
      var amount = parseFloat(row.getAttribute('data-amount')) || 0;
      var status = row.querySelector('select').value;
      if (status === 'unpaid') sum += amount;
      if (status === 'part') {
        var paid = parseFloat((row.querySelector('input').value || '0').replace(/,/g, '')) || 0;
        sum += Math.max(amount - paid, 0);
      }
    });
    return sum;
  }

  function update() {
    var value = Math.round(owed());
    total.textContent = value.toLocaleString('en-KE');
    form.querySelector('[name="owed"]') || form.insertAdjacentHTML('beforeend', '<input type="hidden" name="owed">');
    form.querySelector('[name="owed"]').value = String(value);  // the confirmation pop-up shows it as {owed}
  }

  form.addEventListener('change', update);
  form.addEventListener('input', update);
  form.querySelectorAll('[data-set-all]').forEach(function (button) {
    button.addEventListener('click', function () {
      var value = button.getAttribute('data-set-all');
      form.querySelectorAll('[data-month] select').forEach(function (select) { select.value = value; });
      form.dispatchEvent(new Event('change', { bubbles: true }));
    });
  });
  update();
})();
