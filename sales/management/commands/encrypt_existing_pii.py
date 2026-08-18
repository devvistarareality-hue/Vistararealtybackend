"""Encrypt PII rows written before their columns became EncryptedTextField.

The field encrypts on write and passes plaintext through on read, so a row only
becomes ciphertext once it is re-saved. Existing rows therefore stay readable but
plaintext until this runs.

Safe to re-run: get_prep_value leaves a value alone if it is already ciphertext.

    manage.py encrypt_existing_pii --dry-run     # report only, writes nothing
    manage.py encrypt_existing_pii               # encrypt
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from sales.fields import get_fernet
from sales.models import (Booking, Closure, FollowUp, Lead, LeadStatusHistory,
                          Plot, SiteVisit)

# Only fields that no query filters on — see the audit in test_tenant_isolation
# and the field-classification pass. Encrypting a field the ORM looks up by value
# would silently break that lookup, since Fernet is non-deterministic.
TARGETS = [
    (Lead,              ['alt_phone', 'address', 'telecaller_remarks', 'stm_remarks',
                         'meta_adset_name', 'meta_ad_name']),
    (Booking,           ['client_name', 'address', 'cp_name', 'manual_stm_name']),
    (Closure,           ['client_name', 'client_phone', 'remarks']),
    (SiteVisit,         ['remarks']),
    (FollowUp,          ['remarks']),
    (LeadStatusHistory, ['remarks']),
    (Plot,              ['notes']),
]

BATCH = 500


class Command(BaseCommand):
    help = 'Encrypt PII columns that still hold plaintext from before the field change.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would be encrypted, write nothing.')
        parser.add_argument('--batch', type=int, default=BATCH)

    def handle(self, *args, **opts):
        if get_fernet() is None:
            raise CommandError(
                'FIELD_ENCRYPTION_KEY is not set. Without it the fields pass through '
                'as plaintext and this command would report success having encrypted '
                'nothing. Set the key and re-run.'
            )
        dry, batch = opts['dry_run'], opts['batch']
        total_rows = total_vals = 0

        for model, fields in TARGETS:
            qs = model.objects.all().only('pk', *fields).order_by('pk')
            n_rows = n_vals = 0
            buf = []
            for obj in qs.iterator(chunk_size=batch):
                # A read gives plaintext whether the column holds plaintext or
                # ciphertext; re-saving is what writes ciphertext back.
                vals = [getattr(obj, f) for f in fields]
                if not any(v for v in vals):
                    continue
                n_rows += 1
                n_vals += sum(1 for v in vals if v)
                buf.append(obj)
                if not dry and len(buf) >= batch:
                    with transaction.atomic():
                        model.objects.bulk_update(buf, fields)
                    buf = []
            if not dry and buf:
                with transaction.atomic():
                    model.objects.bulk_update(buf, fields)
            total_rows += n_rows
            total_vals += n_vals
            self.stdout.write(
                f'  {model.__name__:<20} {n_rows:>6} rows, {n_vals:>6} values '
                f'{"(dry run)" if dry else "encrypted"}'
            )

        verb = 'would encrypt' if dry else 'encrypted'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {total_vals} values across {total_rows} rows'))
