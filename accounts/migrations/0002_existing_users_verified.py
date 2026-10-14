from django.db import migrations
from django.utils import timezone


def mark_existing_users_verified(apps, schema_editor):
    """Accounts created before email verification existed were set up by an admin; trust them."""
    User = apps.get_model('auth', 'User')
    Profile = apps.get_model('accounts', 'Profile')
    now = timezone.now()
    for user in User.objects.filter(profile__isnull=True):
        Profile.objects.create(user=user, email_verified_at=now)


class Migration(migrations.Migration):
    dependencies = [('accounts', '0001_initial')]
    operations = [migrations.RunPython(mark_existing_users_verified, migrations.RunPython.noop)]
