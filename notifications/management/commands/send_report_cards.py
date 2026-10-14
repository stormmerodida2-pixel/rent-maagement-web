import datetime

from django.core.management.base import BaseCommand

from notifications.report_cards import previous_month, send_all


class Command(BaseCommand):
    help = "Send each landlord last month's report card (email/SMS). Safe to run daily: each goes out once."

    def add_arguments(self, parser):
        parser.add_argument('--month', help='Month to report on, YYYY-MM (default: last month).')

    def handle(self, *args, **options):
        month = (datetime.datetime.strptime(options['month'], '%Y-%m').date() if options['month']
                 else previous_month())
        self.stdout.write(f'Sent {send_all(month)} report card(s) for {month:%B %Y}.')
