from django.conf import settings
from django.db import models


class Project(models.Model):
    TYPE_CHOICES = [
        ('Residential', 'Residential'),
        ('Commercial',  'Commercial'),
        ('Mixed',       'Mixed'),
    ]
    STATUS_CHOICES = [
        ('Active',    'Active'),
        ('Upcoming',  'Upcoming'),
        ('Completed', 'Completed'),
    ]

    name        = models.CharField(max_length=200)
    location    = models.CharField(max_length=200, blank=True)
    type        = models.CharField(max_length=20, choices=TYPE_CHOICES, default='Residential')
    units       = models.PositiveIntegerField(default=0)
    price_range = models.CharField(max_length=100, blank=True)
    status      = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Active')
    description = models.TextField(blank=True)
    created_by  = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='created_projects',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    @property
    def lead_count(self):
        return self.leads.count()


class Lead(models.Model):
    SOURCE_CHOICES = [
        ('Walk-in',   'Walk-in'),
        ('Phone',     'Phone'),
        ('Online',    'Online'),
        ('Reference', 'Reference'),
        ('Email',     'Email'),
    ]
    STATUS_CHOICES = [
        ('New',  'New'),
        ('Cold', 'Cold'),
        ('Warm', 'Warm'),
        ('Lost', 'Lost'),
    ]

    name        = models.CharField(max_length=200)
    phone       = models.CharField(max_length=20)
    email       = models.EmailField(blank=True)
    project     = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='leads',
    )
    source      = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='Walk-in')
    status      = models.CharField(max_length=10, choices=STATUS_CHOICES, default='New')
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='assigned_leads',
    )
    budget         = models.CharField(max_length=100, blank=True)
    notes          = models.TextField(blank=True)
    next_followup  = models.DateField(null=True, blank=True)
    created_by  = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='created_leads',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} ({self.status})'


class LeadActivity(models.Model):
    TYPE_CHOICES = [
        ('Enquiry',       'Enquiry'),
        ('Call',          'Call'),
        ('Walk-in',       'Walk-in'),
        ('Status Change', 'Status Change'),
        ('Transfer',      'Transfer'),
        ('Note',          'Note'),
    ]

    lead       = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='activities')
    type       = models.CharField(max_length=20, choices=TYPE_CHOICES)
    note       = models.TextField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.lead.name} – {self.type}'
