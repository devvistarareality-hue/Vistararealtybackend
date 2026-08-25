from django.db import models
from django.utils import timezone
from accounts.models import User
from .fields import EncryptedTextField, EncryptedDecimalField, phone_blind_index


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

SV_OUTCOME = [
    ('hot', 'Hot'),
    ('warm', 'Warm'),
    ('cold', 'Cold'),
    ('not_interested', 'Not Interested'),
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
    # 'pratishtha' prices per unit rather than from one project-wide rate: each unit's
    # area/facing/terrace live in Plot.price_book and the booking form derives every
    # line from an editable rate (flats) or rate + unit price (shops).
    FORMULA_SETS = [('kalrav', 'Kalrav'), ('ankhol', 'Ankhol'), ('industrial', 'Industrial'),
                    ('pratishtha', 'Pratishtha')]
    formula_set = models.CharField(max_length=20, choices=FORMULA_SETS, default='kalrav')
    allow_unit_switch = models.BooleanField(default=False)  # sq.yd ↔ sq.ft toggle (Kalrav)
    # Manager user IDs who approve bookings for THIS project (admin-selected).
    booking_approvers = models.JSONField(default=list, blank=True)
    # Separate approver list for bookings whose lead came through a Channel
    # Partner — a CP-sourced booking is gated by this list instead of the one
    # above (see _can_approve_cp_project), so the two routes can name different
    # people without one overriding the other.
    cp_booking_approvers = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    tagline = models.CharField(max_length=300, blank=True)
    rera = models.CharField(max_length=100, blank=True)
    total_area = models.CharField(max_length=100, blank=True)
    total_plots = models.PositiveIntegerField(default=0)
    price_range = models.CharField(max_length=100, blank=True)
    possession = models.CharField(max_length=100, blank=True)
    cover_image_url = models.CharField(max_length=500, blank=True)
    logo_url = models.CharField(max_length=500, blank=True)  # project logo — shown in the LOI PDF header
    master_plan_url = models.CharField(max_length=500, blank=True)
    site_map_image_url = models.CharField(max_length=500, blank=True)
    site_map_zones = models.JSONField(default=list, blank=True)
    plot_type_plans = models.JSONField(default=list, blank=True)
    # Layout mode. False (default) = a plotted scheme: a flat list of plot numbers with
    # an interactive site map. True = a tower (Pratishtha: G+13): units are defined floor
    # by floor, each floor carrying its own numbering run and plan drawing.
    floor_wise = models.BooleanField(default=False)
    # Tower projects: one entry per floor, in display order — the plan drawing a buyer
    # sees when they pick a unit on that floor.
    #   [{floor: 0, label: 'Ground', image_url: '…'}, {floor: 1, label: '1st Floor', …}]
    floor_plans = models.JSONField(default=list, blank=True)
    # Block-wise industrial: floor_wise=True + this flag. Same floor_plans/block
    # machinery as a multi-block tower, but each block is a single ground-level group
    # (no real floors) and an unmapped block raises a block-prefixed EOI instead of
    # falling back to a flat unit list.
    block_industrial = models.BooleanField(default=False)
    # Some projects render a bespoke LOI layout. Keyed off this rather than the
    # project's name, so renaming a project can't silently change its paperwork.
    LOI_VARIANTS = [('', 'Standard'), ('tundav', 'Tundav'), ('kalrav3', 'Kalrav 3')]
    loi_variant = models.CharField(max_length=30, choices=LOI_VARIANTS, blank=True, default='')
    # Standard EOI unit types (pre-approval): [{type, plot_area, const_area}, …].
    # Used to prefill the EOI form so areas are never hardcoded.
    eoi_unit_types = models.JSONField(default=list, blank=True)
    # Kiosk self-booking: when enabled, this project appears in the client-facing Kiosk flow
    # (a walk-in client can self-book a plot / raise an EOI, subject to staff approval).
    kiosk_enabled = models.BooleanField(default=False)
    approver_email = EncryptedTextField(blank=True, default='')
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
    # Soft hold: a rep has selected this unit on the plot map but not yet submitted a
    # booking for it. Set/cleared by PlotHoldView/PlotReleaseView and auto-expired by
    # _release_expired_holds(). Left NULL for an admin's manual hold (PlotDetailView.patch)
    # and for a hard hold backed by an actual pending Booking, so neither ever auto-expires.
    held_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='held_plots')
    held_at = models.DateTimeField(null=True, blank=True)
    size = models.CharField(max_length=100, blank=True)
    construction_area = models.CharField(max_length=100, blank=True)  # sq.ft; auto-maps into booking
    cluster_type = models.CharField(max_length=100, blank=True)
    facing = models.CharField(max_length=50, blank=True)
    price = models.CharField(max_length=100, blank=True)
    notes = EncryptedTextField(blank=True)
    # ── Tower projects (Pratishtha-style: G+13, units stacked per floor) ──
    # Plotted schemes leave `floor` NULL; a tower sets 0 for ground, 1.. upward, so
    # units can be grouped and shown against that floor's plan.
    floor = models.SmallIntegerField(null=True, blank=True)
    # Some flats carry a private terrace, priced separately from the built-up area.
    terrace_area = models.CharField(max_length=100, blank=True)
    # Per-unit price book for Pratishtha: this unit's area, facing, terrace and the
    # derived line items. The booking form recomputes them from the editable drivers,
    # so this is the seed/default rather than a frozen quote.
    # Shape depends on the unit kind — see sales/pricing/pratishtha.py.
    price_book = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = ['project', 'number']
        ordering = ['number']

    def __str__(self):
        return f"{self.project.name} – Plot {self.number}"


