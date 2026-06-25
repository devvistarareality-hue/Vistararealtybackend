from django.db import models
from django.utils import timezone
from accounts.models import User
from .fields import EncryptedTextField, EncryptedDecimalField


LEAD_STATUS = [
    ('new', 'New'),
    ('assigned', 'Assigned'),
    ('contacted', 'Contacted'),
    ('not_reachable', 'Not Reachable'),
    ('warm_transferred', 'Warm Transferred'),
    # STM-driven stages (Overall mirrors the STM status once with sales)
    ('hot', 'Hot'),
    ('warm', 'Warm'),
    ('cold', 'Cold'),
    ('not_interested', 'Not Interested'),
    ('sv_scheduled', 'SV Scheduled'),
    ('sv_done', 'SV Done'),
    ('closed', 'Closed'),
    ('lost', 'Lost'),
]

TC_STATUS = [
    ('warm', 'Warm'),
    ('cold', 'Cold'),
    ('not_interested', 'Not Interested'),
    ('not_reachable', 'Not Reachable'),
    ('callback', 'Callback'),
]

STM_STATUS = [
    ('hot', 'Hot'),
    ('warm', 'Warm'),
    ('cold', 'Cold'),
    ('not_interested', 'Not Interested'),
    ('sv_scheduled', 'SV Scheduled'),
    ('sv_done', 'SV Done'),
    ('closed', 'Closed'),
]

FOLLOWUP_STATUS = [
    ('pending', 'Pending'),
    ('completed', 'Completed'),
    ('missed', 'Missed'),
    ('rescheduled', 'Rescheduled'),
]

SV_STATUS = [
    ('scheduled', 'Scheduled'),
    ('completed', 'Completed'),
    ('cancelled', 'Cancelled'),
    ('no_show', 'No Show'),
]

CLOSURE_STATUS = [
    ('booked', 'Booked'),
    ('cancelled', 'Cancelled'),
    ('refunded', 'Refunded'),
]


class LeadSource(models.Model):
    company = models.ForeignKey(
        'companies.Company', on_delete=models.CASCADE,
        related_name='lead_sources', null=True, blank=True,
    )
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('company', 'name')

    def __str__(self):
        return self.name


class Project(models.Model):
    company = models.ForeignKey(
        'companies.Company', on_delete=models.CASCADE,
        related_name='projects', null=True, blank=True,
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    location = models.CharField(max_length=200, blank=True)
    project_type = models.CharField(max_length=50, default='residential')
    # Booking pricing engine variant (mirrors the GAS "Formula Set").
    FORMULA_SETS = [('kalrav', 'Kalrav'), ('ankhol', 'Ankhol'), ('industrial', 'Industrial')]
    formula_set = models.CharField(max_length=20, choices=FORMULA_SETS, default='kalrav')
    allow_unit_switch = models.BooleanField(default=False)  # sq.yd ↔ sq.ft toggle (Kalrav)
    # Manager user IDs who approve bookings for THIS project (admin-selected).
    booking_approvers = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    tagline = models.CharField(max_length=300, blank=True)
    rera = models.CharField(max_length=100, blank=True)
    total_area = models.CharField(max_length=100, blank=True)
    total_plots = models.PositiveIntegerField(default=0)
    price_range = models.CharField(max_length=100, blank=True)
    possession = models.CharField(max_length=100, blank=True)
    cover_image_url = models.CharField(max_length=500, blank=True)
    master_plan_url = models.CharField(max_length=500, blank=True)
    site_map_image_url = models.CharField(max_length=500, blank=True)
    site_map_zones = models.JSONField(default=list, blank=True)
    plot_type_plans = models.JSONField(default=list, blank=True)
    approver_email = models.EmailField(max_length=254, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class UserProjectAssignment(models.Model):
    user    = models.ForeignKey(User, on_delete=models.CASCADE, related_name='project_assignments')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='user_assignments')

    class Meta:
        unique_together = ['user', 'project']

    def __str__(self):
        return f'{self.user.name} → {self.project.name}'


class Plot(models.Model):
    AVAILABLE = 'available'
    HOLD = 'hold'
    SOLD = 'sold'
    STATUS_CHOICES = [(AVAILABLE, 'Available'), (HOLD, 'Hold'), (SOLD, 'Sold')]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='plots')
    number = models.CharField(max_length=50)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=AVAILABLE)
    size = models.CharField(max_length=100, blank=True)
    cluster_type = models.CharField(max_length=100, blank=True)
    facing = models.CharField(max_length=50, blank=True)
    price = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        unique_together = ['project', 'number']
        ordering = ['number']

    def __str__(self):
        return f"{self.project.name} – Plot {self.number}"


