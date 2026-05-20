from django.db import models
from django.conf import settings


class AttendanceRecord(models.Model):
    user        = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='attendance_records')
    date        = models.DateField()
    in_time     = models.TimeField(null=True, blank=True)
    out_time    = models.TimeField(null=True, blank=True)
    total_hours = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)

    class Meta:
        unique_together = ('user', 'date')
        ordering = ['-date']

    def __str__(self):
        return f'{self.user.user_code} - {self.date}'


class LeaveApplication(models.Model):

    WORK_TYPE_CHOICES = [('leave', 'Leave'), ('wfh', 'WFH')]
    LEAVE_TYPE_CHOICES = [
        ('paid_leave',    'Paid Leave'),
        ('sick_leave',    'Sick Leave'),
        ('casual_leave',  'Casual Leave'),
        ('lop',           'LOP'),
    ]
    DAY_TYPE_CHOICES  = [('full_day', 'Full Day'), ('half_day', 'Half Day')]
    SESSION_CHOICES   = [('first_half', 'First Half'), ('second_half', 'Second Half')]
    STATUS_CHOICES    = [('pending', 'Pending'), ('approved', 'Approved'), ('rejected', 'Rejected')]

    user        = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='leave_applications')
    work_type   = models.CharField(max_length=10,  choices=WORK_TYPE_CHOICES,  default='leave')
    leave_type  = models.CharField(max_length=20,  choices=LEAVE_TYPE_CHOICES)
    day_type    = models.CharField(max_length=10,  choices=DAY_TYPE_CHOICES,   default='full_day')
    session     = models.CharField(max_length=20,  choices=SESSION_CHOICES,    null=True, blank=True)
    from_date   = models.DateField()
    to_date     = models.DateField(null=True, blank=True)
    description = models.TextField(blank=True)
    status      = models.CharField(max_length=10,  choices=STATUS_CHOICES,     default='pending')
    applied_on  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-applied_on']

    def __str__(self):
        return f'{self.user.user_code} - {self.leave_type} - {self.from_date}'


class LeaveTransaction(models.Model):
    DESCRIPTION_CHOICES = [
        ('monthly_credit', 'Monthly Credit'),
        ('leave_applied',  'Leave Applied'),
        ('manual_credit',  'Manual Credit'),
        ('manual_debit',   'Manual Debit'),
    ]
    LEAVE_TYPE_CHOICES = [
        ('paid_leave',   'Paid Leave'),
        ('sick_leave',   'Sick Leave'),
        ('casual_leave', 'Casual Leave'),
        ('lop',          'LOP'),
    ]

    user        = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='leave_transactions')
    date        = models.DateTimeField()
    leave_date  = models.DateField(null=True, blank=True)
    leave_type  = models.CharField(max_length=20, choices=LEAVE_TYPE_CHOICES, default='paid_leave')
    description = models.CharField(max_length=30, choices=DESCRIPTION_CHOICES)
    change      = models.DecimalField(max_digits=5, decimal_places=1)
    balance     = models.DecimalField(max_digits=5, decimal_places=1)

    class Meta:
        ordering = ['-date']

    def __str__(self):
        return f'{self.user.user_code} - {self.description} - {self.change}'


class LeaveBalance(models.Model):
    user      = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='leave_balance')
    available = models.DecimalField(max_digits=5, decimal_places=1, default=0.0)
    utilised  = models.DecimalField(max_digits=5, decimal_places=1, default=0.0)

    def __str__(self):
        return f'{self.user.user_code} - Leave Balance'
