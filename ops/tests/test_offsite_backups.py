"""Off-site copies of the nightly backup in S3, and the backup status the health check reports."""

import base64
import contextlib
import datetime
import hashlib
import tempfile
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from botocore.exceptions import ClientError
from cryptography.fernet import Fernet
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ..models import JobRun
from ..utils import backup_status

KEY = Fernet.generate_key().decode()


def fake_dump():
    if connection.vendor != 'postgresql':
        return contextlib.nullcontext()

    def run(cmd, env, **kwargs):
        Path(cmd[cmd.index('--file') + 1]).write_bytes(b'dump')
        return SimpleNamespace(returncode=0, stderr='')
    return mock.patch('ops.management.commands.backup_db.subprocess.run', side_effect=run)


class OffsiteCopyTests(TestCase):
    def backup(self, tmp, **settings):
        empty = tempfile.mkdtemp()  # no uploaded files: just the database
        options = {'BACKUP_DIR': Path(tmp), 'BACKUP_ENCRYPTION_KEY': KEY, 'BACKUP_S3_BUCKET': 'rentipay-backups',
                   'BACKUP_S3_REGION': 'eu-west-1', 'BACKUP_S3_PREFIX': 'backups/', 'MEDIA_ROOT': Path(empty),
                   'PRIVATE_MEDIA_ROOT': Path(empty) / 'private', **settings}
        out = StringIO()
        with override_settings(**options), fake_dump():
            call_command('backup_db', stdout=out, stderr=StringIO())
        return out.getvalue()

    @mock.patch('boto3.client')
    def test_encrypted_backup_is_copied_to_s3(self, client):
        with tempfile.TemporaryDirectory() as tmp:
            out = self.backup(tmp)
            local = next(Path(tmp).glob('rentals-*.enc'))
            client.assert_called_once_with('s3', region_name='eu-west-1')
            kwargs = client.return_value.put_object.call_args.kwargs
            self.assertEqual((kwargs['Bucket'], kwargs['Key']), ('rentipay-backups', f'backups/{local.name}'))
            self.assertEqual(kwargs['ServerSideEncryption'], 'AES256')
            # S3 verifies the upload against the file's MD5.
            self.assertEqual(kwargs['ContentMD5'],
                             base64.b64encode(hashlib.md5(local.read_bytes(), usedforsecurity=False).digest()).decode())
        self.assertIn('Copied off-site to s3://rentipay-backups/backups/', out)
        # Upload only: the server never lists, reads or deletes what's in the bucket.
        self.assertEqual([name for name, *_ in client.return_value.method_calls], ['put_object'])

    @mock.patch('boto3.client')
    def test_failed_upload_fails_the_job_but_keeps_the_local_backup(self, client):
        client.return_value.put_object.side_effect = ClientError(
            {'Error': {'Code': 'AccessDenied', 'Message': 'Access Denied'}}, 'PutObject')
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesMessage(CommandError, 'The local backup is kept'):
                self.backup(tmp)
            self.assertEqual(len(list(Path(tmp).glob('rentals-*.enc'))), 1)

    @mock.patch('boto3.client')
    def test_never_uploads_unencrypted(self, client):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesMessage(CommandError, 'only copied off the server encrypted'):
                self.backup(tmp, BACKUP_ENCRYPTION_KEY='')
        client.return_value.put_object.assert_not_called()

    @mock.patch('boto3.client')
    def test_no_bucket_no_upload(self, client):
        with tempfile.TemporaryDirectory() as tmp:
            self.backup(tmp, BACKUP_S3_BUCKET='')
        client.assert_not_called()


class BackupStatusTests(TestCase):
    def finished(self, hours_ago, ok=True):
        when = timezone.now() - datetime.timedelta(hours=hours_ago)
        JobRun.objects.update_or_create(name='backup_db', defaults={
            'last_started_at': when, 'last_finished_at': when, 'last_ok': ok})

    def test_states(self):
        with override_settings(BACKUP_S3_BUCKET=''):
            self.assertEqual(backup_status(), 'off')
        with override_settings(BACKUP_S3_BUCKET='rentipay-backups'):
            self.assertEqual(backup_status(), 'pending')
            self.finished(3)
            self.assertEqual(backup_status(), 'ok')
            self.finished(30)
            self.assertEqual(backup_status(), 'stale')
            self.finished(3, ok=False)
            self.assertEqual(backup_status(), 'failed')
            resp = self.client.get(reverse('healthz'))
            self.assertEqual(resp.status_code, 200)  # the site itself is fine
            self.assertEqual(resp.json()['backups'], 'failed')


class FileBackupTests(TestCase):
    """Uploaded files (listing photos, receipts, meter photos) are archived and encrypted with the database."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.media, self.private, self.backups = self.tmp / 'media', self.tmp / 'private_media', self.tmp / 'backups'
        (self.media / 'listings').mkdir(parents=True)
        (self.private / 'receipts').mkdir(parents=True)
        (self.media / 'listings' / 'house.jpg').write_bytes(b'photo')
        (self.private / 'receipts' / 'tap.jpg').write_bytes(b'receipt')

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_backup(self, **extra):
        with override_settings(BACKUP_DIR=self.backups, BACKUP_ENCRYPTION_KEY=KEY, MEDIA_ROOT=self.media,
                               PRIVATE_MEDIA_ROOT=self.private, **extra), fake_dump():
            call_command('backup_db', stdout=StringIO(), stderr=StringIO())

    def test_files_are_archived_encrypted_and_restorable(self):
        import tarfile

        from ..utils import decrypt_file

        self.run_backup(BACKUP_S3_BUCKET='')
        archive = next(self.backups.glob('rentals-files-*.tar.gz.enc'))
        self.assertNotIn(b'receipt', archive.read_bytes())  # encrypted
        restored = self.tmp / 'restored.tar.gz'
        decrypt_file(archive, restored, KEY)
        with tarfile.open(restored) as tar:
            names = tar.getnames()
            self.assertIn('media/listings/house.jpg', names)
            self.assertIn('private_media/receipts/tap.jpg', names)
            self.assertEqual(tar.extractfile('private_media/receipts/tap.jpg').read(), b'receipt')

    @mock.patch('boto3.client')
    def test_files_go_off_site_too(self, client):
        self.run_backup(BACKUP_S3_BUCKET='rentipay-backups', BACKUP_S3_REGION='eu-west-1', BACKUP_S3_PREFIX='backups/')
        keys = [c.kwargs['Key'] for c in client.return_value.put_object.call_args_list]
        self.assertEqual(len(keys), 2)
        self.assertTrue(any(k.startswith('backups/rentals-files-') and k.endswith('.tar.gz.enc') for k in keys))

    def test_nothing_uploaded_yet_no_file_archive(self):
        import shutil

        shutil.rmtree(self.media)
        shutil.rmtree(self.private)
        self.run_backup(BACKUP_S3_BUCKET='')
        self.assertEqual(list(self.backups.glob('rentals-files-*')), [])

