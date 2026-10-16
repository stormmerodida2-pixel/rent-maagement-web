"""Caretaker tools: water meter readings, spending from cash held (approved by the landlord), tenant notes."""

import datetime
import io
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from payments.models import Payment, StaffExpense
from payments.utils import staff_cash
from rentals.models import MeterReading, Property, RentCharge, TenantNote
from rentals.tests import make_lease

from ..models import StaffMember

D = Decimal
PRIVATE = Path(tempfile.mkdtemp())


def photo(name='p.jpg'):
    out = io.BytesIO()
    Image.new('RGB', (40, 30), 'gray').save(out, 'JPEG')
    return SimpleUploadedFile(name, out.getvalue(), content_type='image/jpeg')


@override_settings(PRIVATE_MEDIA_ROOT=PRIVATE)
class CaretakerToolsTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(PRIVATE, ignore_errors=True)

    def setUp(self):
        cache.clear()
        User = get_user_model()
        self.owner = User.objects.create_user('landlord', password='pw', first_name='Grace')
        self.lease = make_lease(self.owner, label='A1')
        Property.objects.filter(pk=self.lease.unit.property_id).update(water_rate=D('150'))
        self.peter_user = User.objects.create_user('peter', password='pw', first_name='Peter')
        self.peter = StaffMember.objects.create(owner=self.owner, user=self.peter_user)
        self.client.force_login(self.peter_user)

    # --- Meter readings ----------------------------------------------------------------------

    def read(self, value, day=None, **extra):
        return self.client.post(reverse('staff:meter_reading', args=[self.lease.unit_id]), {
            'reading_date': (day or timezone.localdate()).isoformat(), 'value': value, **extra})

    def test_meter_readings_bill_water_and_keep_the_photo_private(self):
        self.read('100', timezone.localdate() - datetime.timedelta(days=30))
        resp = self.read('112', photo=photo())
        self.assertRedirects(resp, reverse('staff:meters'))
        reading = MeterReading.objects.get(value=D('112'))
        self.assertEqual(reading.recorded_by, self.peter_user)
        self.assertEqual(RentCharge.objects.get(kind='water').amount, D('1800'))  # 12 units × 150
        self.assertTrue(Path(reading.photo.path).is_relative_to(PRIVATE))
        url = reverse('private_file', args=['meter', reading.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertContains(self.client.get(reverse('rentals:water_readings', args=[self.lease.unit.property_id])), 'by Peter')
        self.client.force_login(get_user_model().objects.create_user('other'))
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_lower_reading_is_refused(self):
        self.read('100')
        self.assertContains(self.read('90'), 'lower than the last reading')

    # --- Spending from cash held -------------------------------------------------------------

    def collect(self, amount):
        Payment.objects.create(lease=self.lease, amount=D(amount), method='cash', recorded_by=self.peter_user,
                               reference=f'C{Payment.objects.count()}')

    def spend(self, amount, **extra):
        return self.client.post(reverse('staff:spending'), {
            'amount': amount, 'description': 'New kitchen tap', 'unit': self.lease.unit_id,
            'spent_on': timezone.localdate().isoformat(), **extra})

    def test_spending_counts_once_approved(self):
        self.collect('5000')
        self.assertRedirects(self.spend('1200', receipt=photo('receipt.jpg')), reverse('staff:spending'))
        expense = StaffExpense.objects.get()
        cash = staff_cash(self.peter)
        self.assertEqual((cash['holding'], cash['spend_pending'], cash['to_hand_over']), (D('5000'), D('1200'), D('3800')))
        self.assertEqual(self.client.get(reverse('private_file', args=['receipt', expense.pk])).status_code, 200)

        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('rentals:dashboard')), 'spending record')
        page = self.client.get(reverse('payments:cash_with_staff'))
        self.assertContains(page, 'See receipt')
        self.client.post(reverse('payments:expense_decide', args=[expense.pk]), {'action': 'approve'})
        self.assertEqual(staff_cash(self.peter)['holding'], D('3800'))

    def test_rejecting_needs_a_reason_and_keeps_the_cash_with_them(self):
        self.collect('5000')
        self.spend('1200')
        expense = StaffExpense.objects.get()
        self.client.force_login(self.owner)
        self.client.post(reverse('payments:expense_decide', args=[expense.pk]), {'action': 'reject'})
        expense.refresh_from_db()
        self.assertEqual(expense.status, 'pending')
        self.client.post(reverse('payments:expense_decide', args=[expense.pk]), {'action': 'reject', 'reason': 'No receipt'})
        expense.refresh_from_db()
        self.assertEqual((expense.status, expense.reject_reason), ('rejected', 'No receipt'))
        self.assertEqual(staff_cash(self.peter)['holding'], D('5000'))

    def test_cannot_spend_more_than_held(self):
        self.collect('1000')
        self.assertContains(self.spend('1500'), 'more than the cash you are holding')
        self.assertFalse(StaffExpense.objects.exists())

    # --- Notes -------------------------------------------------------------------------------

    def test_notes_and_promises(self):
        friday = timezone.localdate() + datetime.timedelta(days=3)
        self.client.post(reverse('staff:note', args=[self.lease.pk]), {
            'text': 'Salary comes on Friday', 'promised_amount': '10000', 'promised_by': friday.isoformat()})
        note = TenantNote.objects.get()
        self.assertEqual((note.author, note.promised_amount), (self.peter_user, D('10000')))
        self.lease.ensure_charge_for(timezone.localdate())
        self.assertContains(self.client.get(reverse('staff:home')), 'Salary comes on Friday')
        self.assertFalse(note.promise_missed())
        self.assertTrue(note.promise_missed(friday + datetime.timedelta(days=1)))  # still owing afterwards

        self.client.force_login(self.owner)
        page = self.client.get(reverse('rentals:lease_detail', args=[self.lease.pk]))
        self.assertContains(page, 'Salary comes on Friday')
        self.assertContains(page, 'Peter (staff)')
        self.client.post(reverse('rentals:lease_note', args=[self.lease.pk]), {'text': 'Called her, all fine'})
        self.assertEqual(TenantNote.objects.count(), 2)

    def test_promise_needs_a_date(self):
        resp = self.client.post(reverse('staff:note', args=[self.lease.pk]), {'text': 'Will pay', 'promised_amount': '500'})
        self.assertContains(resp, 'When did they promise to pay by?')

    # --- Tickboxes ---------------------------------------------------------------------------

    def test_landlord_switches_tools_off(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('staff:edit', args=[self.peter.pk]), {
            'name': 'Peter', 'phone': '0711000111', 'password': '', 'active': 'on', 'can_add_notes': 'on'})
        self.peter.refresh_from_db()
        self.assertEqual((self.peter.can_read_meters, self.peter.can_record_spending, self.peter.can_add_notes),
                         (False, False, True))
        self.client.force_login(self.peter_user)
        self.assertRedirects(self.client.get(reverse('staff:meters')), reverse('staff:home'))
        self.assertRedirects(self.client.get(reverse('staff:spending')), reverse('staff:home'))
        home = self.client.get(reverse('staff:home'))
        self.assertNotContains(home, 'Meter readings')
        self.assertEqual(self.client.get(reverse('staff:note', args=[self.lease.pk])).status_code, 200)
