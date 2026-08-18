"""Scheduled full-database backup — run daily on a cron; only actually produces
a new backup when the configured schedule (BackupSettings.frequency) says one is
due, so a daily check safely serves a weekly/monthly/yearly cadence.

Safe to call from Railway cron; never raises (see backup_service.run_backup).

Usage: python manage.py backup_database [--force]
"""
from django.core.management.base import BaseCommand

from sales.backup_service import is_backup_due, run_backup
from sales.models import BackupSettings


class Command(BaseCommand):
    help = 'Run the scheduled full-database backup if the configured period says one is due.'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true',
                             help='Run a backup now regardless of schedule/enabled state.')

    def handle(self, *args, **opts):
        settings_row, _ = BackupSettings.objects.get_or_create(pk=1)
        if not opts['force'] and not is_backup_due(settings_row):
            self.stdout.write('No backup due yet — skipping.')
            return

        record = run_backup()
        if record.status == 'success':
            self.stdout.write(self.style.SUCCESS(
                f'Backup #{record.id} succeeded — {record.file_path} ({record.file_size_bytes} bytes).'
            ))
        else:
            self.stderr.write(self.style.ERROR(f'Backup #{record.id} failed: {record.error_message}'))
