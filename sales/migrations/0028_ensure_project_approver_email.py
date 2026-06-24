from django.db import migrations


def ensure_approver_email(apps, schema_editor):
    """
    0024 added Project.approver_email to Django STATE only (the column already
    existed on the original DB), so a *fresh* database never gets the column.
    This idempotently adds it on any backend (Postgres, SQLite, …) by checking
    the live columns first — vendor-agnostic, unlike `ADD COLUMN IF NOT EXISTS`.
    """
    conn = schema_editor.connection
    with conn.cursor() as cursor:
        cols = [d.name for d in conn.introspection.get_table_description(cursor, 'sales_project')]
    if 'approver_email' not in cols:
        Project = apps.get_model('sales', 'Project')
        schema_editor.add_field(Project, Project._meta.get_field('approver_email'))


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0027_alter_booking_loi_document'),
    ]

    operations = [
        migrations.RunPython(ensure_approver_email, migrations.RunPython.noop),
    ]
