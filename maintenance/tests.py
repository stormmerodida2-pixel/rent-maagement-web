import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from rentals.tests import make_lease

from .models import MaintenanceRequest


class MaintenanceRequestTests(TestCase):
    def test_resolved_at_tracks_status(self):
        owner = get_user_model().objects.create_user('landlord')
        lease = make_lease(owner)
        req = MaintenanceRequest.objects.create(unit=lease.unit, reported_by=lease.tenant, title='Leaking tap')
        self.assertIsNone(req.resolved_at)

        req.status = MaintenanceRequest.Status.RESOLVED
        req.save()
        self.assertIsNotNone(req.resolved_at)

        req.status = MaintenanceRequest.Status.OPEN
        req.save()
        self.assertIsNone(req.resolved_at)


class MaintenancePagesTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.alice = User.objects.create_user('alice')
        self.lease = make_lease(self.alice)
        self.client.force_login(self.alice)

    def test_log_and_resolve(self):
        self.client.post(reverse('maintenance:create'), {
            'where': f'u{self.lease.unit.pk}', 'title': 'Leaking tap', 'priority': 'high',
        })
        req = MaintenanceRequest.objects.get()
        self.assertContains(self.client.get(reverse('maintenance:list')), 'Leaking tap')
        self.client.post(reverse('maintenance:update', args=[req.pk]), {
            'where': f'u{self.lease.unit.pk}', 'title': 'Leaking tap', 'priority': 'high', 'status': 'resolved',
            'assigned_to': 'Juma', 'cost': '1500',
        })
        req.refresh_from_db()
        self.assertEqual((req.status, req.assigned_to), ('resolved', 'Juma'))
        self.assertIsNotNone(req.resolved_at)
        self.assertEqual(list(self.client.get(reverse('maintenance:list')).context['object_list']), [])
        self.assertEqual(list(self.client.get(reverse('maintenance:list') + '?status=resolved').context['object_list']), [req])

    def test_scoped_to_own_units(self):
        bob_lease = make_lease(get_user_model().objects.create_user('bob'), label='B1')
        bob_req = MaintenanceRequest.objects.create(unit=bob_lease.unit, title='Bob issue')
        self.assertNotContains(self.client.get(reverse('maintenance:list')), 'Bob issue')
        self.assertEqual(self.client.get(reverse('maintenance:update', args=[bob_req.pk])).status_code, 404)
        self.client.post(reverse('maintenance:create'), {'where': f'u{bob_lease.unit.pk}', 'title': 'x', 'priority': 'low'})
        self.assertEqual(MaintenanceRequest.objects.count(), 1)


