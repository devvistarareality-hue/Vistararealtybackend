"""Seed every existing designation with the capabilities its title already implied.

Before this, powers came from matching text in the designation ("stm" in the title
meant the STM pipeline). Seeding from the same rules means nobody's access changes
on the day capabilities ship; companies can then edit their own designations.
"""
from django.db import migrations


def seed(apps, schema_editor):
    from accounts.capabilities import legacy_capabilities
    Designation = apps.get_model('accounts', 'Designation')
    for d in Designation.objects.all().iterator():
        caps = sorted(legacy_capabilities(d.name))
        Designation.objects.filter(pk=d.pk).update(capabilities=caps, capabilities_set=True)


def unseed(apps, schema_editor):
    Designation = apps.get_model('accounts', 'Designation')
    Designation.objects.all().update(capabilities=[], capabilities_set=False)


class Migration(migrations.Migration):
    dependencies = [('accounts', '0013_designation_capabilities')]
    operations = [migrations.RunPython(seed, unseed)]
