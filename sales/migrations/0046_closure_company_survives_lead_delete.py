"""Closures must outlive their lead.

Closure used to hang off the lead (FK CASCADE, no company of its own), so a Data
Reset that cleared Leads silently wiped every conversion while the Bookings — which
carry their own company FK and only SET_NULL their lead — survived. That left the
dashboard reading "0 closures / 113 bookings".

This gives Closure its own company FK plus a client name/phone snapshot, and
switches the lead FK to SET_NULL.
"""
from django.db import migrations, models
import django.db.models.deletion


def backfill(apps, schema_editor):
    Closure = apps.get_model('sales', 'Closure')
    rows = []
    for c in Closure.objects.select_related('lead').iterator():
        if not c.lead_id:
            continue
        c.company_id = c.lead.company_id
        c.client_name = c.client_name or (c.lead.name or '')
        c.client_phone = c.client_phone or (c.lead.phone or '')
        rows.append(c)
        if len(rows) >= 500:
            Closure.objects.bulk_update(rows, ['company', 'client_name', 'client_phone'])
            rows = []
    if rows:
        Closure.objects.bulk_update(rows, ['company', 'client_name', 'client_phone'])


class Migration(migrations.Migration):

    dependencies = [
        ('companies', '0001_initial'),
        ('sales', '0045_project_kiosk_enabled'),
    ]

    operations = [
        migrations.AddField(
            model_name='closure',
            name='company',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                    related_name='closures', to='companies.company'),
        ),
        migrations.AddField(
            model_name='closure',
            name='client_name',
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name='closure',
            name='client_phone',
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='closure',
            name='lead',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name='closures', to='sales.lead'),
        ),
    ]
