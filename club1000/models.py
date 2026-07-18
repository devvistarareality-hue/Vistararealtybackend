from decimal import Decimal
from dateutil.relativedelta import relativedelta
from django.db import models
from django.utils import timezone
from accounts.models import User
from sales.fields import EncryptedDecimalField


INTEREST_PAYOUT_CHOICES = [
    ('monthly', 'Monthly'),
    ('quarterly', 'Quarterly'),
    ('maturity', 'Maturity'),
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

PAYOUT_TYPE_CHOICES = [
    ('interest', 'Interest'),
    ('maturity', 'Maturity'),
    ('premature_redemption', 'Premature Redemption'),
]

PAYOUT_STATUS_CHOICES = [
    ('pending', 'Pending'),
    ('paid', 'Paid'),
]


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
    added_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='club1000_investors_added')
    notes = models.TextField(blank=True)
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


REFERRAL_REWARD_PCT = Decimal('0.5')  # 0.5% of the referred investor's amount_invested


class ReferralReward(models.Model):
    """0.5% of a referred investor's amount_invested, owed to whoever referred
    them (identified by reference_phone, canonicalized at investor-create time).
    One row per referred investor — mirrors Payout's pending/paid lifecycle."""
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
