from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from accounts.models import TwoFactor


class Command(BaseCommand):
    help = "Turn off two-step login for someone who lost their phone and their recovery codes."

    def add_arguments(self, parser):
        parser.add_argument('username')

    def handle(self, username, **options):
        user = get_user_model().objects.filter(username=username).first()
        if user is None:
            raise CommandError(f'No user called {username!r}.')
        deleted, _ = TwoFactor.objects.filter(user=user).delete()
        self.stdout.write(f'Two-step login turned off for {username}. They can log in with their password and set '
                          'it up again under Settings.' if deleted else f'{username} did not have two-step login on.')
