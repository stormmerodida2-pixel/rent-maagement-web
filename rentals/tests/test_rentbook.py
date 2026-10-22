"""Snap your rent book: photos are read by Claude into import rows, then the usual preview and confirm."""

import io
import json
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from ..models import Lease, Property, Unit
from ..rentbook import read_rent_book
from ..utils import ImportError_


def page_photo(name='page1.jpg'):
    out = io.BytesIO()
    Image.new('RGB', (80, 60), 'white').save(out, 'JPEG')
    return SimpleUploadedFile(name, out.getvalue(), content_type='image/jpeg')


def claude_reply(rows, stop_reason='end_turn'):
    text = json.dumps({'rows': rows})
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type='text', text=text)])


ROWS = [
    {'unit': 'A1', 'type': 'One bedroom', 'rent': '12000', 'tenant': 'Jane Wanjiru', 'phone': '0712345678',
     'rent_day': '5', 'moved_in': '2025-03-05', 'deposit': '12000', 'balance': '4000', 'unclear': False},
    {'unit': 'A2', 'type': '', 'rent': '10000', 'tenant': '', 'phone': '', 'rent_day': '', 'moved_in': '',
     'deposit': '', 'balance': '', 'unclear': False},
    {'unit': 'A3', 'type': 'Single room', 'rent': '8000', 'tenant': 'Musa Otieno', 'phone': '0722000111',
     'rent_day': '1', 'moved_in': '', 'deposit': '', 'balance': '0', 'unclear': True},
]


@override_settings(ANTHROPIC_API_KEY='test-key')
class RentBookTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord')
        self.client.force_login(self.owner)

    def post_photos(self, photos, **extra):
        return self.client.post(reverse('rentals:property_import'), {
            'action': 'photos', 'property': 'new', 'new_property': 'Kodi Apartments', 'photos': photos, **extra})

    @mock.patch('anthropic.resources.beta.messages.Messages.create')
    def test_photos_become_a_preview_then_import(self, create):
        create.return_value = claude_reply(ROWS)
        page = self.post_photos([page_photo(), page_photo('page2.jpg')])
        self.assertContains(page, 'Read from your photos')
        self.assertContains(page, 'Jane Wanjiru')
        self.assertContains(page, '>Check</span>')  # A3 was hard to read
        sent = create.call_args.kwargs
        self.assertEqual(sent['model'], 'claude-opus-5-5')
        self.assertEqual(sum(1 for b in sent['messages'][0]['content'] if b['type'] == 'image'), 2)
        self.assertEqual(sent['output_config']['format']['type'], 'json_schema')
        self.assertFalse(Property.objects.exists())  # nothing saved before confirming

        self.client.post(reverse('rentals:property_import'), {'action': 'confirm'})
        prop = Property.objects.get(name='Kodi Apartments')
        self.assertEqual(Unit.objects.filter(property=prop).count(), 3)
        jane = Lease.objects.get(tenant__full_name='Jane Wanjiru')
        self.assertEqual(jane.balance, 4000)

    def test_too_many_or_no_photos(self):
        self.assertContains(self.post_photos([]), 'at least one photo')
        self.assertContains(self.post_photos([page_photo(f'p{i}.jpg') for i in range(6)]), 'Up to 5 photos')

    @mock.patch('anthropic.resources.beta.messages.Messages.create')
    def test_unreadable_or_declined(self, create):
        create.return_value = claude_reply([])
        self.assertContains(self.post_photos([page_photo()]), 'No houses could be read')
        create.return_value = claude_reply([], stop_reason='refusal')
        self.assertContains(self.post_photos([page_photo()]), "couldn&#x27;t be read")

    @mock.patch('anthropic.resources.beta.messages.Messages.create')
    def test_service_errors_are_friendly(self, create):
        import anthropic
        import httpx2

        request = httpx2.Request('POST', 'https://api.anthropic.com/v1/messages')
        create.side_effect = anthropic.APIConnectionError(request=request)
        with self.assertRaisesMessage(ImportError_, 'try again shortly'):
            read_rent_book([b'jpeg'])

    @override_settings(ANTHROPIC_API_KEY='')
    def test_hidden_without_a_key(self):
        page = self.client.get(reverse('rentals:property_import'))
        self.assertNotContains(page, 'Snap your rent book')
        self.assertContains(page, 'Excel or CSV')
