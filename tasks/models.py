"""Task Allocation — a lightweight, ClickUp-style task tracker.

Company-wide by design: any active user in a company can see every Task List
and Task in that company, and assign a task to any other active user in the
same company. There is no reporting-tree restriction here, unlike the Sales
module's manager-tree visibility (`_visible_user_ids`) — assigning work across
teams and roles is the whole point of this module.

Titles and structural fields (status, priority, dates) stay plain so they can
be filtered/sorted/grouped in SQL (board columns, "overdue" queries, search).
Anything a person actually writes — a description, a checklist line, a
comment — is encrypted at rest, matching the rest of the codebase's split.
"""
from django.conf import settings
from django.db import models

from sales.fields import EncryptedTextField


class TaskList(models.Model):
    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='task_lists')
    name = models.CharField(max_length=120)
    description = EncryptedTextField(blank=True)
    color = models.CharField(max_length=20, blank=True, default='')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        indexes = [models.Index(fields=['company', 'archived'])]

    def __str__(self):
        return self.name


class TaskTag(models.Model):
    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='task_tags')
    name = models.CharField(max_length=40)
    color = models.CharField(max_length=20, blank=True, default='')

    class Meta:
        ordering = ['name']
        unique_together = [('company', 'name')]

    def __str__(self):
        return self.name


class Task(models.Model):
    STATUS = [
        ('todo', 'To Do'), ('in_progress', 'In Progress'), ('in_review', 'In Review'),
        ('done', 'Done'), ('blocked', 'Blocked'),
    ]
    PRIORITY = [('urgent', 'Urgent'), ('high', 'High'), ('normal', 'Normal'), ('low', 'Low')]

    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='tasks')
    task_list = models.ForeignKey(TaskList, on_delete=models.CASCADE, related_name='tasks')
    title = models.CharField(max_length=255)
    description = EncryptedTextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default='todo')
    priority = models.CharField(max_length=10, choices=PRIORITY, default='normal')
    due_date = models.DateField(null=True, blank=True)
    start_date = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_tasks')
    assignees = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name='assigned_tasks')
    tags = models.ManyToManyField(TaskTag, blank=True, related_name='tasks')
    position = models.IntegerField(default=0)
    completed_at = models.DateTimeField(null=True, blank=True)
    archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['position', '-created_at']
        indexes = [
            models.Index(fields=['company', 'status']),
            models.Index(fields=['task_list', 'status']),
            models.Index(fields=['company', 'due_date']),
        ]

    def __str__(self):
        return self.title


class TaskChecklistItem(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='checklist_items')
    text = EncryptedTextField()
    is_done = models.BooleanField(default=False)
    order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'id']


class TaskComment(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='comments')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    body = EncryptedTextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']
