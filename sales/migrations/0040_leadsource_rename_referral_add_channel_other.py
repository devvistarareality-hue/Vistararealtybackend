from django.db import migrations


def update_lead_sources(apps, schema_editor):
    LeadSource = apps.get_model('sales', 'LeadSource')

    # Rename Referral → Reference everywhere
    LeadSource.objects.filter(name='Referral').update(name='Reference')

    # For each company that already has sources, add Channel Partner and Other if missing
    company_ids = list(LeadSource.objects.values_list('company_id', flat=True).distinct())
    for company_id in company_ids:
        for name in ['Channel Partner', 'Other']:
            LeadSource.objects.get_or_create(
                company_id=company_id, name=name,
                defaults={'is_active': True},
            )


def reverse_update(apps, schema_editor):
    LeadSource = apps.get_model('sales', 'LeadSource')
    LeadSource.objects.filter(name='Reference').update(name='Referral')
    LeadSource.objects.filter(name__in=['Channel Partner', 'Other']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0039_booking_apply_page_fee'),
    ]

    operations = [
        migrations.RunPython(update_lead_sources, reverse_code=reverse_update),
    ]
