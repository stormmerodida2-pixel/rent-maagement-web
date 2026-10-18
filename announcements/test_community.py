"""Community and growth: the building noticeboard, rating finished repairs, inviting landlords, checking receipts."""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from maintenance.models import MaintenanceRequest, RepairRating
from payments.models import Payment
from rentals.models import Lease, Property, Tenant, Unit

from .models import BoardPost

D = Decimal


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_user('landlord', first_name='Grace')
        self.prop = Property.objects.create(owner=self.owner, name='Kodi Apartments',
                                            building_info='Garbage: Tue and Fri.')
        self.jane = self.tenant('A1', 'Jane Akinyi')
        self.musa = self.tenant('A2', 'Musa Otieno')

    def tenant(self, label, name):
        unit = Unit.objects.create(property=self.prop, label=label, default_rent=D('10000'))
        tenant = Tenant.objects.create(owner=self.owner, full_name=name, phone='0712345678')
        Lease.objects.create(unit=unit, tenant=tenant, start_date=datetime.date(2026, 1, 1), monthly_rent=D('10000'))
        return tenant

    def portal(self, tenant, data=None):
        url = reverse('rentals:tenant_portal', args=[tenant.portal_token])
        return self.client.post(url, data) if data is not None else self.client.get(url)


class NoticeboardTests(Base):
    def test_neighbours_see_posts_and_good_to_know(self):
        self.portal(self.jane, {'action': 'board_post', 'property': self.prop.pk, 'text': 'Found: car keys by the gate'})
        page = self.portal(self.musa)
        self.assertContains(page, 'Kodi Apartments noticeboard')
        self.assertContains(page, 'Garbage: Tue and Fri.')
        self.assertContains(page, 'Found: car keys by the gate')
        self.assertContains(page, 'Jane (A1)')
        self.assertNotContains(page, '0712345678')

    def test_other_buildings_dont_see_it(self):
        other = Property.objects.create(owner=self.owner, name='Elsewhere')
        self.portal(self.jane, {'action': 'board_post', 'property': other.pk, 'text': 'Sneaky'})
        self.assertFalse(BoardPost.objects.exists())

    def test_limits_and_removal(self):
        for n in range(4):
            self.portal(self.jane, {'action': 'board_post', 'property': self.prop.pk, 'text': f'Post {n}'})
        self.assertEqual(BoardPost.objects.count(), 3)
        post = BoardPost.objects.first()
        self.portal(self.musa, {'action': 'board_remove', 'post': post.pk})  # not his
        post.refresh_from_db()
        self.assertFalse(post.removed)
        self.portal(self.jane, {'action': 'board_remove', 'post': post.pk})
        post.refresh_from_db()
        self.assertTrue(post.removed)

    def test_landlord_sees_and_removes_posts(self):
        post = BoardPost.objects.create(property=self.prop, tenant=self.jane, text='Party on Saturday')
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('announcements:list')), 'Party on Saturday')
        self.client.post(reverse('announcements:board_post_remove', args=[post.pk]))
        self.assertNotContains(self.portal(self.musa), 'Party on Saturday')

    def test_old_posts_disappear(self):
        post = BoardPost.objects.create(property=self.prop, tenant=self.jane, text='Ancient news')
        BoardPost.objects.filter(pk=post.pk).update(created_at=timezone.now() - datetime.timedelta(days=31))
        self.assertNotContains(self.portal(self.musa), 'Ancient news')


class RatingTests(Base):
    def test_tenant_rates_a_finished_repair_and_landlord_sees_it(self):
        unit = self.jane.leases.get().unit
        repair = MaintenanceRequest.objects.create(unit=unit, title='Leaking tap', assigned_to='Fundi Joe',
                                                   status=MaintenanceRequest.Status.RESOLVED)
        self.assertContains(self.portal(self.jane), 'How was the fix?')
        self.portal(self.jane, {'action': 'rate', 'repair': repair.pk, 'good': '1'})
        self.assertTrue(RepairRating.objects.get().good)
        self.portal(self.jane, {'action': 'rate', 'repair': repair.pk, 'good': '0'})  # changed their mind
        self.assertFalse(RepairRating.objects.get().good)

        self.client.force_login(self.owner)
        page = self.client.get(reverse('maintenance:list') + '?status=resolved')
        self.assertContains(page, '👎 1')
        self.assertContains(page, 'How fixes were rated')
        self.assertContains(page, 'Fundi Joe')

    def test_cannot_rate_someone_elses_or_unfinished_repairs(self):
        musa_unit = self.musa.leases.get().unit
        theirs = MaintenanceRequest.objects.create(unit=musa_unit, title='x', status=MaintenanceRequest.Status.RESOLVED)
        open_one = MaintenanceRequest.objects.create(unit=self.jane.leases.get().unit, title='y')
        for repair in (theirs, open_one):
            self.portal(self.jane, {'action': 'rate', 'repair': repair.pk, 'good': '1'})
        self.assertFalse(RepairRating.objects.exists())


class ReferralTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_invite_link_signup_and_free_month(self):
        from accounts.models import Referral
        from accounts.utils import referral_code
        from billing.models import BillingAccount, Invoice
        from billing.utils import account_for, mark_paid

        grace = get_user_model().objects.create_user('grace', first_name='Grace')
        self.client.force_login(grace)
        page = self.client.get(reverse('accounts:referrals'))
        self.assertContains(page, f'?ref={referral_code(grace)}')
        self.client.logout()

        self.client.get(reverse('accounts:signup') + f'?ref={referral_code(grace)}')
        self.client.post(reverse('accounts:signup'), {
            'first_name': 'Otieno', 'contact': 'otieno@example.com', 'password1': 'Strong-pass-2026',
            'password2': 'Strong-pass-2026', 'accept_terms': 'on'})
        referral = Referral.objects.get()
        self.assertEqual((referral.referrer, referral.referred.first_name), (grace, 'Otieno'))

        # Otieno pays his first bill: Grace earns a free month, and her next bill uses it.
        account = account_for(referral.referred)
        invoice = Invoice.objects.create(account=account, period_start=account.paid_until,
                                         period_end=account.paid_until + datetime.timedelta(days=30), units=8,
                                         free_units=5, price_per_unit=75, amount=225, due_date=account.paid_until)
        mark_paid(invoice, Invoice.Method.TILL, 'ABC123XYZ9')
        self.assertEqual(BillingAccount.objects.get(owner=grace).free_months, 1)
        mark_paid(invoice, Invoice.Method.TILL, 'ABC123XYZ9')  # once only
        self.assertEqual(BillingAccount.objects.get(owner=grace).free_months, 1)

    def test_made_up_codes_are_ignored(self):
        from accounts.models import Referral

        self.client.get(reverse('accounts:signup') + '?ref=1-zzzzz')
        self.client.post(reverse('accounts:signup'), {
            'first_name': 'Otieno', 'contact': 'otieno@example.com', 'password1': 'Strong-pass-2026',
            'password2': 'Strong-pass-2026', 'accept_terms': 'on'})
        self.assertFalse(Referral.objects.exists())

    def test_free_month_makes_the_next_bill_free(self):
        from billing.models import BillingAccount, BillingSettings, Invoice
        from billing.utils import account_for, make_invoice

        BillingSettings.objects.update_or_create(pk=1, defaults={'enabled': True})
        cache.clear()
        owner = get_user_model().objects.create_user('grace')
        prop = Property.objects.create(owner=owner, name='Big')
        for n in range(8):
            unit = Unit.objects.create(property=prop, label=f'U{n}', default_rent=D('5000'))
            tenant = Tenant.objects.create(owner=owner, full_name=f'T {n}')
            Lease.objects.create(unit=unit, tenant=tenant, start_date=datetime.date(2026, 1, 1), monthly_rent=D('5000'))
        account = account_for(owner)
        BillingAccount.objects.filter(pk=account.pk).update(free_months=1)
        account.refresh_from_db()
        invoice = make_invoice(account, today=account.paid_until)
        self.assertEqual((invoice.status, invoice.method), (Invoice.Status.PAID, Invoice.Method.FREE))
        self.assertEqual(BillingAccount.objects.get(pk=account.pk).free_months, 0)
        cache.clear()


class VerifyReceiptTests(Base):
    def test_genuine_cancelled_and_made_up(self):
        from reports.utils import receipt_code

        lease = self.jane.leases.get()
        payment = Payment.objects.create(lease=lease, amount=D('10000'), method='cash')
        code = receipt_code(payment)
        page = self.client.get(reverse('verify_receipt'), {'code': code.lower()})
        self.assertContains(page, 'Genuine receipt')
        self.assertContains(page, 'J. Akinyi')
        self.assertContains(page, 'Kodi Apartments')
        self.assertNotContains(page, '0712345678')

        self.assertContains(self.client.get(reverse('verify_receipt'), {'code': code[:-1] + 'Q'}), 'No receipt has this code')
        payment.voided_at = timezone.now()
        payment.save()
        self.assertContains(self.client.get(reverse('verify_receipt'), {'code': code}), 'was cancelled')

    def test_code_is_on_the_receipt(self):
        from reports.utils import receipt_code

        payment = Payment.objects.create(lease=self.jane.leases.get(), amount=D('10000'), method='cash')
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('reports:payment_receipt', args=[payment.pk])),
                            receipt_code(payment))
