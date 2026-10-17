from django.core.management.base import BaseCommand

from payments.utils import create_recurring_expenses


class Command(BaseCommand):
    help = "Add this month's copy of each expense set to repeat monthly (e.g. caretaker pay)."

    def handle(self, *args, **options):
        self.stdout.write(f'{create_recurring_expenses()} repeating expense(s) added.')
