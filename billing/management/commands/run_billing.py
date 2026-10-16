from django.core.management.base import BaseCommand

from billing.utils import run_billing


class Command(BaseCommand):
    help = "Make next month's Rentipay bills and send overdue and read-only notices (does nothing while billing is off)."

    def handle(self, *args, **options):
        counts = run_billing()
        self.stdout.write(f"{counts['bills']} bill(s) made, {counts['overdue']} overdue notice(s), "
                          f"{counts['locked']} read-only notice(s).")
