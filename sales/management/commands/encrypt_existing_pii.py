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

from sales.fields import get_fernet, phone_blind_index
from accounts.models import User
from club1000.models import (FollowUp as C1FollowUp, Investor,
                             Lead as C1Lead, LeadStatusHistory as C1History,
                             Payout, ReferralReward)
from companies.models import Company
from sales.models import (Booking, Closure, FollowUp, Lead, LeadStatusHistory,
                          Plot, Project, SiteVisit)

# Only fields that no query filters on — see the audit in test_tenant_isolation
# and the field-classification pass. Encrypting a field the ORM looks up by value
# would silently break that lookup, since Fernet is non-deterministic.
TARGETS = [
    # Lead.name/phone/email are encrypted too; phone stays findable through the
    # phone_key blind index, which save() keeps in step.
    (Lead,              ['name', 'phone', 'email', 'alt_phone', 'address',
                         'telecaller_remarks', 'stm_remarks',
                         'meta_adset_name', 'meta_ad_name']),
    (Booking,           ['client_name', 'address', 'cp_name', 'manual_stm_name']),
    (Closure,           ['client_name', 'client_phone', 'remarks']),
    (SiteVisit,         ['remarks']),
    (FollowUp,          ['remarks']),
    (LeadStatusHistory, ['remarks']),
    (Plot,              ['notes']),
    (Project,           ['approver_email']),
    # Every other module, same rule: only fields nothing looks up by value.
    (Company,           ['address', 'phone', 'email']),
    (User,              ['phone']),
    (C1Lead,            ['name', 'phone', 'alt_phone', 'email',
                         'reference_name', 'reference_phone', 'remarks']),
    (Investor,          ['name', 'phone', 'email', 'pan', 'notes',
                         'reference_name', 'reference_phone']),
    (C1History,         ['remarks']),
    (C1FollowUp,        ['remarks']),
    (Payout,            ['notes']),
    (ReferralReward,    ['reference_name', 'reference_phone']),
]

# Models carrying a phone blind index that must be recomputed from the plaintext.
KEYED = (Lead, C1Lead, Investor)

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

        if dry:
            # Count what is genuinely still plaintext. Reading through the ORM would
            # decrypt first and make every populated row look outstanding, so ask the
            # database for the stored bytes: Fernet tokens all begin 'gAAAAA'.
            from django.db import connection
            outstanding = 0
            with connection.cursor() as cur:
                for model, fields in TARGETS:
                    table = model._meta.db_table
                    n = 0
                    for f in fields:
                        col = model._meta.get_field(f).column
                        cur.execute(
                            f'SELECT count(*) FROM {table} '
                            f"WHERE {col} IS NOT NULL AND {col} <> '' AND {col} NOT LIKE 'gAAAAA%%'"
                        )
                        n += cur.fetchone()[0]
                    outstanding += n
                    self.stdout.write(f'  {model.__name__:<20} {n:>6} values still plaintext')
            missing_key = sum(m.objects.filter(phone_key='').exclude(phone='').count() for m in KEYED)
            if missing_key:
                self.stdout.write(f'  {"phone_key":<20} {missing_key:>6} rows missing a blind index')
                outstanding += missing_key
            self.stdout.write(self.style.SUCCESS(
                f'would encrypt {outstanding} values'
                if outstanding else 'nothing to do — every target value is already encrypted'))
            return

        for model, fields in TARGETS:
            qs = model.objects.all().only('pk', *fields).order_by('pk')
            n_rows = n_vals = 0
            buf = []
            for obj in qs.iterator(chunk_size=batch):
                # A read gives plaintext whether the column holds plaintext or
                # ciphertext; re-saving is what writes ciphertext back.
                vals = [getattr(obj, f) for f in fields]
                if model in KEYED:
                    # Derive the lookup key from the decrypted attribute. Rows written
                    # before the column existed have none, and bulk paths may lag.
                    obj.phone_key = phone_blind_index(obj.phone)
                elif not any(v for v in vals):
                    continue
                n_rows += 1
                n_vals += sum(1 for v in vals if v)
                buf.append(obj)
                if not dry and len(buf) >= batch:
                    cols = fields + (['phone_key'] if model in KEYED else [])
                    with transaction.atomic():
                        model.objects.bulk_update(buf, cols)
                    buf = []
            if not dry and buf:
                cols = fields + (['phone_key'] if model in KEYED else [])
                with transaction.atomic():
                    model.objects.bulk_update(buf, cols)
            total_rows += n_rows
            total_vals += n_vals
            self.stdout.write(
                f'  {model.__name__:<20} {n_rows:>6} rows, {n_vals:>6} values '
                f'{"(dry run)" if dry else "encrypted"}'
            )

        verb = 'would encrypt' if dry else 'encrypted'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {total_vals} values across {total_rows} rows'))
