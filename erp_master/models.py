from django.db import models
from django.db import transaction as db_transaction
from companies.models import Company
from accounts.models import User


class Vendor(models.Model):
    company       = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='vendors')
    code          = models.CharField(max_length=20)
    name          = models.CharField(max_length=200)
    contact_name  = models.CharField(max_length=100, blank=True)
    phone         = models.CharField(max_length=20, blank=True)
    email         = models.EmailField(blank=True)
    address       = models.TextField(blank=True)
    gstin         = models.CharField(max_length=15, blank=True)
    pan           = models.CharField(max_length=10, blank=True)
    payment_terms = models.PositiveIntegerField(default=30)
    is_active     = models.BooleanField(default=True)
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('company', 'code')
        ordering = ['name']

    def __str__(self):
        return f'{self.code} – {self.name}'


class GLAccount(models.Model):
    ACCOUNT_TYPES = [
        ('cost',     'Cost'),
        ('revenue',  'Revenue'),
        ('asset',    'Asset'),
        ('liability','Liability'),
    ]
    company      = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='gl_accounts')
    code         = models.CharField(max_length=20)
    name         = models.CharField(max_length=100)
    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPES)
    cost_center  = models.CharField(max_length=50, blank=True)
    is_active    = models.BooleanField(default=True)

    class Meta:
        unique_together = ('company', 'code')
        ordering = ['code']

    def __str__(self):
        return f'{self.code} – {self.name}'


class Material(models.Model):
    company     = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='materials')
    item_code   = models.CharField(max_length=50)
    name        = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    uom         = models.CharField(max_length=20)
    category    = models.CharField(max_length=100, blank=True)
    gl_account  = models.ForeignKey(GLAccount, null=True, blank=True, on_delete=models.SET_NULL)
    is_active   = models.BooleanField(default=True)

    class Meta:
        unique_together = ('company', 'item_code')
        ordering = ['name']

    def __str__(self):
        return f'{self.item_code} – {self.name}'


class ERPProject(models.Model):
    company         = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='erp_projects')
    sales_project   = models.OneToOneField(
        'sales.Project', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='erp_project'
    )
    code            = models.CharField(max_length=20)
    name            = models.CharField(max_length=200)
    client_name     = models.CharField(max_length=200, blank=True)
    location        = models.CharField(max_length=200, blank=True)
    start_date      = models.DateField(null=True, blank=True)
    end_date        = models.DateField(null=True, blank=True)
    project_manager = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='managed_erp_projects'
    )
    status          = models.CharField(max_length=30, default='Active')
    is_active       = models.BooleanField(default=True)
    created_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('company', 'code')
        ordering = ['name']

    def __str__(self):
        return f'{self.code} – {self.name}'


class WBSActivity(models.Model):
    """BOQ line — anchor for every transaction in the system."""
    project         = models.ForeignKey(ERPProject, on_delete=models.CASCADE,
                                        related_name='activities', db_index=True)
    parent_activity = models.ForeignKey('self', null=True, blank=True,
                                        on_delete=models.SET_NULL, related_name='children')
    wbs_code        = models.CharField(max_length=50)
    description     = models.TextField()
    item_code       = models.ForeignKey(Material, null=True, blank=True, on_delete=models.SET_NULL)
    uom             = models.CharField(max_length=20)
    budgeted_qty    = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    unit_rate       = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    budgeted_cost   = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    is_active       = models.BooleanField(default=True)

    class Meta:
        unique_together = ('project', 'wbs_code')
        ordering = ['wbs_code']
        indexes = [models.Index(fields=['project'])]

    def save(self, *args, **kwargs):
        self.budgeted_cost = self.budgeted_qty * self.unit_rate
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.wbs_code} – {self.description[:60]}'


class DocSequence(models.Model):
    """Thread-safe sequence for document numbering per company + doc_type."""
    company  = models.ForeignKey(Company, on_delete=models.CASCADE)
    doc_type = models.CharField(max_length=20)
    last_seq = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('company', 'doc_type')

    @classmethod
    def next_number(cls, company, doc_type):
        import datetime
        with db_transaction.atomic():
            obj, _ = cls.objects.select_for_update().get_or_create(
                company=company, doc_type=doc_type
            )
            obj.last_seq += 1
            obj.save()
            year = datetime.date.today().year
            return f'{doc_type}-{year}-{str(obj.last_seq).zfill(4)}'


class DocumentTrail(models.Model):
    """One row per document link — trace any doc end-to-end without custom joins."""
    doc_type     = models.CharField(max_length=20, db_index=True)
    doc_no       = models.CharField(max_length=30, db_index=True)
    ref_doc_type = models.CharField(max_length=20)
    ref_doc_no   = models.CharField(max_length=30, db_index=True)
    created_at   = models.DateTimeField(auto_now_add=True)
    notes        = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['doc_type', 'doc_no']),
            models.Index(fields=['ref_doc_type', 'ref_doc_no']),
        ]

    def __str__(self):
        return f'{self.doc_type}:{self.doc_no} → {self.ref_doc_type}:{self.ref_doc_no}'
