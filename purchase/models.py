from django.db import models
from accounts.models import User
from erp_master.models import ERPProject, WBSActivity, Material, Vendor, DocSequence
from execution.models import PRLine

PO_STATUS_CHOICES = [
    ('Draft',     'Draft'),
    ('Confirmed', 'Confirmed'),
    ('Dispatched','Dispatched'),
    ('Closed',    'Closed'),
    ('Cancelled', 'Cancelled'),
]


class POHeader(models.Model):
    po_no         = models.CharField(max_length=25, unique=True, editable=False)
    project       = models.ForeignKey(ERPProject, on_delete=models.PROTECT,
                                      related_name='purchase_orders', db_index=True)
    vendor        = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name='pos')
    po_date       = models.DateField(auto_now_add=True)
    delivery_date = models.DateField(null=True, blank=True)
    status        = models.CharField(max_length=20, choices=PO_STATUS_CHOICES, default='Draft')
    payment_terms = models.PositiveIntegerField(default=30)
    created_by    = models.ForeignKey(User, on_delete=models.PROTECT, related_name='pos_created')
    remarks       = models.TextField(blank=True)
    created_at    = models.DateTimeField(auto_now_add=True)
    updated_at    = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes  = [models.Index(fields=['project', 'status'])]

    def save(self, *args, **kwargs):
        if not self.po_no:
            self.po_no = DocSequence.next_number(self.project.company, 'PO')
        super().save(*args, **kwargs)

    @property
    def total_amount(self):
        return sum(l.amount for l in self.lines.all())

    def __str__(self):
        return self.po_no


class POLine(models.Model):
    po           = models.ForeignKey(POHeader, on_delete=models.CASCADE, related_name='lines')
    pr_line      = models.ForeignKey(PRLine, on_delete=models.PROTECT,
                                     related_name='po_lines', db_index=True)
    activity     = models.ForeignKey(WBSActivity, on_delete=models.PROTECT, db_index=True)
    project      = models.ForeignKey(ERPProject, on_delete=models.PROTECT, db_index=True)
    item_code    = models.ForeignKey(Material, on_delete=models.PROTECT)
    qty_ordered  = models.DecimalField(max_digits=14, decimal_places=3)
    qty_received = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    unit_rate    = models.DecimalField(max_digits=14, decimal_places=2)
    uom          = models.CharField(max_length=20)
    amount       = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    tax_pct      = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    class Meta:
        indexes = [
            models.Index(fields=['activity']),
            models.Index(fields=['project']),
            models.Index(fields=['pr_line']),
        ]

    def save(self, *args, **kwargs):
        self.amount = self.qty_ordered * self.unit_rate
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.po.po_no} / Line {self.pk}'
