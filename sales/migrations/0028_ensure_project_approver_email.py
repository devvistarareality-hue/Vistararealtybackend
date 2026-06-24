from django.db import migrations


class Migration(migrations.Migration):
    """
    Migration 0024 added Project.approver_email to Django STATE only
    (SeparateDatabaseAndState with empty database_operations), because the
    column already existed on the original database. On a *fresh* database the
    column is therefore never created, which breaks loaddata / inserts.

    This idempotently ensures the column exists. No state change — the state
    already has the field from 0024.
    """

    dependencies = [
        ('sales', '0027_alter_booking_loi_document'),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE sales_project ADD COLUMN IF NOT EXISTS approver_email varchar(254) NOT NULL DEFAULT '';",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
