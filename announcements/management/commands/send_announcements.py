from django.core.management.base import BaseCommand

from announcements.sending import send_due


class Command(BaseCommand):
    help = 'Send scheduled announcements that are due (run every minute by the scheduler).'

    def handle(self, *args, **options):
        self.stdout.write(f'Sent {send_due()} scheduled announcement(s).')
