from django.core.management.base import BaseCommand

from notifications.utils import send_stale_reminders


class Command(BaseCommand):
    help = "Remind landlords whose records haven't been updated for a month, weekly until they are (run daily)."

    def handle(self, *args, **options):
        self.stdout.write(f'Sent {send_stale_reminders()} stale-records reminder(s).')
