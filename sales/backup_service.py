"""Core backup orchestration — shared by the cron command and the manual
"Run Backup Now" admin view, so there's exactly one code path for "make a backup".
"""
import gzip
from datetime import timedelta
from io import BytesIO, StringIO

from django.core.management import call_command
from django.utils import timezone

from .backup_storage import ensure_backup_bucket, upload_backup
from .models import BackupRecord, BackupSettings

# Full dump of every business-data app. Deliberately excludes Django's own
# contenttypes/auth.permission/sessions — regenerable, not real business data.
BACKUP_APPS = ['companies', 'accounts', 'attendance', 'sales', 'club1000']

FREQUENCY_DAYS = {'weekly': 7, 'monthly': 30, 'yearly': 365}


def is_backup_due(settings_row=None):
    """Whether the configured schedule calls for a new backup right now."""
    settings_row = settings_row or BackupSettings.objects.first()
    if not settings_row or not settings_row.is_enabled:
        return False
    last_success = BackupRecord.objects.filter(status='success').order_by('-completed_at').first()
    if not last_success or not last_success.completed_at:
        return True
    days = FREQUENCY_DAYS.get(settings_row.frequency, 7)
    return timezone.now() - last_success.completed_at >= timedelta(days=days)


def run_backup(triggered_by=None):
    """Dump every business-data app to gzipped JSON, upload it to the private
    'backups' Supabase bucket, and record the outcome. Never raises — failures
    are recorded on the BackupRecord instead, so a cron run can't crash silently."""
    ensure_backup_bucket()
    record = BackupRecord.objects.create(status='running', triggered_by=triggered_by)
    try:
        buf = StringIO()
        call_command('dumpdata', *BACKUP_APPS, stdout=buf, indent=None)
        gz = BytesIO()
        with gzip.GzipFile(fileobj=gz, mode='wb') as f:
            f.write(buf.getvalue().encode('utf-8'))
        payload = gz.getvalue()

        ts = timezone.now().strftime('%Y%m%d_%H%M%S')
        name = f'backup_{ts}_{record.id}.json.gz'
        upload_backup(payload, name)

        record.status = 'success'
        record.file_path = name
        record.file_size_bytes = len(payload)
        record.completed_at = timezone.now()
        record.save(update_fields=['status', 'file_path', 'file_size_bytes', 'completed_at'])
    except Exception as e:
        record.status = 'failed'
        record.error_message = str(e)[:2000]
        record.completed_at = timezone.now()
        record.save(update_fields=['status', 'error_message', 'completed_at'])
    return record