class Lead(models.Model):
    company = models.ForeignKey(
        'companies.Company', on_delete=models.CASCADE,
        related_name='leads', null=True, blank=True,
    )
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=20)
    alt_phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, blank=True, related_name='leads')
    source = models.ForeignKey(LeadSource, on_delete=models.SET_NULL, null=True, blank=True, related_name='leads')

    # Meta Ads attribution
    meta_campaign_name = models.CharField(max_length=200, blank=True)
    meta_adset_name    = models.CharField(max_length=200, blank=True)
    meta_ad_name       = models.CharField(max_length=200, blank=True)

    # Overall status
    status = models.CharField(max_length=30, choices=LEAD_STATUS, default='new')

    # Telecaller assignment
    telecaller = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='tc_leads'
    )
    telecaller_status = models.CharField(max_length=30, choices=TC_STATUS, blank=True)
    telecaller_remarks = models.TextField(blank=True)
    telecaller_assigned_at = models.DateTimeField(null=True, blank=True)

    # STM assignment
    stm = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='stm_leads'
    )
    stm_status = models.CharField(max_length=30, choices=STM_STATUS, blank=True)
    stm_remarks = models.TextField(blank=True)
    stm_assigned_at = models.DateTimeField(null=True, blank=True)

    # Requirement
    budget_min = models.BigIntegerField(null=True, blank=True)
    budget_max = models.BigIntegerField(null=True, blank=True)
    requirement = models.TextField(blank=True)
    preferred_location = models.CharField(max_length=200, blank=True)

    # Duplicate tracking
    is_duplicate = models.BooleanField(default=False)
    duplicate_of = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True)
    duplicate_count = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['company']),
            models.Index(fields=['status']),
            models.Index(fields=['telecaller_status']),
            models.Index(fields=['stm_status']),
            models.Index(fields=['project']),
            models.Index(fields=['telecaller']),
            models.Index(fields=['stm']),
            models.Index(fields=['created_at']),
            models.Index(fields=['is_duplicate']),
            # Composite indexes matching the actual list-query shape
            # (WHERE <owner> [AND <status>] ORDER BY created_at) so the paginated
            # leads list can satisfy filter+sort+LIMIT from one index instead of
            # filtering on one single-column index and then sorting the whole set.
            models.Index(fields=['company', '-created_at'], name='lead_company_created_idx'),
            models.Index(fields=['telecaller', 'telecaller_status', '-created_at'], name='lead_tc_status_created_idx'),
            models.Index(fields=['stm', 'stm_status', '-created_at'], name='lead_stm_status_created_idx'),
        ]

    def __str__(self):
        return f'{self.name} – {self.phone}'


class LeadStatusHistory(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='history')
    changed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    field_changed = models.CharField(max_length=50)
    old_value = models.CharField(max_length=100, blank=True)
    new_value = models.CharField(max_length=100, blank=True)
    remarks = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class FollowUp(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='follow_ups')
    assigned_to = models.ForeignKey(User, on_delete=models.CASCADE, related_name='follow_ups')
    role_context = models.CharField(max_length=20)
    scheduled_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=FOLLOWUP_STATUS, default='pending')
    remarks = models.TextField(blank=True)
    outcome = models.TextField(blank=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_follow_ups'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['scheduled_at']
        indexes = [
            # Follow-Ups page / lead-detail query: WHERE assigned_to=X [AND lead=Y]
            # ORDER BY scheduled_at.
            models.Index(fields=['assigned_to', 'scheduled_at'], name='followup_assignee_sched_idx'),
            models.Index(fields=['lead'], name='followup_lead_idx'),
        ]


class SiteVisit(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='site_visits')
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, blank=True)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    visited_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=SV_STATUS, default='scheduled')
    stm = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='site_visits'
    )
    referred_by_telecaller = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='referred_site_visits'
    )
    remarks = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']


