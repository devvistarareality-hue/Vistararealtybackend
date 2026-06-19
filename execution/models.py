from django.db import models
from accounts.models import User
from erp_master.models import ERPProject, WBSActivity, Material, DocSequence, DocumentTrail

PR_STATUS_CHOICES = [
    ('Raised',          'Raised'),
    ('Approved',        'Approved'),
    ('PO Created',      'PO Created'),
    ('In Transit',      'In Transit'),
    ('Received',        'Received'),
    ('Issued to Site',  'Issued to Site'),
    ('Closed',          'Closed'),
]

PR_TRANSITIONS = {
    'Raised':         ['Approved'],
    'Approved':       ['PO Created'],
    'PO Created':     ['In Transit'],
    'In Transit':     ['Received'],
    'Received':       ['Issued to Site'],
    'Issued to Site': ['Closed'],
}

STATUS_ORDER = ['Raised', 'Approved', 'PO Created', 'In Transit', 'Received', 'Issued to Site', 'Closed']


class PRHeader(models.Model):
    pr_no         = models.CharField(max_length=25, unique=True, editable=False)
    project       = models.ForeignKey(ERPProject, on_delete=models.PROTECT,
                                      related_name='purchase_requisitions', db_index=True)
    raised_by     = models.ForeignKey(User, on_delete=models.PROTECT, related_name='prs_raised')
    raised_date   = models.DateField(auto_now_add=True)
    status        = models.CharField(max_length=30, choices=PR_STATUS_CHOICES, default='Raised')
    approved_by   = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name='prs_approved')
    approved_date = models.DateField(null=True, blank=True)
    remarks       = models.TextField(blank=True)
    created_at    = models.DateTimeField(auto_now_add=True)
    updated_at    = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes  = [models.Index(fields=['project', 'status'])]

    def save(self, *args, **kwargs):
        if not self.pr_no:
            self.pr_no = DocSequence.next_number(self.project.company, 'PR')
        super().save(*args, **kwargs)

    def sync_status_from_lines(self):
        """Header status = max progress across all lines."""
        statuses = list(self.lines.values_list('status', flat=True))
        if not statuses:
            return
        self.status = max(statuses, key=lambda s: STATUS_ORDER.index(s))
        self.save(update_fields=['status'])

    def __str__(self):
        return self.pr_no


class PRLine(models.Model):
    pr            = models.ForeignKey(PRHeader, on_delete=models.CASCADE, related_name='lines')
    activity      = models.ForeignKey(WBSActivity, on_delete=models.PROTECT, db_index=True)
    project       = models.ForeignKey(ERPProject, on_delete=models.PROTECT, db_index=True)
    item_code     = models.ForeignKey(Material, on_delete=models.PROTECT)
    qty_required  = models.DecimalField(max_digits=14, decimal_places=3)
    qty_on_po     = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    uom           = models.CharField(max_length=20)
    required_date = models.DateField()
    status        = models.CharField(max_length=30, choices=PR_STATUS_CHOICES, default='Raised')
    remarks       = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['activity']),
            models.Index(fields=['project']),
        ]

    def transition(self, new_status):
        allowed = PR_TRANSITIONS.get(self.status, [])
        if new_status not in allowed:
            from rest_framework.exceptions import ValidationError
            raise ValidationError(
                {'status': f'Cannot move from "{self.status}" to "{new_status}".'}
            )
        self.status = new_status
        self.save(update_fields=['status'])
        self.pr.sync_status_from_lines()

    def __str__(self):
        return f'{self.pr.pr_no} / Line {self.pk}'


# ─── Material Issue ───────────────────────────────────────────────────────────

