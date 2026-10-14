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
            'unit': self.lease.unit.pk, 'title': 'Leaking tap', 'priority': 'high',
        })
        req = MaintenanceRequest.objects.get()
        self.assertContains(self.client.get(reverse('maintenance:list')), 'Leaking tap')
        self.client.post(reverse('maintenance:update', args=[req.pk]), {
            'unit': self.lease.unit.pk, 'title': 'Leaking tap', 'priority': 'high', 'status': 'resolved',
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
        self.client.post(reverse('maintenance:create'), {'unit': bob_lease.unit.pk, 'title': 'x', 'priority': 'low'})
        self.assertEqual(MaintenanceRequest.objects.count(), 1)
