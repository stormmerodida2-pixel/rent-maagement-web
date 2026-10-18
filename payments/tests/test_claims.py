"""Tenants say "I've paid" with a photo of the slip; the landlord confirms (payment + receipt) or rejects (SMS)."""

import datetime
import io
import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from notifications.models import SmsMessage
from rentals.models import RentCharge
from rentals.tests import make_lease

from ..models import Payment, PaymentClaim

D = Decimal
PRIVATE = tempfile.mkdtemp()
TODAY = timezone.localdate()


def slip():
    out = io.BytesIO()
    Image.new('RGB', (60, 40), 'white').save(out, 'JPEG')
    return SimpleUploadedFile('slip.jpg', out.getvalue(), content_type='image/jpeg')


@override_settings(PRIVATE_MEDIA_ROOT=PRIVATE)
class ClaimTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(PRIVATE, ignore_errors=True)

    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_user('landlord', first_name='Grace', email='g@example.com')
        from accounts.utils import mark_verified

        mark_verified(self.owner)
        self.lease = make_lease(self.owner)
        RentCharge.objects.create(lease=self.lease, period=TODAY.replace(day=1), due_date=TODAY, amount=D('15000'),
                                  kind=RentCharge.Kind.RENT)
        self.portal = reverse('rentals:tenant_portal', args=[self.lease.tenant.portal_token])

    def claim(self, **data):
        return self.client.post(self.portal, {
            'action': 'claim', 'lease': self.lease.pk, 'amount': '15000', 'method': 'bank',
            'paid_on': TODAY.isoformat(), 'reference': 'eq 1234', 'note': 'Paid at Equity', **data})

    def test_tenant_says_they_paid_with_a_photo(self):
        self.assertContains(self.client.get(self.portal), "I've paid")
        self.claim(photo=slip())
        claim = PaymentClaim.objects.get()
        self.assertEqual((claim.amount, claim.method, claim.reference, claim.status),
                         (D('15000'), 'bank', 'EQ1234', 'pending'))
        self.assertTrue(claim.photo)
        self.assertEqual(self.lease.balance, D('15000'))  # nothing counts until the landlord confirms
        self.assertIn('says they paid KES 15,000', mail.outbox[-1].subject)
        self.assertContains(self.client.get(self.portal), 'Waiting for your landlord')

    def test_landlord_confirms_payment_and_receipt(self):
        self.claim(photo=slip())
        claim = PaymentClaim.objects.get()
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('rentals:dashboard')), '1 tenant says they paid')
        page = self.client.get(reverse('payments:claims'))
        self.assertContains(page, 'EQ1234')
        self.assertEqual(self.client.get(reverse('private_file', args=['claim', claim.pk])).status_code, 200)

        self.client.post(reverse('payments:claim_decide', args=[claim.pk]), {'decision': 'confirm'})
        claim.refresh_from_db()
        payment = Payment.objects.get()
        self.assertEqual((claim.status, claim.payment, payment.method, payment.reference, payment.recorded_by),
                         ('confirmed', payment, 'bank', 'EQ1234', self.owner))
        self.assertEqual(self.lease.balance, D('0'))
        sms = SmsMessage.objects.get(kind=SmsMessage.Kind.RECEIPT)
        self.assertIn('Your landlord confirmed KES 15,000', sms.body)
        # Twice can't record it twice.
        self.client.post(reverse('payments:claim_decide', args=[claim.pk]), {'decision': 'confirm'})
        self.assertEqual(Payment.objects.count(), 1)

    def test_reject_with_a_reason_texts_the_tenant(self):
        self.claim()
        claim = PaymentClaim.objects.get()
        self.client.force_login(self.owner)
        self.client.post(reverse('payments:claim_decide', args=[claim.pk]), {'decision': 'reject', 'reason': ''})
        self.assertEqual(PaymentClaim.objects.get().status, 'pending')  # a reason is needed
        self.client.post(reverse('payments:claim_decide', args=[claim.pk]),
                         {'decision': 'reject', 'reason': 'Not in my bank statement'})
        self.assertEqual(PaymentClaim.objects.get().status, 'rejected')
        self.assertIn('Not in my bank statement', SmsMessage.objects.get().body)
        self.assertFalse(Payment.objects.exists())
        self.assertContains(self.client.get(self.portal), 'Not in my bank statement')

    def test_already_recorded_code_is_flagged_not_doubled(self):
        Payment.objects.create(lease=self.lease, amount=D('15000'), method='mpesa', reference='TJK3XY9ABC')
        self.claim(method='mpesa', reference='tjk3xy9abc')
        claim = PaymentClaim.objects.get()
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('payments:claims')), 'probably a duplicate')
        self.client.post(reverse('payments:claim_decide', args=[claim.pk]), {'decision': 'confirm'})
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(PaymentClaim.objects.get().status, 'pending')

    def test_limits_and_bad_input(self):
        self.claim(amount='0')
        self.claim(paid_on=(TODAY + datetime.timedelta(days=1)).isoformat())
        self.assertFalse(PaymentClaim.objects.exists())
        for _ in range(4):
            self.claim(reference='')
        self.assertEqual(PaymentClaim.objects.count(), 3)

    def test_other_landlords_cannot_see_or_decide(self):
        self.claim(photo=slip())
        claim = PaymentClaim.objects.get()
        self.client.force_login(get_user_model().objects.create_user('stranger'))
        self.assertNotContains(self.client.get(reverse('payments:claims')), 'EQ1234')
        self.assertEqual(self.client.post(reverse('payments:claim_decide', args=[claim.pk]),
                                          {'decision': 'confirm'}).status_code, 404)
        self.assertEqual(self.client.get(reverse('private_file', args=['claim', claim.pk])).status_code, 404)
