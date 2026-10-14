import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from notifications.models import SmsMessage
from notifications.sms import reminder_text, send_rent_reminder
from rentals.models import Lease


class Command(BaseCommand):
    help = 'SMS every tenant with an outstanding balance (run daily from cron).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--min-days-between', type=int, default=3,
            help='Skip tenants who already got a reminder within this many days (default 3).',
        )
        parser.add_argument('--dry-run', action='store_true', help='Print messages instead of sending.')

    def handle(self, *args, **options):
        since = timezone.now() - datetime.timedelta(days=options['min_days_between'])
        recently_reminded = set(
            SmsMessage.objects.filter(
                kind=SmsMessage.Kind.REMINDER, created_at__gte=since,
            ).exclude(status=SmsMessage.Status.FAILED).values_list('lease_id', flat=True)
        )

        sent = failed = 0
        leases = Lease.objects.filter(is_active=True).select_related('tenant', 'unit')
        for lease in leases.exclude(pk__in=recently_reminded):
            balance = lease.balance
            if balance <= 0:
                continue
            if options['dry_run']:
                self.stdout.write(f'{lease.tenant.phone}: {reminder_text(lease, balance)}')
                continue
            msg = send_rent_reminder(lease, balance)
            if msg.status == SmsMessage.Status.FAILED:
                failed += 1
                self.stderr.write(f'Failed for {lease}: {msg.error}')
            else:
                sent += 1
        if not options['dry_run']:
            self.stdout.write(self.style.SUCCESS(f'Sent {sent} reminder(s), {failed} failed.'))
