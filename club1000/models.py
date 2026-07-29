from datetime import date
from decimal import Decimal
from dateutil.relativedelta import relativedelta
from django.db import models
from django.utils import timezone
from accounts.models import User
from sales.fields import EncryptedDecimalField
from sales.models import FOLLOWUP_STATUS


INTEREST_PAYOUT_CHOICES = [
    ('monthly', 'Monthly'),
    ('quarterly', 'Quarterly'),
    ('maturity', 'Maturity'),
]

LEAD_SOURCE_CHOICES = [
    ('referral', 'Referral'),
    ('walk_in', 'Walk-in'),
    ('website', 'Website'),
    ('other', 'Other'),
]

LEAD_STATUS = [
    ('new', 'New'),
    ('contacted', 'Contacted'),
    ('interested', 'Interested'),
    ('not_interested', 'Not Interested'),
    ('converted', 'Converted'),
    ('lost', 'Lost'),
]

PRINCIPAL_PAYOUT_CHOICES = [
    ('maturity', 'Maturity'),
]

INVESTOR_STATUS = [
    ('active', 'Active'),
    ('matured', 'Matured'),
    ('redeemed', 'Redeemed'),
    ('premature_redeemed', 'Premature Redeemed'),
]

INVESTOR_APPROVAL_STATUS = [
    ('pending', 'Pending'),
    ('approved', 'Approved'),
    ('rejected', 'Rejected'),
]

PAYOUT_TYPE_CHOICES = [
    ('interest', 'Interest'),
    ('maturity', 'Maturity'),
    ('premature_redemption', 'Premature Redemption'),
]

PAYOUT_STATUS_CHOICES = [
    ('pending', 'Pending'),
    ('paid', 'Paid'),
]


class Lead(models.Model):
    """A Club 1000 prospect, prior to becoming an Investor. Single assignee (no
    telecaller/STM split — Club 1000 has no such roles) — mirrors sales.Lead's
    shape where it applies, trimmed for this simpler pipeline."""
    company = models.ForeignKey(
        'companies.Company', on_delete=models.CASCADE,
        related_name='club1000_leads', null=True, blank=True,
    )
    name = models.CharField(max_length=150)
    phone = models.CharField(max_length=20, blank=True)
    alt_phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    reference_name = models.CharField(max_length=150, blank=True)
    reference_phone = models.CharField(max_length=20, blank=True)
    source = models.CharField(max_length=20, choices=LEAD_SOURCE_CHOICES, default='other')
    # The date this lead was actually received — editable at add-time (defaults to
    # today) so a lead entered late can be backdated. Distinct from `created_at`,
    # which is the immutable system insert timestamp.
    lead_date = models.DateField(default=date.today, null=True, blank=True)
    scheme_interest = models.ForeignKey('Scheme', on_delete=models.SET_NULL, null=True, blank=True, related_name='interested_leads')
    amount_interested = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    # Prefilled from the chosen scheme's default on the client, but editable per-lead
    # (negotiated during the sales conversation) — carried forward as the Investor's
    # own total_return_pct default if this lead is later converted.
    total_return_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='club1000_leads')
    status = models.CharField(max_length=20, choices=LEAD_STATUS, default='new')
    remarks = models.TextField(blank=True)
    # Mirrors the lead's single open FollowUp (if any) — set when one is scheduled,
    # cleared when it's actioned (completed/missed) or the lead reaches a terminal
    # status (not_interested / lost / converted), so there's never a stale date on
    # a lead that no longer needs following up. Maintained by the FollowUp views
    # and LeadDetailView.patch — not directly writable via the Lead endpoints.
    next_follow_up_date = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class FollowUp(models.Model):
    """Scheduled follow-up on a Club 1000 Lead — mirrors sales.FollowUp's shape,
    but its own model since that one's FK is CASCADE-bound to sales.Lead."""
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='follow_ups')
    assigned_to = models.ForeignKey(User, on_delete=models.CASCADE, related_name='club1000_follow_ups')
    scheduled_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=FOLLOWUP_STATUS, default='pending')
    remarks = models.TextField(blank=True)
    outcome = models.CharField(max_length=100, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['scheduled_at']

    def __str__(self):
        return f'{self.lead.name} - {self.scheduled_at}'


class Scheme(models.Model):
    company = models.ForeignKey(
        'companies.Company', on_delete=models.CASCADE,
        related_name='club1000_schemes', null=True, blank=True,
    )
    name = models.CharField(max_length=100)
    tenure_months = models.PositiveIntegerField()
    fixed_return_pct = models.DecimalField(max_digits=6, decimal_places=2)
    loyalty_benefit_pct = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    total_return_pct = models.DecimalField(max_digits=6, decimal_places=2)
    min_ticket_size = models.DecimalField(max_digits=14, decimal_places=2)
    # Which interest-payout cadences a manager allows for this scheme (checked at
    # scheme-creation time) — investors added under it can only pick from this set.
    interest_payout_options = models.JSONField(default=list)
    # Manager user IDs who approve investors submitted under THIS scheme
    # (admin-selected) — mirrors sales.Project.booking_approvers exactly.
    investor_approvers = models.JSONField(default=list, blank=True)
    principal_payout = models.CharField(max_length=20, choices=PRINCIPAL_PAYOUT_CHOICES, default='maturity')
    premature_redemption_allowed = models.BooleanField(default=False)
    premature_redemption_lock_months = models.PositiveIntegerField(null=True, blank=True)
    premature_redemption_rate_pct_per_month = models.DecimalField(max_digits=5, decimal_places=2, default=1.00)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('company', 'name')

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.total_return_pct is None:
            self.total_return_pct = (self.fixed_return_pct or 0) + (self.loyalty_benefit_pct or 0)
        super().save(*args, **kwargs)


class Investor(models.Model):
    company = models.ForeignKey(
        'companies.Company', on_delete=models.CASCADE,
        related_name='club1000_investors', null=True, blank=True,
    )
    scheme = models.ForeignKey(Scheme, on_delete=models.PROTECT, related_name='investors')
    reference_name = models.CharField(max_length=150, blank=True)
    reference_phone = models.CharField(max_length=20, blank=True)
    name = models.CharField(max_length=150)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    pan = models.CharField(max_length=20, blank=True)
    amount_invested = EncryptedDecimalField(max_digits=14, decimal_places=2)
    investment_date = models.DateField(default=timezone.now)
    maturity_date = models.DateField()
    # Prefilled from the chosen scheme at add-time, but editable per-investor —
    # a manager may negotiate a different payout cadence/rate for one investor.
    interest_payout = models.CharField(max_length=20, choices=INTEREST_PAYOUT_CHOICES, default='maturity')
    total_return_pct = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    document = models.FileField(upload_to='club1000/', null=True, blank=True)  # KYC/ID scan
    status = models.CharField(max_length=20, choices=INVESTOR_STATUS, default='active')
    # Approval gate, separate from `status` (which is the post-approval lifecycle
    # state) — mirrors sales.Booking's approval flow. Payout schedule, referral
    # reward, and source-Lead conversion only fire once this flips to 'approved'
    # (see InvestorActionView); 'rejected' deletes the signed loi_document.
    approval_status = models.CharField(max_length=20, choices=INVESTOR_APPROVAL_STATUS, default='pending')
    # A caller-reviewed/edited payout schedule submitted at add-time, held here
    # until approval (generate_payout_schedule isn't run until then) so the
    # submitter's edits aren't lost — cleared once consumed by InvestorActionView.
    pending_payout_schedule = models.JSONField(null=True, blank=True)
    added_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='club1000_investors_added')
    notes = models.TextField(blank=True)
    # Set when this Investor was created by converting a Lead.
    lead = models.ForeignKey(Lead, on_delete=models.SET_NULL, null=True, blank=True, related_name='investor_conversions')
    # Free-text collateral/security description for the Investment Proposal Form (LOI)
    # — renders "NA" when blank, matching the paper template's fallback style.
    security = models.CharField(max_length=200, blank=True)
    loi_document = models.FileField(max_length=300, null=True, blank=True)
    loi_no = models.CharField(max_length=50, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['company', '-created_at'], name='club1000_inv_company_idx'),
            models.Index(fields=['added_by'], name='club1000_inv_addedby_idx'),
        ]

    def __str__(self):
        return f'{self.name} ({self.scheme.name})'

    def save(self, *args, **kwargs):
        if not self.maturity_date and self.investment_date and self.scheme_id:
            self.maturity_date = self.investment_date + relativedelta(months=self.scheme.tenure_months)
        super().save(*args, **kwargs)


