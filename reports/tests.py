import csv
import datetime
import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from payments.models import Payment
from rentals.tests import make_lease


class ReportTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.alice = User.objects.create_user('alice', first_name='Alice')
        self.bob = User.objects.create_user('bob')
        self.month = datetime.date.today().replace(day=1)
        self.lease = make_lease(self.alice, rent='10000', deposit='5000')
        self.lease.ensure_charge_for(self.month)
        self.lease.bill_deposit()  # deposit is not income
        self.payment = Payment.objects.create(lease=self.lease, amount=Decimal('6000'), method='cash')
        bob_lease = make_lease(self.bob, label='B1')
        bob_lease.ensure_charge_for(self.month)
        self.bob_payment = Payment.objects.create(lease=bob_lease, amount=Decimal('999'), method='cash')
        self.client.force_login(self.alice)

    def test_overview_numbers(self):
        resp = self.client.get(reverse('reports:overview'))
        totals = resp.context['totals']
        self.assertEqual((totals['billed'], totals['collected']), (Decimal('10000'), Decimal('6000')))
        self.assertEqual(totals['arrears'], Decimal('9000'))  # 10,000 rent + 5,000 deposit - 6,000
        self.assertEqual(totals['rate'], 60)
        self.assertEqual(len(resp.context['rows']), 1)
        self.assertEqual(resp.context['trend'][-1]['collected'], Decimal('6000'))

    def test_rent_roll_csv(self):
        resp = self.client.get(reverse('reports:rent_roll_csv'))
        rows = list(csv.reader(io.StringIO(resp.content.decode('utf-8-sig'))))
        self.assertEqual(rows[0][:3], ['Property', 'Unit', 'Tenant'])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][8], '9000.00')
        self.assertEqual(rows[1][-1], self.lease.account_number)

    def test_payments_csv_only_own(self):
        resp = self.client.get(reverse('reports:payments_csv') + f'?month={self.month:%Y-%m}')
        body = resp.content.decode('utf-8-sig')
        self.assertIn('6000', body)
        self.assertNotIn('999', body)

    def test_print_statement_and_receipt(self):
        statement = self.client.get(reverse('reports:lease_statement', args=[self.lease.pk]))
        # Rent covers a month from the rent day (the 5th here): "Rent 5 Oct – 4 Nov 2026".
        start, end = self.lease.cycle_start(self.month), self.lease.cycle_end(self.month)
        self.assertContains(statement, f'Rent {start.day} {start:%b} – {end.day} {end:%b %Y}')
        self.assertContains(statement, 'Amount due')
        receipt = self.client.get(reverse('reports:payment_receipt', args=[self.payment.pk]))
        self.assertContains(receipt, 'KES 6,000.00')
        self.assertContains(receipt, f'R-{self.payment.pk:06d}')

    def test_cannot_print_other_landlords_documents(self):
        self.assertEqual(self.client.get(reverse('reports:payment_receipt', args=[self.bob_payment.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('reports:lease_statement', args=[self.bob_payment.lease_id])).status_code, 404)

    def test_tenant_can_print_only_their_own(self):
        self.client.logout()
        token = self.lease.tenant.portal_token
        self.assertEqual(self.client.get(reverse('reports:tenant_statement', args=[token, self.lease.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('reports:tenant_receipt', args=[token, self.payment.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('reports:tenant_receipt', args=[token, self.bob_payment.pk])).status_code, 404)
        self.assertEqual(
            self.client.get(reverse('reports:tenant_statement', args=[token, self.bob_payment.lease_id])).status_code, 404)

    def test_reports_require_login(self):
        self.client.logout()
        for name in ['overview', 'rent_roll_csv', 'payments_csv']:
            self.assertEqual(self.client.get(reverse(f'reports:{name}')).status_code, 302, name)
