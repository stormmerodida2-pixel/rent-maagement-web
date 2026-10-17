import datetime
import os
import sqlite3
import subprocess
import tarfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from ops.utils import OffsiteError, encrypt_file, upload_offsite


class Command(BaseCommand):
    help = (
        'Back up the database, and an archive of uploaded files (listing photos, receipts, meter photos), to '
        'BACKUP_DIR, encrypted with BACKUP_ENCRYPTION_KEY when set; copy them to the BACKUP_S3_BUCKET when set; '
        'and delete local backups older than BACKUP_KEEP_DAYS. Restore with decrypt_backup, then pg_restore '
        '(database) or tar -xzf (files).'
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

        files_final = self._backup_files(backup_dir, stamp, key)

        cutoff = datetime.datetime.now() - datetime.timedelta(days=settings.BACKUP_KEEP_DAYS)
        removed = 0
        for old in backup_dir.glob('rentals-*'):
            if old not in (final, files_final) and datetime.datetime.fromtimestamp(old.stat().st_mtime) < cutoff:
                old.unlink()
                removed += 1
        size_kb = final.stat().st_size / 1024
        self.stdout.write(self.style.SUCCESS(
            f"Backed up to {final} ({size_kb:,.0f} KB, {'encrypted' if key else 'NOT encrypted'}); "
            f'removed {removed} old backup(s).'
        ))
        if settings.BACKUP_S3_BUCKET:
            if not key:
                raise CommandError('BACKUP_S3_BUCKET is set but BACKUP_ENCRYPTION_KEY is not: backups are only '
                                   'copied off the server encrypted.')
            for path in filter(None, [final, files_final]):
                try:
                    object_key = upload_offsite(path)
                except OffsiteError as exc:
                    raise CommandError(f'{exc} The local backup is kept.') from exc
                self.stdout.write(self.style.SUCCESS(f'Copied off-site to s3://{settings.BACKUP_S3_BUCKET}/{object_key}.'))

    def _backup_files(self, backup_dir, stamp, key):
        """One archive of all uploaded files (public listing photos and private receipts/meter photos), encrypted
        like the database. Returns its path, or None when nothing has been uploaded yet."""
        roots = [(Path(settings.MEDIA_ROOT), 'media'), (Path(settings.PRIVATE_MEDIA_ROOT), 'private_media')]
        roots = [(root, name) for root, name in roots if root.is_dir() and any(p.is_file() for p in root.rglob('*'))]
        if not roots:
            return None
        plain = backup_dir / f'.incomplete-files-{stamp}.tar.gz'
        final = backup_dir / (f'rentals-files-{stamp}.tar.gz.enc' if key else f'rentals-files-{stamp}.tar.gz')
        try:
            with tarfile.open(plain, 'w:gz') as archive:
                for root, name in roots:
                    archive.add(root, arcname=name)
            os.chmod(plain, 0o600)
            if key:
                encrypt_file(plain, final, key)
                plain.unlink()
            else:
                plain.rename(final)
            os.chmod(final, 0o600)
        except BaseException:
            plain.unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            raise
        self.stdout.write(self.style.SUCCESS(f'Backed up uploaded files to {final} ({final.stat().st_size / 1024:,.0f} KB).'))
        return final

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
