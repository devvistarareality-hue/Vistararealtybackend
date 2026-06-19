from django.db import models
from accounts.models import User
from erp_master.models import ERPProject, WBSActivity, Material, Vendor, DocSequence
from purchase.models import POHeader, POLine

GRN_STATUS_CHOICES = [
    ('Pending QC', 'Pending QC'),
    ('QC Passed',  'QC Passed'),
    ('QC Failed',  'QC Failed'),
    ('Stocked',    'Stocked'),
]

QC_STATUS_CHOICES = [
    ('Pending',  'Pending'),
    ('Accepted', 'Accepted'),
    ('Rejected', 'Rejected'),
    ('Partial',  'Partial'),
]


class GRNHeader(models.Model):
    grn_no        = models.CharField(max_length=25, unique=True, editable=False)
    project       = models.ForeignKey(ERPProject, on_delete=models.PROTECT,
                                      related_name='grns', db_index=True)
    po            = models.ForeignKey(POHeader, on_delete=models.PROTECT, related_name='grns')
    vendor        = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name='grns')
    received_date = models.DateField()
    received_by   = models.ForeignKey(User, on_delete=models.PROTECT, related_name='grns_received')
    dc_no         = models.CharField(max_length=50, blank=True)
    vehicle_no    = models.CharField(max_length=30, blank=True)
    status        = models.CharField(max_length=20, choices=GRN_STATUS_CHOICES, default='Pending QC')
    remarks       = models.TextField(blank=True)
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes  = [models.Index(fields=['project', 'status'])]

    def save(self, *args, **kwargs):
        if not self.grn_no:
            self.grn_no = DocSequence.next_number(self.project.company, 'GRN')
        super().save(*args, **kwargs)

    def __str__(self):
        return self.grn_no


class GRNLine(models.Model):
    grn          = models.ForeignKey(GRNHeader, on_delete=models.CASCADE, related_name='lines')
    po_line      = models.ForeignKey(POLine, on_delete=models.PROTECT,
                                     related_name='grn_lines', db_index=True)
    activity     = models.ForeignKey(WBSActivity, on_delete=models.PROTECT, db_index=True)
    project      = models.ForeignKey(ERPProject, on_delete=models.PROTECT, db_index=True)
    item_code    = models.ForeignKey(Material, on_delete=models.PROTECT)
    qty_received = models.DecimalField(max_digits=14, decimal_places=3)
    qty_accepted = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    qty_rejected = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    uom          = models.CharField(max_length=20)
    qc_status    = models.CharField(max_length=20, choices=QC_STATUS_CHOICES, default='Pending')
    qc_remarks   = models.TextField(blank=True)
    batch_no     = models.CharField(max_length=50, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['activity']),
            models.Index(fields=['project']),
            models.Index(fields=['po_line']),
        ]

    def __str__(self):
        return f'{self.grn.grn_no} / Line {self.pk}'


class StockLedger(models.Model):
    """Double-entry ledger — every receipt/issue is a row. Current stock = SUM(qty)."""
    TXN_TYPES = [
        ('GRN_IN',     'GRN Receipt'),
        ('ISSUE_OUT',  'Issue to Site'),
        ('RETURN_IN',  'Return from Site'),
        ('REJECT_OUT', 'QC Rejection'),
        ('ADJ_IN',     'Adjustment In'),
        ('ADJ_OUT',    'Adjustment Out'),
    ]
    project      = models.ForeignKey(ERPProject, on_delete=models.PROTECT,
                                     related_name='stock_ledger', db_index=True)
    item_code    = models.ForeignKey(Material, on_delete=models.PROTECT, db_index=True)
    txn_type     = models.CharField(max_length=20, choices=TXN_TYPES)
    ref_doc_type = models.CharField(max_length=20)
    ref_doc_no   = models.CharField(max_length=25)
    qty          = models.DecimalField(max_digits=14, decimal_places=3)  # +ve=in, -ve=out
    cost_rate    = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    txn_date     = models.DateField()
    created_at   = models.DateTimeField(auto_now_add=True)
    created_by   = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ['-txn_date', '-created_at']
        indexes  = [models.Index(fields=['project', 'item_code'])]

    def __str__(self):
        return f'{self.txn_type} {self.qty} {self.item_code.item_code} ({self.ref_doc_no})'
