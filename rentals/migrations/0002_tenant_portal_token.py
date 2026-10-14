import secrets

from django.db import migrations, models

import rentals.models


def fill_tokens(apps, schema_editor):
    Tenant = apps.get_model('rentals', 'Tenant')
    for tenant in Tenant.objects.filter(portal_token__isnull=True):
        tenant.portal_token = secrets.token_urlsafe(16)
        tenant.save(update_fields=['portal_token'])


class Migration(migrations.Migration):

    dependencies = [
        ('rentals', '0001_initial'),
    ]

    # Add nullable, give existing tenants distinct tokens, then make it unique and required.
    operations = [
        migrations.AddField(
            model_name='tenant',
            name='portal_token',
            field=models.CharField(editable=False, max_length=32, null=True),
        ),
        migrations.RunPython(fill_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='tenant',
            name='portal_token',
            field=models.CharField(default=rentals.models.new_portal_token, editable=False, help_text="Secret part of the tenant's self-service link.", max_length=32, unique=True),
        ),
    ]
