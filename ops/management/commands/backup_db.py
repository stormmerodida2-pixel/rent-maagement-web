import datetime
import os
import sqlite3
import subprocess

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from ops.utils import encrypt_file


class Command(BaseCommand):
    help = (
        'Back up the database to BACKUP_DIR, encrypted with BACKUP_ENCRYPTION_KEY when set, and delete '
        'backups older than BACKUP_KEEP_DAYS. Restore with decrypt_backup, then pg_restore.'
    )

    def handle(self, *args, **options):
        backup_dir = settings.BACKUP_DIR
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        suffix = 'dump' if connection.vendor == 'postgresql' else 'sqlite3'
        # Unencrypted data only ever lives in this hidden temp file, readable by us alone.
        plain = backup_dir / f'.incomplete-{stamp}.{suffix}'
        key = settings.BACKUP_ENCRYPTION_KEY
        final = backup_dir / (f'rentals-{stamp}.{suffix}.enc' if key else f'rentals-{stamp}.{suffix}')
        try:
            self._dump(plain)
            os.chmod(plain, 0o600)
            if key:
                encrypt_file(plain, final, key)
                plain.unlink()
            else:
                plain.rename(final)
                self.stderr.write('BACKUP_ENCRYPTION_KEY is not set: this backup is NOT encrypted.')
            os.chmod(final, 0o600)
        except BaseException:
            plain.unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            raise

        cutoff = datetime.datetime.now() - datetime.timedelta(days=settings.BACKUP_KEEP_DAYS)
        removed = 0
        for old in backup_dir.glob('rentals-*'):
            if old != final and datetime.datetime.fromtimestamp(old.stat().st_mtime) < cutoff:
                old.unlink()
                removed += 1
        size_kb = final.stat().st_size / 1024
        self.stdout.write(self.style.SUCCESS(
            f"Backed up to {final} ({size_kb:,.0f} KB, {'encrypted' if key else 'NOT encrypted'}); "
            f'removed {removed} old backup(s).'
        ))

    def _dump(self, path):
        db = settings.DATABASES['default']
        if connection.vendor == 'postgresql':
            env = {**os.environ, 'PGPASSWORD': str(db.get('PASSWORD') or '')}
            cmd = ['pg_dump', '--format=custom', '--no-owner', '--file', str(path),
                   '--host', str(db.get('HOST') or 'localhost'), '--port', str(db.get('PORT') or 5432),
                   '--username', str(db.get('USER') or ''), str(db['NAME'])]
            result = subprocess.run(cmd, env=env, capture_output=True, text=True)
            if result.returncode != 0:
                raise CommandError(f'pg_dump failed: {result.stderr.strip()}')
        elif connection.vendor == 'sqlite':
            with sqlite3.connect(db['NAME']) as source, sqlite3.connect(path) as target:
                source.backup(target)
        else:
            raise CommandError(f'Backups are not implemented for {connection.vendor}.')
