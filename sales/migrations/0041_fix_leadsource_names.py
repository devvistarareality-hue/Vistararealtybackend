from django.db import migrations


def fix_sources(apps, schema_editor):
    LeadSource = apps.get_model('sales', 'LeadSource')

    # Rename any case variant of "referral" → "Reference"
    for src in LeadSource.objects.filter(name__iexact='referral'):
        conflict = LeadSource.objects.filter(company_id=src.company_id, name='Reference').exclude(pk=src.pk).first()
        if conflict:
            src.delete()
        else:
            src.name = 'Reference'
            src.save()

    # Normalise any case variant of "other" → "Other"
    for src in LeadSource.objects.filter(name__iexact='other'):
        if src.name != 'Other':
            conflict = LeadSource.objects.filter(company_id=src.company_id, name='Other').exclude(pk=src.pk).first()
            if conflict:
                src.delete()
            else:
                src.name = 'Other'
                src.save()

    # Add "Channel Partner" for every company that has any sources (if missing)
    company_ids = list(LeadSource.objects.values_list('company_id', flat=True).distinct())
    for company_id in company_ids:
        if not LeadSource.objects.filter(company_id=company_id, name__iexact='channel partner').exists():
            LeadSource.objects.create(company_id=company_id, name='Channel Partner', is_active=True)


def reverse_fix(apps, schema_editor):
    LeadSource = apps.get_model('sales', 'LeadSource')
    LeadSource.objects.filter(name='Reference').update(name='referral')
    LeadSource.objects.filter(name='Other').update(name='other')
    LeadSource.objects.filter(name='Channel Partner').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0040_leadsource_rename_referral_add_channel_other'),
    ]

    operations = [
        migrations.RunPython(fix_sources, reverse_code=reverse_fix),
    ]
