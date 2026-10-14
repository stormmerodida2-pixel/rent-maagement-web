import datetime

from django.core.management.base import BaseCommand

from rentals.billing import apply_late_fees
from rentals.models import Lease


class Command(BaseCommand):
    help = (
        "Bill this month's rent and service charges for every active lease, then add late fees "
        'where rent is unpaid past the grace period. Safe to run repeatedly; run daily from cron.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--month', help='Month to bill as YYYY-MM (default: current month).')
        parser.add_argument('--no-late-fees', action='store_true', help='Only bill rent.')

    def handle(self, *args, **options):
        if options['month']:
            period = datetime.datetime.strptime(options['month'], '%Y-%m').date()
        else:
            period = datetime.date.today().replace(day=1)

        leases = Lease.objects.filter(is_active=True)
        created = 0
        for lease in leases:
            _, was_created = lease.ensure_charge_for(period)
            created += was_created
        self.stdout.write(self.style.SUCCESS(f'Created {created} rent charge(s) for {period:%B %Y}.'))

        if not options['no_late_fees'] and not options['month']:
            fees = apply_late_fees(leases.filter(late_fee__gt=0))
            self.stdout.write(self.style.SUCCESS(f'Added {len(fees)} late fee(s).'))