class Booking(models.Model):
    """Full plot-booking record — the ERP-native equivalent of the GAS submission
    sheet row. Holds client, pricing, schedule, LOI doc and approval state."""
    STATUS = [('pending', 'Pending Approval'), ('sold', 'Sold'), ('rejected', 'Rejected'), ('hold', 'Hold')]

    company   = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='bookings', null=True, blank=True)
    project   = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings')
    plot      = models.ForeignKey(Plot, on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings')
    lead      = models.ForeignKey(Lead, on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings')
    closure   = models.ForeignKey('Closure', on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings')
    stm       = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings')

    # Client
    client_name = models.CharField(max_length=200, blank=True)
    gender      = models.CharField(max_length=10, blank=True)
    phone       = models.CharField(max_length=20, blank=True)
    address     = models.TextField(blank=True)
    source      = models.CharField(max_length=100, blank=True)

    # Plot / type
    formula_set  = models.CharField(max_length=20, default='kalrav')
    area         = models.CharField(max_length=30, blank=True)
    area_unit    = models.CharField(max_length=10, default='sq.yd')
    const_area   = models.CharField(max_length=30, blank=True)
    villa_type   = models.CharField(max_length=50, blank=True)
    bunglow_type = models.CharField(max_length=50, blank=True)

    # Rates
    land_rate          = EncryptedDecimalField(max_digits=14, decimal_places=2, default=0)
    dev_rate           = EncryptedDecimalField(max_digits=14, decimal_places=2, default=0)
    const_rate         = EncryptedDecimalField(max_digits=14, decimal_places=2, default=0)
    sale_deed_rate     = EncryptedDecimalField(max_digits=14, decimal_places=2, default=0)
    dev_agreement_rate = EncryptedDecimalField(max_digits=14, decimal_places=2, default=0)
    maint_rate         = EncryptedDecimalField(max_digits=14, decimal_places=2, default=0)
    maint_months       = models.IntegerField(default=0)

    # Amounts
    plot_basic       = EncryptedDecimalField(max_digits=16, decimal_places=2, default=0)
    plot_dev         = EncryptedDecimalField(max_digits=16, decimal_places=2, default=0)
    const_amt        = EncryptedDecimalField(max_digits=16, decimal_places=2, default=0)
    sale_deed        = EncryptedDecimalField(max_digits=16, decimal_places=2, default=0)
    dev_agreement    = EncryptedDecimalField(max_digits=16, decimal_places=2, default=0)
    land_sale_deed   = EncryptedDecimalField(max_digits=16, decimal_places=2, default=0)
    const_agreement  = EncryptedDecimalField(max_digits=16, decimal_places=2, default=0)
    stamp_duty       = EncryptedDecimalField(max_digits=16, decimal_places=2, default=0)
    reg_fees         = EncryptedDecimalField(max_digits=16, decimal_places=2, default=0)
    gst              = EncryptedDecimalField(max_digits=16, decimal_places=2, default=0)
    maintenance      = EncryptedDecimalField(max_digits=16, decimal_places=2, default=0)
    maint_deposit    = EncryptedDecimalField(max_digits=16, decimal_places=2, default=0)
    maint_advance    = EncryptedDecimalField(max_digits=16, decimal_places=2, default=0)
    legal_charges    = EncryptedDecimalField(max_digits=16, decimal_places=2, default=0)
    premium_location = EncryptedDecimalField(max_digits=16, decimal_places=2, default=0)
    total_extra      = EncryptedDecimalField(max_digits=16, decimal_places=2, default=0)
    discount         = EncryptedDecimalField(max_digits=16, decimal_places=2, default=0)
    final_amount     = EncryptedDecimalField(max_digits=16, decimal_places=2, default=0)

    # Toggles
    apply_reg_fee    = models.CharField(max_length=5, default='Yes')
    apply_stamp_duty = models.CharField(max_length=5, default='Yes')
    apply_gst        = models.CharField(max_length=5, default='Yes')

    # Schedule / extras
    installments   = models.JSONField(default=list, blank=True)
    extra_work_desc = models.CharField(max_length=300, blank=True)
    extra_work_amount = EncryptedDecimalField(max_digits=16, decimal_places=2, default=0)
    extra_work_inst = models.JSONField(default=list, blank=True)
    extra_terms    = models.JSONField(default=list, blank=True)

    booking_date = models.DateField(null=True, blank=True)
    cp_name      = models.CharField(max_length=200, blank=True)
    loi_document = models.FileField(upload_to='', null=True, blank=True)  # path set explicitly (project/plot/rev)

    status          = models.CharField(max_length=20, choices=STATUS, default='pending')
    approval_status = models.CharField(max_length=40, blank=True)
    revision_no     = models.IntegerField(default=0)
    pending_revision = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Booking {self.project_id}/{self.plot_id} – {self.client_name}'


class Closure(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='closures')
    site_visit = models.ForeignKey(SiteVisit, on_delete=models.SET_NULL, null=True, blank=True)
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, blank=True)
    stm = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='closures'
    )
    referred_by_telecaller = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='referred_closures'
    )
    status = models.CharField(max_length=20, choices=CLOSURE_STATUS, default='booked')
    closure_date = models.DateField()
    unit_no = models.CharField(max_length=50, blank=True)
    unit_type = models.CharField(max_length=50, blank=True)
    booking_amount = EncryptedDecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    total_amount = EncryptedDecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    remarks = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-closure_date']


