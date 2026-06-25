"""Encrypt (or re-encrypt) all confidential money values.

Idempotent: reads tolerate plaintext + ciphertext, so it's safe to run any time
— e.g. if the FIELD_ENCRYPTION_KEY was provisioned after migrating.
"""
import os
from django.core.management.base import BaseCommand
from sales.models import Booking, Closure

BOOKING_FIELDS = [
    'land_rate', 'dev_rate', 'const_rate', 'sale_deed_rate', 'dev_agreement_rate', 'maint_rate',
    'plot_basic', 'plot_dev', 'const_amt', 'sale_deed', 'dev_agreement', 'land_sale_deed',
    'const_agreement', 'stamp_duty', 'reg_fees', 'gst', 'maintenance', 'maint_deposit',
    'maint_advance', 'legal_charges', 'premium_location', 'total_extra', 'discount',
    'final_amount', 'extra_work_amount',
]
CLOSURE_FIELDS = ['booking_amount', 'total_amount']


class Command(BaseCommand):
    help = 'Encrypt confidential money values on Booking and Closure rows.'

    def handle(self, *args, **opts):
        if not os.getenv('FIELD_ENCRYPTION_KEY', '').strip():
            self.stderr.write('FIELD_ENCRYPTION_KEY is not set — refusing to run (would store plaintext).')
            return
        nb = 0
        for b in Booking.objects.all().iterator():
            b.save(update_fields=BOOKING_FIELDS)
            nb += 1
        nc = 0
        for c in Closure.objects.all().iterator():
            c.save(update_fields=CLOSURE_FIELDS)
            nc += 1
        self.stdout.write(self.style.SUCCESS(f'Encrypted {nb} bookings and {nc} closures.'))
