"""StaffMember moved here from the old `staff` app.

Databases that already have its table (staff_staffmember) keep their rows: the model is added to
accounts without touching the database, the table is created only if it isn't there (a new database),
and then renamed to accounts_staffmember.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

OLD_TABLE = 'staff_staffmember'


def create_table_if_missing(apps, schema_editor):
    model = apps.get_model('accounts', 'StaffMember')
    if OLD_TABLE not in schema_editor.connection.introspection.table_names():
        schema_editor.create_model(model)


def drop_old_app_records(apps, schema_editor):
    """The old app's content type and permissions now point at nothing."""
    apps.get_model('contenttypes', 'ContentType').objects.filter(app_label='staff').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0004_two_factor'),
        ('contenttypes', '0002_remove_content_type_name'),
        ('rentals', '0007_tenant_language'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name='StaffMember',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('phone', models.CharField(blank=True, max_length=20)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='staff_members', to=settings.AUTH_USER_MODEL)),
                        ('properties', models.ManyToManyField(blank=True, help_text='Leave all unticked to cover every property, including ones added later.', related_name='staff_members', to='rentals.property')),
                        ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='staff_membership', to=settings.AUTH_USER_MODEL)),
                    ],
                    options={
                        'ordering': ['user__first_name', 'user__username'],
                        'db_table': OLD_TABLE,
                    },
                ),
            ],
            database_operations=[],
        ),
        migrations.RunPython(create_table_if_missing, migrations.RunPython.noop),
        migrations.AlterModelTable(name='staffmember', table=None),
        migrations.RunPython(drop_old_app_records, migrations.RunPython.noop),
    ]
