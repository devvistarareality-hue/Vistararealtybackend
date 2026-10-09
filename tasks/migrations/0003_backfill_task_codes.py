"""Give the tasks that predate the reference field one.

Numbered oldest-first within each company, so the sequence reads as the order
the work was actually created rather than whatever order the rows come back in.
Reversible to blank: the column is the only thing this adds.
"""
from django.db import migrations


def fill(apps, schema_editor):
    Task = apps.get_model('tasks', 'Task')
    seen = {}
    for task in Task.objects.filter(code='').order_by('company_id', 'created_at', 'id'):
        n = seen.get(task.company_id)
        if n is None:
            # Start after any code already issued for this company, so a backfill
            # run alongside live traffic cannot collide with a fresh task.
            n = Task.objects.filter(company_id=task.company_id).exclude(code='').count()
        n += 1
        seen[task.company_id] = n
        task.code = f'TSK-{n:03d}'
        task.save(update_fields=['code'])


def unfill(apps, schema_editor):
    apps.get_model('tasks', 'Task').objects.update(code='')


class Migration(migrations.Migration):
    dependencies = [('tasks', '0002_task_code_task_uniq_task_code_per_company')]
    operations = [migrations.RunPython(fill, unfill)]
