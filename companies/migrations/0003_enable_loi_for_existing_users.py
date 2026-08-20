"""Grandfather the LOI entitlement for companies already using it.

loi_enabled defaults to False so a newly onboarded company cannot raise booking
documents. Applying that to an existing installation would switch the feature off
for whoever is already relying on it, so this enables it for any company that has
at least one booking carrying an LOI/EOI document.

Data-driven rather than naming a company: the database already knows who uses the
feature, and hardcoding a code here would be wrong the moment it changed.
"""
from django.db import migrations


def enable_for_current_users(apps, schema_editor):
    Company = apps.get_model('companies', 'Company')
    Booking = apps.get_model('sales', 'Booking')
    ids = (Booking.objects
           .exclude(loi_document='')
           .exclude(loi_document=None)
           .values_list('company_id', flat=True)
           .distinct())
    Company.objects.filter(id__in=[i for i in ids if i]).update(loi_enabled=True)


def disable_all(apps, schema_editor):
    apps.get_model('companies', 'Company').objects.update(loi_enabled=False)


class Migration(migrations.Migration):

    dependencies = [
        ('companies', '0002_company_loi_enabled'),
        ('sales', '0057_lead_phone_key_alter_lead_email_alter_lead_name_and_more'),
    ]

    operations = [
        migrations.RunPython(enable_for_current_users, disable_all),
    ]
