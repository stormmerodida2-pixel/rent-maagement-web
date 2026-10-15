"""Vacancy listings: advertising a unit, the public page, applications and approving a tenant."""

import datetime
import io
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from accounts.utils import mark_verified
from rentals.models import Lease, Property, Tenant, Unit
from rentals.tests import make_lease

from .models import Application, Listing

TODAY = datetime.date.today()
MEDIA = tempfile.mkdtemp()


def photo(name='room.jpg', size=(3000, 2000), fmt='JPEG', gps=False):
    image = Image.new('RGB', size, (200, 120, 60))
    out = io.BytesIO()
    kwargs = {}
    if gps:
        exif = Image.Exif()
        exif[0x8825] = {1: 'S', 2: (1.0, 17.0, 30.0)}  # GPSInfo: where the photo was taken
        exif[0x0110] = 'Phone model'
        kwargs['exif'] = exif.tobytes()
    image.save(out, fmt, **kwargs)
    return SimpleUploadedFile(name, out.getvalue(), content_type=f'image/{fmt.lower()}')


@override_settings(MEDIA_ROOT=MEDIA)
class ListingTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        cache.clear()
        User = get_user_model()
        self.owner = User.objects.create_user('landlord', email='l@example.com', password='pw')
        mark_verified(self.owner)
        prop = Property.objects.create(owner=self.owner, name='Riverside Court', address='Ruaka')
        self.vacant = Unit.objects.create(property=prop, label='B4', bedrooms=2, default_rent=22500)
        self.client.force_login(self.owner)

    def create(self, **extra):
        data = {'title': '2-bedroom at Riverside Court', 'rent': '22500', 'deposit': '22500',
                'available_from': TODAY.isoformat(), 'description': 'Water 24/7, parking.',
                'contact_phone': '0712345678', **extra}
        return self.client.post(reverse('listings:create', args=[self.vacant.pk]), data)

    # --- Advertising ----------------------------------------------------------------------

    def test_vacant_and_moving_out_units_can_be_advertised(self):
        let = make_lease(self.owner, label='A1')
        leaving = make_lease(self.owner, label='A2')
        Lease.objects.filter(pk=leaving.pk).update(end_date=TODAY + datetime.timedelta(days=20))
        page = self.client.get(reverse('listings:list'))
        self.assertContains(page, reverse('listings:create', args=[self.vacant.pk]))
        self.assertContains(page, reverse('listings:create', args=[leaving.unit.pk]))
        self.assertNotContains(page, reverse('listings:create', args=[let.unit.pk]))
        # A moving-out unit is available the day after they leave.
        form = self.client.get(reverse('listings:create', args=[leaving.unit.pk]))
        self.assertContains(form, (TODAY + datetime.timedelta(days=21)).isoformat())

    def test_photos_are_resized_and_lose_their_location(self):
        resp = self.create(photos=[photo(gps=True), photo('plan.png', (800, 600), 'PNG')])
        listing = Listing.objects.get()
        self.assertRedirects(resp, reverse('listings:detail', args=[listing.pk]))
        self.assertEqual(listing.photos.count(), 2)
        with Image.open(listing.photos.first().image.path) as saved:
            self.assertEqual(saved.format, 'JPEG')
            self.assertLessEqual(max(saved.size), 1600)
            self.assertEqual(len(saved.getexif()), 0)  # no GPS, no phone model

    def test_rejects_files_that_arent_photos(self):
        bad = SimpleUploadedFile('notes.jpg', b'not really an image', content_type='image/jpeg')
        resp = self.create(photos=[bad])
        self.assertContains(resp, "isn&#x27;t a photo we can read")
        self.assertFalse(Listing.objects.exists())  # nothing half-saved

    def test_photo_limit(self):
        resp = self.create(photos=[photo(f'p{i}.jpg', (50, 50)) for i in range(9)])
        self.assertContains(resp, 'You can add 8 more photos')

    # --- Public page --------------------------------------------------------------------------

    def test_public_page_for_sharing(self):
        self.create(photos=[photo()])
        listing = Listing.objects.get()
        self.client.logout()
        page = self.client.get(listing.get_absolute_url())
        self.assertContains(page, '2-bedroom at Riverside Court')
        self.assertContains(page, 'KES 22,500')
        self.assertContains(page, 'property="og:image"')  # photo preview on WhatsApp/Facebook
        self.assertContains(page, 'Call +254712345678')
        self.assertEqual(Listing.objects.get().views, 1)
        self.client.force_login(self.owner)
        detail = self.client.get(reverse('listings:detail', args=[listing.pk]))
        self.assertContains(detail, 'https://wa.me/?text=')

    def test_apply_notifies_the_landlord_once(self):
        self.create()
        listing = Listing.objects.get()
        self.client.logout()
        data = {'name': 'Jane Wanjiru', 'phone': '0722 000 111', 'occupants': '2',
                'move_in': (TODAY + datetime.timedelta(days=10)).isoformat(), 'message': 'I work nearby.'}
        resp = self.client.post(listing.get_absolute_url(), data, follow=True)
        self.assertContains(resp, 'Your application has been sent')
        application = Application.objects.get()
        self.assertEqual(application.phone, '254722000111')
        self.assertIn('Jane Wanjiru', mail.outbox[0].subject)
        self.client.post(listing.get_absolute_url(), data)  # same phone again: no duplicate
        self.assertEqual(Application.objects.count(), 1)

    def test_bots_and_floods_are_stopped(self):
        self.create()
        listing = Listing.objects.get()
        self.client.logout()
        self.client.post(listing.get_absolute_url(), {'name': 'Bot', 'phone': '0722000111', 'website': 'spam'})
        self.assertFalse(Application.objects.exists())
        for i in range(12):
            self.client.post(listing.get_absolute_url(), {'name': f'P{i}', 'phone': f'07220001{i:02d}'})
        self.assertEqual(Application.objects.count(), 10)

    # --- Choosing a tenant -------------------------------------------------------------------------

    def test_approve_creates_the_tenant_and_letting_closes_the_listing(self):
        self.create()
        listing = Listing.objects.get()
        application = Application.objects.create(listing=listing, name='Jane Wanjiru', phone='254722000111')
        resp = self.client.post(reverse('listings:application_action', args=[application.pk]), {'action': 'approve'})
        tenant = Tenant.objects.get(full_name='Jane Wanjiru')
        self.assertRedirects(resp, f"{reverse('rentals:lease_create')}?unit={self.vacant.pk}&tenant={tenant.pk}",
                             fetch_redirect_response=False)
        Lease.objects.create(unit=self.vacant, tenant=tenant, start_date=TODAY, monthly_rent=22500, due_day=TODAY.day)
        listing.refresh_from_db()
        self.assertFalse(listing.is_open)
        self.client.logout()
        closed = self.client.get(listing.get_absolute_url())
        self.assertContains(closed, 'This place has been let')
        self.assertNotContains(closed, 'Send application')

    def test_private_to_the_landlord(self):
        self.create()
        listing = Listing.objects.get()
        application = Application.objects.create(listing=listing, name='Jane', phone='254722000111')
        self.client.force_login(get_user_model().objects.create_user('other'))
        self.assertEqual(self.client.get(reverse('listings:detail', args=[listing.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('listings:application_action', args=[application.pk]),
                                          {'action': 'approve'}).status_code, 404)
        self.assertEqual(self.client.get(reverse('listings:create', args=[self.vacant.pk])).status_code, 404)

    def test_search_engines(self):
        self.create()
        listing = Listing.objects.get()
        self.assertContains(self.client.get('/sitemap.xml'), listing.get_absolute_url())
        self.assertContains(self.client.get('/robots.txt'), 'Disallow: /vacancies/')
