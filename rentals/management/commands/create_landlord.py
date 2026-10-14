from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError

LANDLORD_MODELS = {
    'rentals': ['property', 'unit', 'tenant', 'lease', 'rentcharge'],
    'payments': ['payment', 'mpesatransaction', 'c2bpayment'],
    'maintenance': ['maintenancerequest'],
    'notifications': ['smsmessage'],
}


def landlord_group():
    group, _ = Group.objects.get_or_create(name='Landlords')
    perms = Permission.objects.filter(
        content_type__app_label__in=LANDLORD_MODELS.keys(),
    )
    group.permissions.set(
        p for p in perms if p.content_type.model in LANDLORD_MODELS[p.content_type.app_label]
    )
    return group


class Command(BaseCommand):
    help = 'Create a landlord account that can log in to the dashboard and admin (sees only their own data).'

    def add_arguments(self, parser):
        parser.add_argument('username')
        parser.add_argument('--email', default='')

    def handle(self, *args, **options):
        User = get_user_model()
        if User.objects.filter(username=options['username']).exists():
            raise CommandError(f'User {options["username"]!r} already exists.')
        user = User.objects.create_user(options['username'], email=options['email'], is_staff=True)
        user.groups.add(landlord_group())
        self.stdout.write(self.style.SUCCESS(
            f'Created landlord {user.username!r}. Set a password with: '
            f'python manage.py changepassword {user.username}'
        ))
