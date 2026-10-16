from collections import Counter

from django.db import migrations


def copy_phones(apps, schema_editor):
    """Landlords who gave a phone number for SMS can log in with it too (when nobody else has it)."""
    Prefs = apps.get_model('notifications', 'LandlordPreferences')
    Profile = apps.get_model('accounts', 'Profile')
    rows = list(Prefs.objects.exclude(phone='').values_list('user_id', 'phone'))
    counts = Counter(phone for _, phone in rows)
    taken = set(Profile.objects.exclude(phone=None).values_list('phone', flat=True))
    for user_id, phone in rows:
        if counts[phone] == 1 and phone not in taken and len(phone) == 12 and phone.startswith('254'):
            profile, _ = Profile.objects.get_or_create(user_id=user_id)
            if not profile.phone:
                profile.phone = phone
                profile.save(update_fields=['phone'])


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0006_login_phone'),
        ('notifications', '0003_alter_smsmessage_kind_landlordpreferences_and_more'),
    ]

    operations = [migrations.RunPython(copy_phones, migrations.RunPython.noop)]