LEAD_PURPOSE_CHOICES = [
    ('investment', 'Investment'),
    ('end_use', 'End Use'),
    ('other', 'Other'),
]

BUDGET_BUCKETS = [
    ('lt_10l', 'Less than ₹10 Lakh'),
    ('10_50l', '₹10 – 50 Lakh'),
    ('50l_1cr', '₹50 Lakh – ₹1 Cr'),
    ('1_2cr', '₹1 – 2 Cr'),
    ('2_3cr', '₹2 – 3 Cr'),
    ('3_5cr', '₹3 – 5 Cr'),
    ('gt_5cr', 'Above ₹5 Cr'),
]

CP_CATEGORY = [
    ('premium',  'Premium'),
    ('normal',   'Normal'),
    ('referral', 'Referral'),
]

CP_SEGMENT = [
    ('residential', 'Residential'),
    ('industrial', 'Industrial'),
    ('both', 'Both'),
]


class ChannelPartner(models.Model):
    """An external referral partner (broker/agent/firm) — distinct from a 'CP
    Executive' employee, who manages the relationship with these but isn't one."""
    company = models.ForeignKey(
        'companies.Company', on_delete=models.CASCADE,
        related_name='channel_partners',
    )
    name = EncryptedTextField()
    contact_no = EncryptedTextField()
    # Searchable fingerprint of `contact_no` — mirrors Lead.phone_key, since an
    # encrypted column can't be looked up or deduped on directly.
    contact_key = models.CharField(max_length=64, blank=True, db_index=True)
    firm_name = models.CharField(max_length=150, blank=True)
    category = models.CharField(max_length=10, choices=CP_CATEGORY, default='normal')
    segment = models.CharField(max_length=15, choices=CP_SEGMENT, blank=True)
    area = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='channel_partners_created',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Encrypted columns sort by ciphertext, not plaintext, so name can't be
        # the DB-level ordering — newest first, same as Lead.
        ordering = ['-created_at']
        indexes = [models.Index(fields=['company'])]

    def save(self, *args, **kwargs):
        self.contact_key = phone_blind_index(self.contact_no)
        if 'update_fields' in kwargs and kwargs['update_fields'] is not None:
            uf = set(kwargs['update_fields'])
            if 'contact_no' in uf:
                uf.add('contact_key')
                kwargs['update_fields'] = list(uf)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Lead(models.Model):
    company = models.ForeignKey(
        'companies.Company', on_delete=models.CASCADE,
        related_name='leads', null=True, blank=True,
    )
    name = EncryptedTextField()
    phone = EncryptedTextField()
    # Searchable fingerprint of `phone` — an encrypted column can't be looked up,
    # so duplicate detection and phone search go through this instead. Kept in step
    # with `phone` by save() and, for bulk paths, by the caller.
    phone_key = models.CharField(max_length=64, blank=True, db_index=True)
    alt_phone = EncryptedTextField(blank=True)
    email = EncryptedTextField(blank=True)
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, blank=True, related_name='leads')
    source = models.ForeignKey(LeadSource, on_delete=models.SET_NULL, null=True, blank=True, related_name='leads')
    channel_partner = models.ForeignKey(
        ChannelPartner, on_delete=models.SET_NULL, null=True, blank=True, related_name='leads',
    )

    # Meta Ads attribution
    meta_campaign_name = models.CharField(max_length=200, blank=True)
    meta_adset_name    = EncryptedTextField(blank=True)
    meta_ad_name       = EncryptedTextField(blank=True)
    # The Meta Lead Ads form this lead came from — drives form→project routing and
    # lets a later mapping retroactively backfill the project.
    meta_form_id       = models.CharField(max_length=100, blank=True, db_index=True)

    # Overall status
    status = models.CharField(max_length=30, choices=LEAD_STATUS, default='new')

    # Telecaller assignment
    telecaller = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='tc_leads'
    )
    telecaller_status = models.CharField(max_length=30, choices=TC_STATUS, blank=True)
    telecaller_remarks = EncryptedTextField(blank=True)
    telecaller_assigned_at = models.DateTimeField(null=True, blank=True)

    # STM assignment
    stm = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='stm_leads'
    )
    stm_status = models.CharField(max_length=30, choices=STM_STATUS, blank=True)
    stm_remarks = EncryptedTextField(blank=True)
    stm_assigned_at = models.DateTimeField(null=True, blank=True)

    # Requirement
    budget_min = models.BigIntegerField(null=True, blank=True)
    budget_max = models.BigIntegerField(null=True, blank=True)
    requirement = models.TextField(blank=True)
    preferred_location = models.CharField(max_length=200, blank=True)

    # Structured requirement (Location / Purpose / Budget bucket)
    city = models.CharField(max_length=120, blank=True)
    address = EncryptedTextField(blank=True)
    purpose = models.JSONField(default=list, blank=True)   # multi-select: investment/end_use/other
    budget_bucket = models.CharField(max_length=20, choices=BUDGET_BUCKETS, blank=True)

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

    def save(self, *args, **kwargs):
        # Derive the lookup key from the plaintext attribute before the field
        # encrypts it on write. bulk_create/bulk_update skip save(), so those
        # callers set phone_key themselves (see the CSV import).
        self.phone_key = phone_blind_index(self.phone)
        if 'update_fields' in kwargs and kwargs['update_fields'] is not None:
            uf = set(kwargs['update_fields'])
            if 'phone' in uf:
                uf.add('phone_key')
                kwargs['update_fields'] = list(uf)
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.name} – {self.phone}'


