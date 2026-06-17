from django.db import models
from django.utils import timezone
from accounts.models import User


LEAD_STATUS = [
    ('new', 'New'),
    ('assigned', 'Assigned'),
    ('contacted', 'Contacted'),
    ('not_reachable', 'Not Reachable'),
    ('warm_transferred', 'Warm Transferred'),
    ('sv_scheduled', 'SV Scheduled'),
    ('sv_done', 'SV Done'),
    ('closed', 'Closed'),
    ('lost', 'Lost'),
]

TC_STATUS = [
    ('hot', 'Hot'),
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
    name = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Project(models.Model):
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    location = models.CharField(max_length=200, blank=True)
    project_type = models.CharField(max_length=50, default='residential')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class Lead(models.Model):
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=20)
    alt_phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, blank=True, related_name='leads')
    source = models.ForeignKey(LeadSource, on_delete=models.SET_NULL, null=True, blank=True, related_name='leads')

    # Meta Ads attribution
    meta_campaign_name = models.CharField(max_length=200, blank=True)
    meta_ad_name = models.CharField(max_length=200, blank=True)

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
    booking_amount = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
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


class DistributionLog(models.Model):
    DIST_TYPE = [('telecaller', 'Telecaller'), ('stm', 'STM')]
    dist_type = models.CharField(max_length=20, choices=DIST_TYPE)
    triggered_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    leads_distributed = models.IntegerField(default=0)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
