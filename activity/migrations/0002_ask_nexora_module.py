"""Ask Nexora questions are their own module in the Activity Log ("Ask Nexora"),
not filed under whichever screen they were asked from."""
from django.db import migrations


def forwards(apps, schema_editor):
    ActivityLog = apps.get_model('activity', 'ActivityLog')
    ActivityLog.objects.filter(target_type='ai question').update(module='Ask Nexora')


class Migration(migrations.Migration):
    dependencies = [('activity', '0001_activity_log')]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
