"""Accounts Receivable.

An approved booking (Sales AND Accounts) becomes an ARAccount. Its payment plan
is not stored here — it is read from the booking every time (installments plus
the "Legal & Other Charges" line), so a revision replaces the plan simply by the
account pointing at the new booking. What IS stored is money actually received:
one ARReceipt per payment, entered once. Allocation, interest, ageing and status
are all computed (see engine.py), never typed, so they cannot drift.

Amounts and remarks are encrypted at rest like every other money field in the
system; the engine aggregates in Python for that reason.
"""
from django.db import models
from django.conf import settings

from sales.fields import EncryptedDecimalField, EncryptedTextField


class ARAccount(models.Model):
    STATUS = [('active', 'Active'), ('frozen', 'Frozen')]

    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='ar_accounts')
    # The first booking of a revision chain identifies the deal for good…
    root_booking = models.OneToOneField('sales.Booking', on_delete=models.PROTECT, related_name='ar_account_root')
    # …and this is the latest fully-approved booking in that chain, whose plan applies.
    booking = models.ForeignKey('sales.Booking', on_delete=models.PROTECT, related_name='ar_accounts')
    status = models.CharField(max_length=10, choices=STATUS, default='active')
    frozen_at = models.DateTimeField(null=True, blank=True)
    # "Legal & Other Charges" are due at sale deed or possession, whichever is
    # earlier — unknown at booking time. No date means no interest until set.
    legal_due_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['company', 'status'])]

    def __str__(self):
        return f'AR {self.pk} · booking {self.booking_id}'


class ARReceipt(models.Model):
    MODES = [('bank', 'Bank'), ('nbfc', 'NBFC'), ('cash', 'Cash'), ('cheque', 'Cheque')]
    SOURCES = [('manual', 'Entered'), ('import', 'Excel import')]

    account = models.ForeignKey(ARAccount, on_delete=models.CASCADE, related_name='receipts')
    paid_on = models.DateField()
    amount = EncryptedDecimalField(max_digits=16, decimal_places=2)
    mode = models.CharField(max_length=10, choices=MODES)
    remarks = EncryptedTextField(blank=True)
    source = models.CharField(max_length=10, choices=SOURCES, default='manual')
    # Soft delete: a removed receipt stays in the audit trail, never vanishes.
    is_deleted = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['paid_on', 'id']
        indexes = [models.Index(fields=['account', 'is_deleted', 'paid_on'])]


class ARReceiptAudit(models.Model):
    """Every create / edit / delete of a receipt: who, when, and the values before
    and after. Money is involved, so nothing about a receipt changes silently."""
    ACTIONS = [('create', 'Created'), ('update', 'Edited'), ('delete', 'Deleted')]

    receipt = models.ForeignKey(ARReceipt, on_delete=models.CASCADE, related_name='audit')
    action = models.CharField(max_length=10, choices=ACTIONS)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    changed_at = models.DateTimeField(auto_now_add=True)
    before = EncryptedTextField(blank=True)   # JSON snapshot
    after = EncryptedTextField(blank=True)    # JSON snapshot

    class Meta:
        ordering = ['-changed_at']