class Payout(models.Model):
    investor = models.ForeignKey(Investor, on_delete=models.CASCADE, related_name='payouts')
    payout_type = models.CharField(max_length=25, choices=PAYOUT_TYPE_CHOICES)
    due_date = models.DateField()
    amount_due = EncryptedDecimalField(max_digits=14, decimal_places=2)
    status = models.CharField(max_length=10, choices=PAYOUT_STATUS_CHOICES, default='pending')
    paid_date = models.DateField(null=True, blank=True)
    paid_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['due_date']

    def __str__(self):
        return f'{self.investor.name} - {self.payout_type} due {self.due_date}'


REFERRAL_REWARD_PCT = Decimal('0.5')              # referred person's 1st (approved) investment
REINVESTMENT_REFERRAL_REWARD_PCT = Decimal('0.25')  # every approved investment after their 1st


class ReferralReward(models.Model):
    """Owed to whoever referred this investor (identified by reference_phone):
    0.5% of amount_invested on the referred person's first approved investment,
    0.25% on every one after — for as long as they keep investing. Tiering is
    computed at approval time in InvestorActionView by matching the investor's
    OWN phone number across their approved Investor rows (one row per
    investment); the reward always attributes to the ORIGINAL referrer found on
    that person's first approved investment, even if a later submission's
    reference fields are blank or different. One row per Investor (investment)
    — mirrors Payout's pending/paid lifecycle."""
    investor = models.OneToOneField(Investor, on_delete=models.CASCADE, related_name='referral_reward')
    reference_name = models.CharField(max_length=150)
    reference_phone = models.CharField(max_length=20)
    amount = EncryptedDecimalField(max_digits=14, decimal_places=2)
    status = models.CharField(max_length=10, choices=PAYOUT_STATUS_CHOICES, default='pending')
    paid_date = models.DateField(null=True, blank=True)
    paid_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.reference_name} - reward for {self.investor.name}'
