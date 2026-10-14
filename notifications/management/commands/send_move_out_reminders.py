from django.core.management.base import BaseCommand

from notifications.utils import send_move_out_reminders


class Command(BaseCommand):
    help = 'Remind landlords a few days before a tenant moves out (run daily).'

    def handle(self, *args, **options):
        self.stdout.write(f'Sent {send_move_out_reminders()} move-out reminder(s).')
