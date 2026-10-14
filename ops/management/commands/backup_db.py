import datetime
import os
import sqlite3
import subprocess

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = 'Back up the database to BACKUP_DIR and delete backups older than BACKUP_KEEP_DAYS.'

    def handle(self, *args, **options):
        backup_dir = settings.BACKUP_DIR
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        db = settings.DATABASES['default']

        if connection.vendor == 'postgresql':
            path = backup_dir / f'rentals-{stamp}.dump'
            env = {**os.environ, 'PGPASSWORD': str(db.get('PASSWORD') or '')}
            cmd = ['pg_dump', '--format=custom', '--no-owner', '--file', str(path),
                   '--host', str(db.get('HOST') or 'localhost'), '--port', str(db.get('PORT') or 5432),
                   '--username', str(db.get('USER') or ''), str(db['NAME'])]
            result = subprocess.run(cmd, env=env, capture_output=True, text=True)
            if result.returncode != 0:
                path.unlink(missing_ok=True)
                raise CommandError(f'pg_dump failed: {result.stderr.strip()}')
        elif connection.vendor == 'sqlite':
            path = backup_dir / f'rentals-{stamp}.sqlite3'
            with sqlite3.connect(db['NAME']) as source, sqlite3.connect(path) as target:
                source.backup(target)
        else:
            raise CommandError(f'Backups are not implemented for {connection.vendor}.')

        cutoff = datetime.datetime.now() - datetime.timedelta(days=settings.BACKUP_KEEP_DAYS)
        removed = 0
        for old in backup_dir.glob('rentals-*'):
            if old != path and datetime.datetime.fromtimestamp(old.stat().st_mtime) < cutoff:
                old.unlink()
                removed += 1
        size_kb = path.stat().st_size / 1024
        self.stdout.write(self.style.SUCCESS(
            f'Backed up to {path} ({size_kb:,.0f} KB); removed {removed} old backup(s).'
        ))
