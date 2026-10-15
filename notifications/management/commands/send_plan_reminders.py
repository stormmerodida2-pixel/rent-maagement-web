from django.core.management.base import BaseCommand

from notifications.utils import send_plan_reminders


class Command(BaseCommand):
    help = 'SMS tenants two days before each payment-plan instalment (run daily).'

    def handle(self, *args, **options):
        self.stdout.write(f'Sent {send_plan_reminders()} payment plan reminder(s).')
