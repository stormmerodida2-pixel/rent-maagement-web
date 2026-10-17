"""Expenses and profit: what landlords spend, and what they make each month."""

import datetime
import io
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from accounts.models import StaffMember
from maintenance.models import MaintenanceRequest
from rentals.models import Property, Unit
from rentals.tests import make_lease

from ..models import Expense, Payment, StaffExpense
from ..utils import create_recurring_expenses, decide_expense

D = Decimal
TODAY = timezone.localdate()
PRIVATE = Path(tempfile.mkdtemp())


@override_settings(PRIVATE_MEDIA_ROOT=PRIVATE)
class ExpenseTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(PRIVATE, ignore_errors=True)

    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        self.lease = make_lease(self.owner, label='A1')
        self.prop = self.lease.unit.property
        self.client.force_login(self.owner)

    def add(self, **data):
        return self.client.post(reverse('payments:expense_create'), {
            'category': 'repairs', 'description': 'New tap', 'amount': '1500', 'paid_on': TODAY.isoformat(),
            'property': self.prop.pk, 'unit': '', 'method': 'mpesa', 'reference': '', **data})

    def test_add_with_receipt_and_see_profit(self):
        out = io.BytesIO()
        Image.new('RGB', (30, 30), 'white').save(out, 'JPEG')
        self.add(receipt_photo=SimpleUploadedFile('r.jpg', out.getvalue(), content_type='image/jpeg'))
        expense = Expense.objects.get()
        self.assertEqual((expense.owner, expense.amount, expense.recorded_by), (self.owner, D('1500'), self.owner))
        self.assertTrue(Path(expense.receipt.path).is_relative_to(PRIVATE))
        self.assertEqual(self.client.get(reverse('private_file', args=['expense', expense.pk])).status_code, 200)

        Payment.objects.create(lease=self.lease, amount=D('15000'), method='cash')
        Expense.objects.create(owner=self.owner, category='staff', description="Peter's pay", amount=D('5000'))
        report = self.client.get(reverse('reports:overview'))
        self.assertEqual(report.context['totals']['spent'], D('6500'))
        self.assertEqual(report.context['totals']['net'], D('8500'))
        self.assertEqual(report.context['shared'], D('5000'))  # no property: shared by all
        self.assertEqual(report.context['rows'][0]['net'], D('13500'))  # this property: 15,000 − 1,500
        page = self.client.get(reverse('payments:expense_list'))
        self.assertContains(page, 'Caretaker &amp; staff pay')

    def test_unit_must_belong_to_the_property(self):
        other = Property.objects.create(owner=self.owner, name='Other')
        unit = Unit.objects.create(property=other, label='Z1', default_rent=1)
        self.assertContains(self.add(unit=unit.pk), 'is not at')
        self.add(property='', unit=unit.pk)  # just the unit: its property is filled in
        self.assertEqual(Expense.objects.get().property, other)

    def test_monthly_costs_repeat_until_stopped(self):
        start = (TODAY.replace(day=1) - datetime.timedelta(days=70)).replace(day=28)
        self.add(category='staff', description="Peter's pay", amount='8000', paid_on=start.isoformat(),
                 repeats_monthly='on')
        original = Expense.objects.get()
        made = create_recurring_expenses(TODAY)
        self.assertGreaterEqual(made, 2)  # caught up on the months since
        self.assertEqual(create_recurring_expenses(TODAY), 0)  # not twice
        copy = original.repeats.order_by('paid_on').first()
        copy.void(self.owner, 'Paid him in the next month')
        self.assertEqual(create_recurring_expenses(TODAY), 0)  # a voided month isn't added back
        self.client.post(reverse('payments:expense_stop_repeat', args=[original.pk]))
        original.refresh_from_db()
        self.assertFalse(original.repeats_monthly)

    def test_void_takes_it_out_of_profit(self):
        self.add()
        expense = Expense.objects.get()
        self.client.post(reverse('payments:expense_void', args=[expense.pk]), {'reason': 'Typed twice'})
        self.assertFalse(Expense.objects.exists())
        self.assertTrue(Expense.all_objects.filter(pk=expense.pk).exists())

    def test_approved_caretaker_spending_becomes_an_expense(self):
        member = StaffMember.objects.create(owner=self.owner, user=get_user_model().objects.create_user('peter',
                                                                                                         first_name='Peter'))
        spending = StaffExpense.objects.create(owner=self.owner, staff=member, amount=D('1200'),
                                               description='Plumber', unit=self.lease.unit)
        decide_expense(spending, True, self.owner)
        expense = Expense.objects.get()
        self.assertEqual((expense.amount, expense.method, expense.property), (D('1200'), 'staff_cash', self.prop))
        decide_expense(spending, True, self.owner)  # again: still one
        self.assertEqual(Expense.objects.count(), 1)

    def test_repair_cost_prefills_the_form(self):
        repair = MaintenanceRequest.objects.create(unit=self.lease.unit, title='Fix roof', cost=D('7000'))
        page = self.client.get(reverse('payments:expense_create') + f'?repair={repair.pk}')
        self.assertContains(page, 'value="Fix roof"')
        self.assertContains(page, 'value="7000')

    def test_private(self):
        self.add()
        expense = Expense.objects.get()
        self.client.force_login(get_user_model().objects.create_user('other'))
        self.assertEqual(self.client.get(reverse('payments:expense_void', args=[expense.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse('payments:expense_list')), 'New tap')
        self.assertEqual(self.client.get(reverse('private_file', args=['expense', expense.pk])).status_code, 404)
