import contextlib
import datetime
import os
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.ratelimit import client_ip

from .jobs import JOBS, Job, run_due_jobs
from .models import JobRun

EAT = timezone.get_fixed_timezone(180)


def at(hour, minute=0, day=14):
    return datetime.datetime(2026, 10, day, hour, minute, tzinfo=EAT)


class ScheduleTests(TestCase):
    def test_interval_job(self):
        job = Job('x', 'x', every=datetime.timedelta(minutes=5))
        self.assertTrue(job.is_due(at(10), None))
        self.assertFalse(job.is_due(at(10, 4), at(10)))
        self.assertTrue(job.is_due(at(10, 5), at(10)))

    def test_daily_job_runs_once_per_day_and_catches_up_after_downtime(self):
        job = Job('x', 'x', daily_at=datetime.time(6, 0))
        self.assertFalse(job.is_due(at(5, 59), None))
        self.assertTrue(job.is_due(at(6, 0), at(6, 0, day=13)))
        self.assertFalse(job.is_due(at(7), at(6, 0)))  # already ran today
        self.assertTrue(job.is_due(at(14), at(6, 0, day=12)))  # server was down at 06:00: run late

    def test_run_due_jobs_records_runs_and_heartbeat(self):
        cache.clear()
        with mock.patch('ops.jobs.call_command') as call:
            ran = run_due_jobs(now=at(10))
        self.assertIn('check_pending_stk', ran)
        self.assertIn('generate_rent_charges', ran)  # 06:00 job, first run
        self.assertIn('send_rent_reminders', ran)  # 09:00 job, also due at 10:00
        self.assertEqual(call.call_count, len(ran))
        self.assertTrue(JobRun.objects.get(name='check_pending_stk').last_ok)
        self.assertIsNotNone(cache.get('ops:scheduler-heartbeat'))
        with mock.patch('ops.jobs.call_command'):
            self.assertEqual(run_due_jobs(now=at(10, 1)), [])  # nothing due a minute later

    def test_failed_job_is_recorded_and_others_still_run(self):
        cache.clear()

        def flaky(name, **kwargs):
            if name == 'backup_db':
                raise RuntimeError('disk full')

        with mock.patch('ops.jobs.call_command', side_effect=flaky):
            ran = run_due_jobs(now=at(10))
        self.assertFalse(JobRun.objects.get(name='backup_db').last_ok)
        self.assertEqual(len(ran), len(JOBS))

    def test_scheduler_once(self):
        cache.clear()
        out = StringIO()
        with mock.patch('ops.jobs.call_command'):
            call_command('run_scheduler', '--once', stdout=out)
        self.assertIn('check_pending_stk', out.getvalue())


class BackupTests(TestCase):
    """Real pg_dump runs in CI's Docker smoke test; here pg_dump is simulated on PostgreSQL."""

    def fake_pg_dump(self, calls):
        def run(cmd, env, **kwargs):
            calls.append((cmd, env))
            Path(cmd[cmd.index('--file') + 1]).write_bytes(b'dump')
            return SimpleNamespace(returncode=0, stderr='')
        return mock.patch('ops.management.commands.backup_db.subprocess.run', side_effect=run)

    def test_backup_and_rotation(self):
        calls = []
        dump = self.fake_pg_dump(calls) if connection.vendor == 'postgresql' else contextlib.nullcontext()
        with tempfile.TemporaryDirectory() as tmp, dump:
            old = Path(tmp) / 'rentals-20000101-000000.sqlite3'
            old.write_text('old')
            ancient = (timezone.now() - datetime.timedelta(days=30)).timestamp()
            os.utime(old, (ancient, ancient))
            with override_settings(BACKUP_DIR=Path(tmp), BACKUP_KEEP_DAYS=14):
                out = StringIO()
                call_command('backup_db', stdout=out)
            files = list(Path(tmp).glob('rentals-*'))
            self.assertEqual(len(files), 1)
            self.assertFalse(old.exists())
            self.assertIn('removed 1 old backup', out.getvalue())
        if calls:
            cmd, env = calls[0]
            self.assertEqual(cmd[0], 'pg_dump')
            self.assertIn('--format=custom', cmd)
            self.assertIn('PGPASSWORD', env)  # password via env, never on the command line

    @unittest.skipUnless(connection.vendor == 'postgresql', 'pg_dump is only used with PostgreSQL')
    def test_failed_pg_dump_leaves_no_partial_file(self):
        failed = SimpleNamespace(returncode=1, stderr='connection refused')
        with tempfile.TemporaryDirectory() as tmp, override_settings(BACKUP_DIR=Path(tmp)), \
                mock.patch('ops.management.commands.backup_db.subprocess.run', return_value=failed):
            with self.assertRaisesMessage(CommandError, 'connection refused'):
                call_command('backup_db', stdout=StringIO())
            self.assertEqual(list(Path(tmp).iterdir()), [])


class HealthTests(TestCase):
    def test_healthz(self):
        cache.clear()
        resp = self.client.get(reverse('healthz'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['database'], 'ok')
        self.assertEqual(resp.json()['scheduler'], 'not running')


class ClientIpTests(TestCase):
    def request(self, xff=None):
        meta = {'REMOTE_ADDR': '10.0.0.2'}
        if xff:
            meta['HTTP_X_FORWARDED_FOR'] = xff
        return RequestFactory().get('/', **meta)

    def test_direct_connection(self):
        with override_settings(TRUSTED_PROXY_COUNT=0):
            self.assertEqual(client_ip(self.request('6.6.6.6')), '10.0.0.2')  # header ignored

    def test_behind_one_proxy_ignores_forged_entries(self):
        with override_settings(TRUSTED_PROXY_COUNT=1):
            self.assertEqual(client_ip(self.request('41.90.1.1')), '41.90.1.1')
            # A client can prepend fake addresses; only the one the proxy added counts.
            self.assertEqual(client_ip(self.request('1.2.3.4, 41.90.1.1')), '41.90.1.1')
            self.assertEqual(client_ip(self.request()), '10.0.0.2')
