from django.db import migrations


def set_till(apps, schema_editor):
    """Rentipay's own Till, where landlords pay their bills (changeable in Billing overview)."""
    BillingSettings = apps.get_model('billing', 'BillingSettings')
    row, _ = BillingSettings.objects.get_or_create(pk=1)
    if not row.till:
        row.till = '9505957'
        row.save(update_fields=['till'])


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0002_billing_settings'),
    ]

    operations = [
        migrations.RunPython(set_till, migrations.RunPython.noop),
    ]
