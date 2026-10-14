"""Operations helpers: encrypted backups, the job scheduler and session middleware."""

import datetime
import io
import logging
import struct
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from django.contrib.sessions.middleware import SessionMiddleware as DjangoSessionMiddleware
from django.core.cache import cache
from django.core.management import call_command
from django.utils import timezone

from .models import JobRun


# --- Backup encryption -------------------------------------------------------------------------------

# Encrypt backups in chunks so large dumps never have to fit in memory.

MAGIC = b'RENTALS-BACKUP-1\n'
CHUNK = 8 * 1024 * 1024


class BackupKeyError(Exception):
    pass


def encrypt_file(src, dst, key):
    fernet = Fernet(key)
    with open(src, 'rb') as fin, open(dst, 'wb') as fout:
        fout.write(MAGIC)
        while chunk := fin.read(CHUNK):
            token = fernet.encrypt(chunk)
            fout.write(struct.pack('>I', len(token)))
            fout.write(token)


def decrypt_file(src, dst, key):
    fernet = Fernet(key)
    with open(src, 'rb') as fin, open(dst, 'wb') as fout:
        if fin.read(len(MAGIC)) != MAGIC:
            raise BackupKeyError(f'{src} is not an encrypted backup made by this app.')
        while header := fin.read(4):
            (length,) = struct.unpack('>I', header)
            try:
                fout.write(fernet.decrypt(fin.read(length)))
            except InvalidToken:
                raise BackupKeyError('Wrong BACKUP_ENCRYPTION_KEY, or the file is damaged.') from None


# --- Scheduled jobs ----------------------------------------------------------------------------------

# Scheduled jobs, run by `python manage.py run_scheduler` (one instance per deployment).

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Job:
    name: str
    command: str
    every: datetime.timedelta | None = None  # run at this interval…
    daily_at: datetime.time | None = None    # …or once a day at this local time

    def is_due(self, now, last_started):
        if self.every is not None:
            return last_started is None or now - last_started >= self.every
        local_now = timezone.localtime(now)
        today_slot = timezone.make_aware(datetime.datetime.combine(local_now.date(), self.daily_at))
        if local_now < today_slot:
            return False
        return last_started is None or last_started < today_slot


JOBS = [
    Job('check_pending_stk', 'check_pending_stk', every=datetime.timedelta(minutes=5)),
    Job('send_announcements', 'send_announcements', every=datetime.timedelta(minutes=1)),
    Job('backup_db', 'backup_db', daily_at=datetime.time(2, 0)),
    Job('clearsessions', 'clearsessions', daily_at=datetime.time(3, 0)),
    Job('generate_rent_charges', 'generate_rent_charges', daily_at=datetime.time(6, 0)),
    # Daily, but each landlord gets last month's report card once (normally on the 1st).
    Job('send_report_cards', 'send_report_cards', daily_at=datetime.time(7, 0)),
    Job('send_move_out_reminders', 'send_move_out_reminders', daily_at=datetime.time(8, 0)),
    Job('send_rent_reminders', 'send_rent_reminders', daily_at=datetime.time(9, 0)),
]


def run_job(job, now=None):
    """Run one job and record the outcome. Returns True on success."""
    now = now or timezone.now()
    # Guard against a second scheduler running the same job at the same time.
    lock = f'ops:job-lock:{job.name}'
    if not cache.add(lock, 1, 60 * 60):
        logger.warning('Skipping %s: already running elsewhere', job.name)
        return False
    record, _ = JobRun.objects.get_or_create(name=job.name)
    record.last_started_at = now
    record.save(update_fields=['last_started_at'])
    out = io.StringIO()
    try:
        call_command(job.command, stdout=out, stderr=out)
        ok = True
    except Exception:
        logger.exception('Scheduled job %s failed', job.name)
        ok = False
    finally:
        cache.delete(lock)
    record.last_finished_at = timezone.now()
    record.last_ok = ok
    record.last_output = out.getvalue()[-4000:]
    record.save(update_fields=['last_finished_at', 'last_ok', 'last_output'])
    logger.info('Job %s %s: %s', job.name, 'ok' if ok else 'FAILED', out.getvalue().strip()[:300])
    return ok


def run_due_jobs(now=None):
    now = now or timezone.now()
    last = dict(JobRun.objects.values_list('name', 'last_started_at'))
    ran = []
    for job in JOBS:
        if job.is_due(now, last.get(job.name)):
            run_job(job, now)
            ran.append(job.name)
    cache.set('ops:scheduler-heartbeat', now.isoformat(), 60 * 60)
    return ran


# --- Middleware --------------------------------------------------------------------------------------

class SessionMiddleware(DjangoSessionMiddleware):
    """Django's session middleware, except background requests can opt out of counting as activity.

    With SESSION_SAVE_EVERY_REQUEST every request pushes the idle-logout deadline back. A view
    sets request.passive_session = True (e.g. the dashboard's live polling) so that automatic
    background requests don't keep an unattended session signed in forever.
    """

    def process_response(self, request, response):
        if getattr(request, 'passive_session', False) and not request.session.modified:
            return response
        return super().process_response(request, response)
