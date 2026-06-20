"""One-time correction: 0017 attributed all legacy Sales data to the first
non-VRL company (MMDA), but VRL ("VISTARA GROUP") is the real operating company
that owns the sales team, projects and leads. Move the mis-attributed data back
to VRL. No-op on any database that doesn't have both companies (tests/CI/other
deployments).
"""
from django.db import migrations


def reassign(apps, schema_editor):
    Company        = apps.get_model('companies', 'Company')
    Lead           = apps.get_model('sales', 'Lead')
    Project        = apps.get_model('sales', 'Project')
    LeadSource     = apps.get_model('sales', 'LeadSource')
    DistributionLog = apps.get_model('sales', 'DistributionLog')
    MetaFormMapping = apps.get_model('sales', 'MetaFormMapping')
    MetaWebhookConfig = apps.get_model('sales', 'MetaWebhookConfig')

    vrl  = Company.objects.filter(code__iexact='VRL').first()
    mmda = Company.objects.filter(code__iexact='MMDA').first()
    if not vrl or not mmda:
        return  # Nothing to correct on this database.

    # Simple bulk moves (no unique constraints in the way).
    Lead.objects.filter(company=mmda).update(company=vrl)
    Project.objects.filter(company=mmda).update(company=vrl)
    DistributionLog.objects.filter(company=mmda).update(company=vrl)
    MetaFormMapping.objects.filter(company=mmda).update(company=vrl)

    # LeadSource has unique_together (company, name) — merge on name collisions.
    for src in LeadSource.objects.filter(company=mmda):
        existing = LeadSource.objects.filter(company=vrl, name=src.name).first()
        if existing:
            Lead.objects.filter(source=src).update(source=existing)
            src.delete()
        else:
            src.company = vrl
            src.save(update_fields=['company'])

    # MetaWebhookConfig is OneToOne(company) — move only if VRL has none.
    mmda_cfg = MetaWebhookConfig.objects.filter(company=mmda).first()
    if mmda_cfg:
        if MetaWebhookConfig.objects.filter(company=vrl).exists():
            mmda_cfg.delete()
        else:
            mmda_cfg.company = vrl
            mmda_cfg.save(update_fields=['company'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0017_backfill_company'),
    ]

    operations = [
        migrations.RunPython(reassign, noop),
    ]