class LeadStatusHistory(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='history')
    changed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    field_changed = models.CharField(max_length=50)
    old_value = models.CharField(max_length=100, blank=True)
    new_value = models.CharField(max_length=100, blank=True)
    remarks = EncryptedTextField(blank=True)
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
    remarks = EncryptedTextField(blank=True)
    outcome = models.TextField(blank=True)
    # Phase-2 scheduled reminders: set once each so the cron never double-notifies.
    reminder_sent_at = models.DateTimeField(null=True, blank=True)   # assignee nudged (overdue)
    escalated_at = models.DateTimeField(null=True, blank=True)       # manager escalated
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
    # Post-visit outcome — set alongside remarks the moment a visit is marked Done,
    # so the pipeline can tell an interested walk-in from a dead one at a glance.
    outcome = models.CharField(max_length=20, choices=SV_OUTCOME, blank=True)
    stm = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='site_visits'
    )
    referred_by_telecaller = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='referred_site_visits'
    )
    remarks = EncryptedTextField(blank=True)
    # Phase-2 scheduled reminders: set once each so the cron never double-notifies.
    reminder_sent_at = models.DateTimeField(null=True, blank=True)   # STM/TC nudged (overdue)
    escalated_at = models.DateTimeField(null=True, blank=True)       # manager escalated
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']


