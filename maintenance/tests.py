from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

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
