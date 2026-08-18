from datetime import date
from decimal import Decimal
from dateutil.relativedelta import relativedelta
from django.db import models
from django.utils import timezone
from accounts.models import User
from sales.fields import EncryptedDecimalField, EncryptedTextField, phone_blind_index
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
    name = EncryptedTextField()
    phone = EncryptedTextField()
    # Searchable fingerprint of `phone` — an encrypted column can't be looked
    # up, so duplicate checks and phone search go through this instead.
    phone_key = models.CharField(max_length=64, blank=True, db_index=True)
    alt_phone = EncryptedTextField(blank=True)
    email = EncryptedTextField(blank=True)
    reference_name = EncryptedTextField(blank=True)
    reference_phone = EncryptedTextField(blank=True)
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
    remarks = EncryptedTextField(blank=True)
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

    def save(self, *args, **kwargs):
        # Derive the key from the plaintext attribute before the field encrypts it.
        self.phone_key = phone_blind_index(self.phone)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class LeadStatusHistory(models.Model):
    """Per-lead activity timeline — mirrors sales.LeadStatusHistory exactly,
    trimmed to the fields Club 1000's simpler single-assignee Lead actually has
    (no telecaller/STM split, no site visits/closures)."""
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='history')
    changed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    # Free-text event-type discriminator, not choices — observed values: 'created',
    # 'status', 'assigned_to'.
    field_changed = models.CharField(max_length=50)
    old_value = models.CharField(max_length=100, blank=True)
    new_value = models.CharField(max_length=100, blank=True)
    remarks = EncryptedTextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.lead.name} - {self.field_changed}'


class FollowUp(models.Model):
    """Scheduled follow-up on a Club 1000 Lead — mirrors sales.FollowUp's shape,
    but its own model since that one's FK is CASCADE-bound to sales.Lead."""
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='follow_ups')
    assigned_to = models.ForeignKey(User, on_delete=models.CASCADE, related_name='club1000_follow_ups')
    scheduled_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=FOLLOWUP_STATUS, default='pending')
    remarks = EncryptedTextField(blank=True)
    outcome = models.TextField(blank=True)
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
    min_ticket_size = models.DecimalField(max_digits=14, decimal_places=2)
    # Which interest-payout cadences a manager allows for this scheme (checked at
    # scheme-creation time) — investors added under it can only pick from this set.
    interest_payout_options = models.JSONField(default=list)
    # Each selected interest_payout_options entry gets its OWN annual return %,
    # e.g. {"quarterly": "15.00", "maturity": "18.00"} — a scheme's quarterly
    # and maturity investors are legitimately different products with
    # different rates, not one flat rate stretched across every cadence.
    payout_rates = models.JSONField(default=dict, blank=True)
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

    def rate_for(self, frequency):
        """The scheme's annual return % for one specific payout cadence."""
        return self.payout_rates.get(frequency)


