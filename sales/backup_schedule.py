"""Taking a company's Excel backup on a schedule, and keeping it.

The on-demand backup streams straight to the browser and is never stored. A
scheduled one has nobody watching, so it goes to the private backups bucket and
is fetched later through a signed URL.
"""
import logging
import re
from datetime import timedelta
from io import BytesIO

from django.utils import timezone

logger = logging.getLogger(__name__)

# How long after the last one a schedule is due again.
EVERY = {'daily': timedelta(days=1), 'weekly': timedelta(days=7), 'monthly': timedelta(days=30)}


def due(schedule, now=None):
    """Whether this schedule should produce a backup now."""
    if not schedule.is_enabled:
        return False
    from .models import BackupStamp
    now = now or timezone.now()
    last = (BackupStamp.objects
            .filter(company=schedule.company, automatic=True)
            .exclude(file_path='')
            .order_by('-taken_at')
            .first())
    if last is None:
        return True
    return (now - last.taken_at) >= EVERY.get(schedule.frequency, EVERY['weekly'])


def take(schedule, triggered_by=None):
    """Build this company's workbook, store it, and record the stamp."""
    from .backup_excel import MODULES, build_workbook, reset_counts
    from .backup_storage import ensure_backup_bucket, upload_backup
    from .models import BackupStamp

    company = schedule.company
    # Always the whole company: a partial workbook could not be restored alone.
    modules = list(MODULES)

    buf = BytesIO()
    build_workbook(company).save(buf)
    payload = buf.getvalue()

    safe = re.sub(r'[^A-Za-z0-9]+', '-', company.name).strip('-') or 'company'
    name = f'{safe}-{timezone.localtime().strftime("%Y%m%d-%H%M")}.xlsx'

    ensure_backup_bucket()
    path = upload_backup(payload, name)
    if not path:
        logger.error('Scheduled backup for company %s could not be stored', company.id)
        return None

    stamp = BackupStamp.objects.create(
        company=company, taken_by=triggered_by, modules=modules, automatic=True,
        rows=sum(reset_counts(company).values()),
        file_path=path, file_size=len(payload))
    prune(schedule)
    return stamp


def prune(schedule):
    """Keep only the newest `keep_last` stored backups for this company."""
    from .models import BackupStamp
    keep = max(1, schedule.keep_last or 10)
    stored = (BackupStamp.objects
              .filter(company=schedule.company, automatic=True)
              .exclude(file_path='')
              .order_by('-taken_at'))
    for old in stored[keep:]:
        old.delete()


def run_due(now=None):
    """Every schedule that is due. Used by the cron command."""
    from .models import BackupSchedule
    taken = []
    for schedule in BackupSchedule.objects.filter(is_enabled=True).select_related('company'):
        if not due(schedule, now):
            continue
        try:
            stamp = take(schedule)
            if stamp:
                taken.append((schedule.company.name, stamp.rows))
        except Exception:
            # One company's failure must not stop the rest of the run.
            logger.exception('Scheduled backup failed for company %s', schedule.company_id)
    return taken
