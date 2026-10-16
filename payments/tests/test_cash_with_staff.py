"""Cash with staff: what caretakers collect, hand over and are still holding."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import StaffMember
from notifications.models import LandlordPreferences
from notifications.utils import payment_summary
from rentals.tests import make_lease

from ..models import CashHandover, Payment
from ..utils import staff_cash

D = Decimal


class CashWithStaffTests(TestCase):
    def setUp(self):
        cache.clear()
        User = get_user_model()
        self.owner = User.objects.create_user('landlord', password='pw', email='landlord@example.com', first_name='Grace')
        self.lease = make_lease(self.owner, label='A1')
        self.peter_user = User.objects.create_user('peter', password='pw', first_name='Peter')
        self.peter = StaffMember.objects.create(owner=self.owner, user=self.peter_user)
        self.client.force_login(self.peter_user)
        self.client.post(reverse('staff:record_cash', args=[self.lease.pk]), {
            'amount': '5000', 'paid_at': timezone.localtime().strftime('%Y-%m-%dT%H:%M'), 'reference': '', 'notes': ''})

    def hand_over(self, **data):
        return self.client.post(reverse('staff:handover'),
                                {'amount': '3000', 'method': 'in_person', 'reference': '', 'note': '', **data})

    def test_caretaker_sees_what_they_hold_and_hands_it_over(self):
        self.assertContains(self.client.get(reverse('staff:home')), 'KES 5,000')
        self.assertRedirects(self.hand_over(), reverse('staff:home'))
        handover = CashHandover.objects.get()
        self.assertEqual((handover.status, handover.amount), ('pending', D('3000')))
        cash = staff_cash(self.peter)
        self.assertEqual((cash['holding'], cash['pending'], cash['to_hand_over']), (D('5000'), D('3000'), D('2000')))
        self.assertIn('Peter handed over KES 3,000', mail.outbox[-1].subject)

    def test_cannot_hand_over_more_than_held_and_bank_needs_a_slip(self):
        self.assertContains(self.hand_over(amount='6000'), 'more than the cash you are holding')
        self.assertContains(self.hand_over(method='banked'), 'bank slip number')
        self.assertFalse(CashHandover.objects.exists())

    def test_landlord_confirms_or_disputes(self):
        self.hand_over()
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('rentals:dashboard')), 'says they handed over')
        page = self.client.get(reverse('payments:cash_with_staff'))
        self.assertContains(page, 'Peter is holding')
        handover = CashHandover.objects.get()
        self.client.post(reverse('payments:handover_decide', args=[handover.pk]), {'action': 'confirm'})
        self.assertEqual(staff_cash(self.peter)['holding'], D('2000'))

        self.client.force_login(self.peter_user)
        self.hand_over(amount='2000')
        self.client.force_login(self.owner)
        second = CashHandover.objects.get(status='pending')
        self.client.post(reverse('payments:handover_decide', args=[second.pk]), {'action': 'dispute'})
        second.refresh_from_db()
        self.assertEqual(second.status, 'disputed')
        self.assertEqual(staff_cash(self.peter)['holding'], D('2000'))  # still counts as held

    def test_landlord_records_cash_they_received(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('payments:cash_with_staff'),
                         {'staff': self.peter.pk, 'amount': '5000', 'method': 'in_person', 'reference': '', 'note': ''})
        self.assertEqual(CashHandover.objects.get().status, 'confirmed')
        self.assertEqual(staff_cash(self.peter)['holding'], D('0'))

    def test_voided_cash_is_not_counted(self):
        payment = Payment.objects.get()
        payment.voided_at = timezone.now()
        payment.save()
        self.assertEqual(staff_cash(self.peter)['holding'], D('0'))

    def test_evening_summary_mentions_cash_held(self):
        LandlordPreferences.objects.update_or_create(user=self.owner, defaults={'phone': '254712345678'})
        parts = payment_summary(self.owner, timezone.localdate())
        self.assertIn('1 cash by Peter', parts[2])
        self.assertIn('Peter is holding KES 5,000 cash.', parts[3])

    def test_private(self):
        self.hand_over()
        handover = CashHandover.objects.get()
        other = get_user_model().objects.create_user('other')
        self.client.force_login(other)
        self.assertEqual(self.client.post(reverse('payments:handover_decide', args=[handover.pk]),
                                          {'action': 'confirm'}).status_code, 404)
        self.assertNotContains(self.client.get(reverse('payments:cash_with_staff')), 'Peter')
        resp = self.client.post(reverse('payments:cash_with_staff'),
                                {'staff': self.peter.pk, 'amount': '1', 'method': 'in_person'})
        self.assertEqual(resp.status_code, 200)  # not their caretaker: form error, nothing saved
        self.assertEqual(CashHandover.objects.count(), 1)
