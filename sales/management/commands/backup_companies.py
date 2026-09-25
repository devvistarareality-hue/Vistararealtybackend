"""Take the Excel backups that are due. Run from cron."""
from django.core.management.base import BaseCommand

from sales.backup_schedule import run_due


class Command(BaseCommand):
    help = "Take a scheduled Excel backup for every company that is due one."

    def handle(self, *args, **options):
        taken = run_due()
        if not taken:
            self.stdout.write('No company was due a backup.')
            return
        for name, rows in taken:
            self.stdout.write(f'Backed up {name}: {rows} rows')
