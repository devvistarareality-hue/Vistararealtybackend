from django.db import migrations, models
import django.db.models.deletion


def add_designation_column(apps, schema_editor):
    """Idempotently add accounts_user.designation for old partial deployments.

    The column is normally created by 0001_initial; this is only a safety patch
    for databases that were deployed before the field existed. Postgres-only
    syntax, so it is skipped on other backends (e.g. SQLite used by tests/CI),
    where 0001_initial already created the column.
    """
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(
            "ALTER TABLE accounts_user ADD COLUMN IF NOT EXISTS designation VARCHAR(100) NOT NULL DEFAULT '';"
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_user_manager_modules_user_modules'),
        ('companies', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(add_designation_column, noop),
        migrations.CreateModel(
            name='Designation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('module', models.CharField(max_length=100)),
                ('company', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='designations', to='companies.company')),
            ],
            options={
                'ordering': ['module', 'name'],
                'unique_together': {('company', 'name', 'module')},
            },
        ),
    ]