class Investor(models.Model):
    company = models.ForeignKey(
        'companies.Company', on_delete=models.CASCADE,
        related_name='club1000_investors', null=True, blank=True,
    )
    scheme = models.ForeignKey(Scheme, on_delete=models.PROTECT, related_name='investors')
    source = models.CharField(max_length=20, choices=LEAD_SOURCE_CHOICES, default='referral')
    reference_name = EncryptedTextField(blank=True)
    reference_phone = EncryptedTextField(blank=True)
    name = EncryptedTextField()
    phone = EncryptedTextField()
    # Searchable fingerprint of `phone` — an encrypted column can't be looked
    # up, so duplicate checks and phone search go through this instead.
    phone_key = models.CharField(max_length=64, blank=True, db_index=True)
    email = EncryptedTextField(blank=True)
    pan = EncryptedTextField(blank=True)
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
    notes = EncryptedTextField(blank=True)
    # Set when this Investor was created by converting a Lead.
    lead = models.ForeignKey(Lead, on_delete=models.SET_NULL, null=True, blank=True, related_name='investor_conversions')
    # Free-text collateral/security description for the Investment Proposal Form (LOI)
    # — renders "NA" when blank, matching the paper template's fallback style.
    security = models.CharField(max_length=200, blank=True)
    loi_document = models.FileField(max_length=300, null=True, blank=True)
    loi_no = models.CharField(max_length=50, blank=True)
    # Revision flow ("Revise LOI") — mirrors sales.Booking's revision_no, but since
    # Investor (unlike Booking→Closure) IS the final record, a revision updates
    # THIS row in place rather than creating a new one (a new row would double-count
    # the payout schedule and referral reward). Proposed new terms + the newly
    # signed LOI are held in the pending_* fields below and only applied to the
    # live fields on approval (see InvestorActionView) — a rejected revision
    # leaves the original terms and loi_document completely untouched.
    revision_no = models.IntegerField(default=0)
    pending_revision = models.JSONField(null=True, blank=True)
    pending_loi_document = models.FileField(max_length=300, null=True, blank=True)
    # Which kind of proposed change pending_revision holds — a plain "Revise LOI"
    # edit (terms change, dates untouched) or a maturity "Renew" (investment_date
    # resets to the renewal date, a new maturity_date is computed, and the
    # referral reward is forced to REINVESTMENT_REFERRAL_REWARD_PCT). Read by
    # InvestorActionView.approve to decide how to apply pending_revision.
    pending_revision_type = models.CharField(
        max_length=10, choices=[('revise', 'Revise'), ('renew', 'Renew')], default='revise',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['company', '-created_at'], name='club1000_inv_company_idx'),
            models.Index(fields=['added_by'], name='club1000_inv_addedby_idx'),
        ]


    def __str__(self):
        return f'{self.name} ({self.scheme.name})'

    def is_matured(self):
        """Past its maturity date and still sitting active — i.e. due for the
        investor to choose Renew or Payout. Computed on the fly (no scheduled
        job ever flips `status` to 'matured') so it's always accurate."""
        return self.status == 'active' and bool(self.maturity_date) and self.maturity_date <= date.today()

    def save(self, *args, **kwargs):
        # Derive the phone key from the plaintext attribute before the field
        # encrypts it. Folded into this save() rather than a second one, which
        # Python would simply shadow.
        self.phone_key = phone_blind_index(self.phone)
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
    notes = EncryptedTextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['due_date']

    def __str__(self):
        return f'{self.investor.name} - {self.payout_type} due {self.due_date}'


REFERRAL_REWARD_PCT = Decimal('0.5')                # a new investment (maiden or an additional one made before an existing investment matures)
REINVESTMENT_REFERRAL_REWARD_PCT = Decimal('0.25')  # renewing an investment at maturity (upgrade or downgrade amount)


class ReferralReward(models.Model):
    """Owed to whoever referred this investor (identified by reference_phone):
    0.5% of the invested amount on a new investment (this person's first, or
    any additional one made before an existing investment of theirs matures),
    0.25% of the renewed amount when they renew a matured investment instead
    of taking a payout. Computed at approval time in InvestorActionView, using
    whichever rate applies to that specific approval — a new-investment
    approval or a "Renew" approval each create a fresh reward row (FK, not
    OneToOne: a single Investor row can earn a reward more than once over its
    life as it's renewed at successive maturities); a plain "Revise LOI" edit
    just rescales the most recent still-pending reward's amount at ITS
    already-locked-in rate, never changing the rate itself. Mirrors Payout's
    pending/paid lifecycle."""
    investor = models.ForeignKey(Investor, on_delete=models.CASCADE, related_name='referral_rewards')
    reference_name = EncryptedTextField(blank=True)
    reference_phone = EncryptedTextField(blank=True)
    amount = EncryptedDecimalField(max_digits=14, decimal_places=2)
    # The % rate locked in for THIS reward at creation time (0.5 for a new
    # investment, 0.25 for a renewal) — kept so a later plain Revise can
    # rescale `amount` off a changed amount_invested without re-deriving tier.
    pct = models.DecimalField(max_digits=5, decimal_places=2, default=REFERRAL_REWARD_PCT)
    status = models.CharField(max_length=10, choices=PAYOUT_STATUS_CHOICES, default='pending')
    paid_date = models.DateField(null=True, blank=True)
    paid_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.reference_name} - reward for {self.investor.name}'
