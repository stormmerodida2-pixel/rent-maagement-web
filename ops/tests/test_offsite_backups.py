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
        options = {'BACKUP_DIR': Path(tmp), 'BACKUP_ENCRYPTION_KEY': KEY, 'BACKUP_S3_BUCKET': 'rentipay-backups',
                   'BACKUP_S3_REGION': 'eu-west-1', 'BACKUP_S3_PREFIX': 'backups/', **settings}
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
