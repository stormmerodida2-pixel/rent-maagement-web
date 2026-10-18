from django.core.management.base import BaseCommand

from rentals.rewards import give_rewards


class Command(BaseCommand):
    help = "Credit tenants who've paid on time enough months running under their landlord's reward (run daily)."

    def handle(self, *args, **options):
        self.stdout.write(f'Gave {give_rewards()} reward(s).')
