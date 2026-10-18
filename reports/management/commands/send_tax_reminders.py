from django.core.management.base import BaseCommand

from reports.tax import send_tax_reminders


class Command(BaseCommand):
    help = "On the 15th, remind landlords whose last month's KRA rental income tax isn't marked filed (run daily)."

    def handle(self, *args, **options):
        self.stdout.write(f'Sent {send_tax_reminders()} tax reminder(s).')
