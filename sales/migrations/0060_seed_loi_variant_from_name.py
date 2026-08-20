"""Seed loi_variant from the project names the LOI used to branch on.

The PDF builders decided their layout by comparing the project's *name* against
'tundav' and 'kalrav 3'. That is now a stored field, so this carries the current
behaviour across: the projects that render those layouts today keep rendering
them, and a later rename no longer changes anyone's paperwork.

Matching on name here is correct — it is exactly the rule being retired, applied
once — whereas leaving it in the render path would keep it live forever.
"""
from django.db import migrations


def seed(apps, schema_editor):
    Project = apps.get_model('sales', 'Project')
    for variant, name, formula in (('tundav', 'tundav', 'industrial'),
                                   ('kalrav3', 'kalrav 3', 'kalrav')):
        for p in Project.objects.filter(formula_set=formula):
            if (p.name or '').strip().lower() == name:
                p.loi_variant = variant
                p.save(update_fields=['loi_variant'])


def unseed(apps, schema_editor):
    apps.get_model('sales', 'Project').objects.update(loi_variant='')


class Migration(migrations.Migration):

    dependencies = [('sales', '0059_project_loi_variant')]

    operations = [migrations.RunPython(seed, unseed)]
