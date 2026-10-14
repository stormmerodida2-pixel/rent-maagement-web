from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ops.utils import BackupKeyError, decrypt_file


class Command(BaseCommand):
    help = 'Decrypt a backup made by backup_db, ready for pg_restore. Needs the same BACKUP_ENCRYPTION_KEY.'

    def add_arguments(self, parser):
        parser.add_argument('backup', help='The .enc file')
        parser.add_argument('-o', '--output', help='Where to write the decrypted dump (default: without .enc)')

    def handle(self, *args, backup, output=None, **options):
        src = Path(backup)
        if not settings.BACKUP_ENCRYPTION_KEY:
            raise CommandError('Set BACKUP_ENCRYPTION_KEY to the key the backup was made with.')
        dst = Path(output) if output else src.with_suffix('')
        if dst == src:
            raise CommandError('Give an --output path different from the backup file.')
        try:
            decrypt_file(src, dst, settings.BACKUP_ENCRYPTION_KEY)
        except BackupKeyError as exc:
            dst.unlink(missing_ok=True)
            raise CommandError(str(exc)) from None
        dst.chmod(0o600)
        self.stdout.write(self.style.SUCCESS(f'Decrypted to {dst}. Delete it once you have restored it.'))
