import django.db.models.deletion
from django.db import migrations, models


def fill_property(apps, schema_editor):
    MaintenanceRequest = apps.get_model('maintenance', 'MaintenanceRequest')
    for request in MaintenanceRequest.objects.select_related('unit').iterator():
        request.property_id = request.unit.property_id
        request.save(update_fields=['property'])


class Migration(migrations.Migration):

    dependencies = [
        ('maintenance', '0001_initial'),
        ('rentals', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='maintenancerequest',
            name='property',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE,
                                    related_name='maintenance_requests', to='rentals.property'),
        ),
        migrations.RunPython(fill_property, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='maintenancerequest',
            name='property',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                    related_name='maintenance_requests', to='rentals.property'),
        ),
        migrations.AlterField(
            model_name='maintenancerequest',
            name='unit',
            field=models.ForeignKey(blank=True, help_text='Leave blank for general maintenance affecting the whole property.',
                                    null=True, on_delete=django.db.models.deletion.CASCADE,
                                    related_name='maintenance_requests', to='rentals.unit'),
        ),
    ]
