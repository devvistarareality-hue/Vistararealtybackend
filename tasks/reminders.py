"""Scheduled Task Allocation notifications, run by `run_scheduled_notifications`
on the cron — same idempotency pattern as `receivables/reminders.py`: a
Notification row already sent for a task+due-date pair is never sent twice."""
from datetime import timedelta

from django.utils import timezone

from accounts.models import Notification
from notifications import notify_many

from .models import Task

DUE_SOON_DAYS = 2


def due_soon(now, dry=False):
    """A task due within DUE_SOON_DAYS, still open, gets its assignees nudged
    once per due date (a due_date edit naturally resets the reminder since the
    idempotency key includes the date)."""
    horizon = timezone.localdate() + timedelta(days=DUE_SOON_DAYS)
    qs = Task.objects.filter(
        archived=False, due_date__isnull=False, due_date__lte=horizon, due_date__gte=timezone.localdate(),
    ).exclude(status='done').prefetch_related('assignees')
    n = 0
    for t in qs.iterator():
        assignees = [u for u in t.assignees.all() if u.is_active]
        if not assignees:
            continue
        due_key = t.due_date.isoformat()
        already = Notification.objects.filter(
            type='task_due_soon', data__task_id=t.id, data__due=due_key,
        ).exists()
        if already:
            continue
        try:
            when = 'today' if t.due_date == timezone.localdate() else f'on {t.due_date.strftime("%d %b")}'
            if not dry:
                notify_many(assignees, 'task_due_soon', 'Task due soon',
                            f'"{t.title}" is due {when}.', {'task_id': t.id, 'due': due_key})
            n += 1
        except Exception:
            pass
    return n


def run(now, dry=False):
    return {'task_due_soon': due_soon(now, dry)}
