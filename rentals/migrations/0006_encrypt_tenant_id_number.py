from django.db import migrations

import payments.utils


def encrypt_existing(apps, schema_editor):
    Tenant = apps.get_model('rentals', 'Tenant')
    for tenant in Tenant.objects.exclude(id_number_plain=''):
        tenant.id_number = tenant.id_number_plain  # EncryptedTextField encrypts on save
        tenant.save(update_fields=['id_number'])


def decrypt_back(apps, schema_editor):
    Tenant = apps.get_model('rentals', 'Tenant')
    for tenant in Tenant.objects.exclude(id_number=''):
        tenant.id_number_plain = tenant.id_number[:50]
        tenant.save(update_fields=['id_number_plain'])


class Migration(migrations.Migration):
    """Encrypt tenants' ID numbers, including ones already stored.

    Rename the plain column aside, add the encrypted one, copy (encrypting) and drop the plain one.
    """

    dependencies = [('rentals', '0005_meterreading_void_reason_meterreading_voided_at_and_more')]

    operations = [
        migrations.RenameField('tenant', 'id_number', 'id_number_plain'),
        migrations.AddField(
            model_name='tenant', name='id_number',
            field=payments.utils.EncryptedTextField(blank=True, default='', verbose_name='national ID / passport'),
            preserve_default=False,
        ),
        migrations.RunPython(encrypt_existing, decrypt_back),
        migrations.RemoveField('tenant', 'id_number_plain'),
    ]