class GeneralMaintenanceTests(TestCase):
    """Problems affecting most tenants (water, the gate, drainage, cleanliness) are logged for the whole property."""

    def setUp(self):
        self.alice = get_user_model().objects.create_user('alice')
        self.lease = make_lease(self.alice)
        self.prop = self.lease.unit.property
        self.client.force_login(self.alice)

    def test_log_for_the_whole_property(self):
        form = self.client.get(reverse('maintenance:create'))
        self.assertContains(form, f'General: whole of {self.prop.name}')
        self.client.post(reverse('maintenance:create'), {
            'where': f'p{self.prop.pk}', 'title': 'Compound not swept', 'priority': 'medium'})
        req = MaintenanceRequest.objects.get()
        self.assertEqual((req.property, req.unit), (self.prop, None))
        self.assertTrue(req.is_general)
        self.assertContains(self.client.get(reverse('maintenance:list')), 'General')
        self.assertContains(self.client.get(reverse('rentals:dashboard')), f'{self.prop.name} · General')

        # Switch it to one house and back.
        self.client.post(reverse('maintenance:update', args=[req.pk]), {
            'where': f'u{self.lease.unit.pk}', 'title': 'Compound not swept', 'priority': 'medium', 'status': 'open'})
        req.refresh_from_db()
        self.assertEqual(req.unit, self.lease.unit)

    def test_unit_repairs_know_their_property(self):
        req = MaintenanceRequest.objects.create(unit=self.lease.unit, title='Leaking tap')
        self.assertEqual(req.property, self.prop)
        self.assertFalse(req.is_general)

    def test_tenants_see_open_general_issues(self):
        MaintenanceRequest.objects.create(property=self.prop, title='Water pump broken')
        MaintenanceRequest.objects.create(property=self.prop, title='Gate fixed', status='resolved')
        other = make_lease(get_user_model().objects.create_user('bob'), label='B1')
        MaintenanceRequest.objects.create(property=other.unit.property, title='Not their building')
        page = self.client.get(reverse('rentals:tenant_portal', args=[self.lease.tenant.portal_token]))
        self.assertContains(page, 'Being fixed in your building')
        self.assertContains(page, 'Water pump broken')
        self.assertNotContains(page, 'Gate fixed')
        self.assertNotContains(page, 'Not their building')

    def test_caretaker_logs_general_maintenance_for_their_properties_only(self):
        from accounts.models import StaffMember

        User = get_user_model()
        peter = User.objects.create_user('peter', first_name='Peter')
        StaffMember.objects.create(owner=self.alice, user=peter)
        bob_lease = make_lease(User.objects.create_user('bob'), label='B1')
        self.client.force_login(peter)
        self.client.post(reverse('staff:repair_create'), {
            'where': f'p{self.prop.pk}', 'title': 'Drainage blocked', 'priority': 'high'})
        resp = self.client.post(reverse('staff:repair_create'), {
            'where': f'p{bob_lease.unit.property_id}', 'title': 'Sneaky', 'priority': 'low'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(list(MaintenanceRequest.objects.values_list('title', 'unit')), [('Drainage blocked', None)])
        self.assertContains(self.client.get(reverse('staff:repairs')), f'{self.prop.name} · General')


class TenantsHearAboutRepairsTests(TestCase):
    """Tenants see every repair to their house, and can be texted when one is logged, started or done."""

    def setUp(self):
        from rentals.models import Lease, Tenant

        self.alice = get_user_model().objects.create_user('alice')
        self.lease = make_lease(self.alice)  # Tenant A1, 0712345678
        self.prop, self.unit = self.lease.unit.property, self.lease.unit
        from rentals.models import Unit

        neighbour_unit = Unit.objects.create(property=self.prop, label='A2', default_rent=10000)
        neighbour = Tenant.objects.create(owner=self.alice, full_name='Amina Hassan', phone='0722000333',
                                          language='sw')
        Lease.objects.create(unit=neighbour_unit, tenant=neighbour, start_date=self.lease.start_date,
                             monthly_rent=10000)
        self.client.force_login(self.alice)

    def log(self, where, notify=True, **extra):
        return self.client.post(reverse('maintenance:create'), {
            'where': where, 'title': 'Leaking kitchen tap', 'priority': 'medium',
            **({'notify_tenants': 'on'} if notify else {}), **extra})

    def test_tenant_sees_repairs_to_their_house_logged_by_the_landlord(self):
        self.log(f'u{self.unit.pk}', notify=False)
        MaintenanceRequest.objects.create(unit=self.unit, title='Before they moved in')
        MaintenanceRequest.objects.filter(title='Before they moved in').update(
            created_at=timezone.make_aware(datetime.datetime(2025, 1, 1)))
        page = self.client.get(reverse('rentals:tenant_portal', args=[self.lease.tenant.portal_token]))
        self.assertContains(page, 'Repairs to your house')
        self.assertContains(page, 'Leaking kitchen tap')
        self.assertNotContains(page, 'Before they moved in')  # the previous tenant's

    def test_texts_the_tenant_when_logged_and_when_done(self):
        from notifications.models import SmsMessage

        self.log(f'u{self.unit.pk}')
        sms = SmsMessage.objects.get()
        self.assertEqual(sms.phone, '254712345678')
        self.assertIn('will fix it: Leaking kitchen tap', sms.body)
        repair = MaintenanceRequest.objects.get()
        self.client.post(reverse('maintenance:update', args=[repair.pk]), {
            'where': f'u{self.unit.pk}', 'title': 'Leaking kitchen tap', 'priority': 'medium', 'status': 'resolved',
            'notify_tenants': 'on'})
        self.assertIn('this repair is done', SmsMessage.objects.latest('pk').body)

    def test_no_text_unless_ticked(self):
        from notifications.models import SmsMessage

        self.log(f'u{self.unit.pk}', notify=False)
        self.assertFalse(SmsMessage.objects.exists())

    def test_general_repairs_text_everyone_in_their_language(self):
        from notifications.models import SmsMessage

        self.client.post(reverse('maintenance:create'), {
            'where': f'p{self.prop.pk}', 'title': 'Water pump', 'priority': 'urgent', 'notify_tenants': 'on'})
        bodies = dict(SmsMessage.objects.values_list('phone', 'body'))
        self.assertEqual(set(bodies), {'254712345678', '254722000333'})
        self.assertIn('will fix it: Water pump', bodies['254712345678'])
        self.assertIn('Habari Amina, mwenye nyumba anafahamu', bodies['254722000333'])

    def test_caretaker_can_text_tenants_too(self):
        from accounts.models import StaffMember
        from notifications.models import SmsMessage

        peter = get_user_model().objects.create_user('peter', first_name='Peter')
        StaffMember.objects.create(owner=self.alice, user=peter)
        self.client.force_login(peter)
        self.client.post(reverse('staff:repair_create'), {
            'where': f'u{self.unit.pk}', 'title': 'Broken window', 'priority': 'high', 'notify_tenants': 'on'})
        self.assertIn('Broken window', SmsMessage.objects.get().body)
