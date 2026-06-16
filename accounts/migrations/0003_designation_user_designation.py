from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_user_manager_modules_user_modules'),
        ('companies', '0001_initial'),
    ]

    operations = [
        # Use raw SQL with IF NOT EXISTS so this is safe whether or not
        # the column was already added by a previous partial deployment.
        migrations.RunSQL(
            sql="ALTER TABLE accounts_user ADD COLUMN IF NOT EXISTS designation VARCHAR(100) NOT NULL DEFAULT '';",
            reverse_sql="ALTER TABLE accounts_user DROP COLUMN IF EXISTS designation;",
        ),
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
