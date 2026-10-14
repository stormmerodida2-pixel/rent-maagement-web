"""Content-Security-Policy, encrypted ID numbers and backups, and idle logout."""

import re
import tempfile
import time
from io import StringIO
from pathlib import Path

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse

from rentals.models import Tenant
from rentals.tests import make_lease

from ..utils import MAGIC, BackupKeyError, decrypt_file, encrypt_file


class ContentSecurityPolicyTests(TestCase):
    def test_header_and_nonce(self):
        resp = self.client.get(reverse('rentals:home'))
        policy = resp.headers['Content-Security-Policy']
        self.assertIn("default-src 'self'", policy)
        self.assertIn("object-src 'none'", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        nonce = re.search(r"script-src 'self' 'nonce-([^']+)'", policy).group(1)
        self.assertContains(resp, f'<script nonce="{nonce}">')  # the early theme script may run

    def test_nonce_changes_every_request(self):
        first = self.client.get(reverse('login')).headers['Content-Security-Policy']
        second = self.client.get(reverse('login')).headers['Content-Security-Policy']
        self.assertNotEqual(first, second)

    def test_pages_have_no_inline_handlers(self):
        owner = get_user_model().objects.create_user('landlord')
        lease = make_lease(owner)
        self.client.force_login(owner)
        for url in [reverse('rentals:lease_detail', args=[lease.pk]), reverse('payments:payment_list'),
                    reverse('payments:mpesa_settings'), reverse('reports:lease_statement', args=[lease.pk]),
                    reverse('rentals:tenant_delete', args=[lease.tenant.pk])]:
            html = self.client.get(url).content.decode()
            self.assertIsNone(re.search(r'\son(click|change|submit|load)=|javascript:', html), url)
            for script in re.findall(r'<script(?![^>]*\bsrc=)[^>]*>', html):
                self.assertIn('nonce=', script, url)


class EncryptedIdNumberTests(TestCase):
    def test_id_number_is_unreadable_in_the_database(self):
        owner = get_user_model().objects.create_user('landlord')
        tenant = Tenant.objects.create(owner=owner, full_name='T', phone='0712345678', id_number='29876543')
        with connection.cursor() as cursor:
            cursor.execute('SELECT id_number FROM rentals_tenant WHERE id = %s', [tenant.pk])
            raw = cursor.fetchone()[0]
        self.assertNotIn('29876543', raw)
        self.assertEqual(Tenant.objects.get(pk=tenant.pk).id_number, '29876543')

    def test_tenant_form_still_saves_it(self):
        owner = get_user_model().objects.create_user('landlord')
        self.client.force_login(owner)
        self.client.post(reverse('rentals:tenant_create'), {'full_name': 'T', 'phone': '0712345678', 'id_number': 'A123'})
        self.assertEqual(Tenant.objects.get().id_number, 'A123')


KEY = Fernet.generate_key().decode()


class EncryptedBackupTests(TestCase):
    def test_round_trip_in_several_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain, enc, out = Path(tmp, 'p'), Path(tmp, 'e'), Path(tmp, 'o')
            data = b'tenant 29876543 ' * 700_000  # ~11 MB: more than one chunk
            plain.write_bytes(data)
            encrypt_file(plain, enc, KEY)
            self.assertTrue(enc.read_bytes().startswith(MAGIC))
            self.assertNotIn(b'29876543', enc.read_bytes())
            decrypt_file(enc, out, KEY)
            self.assertEqual(out.read_bytes(), data)
            with self.assertRaises(BackupKeyError):
                decrypt_file(enc, out, Fernet.generate_key().decode())

    @override_settings(BACKUP_ENCRYPTION_KEY=KEY)
    def test_backup_db_encrypts_and_decrypt_backup_restores(self):
        if connection.vendor != 'sqlite':
            self.skipTest('Real pg_dump runs in the Docker smoke test')
        get_user_model().objects.create_user('someone-recognisable')
        with tempfile.TemporaryDirectory() as tmp, override_settings(BACKUP_DIR=Path(tmp)):
            out = StringIO()
            call_command('backup_db', stdout=out)
            self.assertIn('encrypted', out.getvalue())
            files = list(Path(tmp).iterdir())
            self.assertEqual(len(files), 1)  # no unencrypted temp file left behind
            backup = files[0]
            self.assertTrue(backup.name.endswith('.sqlite3.enc'))
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(b'someone-recognisable', backup.read_bytes())
            restored = Path(tmp, 'restored.sqlite3')
            call_command('decrypt_backup', str(backup), '-o', str(restored), stdout=StringIO())
            self.assertIn(b'someone-recognisable', restored.read_bytes())
            with override_settings(BACKUP_ENCRYPTION_KEY=Fernet.generate_key().decode()):
                with self.assertRaisesMessage(CommandError, 'Wrong BACKUP_ENCRYPTION_KEY'):
                    call_command('decrypt_backup', str(backup), '-o', str(Path(tmp, 'x')), stdout=StringIO())
            self.assertFalse(Path(tmp, 'x').exists())

    @override_settings(BACKUP_ENCRYPTION_KEY='')
    def test_without_a_key_backups_say_they_are_not_encrypted(self):
        if connection.vendor != 'sqlite':
            self.skipTest('Real pg_dump runs in the Docker smoke test')
        with tempfile.TemporaryDirectory() as tmp, override_settings(BACKUP_DIR=Path(tmp)):
            out, err = StringIO(), StringIO()
            call_command('backup_db', stdout=out, stderr=err)
            self.assertIn('NOT encrypted', out.getvalue())
            self.assertIn('NOT encrypted', err.getvalue())


class IdleLogoutTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('landlord', password='pw')

    def test_defaults(self):
        self.assertEqual(settings.SESSION_COOKIE_AGE, 60 * 60)
        self.assertTrue(settings.SESSION_SAVE_EVERY_REQUEST)

    @override_settings(SESSION_COOKIE_AGE=2)
    def test_idle_session_is_logged_out_but_activity_keeps_it(self):
        self.client.post(reverse('login'), {'username': 'landlord', 'password': 'pw'})
        dashboard = reverse('rentals:dashboard')
        for _ in range(3):  # keep clicking: every request pushes the expiry back
            time.sleep(1)
            self.assertEqual(self.client.get(dashboard).status_code, 200)
        time.sleep(2.5)  # now go idle
        resp = self.client.get(dashboard)
        self.assertRedirects(resp, f"{reverse('login')}?next={dashboard}")
