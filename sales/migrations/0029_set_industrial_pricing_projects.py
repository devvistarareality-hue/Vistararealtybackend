from django.db import migrations

# Projects that use the Industrial pricing/formula set instead of the default Kalrav.
INDUSTRIAL_PROJECT_NAMES = ['VIP ORAN', 'Tundav']


def set_industrial(apps, schema_editor):
    Project = apps.get_model('sales', 'Project')
    Project.objects.filter(name__in=INDUSTRIAL_PROJECT_NAMES).update(formula_set='industrial')


def revert_to_kalrav(apps, schema_editor):
    Project = apps.get_model('sales', 'Project')
    Project.objects.filter(name__in=INDUSTRIAL_PROJECT_NAMES).update(formula_set='kalrav')


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0028_ensure_project_approver_email'),
    ]

    operations = [
        migrations.RunPython(set_industrial, revert_to_kalrav),
    ]
