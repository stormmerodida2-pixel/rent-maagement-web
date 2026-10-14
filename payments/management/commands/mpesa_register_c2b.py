from django.core.management.base import BaseCommand, CommandError

from payments import mpesa
from payments.models import MpesaAccount


class Command(BaseCommand):
    help = (
        "Register C2B validation/confirmation URLs with Safaricom for landlords' M-Pesa accounts. "
        'Re-run after the public URL (MPESA_CALLBACK_BASE_URL) changes.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--owner', help='Only this landlord (username).')

    def handle(self, *args, **options):
        accounts = MpesaAccount.objects.select_related('owner')
        if options['owner']:
            accounts = accounts.filter(owner__username=options['owner'])
            if not accounts:
                raise CommandError(f"{options['owner']!r} has no M-Pesa account.")
        failed = 0
        for account in accounts:
            if not account.is_complete():
                self.stdout.write(f'Skipping {account.owner}: settings incomplete.')
                continue
            try:
                mpesa.register_c2b_urls(account)
            except mpesa.MpesaError as exc:
                failed += 1
                self.stderr.write(f'{account.owner}: {exc}')
            else:
                self.stdout.write(self.style.SUCCESS(f'{account.owner}: registered {account.pay_number}'))
        if failed:
            raise CommandError(f'{failed} account(s) failed.')
