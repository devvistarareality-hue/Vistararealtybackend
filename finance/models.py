from django.db import models
from accounts.models import User
from erp_master.models import ERPProject, WBSActivity, Material, Vendor, DocSequence
from purchase.models import POHeader, POLine
from inventory.models import GRNHeader, GRNLine

MATCH_STATUS_CHOICES = [
    ('Pending',  'Pending'),
    ('2-Way',    '2-Way Matched'),
    ('3-Way',    '3-Way Matched'),
    ('Approved', 'Approved'),
    ('Disputed', 'Disputed'),
]

PAYMENT_STATUS_CHOICES = [
    ('Pending', 'Pending'),
    ('Partial', 'Partial'),
    ('Paid',    'Paid'),
]


class VendorInvoice(models.Model):
    invoice_no     = models.CharField(max_length=30, unique=True)
    vendor         = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name='invoices')
    project        = models.ForeignKey(ERPProject, on_delete=models.PROTECT,
                                       related_name='vendor_invoices', db_index=True)
    po             = models.ForeignKey(POHeader, on_delete=models.PROTECT, related_name='invoices')
    invoice_date   = models.DateField()
    due_date       = models.DateField(null=True, blank=True)
    invoice_amount = models.DecimalField(max_digits=16, decimal_places=2)
    tax_amount     = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    total_amount   = models.DecimalField(max_digits=16, decimal_places=2)
    match_status   = models.CharField(max_length=20, choices=MATCH_STATUS_CHOICES, default='Pending')
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default='Pending')
    remarks        = models.TextField(blank=True)
    created_by     = models.ForeignKey(User, on_delete=models.PROTECT)
    created_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes  = [models.Index(fields=['project', 'match_status'])]

    def __str__(self):
        return self.invoice_no


class VendorInvoiceLine(models.Model):
    invoice      = models.ForeignKey(VendorInvoice, on_delete=models.CASCADE, related_name='lines')
    po_line      = models.ForeignKey(POLine, on_delete=models.PROTECT)
    grn_line     = models.ForeignKey(GRNLine, null=True, blank=True, on_delete=models.SET_NULL)
    activity     = models.ForeignKey(WBSActivity, on_delete=models.PROTECT, db_index=True)
    item_code    = models.ForeignKey(Material, on_delete=models.PROTECT)
    billed_qty   = models.DecimalField(max_digits=14, decimal_places=3)
    billed_rate  = models.DecimalField(max_digits=14, decimal_places=2)
    amount       = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    # 3-way match results
    rate_match   = models.BooleanField(default=False)
    qty_match    = models.BooleanField(default=False)
    match_note   = models.CharField(max_length=200, blank=True)

    class Meta:
        indexes = [models.Index(fields=['activity'])]

    def save(self, *args, **kwargs):
        self.amount = self.billed_qty * self.billed_rate
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.invoice.invoice_no} / Line {self.pk}'


class Payment(models.Model):
    PAYMENT_MODES = [
        ('NEFT',   'NEFT'),
        ('RTGS',   'RTGS'),
        ('Cheque', 'Cheque'),
        ('Cash',   'Cash'),
        ('UPI',    'UPI'),
    ]
    payment_no   = models.CharField(max_length=25, unique=True, editable=False)
    invoice      = models.ForeignKey(VendorInvoice, on_delete=models.PROTECT,
                                     related_name='payments')
    vendor       = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name='payments')
    payment_date = models.DateField()
    amount       = models.DecimalField(max_digits=16, decimal_places=2)
    payment_mode = models.CharField(max_length=20, choices=PAYMENT_MODES)
    reference_no = models.CharField(max_length=50, blank=True)
    remarks      = models.TextField(blank=True)
    created_by   = models.ForeignKey(User, on_delete=models.PROTECT)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.payment_no:
            self.payment_no = DocSequence.next_number(self.invoice.project.company, 'PAY')
        super().save(*args, **kwargs)
        # Update invoice payment status
        total_paid = sum(p.amount for p in self.invoice.payments.all())
        if total_paid >= self.invoice.total_amount:
            self.invoice.payment_status = 'Paid'
        elif total_paid > 0:
            self.invoice.payment_status = 'Partial'
        self.invoice.save(update_fields=['payment_status'])

    def __str__(self):
        return self.payment_no