CRM_ROLES = [
    ('telecaller', 'Telecaller'),
    ('stm', 'STM (Sales)'),
    ('manager', 'Manager'),
]


class SalesTeamMember(models.Model):
    """Links an ERP user to a CRM role for the Sales module."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='sales_profile')
    crm_role = models.CharField(max_length=20, choices=CRM_ROLES, default='telecaller')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.user.name} ({self.crm_role})'


class DistributionSettings(models.Model):
    """Per-company sign-in / sign-out windows for TC and STM distribution."""
    company = models.OneToOneField(
        'companies.Company', on_delete=models.CASCADE, related_name='dist_settings'
    )
    tc_signin_time  = models.TimeField(default='10:20')
    tc_signout_time = models.TimeField(default='22:00')
    stm_signin_time  = models.TimeField(default='10:20')
    stm_signout_time = models.TimeField(default='22:00')
    weights_reset_at = models.DateTimeField(null=True, blank=True)
    # User IDs (managers) the admin picks to approve plot bookings.
    booking_approvers = models.JSONField(default=list, blank=True)

    def __str__(self):
        return f'DistSettings({self.company})'


class UserAvailability(models.Model):
    """Daily sign-in record for TC/STM users."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='availability')
    date = models.DateField()
    is_available = models.BooleanField(default=False)
    checked_in_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ['user', 'date']
        ordering = ['-date']

    def __str__(self):
        return f'{self.user.name} – {self.date} – {"in" if self.is_available else "out"}'


class UserDistributionWeight(models.Model):
    """Per-user weight for weighted round-robin distribution."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='dist_weight')
    weight = models.PositiveIntegerField(default=1)

    def __str__(self):
        return f'{self.user.name} w={self.weight}'


class DistributionLog(models.Model):
    DIST_TYPE = [('telecaller', 'Telecaller'), ('stm', 'STM')]
    company = models.ForeignKey(
        'companies.Company', on_delete=models.CASCADE,
        related_name='distribution_logs', null=True, blank=True,
    )
    dist_type = models.CharField(max_length=20, choices=DIST_TYPE)
    triggered_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    leads_distributed = models.IntegerField(default=0)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class MetaFormMapping(models.Model):
    """Maps a Meta Lead Ads form_id to a specific project."""
    company = models.ForeignKey(
        'companies.Company', on_delete=models.CASCADE,
        related_name='meta_form_mappings', null=True, blank=True,
    )
    form_id = models.CharField(max_length=100, unique=True)
    form_name = models.CharField(max_length=200, blank=True)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='meta_form_mappings')
    total_leads = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.form_name or self.form_id} → {self.project.name}'


class MetaWebhookConfig(models.Model):
    """Per-company config for Meta Lead Ads webhook integration."""
    company = models.OneToOneField(
        'companies.Company', on_delete=models.CASCADE,
        related_name='meta_webhook_config', null=True, blank=True,
    )
    verify_token = models.CharField(max_length=200)
    # Encrypted at rest (Fernet). Long-lived FB Page token = full page API access.
    # verify_token stays plaintext — it's used in an equality lookup and is low-value.
    page_access_token = EncryptedTextField(blank=True)
    default_project = models.ForeignKey(
        Project, on_delete=models.SET_NULL, null=True, blank=True
    )
    is_active = models.BooleanField(default=False)
    total_leads_received = models.IntegerField(default=0)
    last_lead_at = models.DateTimeField(null=True, blank=True)
    subscribed_pages = models.JSONField(default=list, blank=True)
    pages_data = models.JSONField(default=list, blank=True)  # [{page_id, page_name, forms:[{id,name}]}]
    pages_refreshed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'MetaWebhookConfig (active={self.is_active})'