class MaterialIssueHeader(models.Model):
    issue_no    = models.CharField(max_length=25, unique=True, editable=False)
    project     = models.ForeignKey(ERPProject, on_delete=models.PROTECT,
                                    related_name='material_issues', db_index=True)
    activity    = models.ForeignKey(WBSActivity, on_delete=models.PROTECT, db_index=True)
    issued_date = models.DateField()
    issued_by   = models.ForeignKey(User, on_delete=models.PROTECT, related_name='issues_made')
    received_by = models.CharField(max_length=100)
    remarks     = models.TextField(blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes  = [models.Index(fields=['project', 'activity'])]

    def save(self, *args, **kwargs):
        if not self.issue_no:
            self.issue_no = DocSequence.next_number(self.project.company, 'ISSUE')
        super().save(*args, **kwargs)

    def __str__(self):
        return self.issue_no


class MaterialIssueLine(models.Model):
    issue      = models.ForeignKey(MaterialIssueHeader, on_delete=models.CASCADE,
                                   related_name='lines')
    item_code  = models.ForeignKey(Material, on_delete=models.PROTECT)
    grn_line   = models.ForeignKey('inventory.GRNLine', null=True, blank=True,
                                   on_delete=models.SET_NULL)   # traceability
    qty_issued = models.DecimalField(max_digits=14, decimal_places=3)
    cost_rate  = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    uom        = models.CharField(max_length=20)

    def __str__(self):
        return f'{self.issue.issue_no} / {self.item_code.item_code}'


# ─── Measurement Book & RA Bill ───────────────────────────────────────────────

MB_STATUS_CHOICES = [
    ('Draft',     'Draft'),
    ('Submitted', 'Submitted'),
    ('Certified', 'Certified'),
    ('Billed',    'Billed'),
]


class MeasurementBook(models.Model):
    mb_no        = models.CharField(max_length=25, unique=True, editable=False)
    project      = models.ForeignKey(ERPProject, on_delete=models.PROTECT,
                                     related_name='measurement_books', db_index=True)
    mb_date      = models.DateField()
    prepared_by  = models.ForeignKey(User, on_delete=models.PROTECT, related_name='mbs_prepared')
    certified_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='mbs_certified')
    status       = models.CharField(max_length=20, choices=MB_STATUS_CHOICES, default='Draft')
    remarks      = models.TextField(blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.mb_no:
            self.mb_no = DocSequence.next_number(self.project.company, 'MB')
        super().save(*args, **kwargs)

    def __str__(self):
        return self.mb_no


class MBLine(models.Model):
    mb           = models.ForeignKey(MeasurementBook, on_delete=models.CASCADE, related_name='lines')
    activity     = models.ForeignKey(WBSActivity, on_delete=models.PROTECT, db_index=True)
    description  = models.TextField(blank=True)
    qty_executed = models.DecimalField(max_digits=14, decimal_places=3)
    unit_rate    = models.DecimalField(max_digits=14, decimal_places=2)
    amount       = models.DecimalField(max_digits=16, decimal_places=2, default=0)

    def save(self, *args, **kwargs):
        self.amount = self.qty_executed * self.unit_rate
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.mb.mb_no} / {self.activity.wbs_code}'


RA_STATUS_CHOICES = [
    ('Draft',     'Draft'),
    ('Submitted', 'Submitted'),
    ('Certified', 'Certified'),
    ('Invoiced',  'Invoiced'),
    ('Paid',      'Paid'),
]


class RABill(models.Model):
    ra_bill_no   = models.CharField(max_length=25, unique=True, editable=False)
    project      = models.ForeignKey(ERPProject, on_delete=models.PROTECT,
                                     related_name='ra_bills', db_index=True)
    mb           = models.ForeignKey(MeasurementBook, on_delete=models.PROTECT,
                                     related_name='ra_bills')
    period_from  = models.DateField()
    period_to    = models.DateField()
    total_amount = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    status       = models.CharField(max_length=20, choices=RA_STATUS_CHOICES, default='Draft')
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.ra_bill_no:
            self.ra_bill_no = DocSequence.next_number(self.project.company, 'RA')
        super().save(*args, **kwargs)

    def __str__(self):
        return self.ra_bill_no
