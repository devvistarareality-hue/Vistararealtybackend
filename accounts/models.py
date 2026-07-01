import uuid
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from companies.models import Company


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError('Email is required')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault('is_staff', True)
        extra.setdefault('is_superuser', True)
        return self.create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    company      = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='users', null=True, blank=True)
    user_code    = models.CharField(max_length=20, blank=True)
    name         = models.CharField(max_length=100, blank=True)
    email        = models.EmailField(unique=True)
    phone        = models.CharField(max_length=20, blank=True)
    role         = models.CharField(max_length=100, blank=True)
    department   = models.CharField(max_length=100, blank=True)
    designation  = models.CharField(max_length=100, blank=True)
    avatar_url      = models.URLField(blank=True)
    modules            = models.JSONField(default=list, blank=True)
    manager_modules    = models.JSONField(default=list, blank=True)
    reporting_manager  = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='subordinates'
    )
    is_active         = models.BooleanField(default=True)
    is_staff          = models.BooleanField(default=False)
    date_joined       = models.DateTimeField(auto_now_add=True)
    session_token_app = models.UUIDField(default=uuid.uuid4)
    session_token_web = models.UUIDField(default=uuid.uuid4)

    objects = UserManager()

    USERNAME_FIELD  = 'email'
    REQUIRED_FIELDS = []

    class Meta:
        unique_together = ('company', 'user_code')

    def __str__(self):
        return f'{self.user_code} ({self.company.code if self.company else "admin"})'


class Notification(models.Model):
    """In-app / web notification. Mirrors a OneSignal push but is also queryable
    so the bell shows history + unread counts on web and app."""
    recipient  = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    type       = models.CharField(max_length=40, blank=True)   # new_lead, followup, sv_done, booking_approved …
    title      = models.CharField(max_length=180)
    body       = models.TextField(blank=True)
    data       = models.JSONField(default=dict, blank=True)     # deep-link payload {lead_id, booking_id, …}
    is_read    = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['recipient', 'is_read']),
            models.Index(fields=['recipient', '-created_at']),
        ]

    def __str__(self):
        return f'{self.type} → {self.recipient_id}: {self.title}'


class Designation(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='designations')
    name    = models.CharField(max_length=100)
    module  = models.CharField(max_length=100)

    class Meta:
        unique_together = ('company', 'name', 'module')
        ordering = ['module', 'name']

    def __str__(self):
        return f'{self.name} ({self.module})'
