"""Activity log: who did what, when — across every module.

One row per change made through the API (create, edit, delete, approve, reject,
cancel…). Rows are only ever added, never edited or deleted by the app. What was
done to whom can name a customer, so the description, details and IP address are
encrypted; who, when, which module and which record stay plain so the log can be
filtered.
"""
from django.conf import settings
from django.db import models

from sales.fields import EncryptedTextField


class ActivityLog(models.Model):
    company = models.ForeignKey('companies.Company', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    # Kept even if the user is later deleted.
    actor_name = EncryptedTextField(blank=True)
    module = models.CharField(max_length=30, db_index=True)
    action = models.CharField(max_length=30)
    target_type = models.CharField(max_length=40, blank=True)
    target_id = models.CharField(max_length=40, blank=True)
    summary = EncryptedTextField(blank=True)
    details = EncryptedTextField(blank=True)   # JSON
    method = models.CharField(max_length=8, blank=True)
    path = models.CharField(max_length=255, blank=True)
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    ip = EncryptedTextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['company', '-created_at']),
            models.Index(fields=['target_type', 'target_id']),
            models.Index(fields=['actor', '-created_at']),
        ]
