"""Encrypt the existing plaintext money values in place.

0030 changed the money columns to text; this re-saves every Booking/Closure so
get_prep_value() encrypts the value. No-ops if FIELD_ENCRYPTION_KEY isn't set
(then run `manage.py encrypt_money` after provisioning the key). Reads tolerate
plaintext + ciphertext, so this is safe to re-run / resume.
"""
import os
from django.db import migrations

BOOKING_FIELDS = [
    'land_rate', 'dev_rate', 'const_rate', 'sale_deed_rate', 'dev_agreement_rate', 'maint_rate',
    'plot_basic', 'plot_dev', 'const_amt', 'sale_deed', 'dev_agreement', 'land_sale_deed',
    'const_agreement', 'stamp_duty', 'reg_fees', 'gst', 'maintenance', 'maint_deposit',
    'maint_advance', 'legal_charges', 'premium_location', 'total_extra', 'discount',
    'final_amount', 'extra_work_amount',
]
CLOSURE_FIELDS = ['booking_amount', 'total_amount']


def encrypt_rows(apps, schema_editor):
    if not os.getenv('FIELD_ENCRYPTION_KEY', '').strip():
        return  # key not provisioned yet — leave plaintext, run encrypt_money later
    Booking = apps.get_model('sales', 'Booking')
    for b in Booking.objects.all().iterator():
        b.save(update_fields=BOOKING_FIELDS)
    Closure = apps.get_model('sales', 'Closure')
    for c in Closure.objects.all().iterator():
        c.save(update_fields=CLOSURE_FIELDS)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [('sales', '0030_alter_booking_const_agreement_and_more')]
    operations = [migrations.RunPython(encrypt_rows, noop_reverse)]
