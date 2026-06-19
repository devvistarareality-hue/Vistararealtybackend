from django.db import migrations
from collections import Counter


def backfill_company(apps, schema_editor):
    Company        = apps.get_model('companies', 'Company')
    Lead           = apps.get_model('sales', 'Lead')
    Project        = apps.get_model('sales', 'Project')
    LeadSource     = apps.get_model('sales', 'LeadSource')
    DistributionLog = apps.get_model('sales', 'DistributionLog')
    MetaFormMapping = apps.get_model('sales', 'MetaFormMapping')
    MetaWebhookConfig = apps.get_model('sales', 'MetaWebhookConfig')

    # Default tenant = first non-VRL company, else first company overall.
    default = (
        Company.objects.exclude(code__iexact='VRL').order_by('id').first()
        or Company.objects.order_by('id').first()
    )
    if default is None:
        return  # No companies yet — nothing to backfill.

    def company_of_user(user):
        return user.company_id if user and user.company_id else None

    # ── Projects: infer from the companies of their leads' assigned users ──
    for project in Project.objects.filter(company__isnull=True):
        votes = Counter()
        for lead in Lead.objects.filter(project_id=project.id):
            cid = company_of_user(lead.telecaller) or company_of_user(lead.stm)
            if cid:
                votes[cid] += 1
        project.company_id = votes.most_common(1)[0][0] if votes else default.id
        project.save(update_fields=['company'])

    # ── Leads: telecaller → stm → project → default ──
    for lead in Lead.objects.filter(company__isnull=True).select_related('project'):
        cid = (
            company_of_user(lead.telecaller)
            or company_of_user(lead.stm)
            or (lead.project.company_id if lead.project_id and lead.project else None)
            or default.id
        )
        lead.company_id = cid
        lead.save(update_fields=['company'])

    # ── Lead sources: shared lookups → default company ──
    # (unique_together is now (company, name); collapse any name dupes safely)
    seen = set()
    for src in LeadSource.objects.filter(company__isnull=True).order_by('id'):
        if src.name in seen:
            src.delete()
            continue
        seen.add(src.name)
        src.company_id = default.id
        src.save(update_fields=['company'])

    # ── Distribution logs: triggered_by's company → default ──
    for log in DistributionLog.objects.filter(company__isnull=True):
        log.company_id = company_of_user(log.triggered_by) or default.id
        log.save(update_fields=['company'])

    # ── Meta form mappings: owning project's company → default ──
    for m in MetaFormMapping.objects.filter(company__isnull=True).select_related('project'):
        m.company_id = (m.project.company_id if m.project_id and m.project else None) or default.id
        m.save(update_fields=['company'])

    # ── Meta webhook config: default_project's company → default ──
    # OneToOne(company): keep at most one config per company, drop extras.
    used = set()
    for cfg in MetaWebhookConfig.objects.all().order_by('id'):
        cid = None
        if cfg.default_project_id:
            dp = Project.objects.filter(id=cfg.default_project_id).first()
            cid = dp.company_id if dp else None
        cid = cid or default.id
        if cid in used:
            cfg.delete()
            continue
        used.add(cid)
        cfg.company_id = cid
        cfg.save(update_fields=['company'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0016_distributionlog_company_lead_company_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_company, noop),
    ]