class Booking(models.Model):
    """Full plot-booking record — the ERP-native equivalent of the GAS submission
    sheet row. Holds client, pricing, schedule, LOI doc and approval state."""
    STATUS = [('draft', 'Draft'), ('pending', 'Pending Approval'), ('sold', 'Sold'), ('rejected', 'Rejected'), ('hold', 'Hold')]

    company   = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='bookings', null=True, blank=True)
    project   = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings')
    plot      = models.ForeignKey(Plot, on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings')
    # Multi-plot booking: `plot` stays the primary (first) plot for backward compat;
    # plot_ids holds ALL selected plot ids and plot_numbers is the comma display.
    plot_ids     = models.JSONField(default=list, blank=True)
    plot_numbers = models.CharField(max_length=200, blank=True)
    lead      = models.ForeignKey(Lead, on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings')
    closure   = models.ForeignKey('Closure', on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings')
    stm       = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings')

    # Client
    client_name = EncryptedTextField(blank=True)
    gender      = models.CharField(max_length=10, blank=True)
    phone       = EncryptedTextField(blank=True)
    address     = EncryptedTextField(blank=True)
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
    # Ankhol sale-deed percentage (editable per booking; defaults to 60%).
    sale_deed_pct      = models.DecimalField(max_digits=5, decimal_places=2, default=60)
    # Exact Unit Price override (Ankhol): when set (>0), used verbatim as the sale deed
    # instead of re-deriving from the rounded %, so the entered amount stays exact.
    sale_deed_amount   = EncryptedDecimalField(max_digits=16, decimal_places=2, default=0)
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
    apply_page_fee   = models.CharField(max_length=5, default='Yes')  # ₹1,500 page fee inside reg fee
    apply_stamp_duty = models.CharField(max_length=5, default='Yes')
    apply_gst        = models.CharField(max_length=5, default='Yes')

    # Schedule / extras
    installments   = models.JSONField(default=list, blank=True)
    extra_work_desc = models.CharField(max_length=300, blank=True)
    extra_work_amount = EncryptedDecimalField(max_digits=16, decimal_places=2, default=0)
    extra_work_inst = models.JSONField(default=list, blank=True)
    extra_terms    = models.JSONField(default=list, blank=True)

    booking_date = models.DateField(null=True, blank=True)
    cp_name      = EncryptedTextField(blank=True)
    # Kiosk self-booking: `stm` is the kiosk account, not the salesperson who assisted,
    # so the staff member types their name here. Preferred over stm.name wherever a
    # booking's STM is displayed. Named to avoid clashing with the serializer's
    # existing stm_name (which reads stm.name).
    manual_stm_name = EncryptedTextField(blank=True)
    # max_length must be generous: the GAS-style path is Project/Plot <no> - <Client>/R<rev>_LOI_...pdf
    # and long project+client names exceed the FileField default of 100 (silently failed the DB save).
    loi_document = models.FileField(upload_to='', null=True, blank=True, max_length=300)  # path set explicitly (project/plot/rev)

    status          = models.CharField(max_length=20, choices=STATUS, default='pending')
    approval_status = models.CharField(max_length=40, blank=True)
    # Set once, the moment an approver actually approves (BookingActionView) —
    # deliberately separate from updated_at, which changes on every save and
    # would drift away from the true approval moment if the booking is ever
    # touched again afterwards (e.g. LOI regenerated).
    approved_at     = models.DateTimeField(null=True, blank=True)
    revision_no     = models.IntegerField(default=0)
    # The booking this one revises. The client has always posted `revision_of` but it
    # was never stored, leaving revision chains to be inferred from closure/unit —
    # which broke when an EOI was converted to an LOI and the unit was renumbered.
    revision_of = models.ForeignKey('self', null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name='revisions')
    pending_revision = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Booking {self.project_id}/{self.plot_id} – {self.client_name}'


class Closure(models.Model):
    """A booked/closed unit — the revenue record. It is deliberately NOT owned by the
    lead: `company` is denormalised (like Booking) and `lead` is SET_NULL, so wiping
    leads never destroys the conversion history that Bookings still point at. The
    client name/phone are snapshotted for the same reason."""
    company = models.ForeignKey(
        'companies.Company', on_delete=models.CASCADE, related_name='closures', null=True, blank=True
    )
    lead = models.ForeignKey(Lead, on_delete=models.SET_NULL, null=True, blank=True, related_name='closures')
    # Snapshot of the client at closure time — survives the lead being deleted.
    client_name = EncryptedTextField(blank=True)
    client_phone = EncryptedTextField(blank=True)
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
    remarks = EncryptedTextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-closure_date']

    def save(self, *args, **kwargs):
        # Derive the company + client snapshot from the lead on first write so callers
        # that only know the lead (serializer POST, imports) still get a self-contained
        # row. bulk_create() bypasses this — those call sites set the fields directly.
        if self.lead_id:
            if not self.company_id:
                self.company_id = Lead.objects.filter(pk=self.lead_id).values_list('company_id', flat=True).first()
            if not self.client_name or not self.client_phone:
                lead = self.lead
                self.client_name = self.client_name or (lead.name or '')
                self.client_phone = self.client_phone or (lead.phone or '')
        return super().save(*args, **kwargs)


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
    # Meta signs every webhook POST with HMAC-SHA256 of the raw body, keyed on the
    # app secret. Without it the endpoint accepts anything the internet sends.
    # Encrypted for the same reason as the page token.
    app_secret = EncryptedTextField(blank=True)
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


class BackupSettings(models.Model):
    """Singleton (always pk=1) — a platform-wide schedule, not per-company. Controls
    when the automatic full-database backup cron actually produces a new backup."""
    FREQUENCY_CHOICES = [('weekly', 'Weekly'), ('monthly', 'Monthly'), ('yearly', 'Yearly')]
    frequency  = models.CharField(max_length=10, choices=FREQUENCY_CHOICES, default='weekly')
    is_enabled = models.BooleanField(default=True)
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'BackupSettings ({self.frequency}, enabled={self.is_enabled})'


class BackupRecord(models.Model):
    """One row per backup attempt (automatic or manually triggered by a super user)."""
    STATUS_CHOICES = [('running', 'Running'), ('success', 'Success'), ('failed', 'Failed')]
    status          = models.CharField(max_length=10, choices=STATUS_CHOICES, default='running')
    file_path       = models.CharField(max_length=300, blank=True)  # Supabase object path
    file_size_bytes = models.BigIntegerField(null=True, blank=True)
    # null = automatic (cron-triggered)
    triggered_by    = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    error_message   = models.TextField(blank=True)
    started_at      = models.DateTimeField(auto_now_add=True)
    completed_at    = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f'Backup #{self.id} ({self.status})'


TRANSFER_STATUS = [
    ('pending', 'Pending Approval'),
    ('approved', 'Approved'),
    ('rejected', 'Rejected'),
    ('cancelled', 'Cancelled'),
]


class LeadTransfer(models.Model):
    """One STM asking to hand a lead to another, held until an approver signs it off.

    The handover is NOT applied when the request is made — the lead stays with the
    current STM until approval, so a rep cannot move work off their own name (or onto
    someone else's) unilaterally. Approval is by the project's configured booking
    approvers, the same people and the same list that gate a booking on that project.

    from_stm is recorded at request time rather than read off the lead at approval,
    so the record still says who it came from even if the lead moves in between.
    """
    company   = models.ForeignKey('companies.Company', on_delete=models.CASCADE,
                                  related_name='lead_transfers')
    lead      = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='transfers')
    project   = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='lead_transfers')
    from_stm  = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='lead_transfers_out')
    to_stm    = models.ForeignKey(User, on_delete=models.CASCADE, related_name='lead_transfers_in')
    requested_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='lead_transfers_requested')
    reason    = EncryptedTextField(blank=True)
    status    = models.CharField(max_length=12, choices=TRANSFER_STATUS, default='pending')
    decided_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='lead_transfers_decided')
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = EncryptedTextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # The approver queue: WHERE company=X AND status='pending' ORDER BY created_at.
            models.Index(fields=['company', 'status', '-created_at'], name='leadxfer_co_status_idx'),
            models.Index(fields=['lead'], name='leadxfer_lead_idx'),
        ]
        constraints = [
            # One open request per lead. Without this two reps can both have a pending
            # transfer for the same lead and whichever is approved second silently
            # overwrites the first.
            models.UniqueConstraint(fields=['lead'], condition=models.Q(status='pending'),
                                    name='one_pending_transfer_per_lead'),
        ]

    def __str__(self):
        return 'Transfer lead %s -> %s (%s)' % (self.lead_id, self.to_stm_id, self.status)
