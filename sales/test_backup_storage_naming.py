"""A stored backup has to come back as the workbook it is.

The scheduled backup is an .xlsx, but backup_storage was written when the only
stored backup was a gzipped JSON dump and hard-coded 'application/gzip' on every
upload. Supabase served it back under that type, so the browser saved a valid
workbook as "Company-20260925-1641.gz" — a file Excel refuses to open and the
Restore picker refuses to list. Nothing was corrupt; a header was wrong.
"""
from django.test import SimpleTestCase

from sales.backup_storage import XLSX_TYPE, content_type_for


class StoredBackupContentType(SimpleTestCase):

    def test_a_workbook_is_labelled_a_workbook(self):
        self.assertEqual(content_type_for('Acme-20260925-1641.xlsx'), XLSX_TYPE)

    def test_case_does_not_matter(self):
        self.assertEqual(content_type_for('Acme.XLSX'), XLSX_TYPE)

    def test_the_old_gzip_dumps_still_say_gzip(self):
        self.assertEqual(content_type_for('db-20260101.json.gz'), 'application/gzip')

    def test_anything_else_is_not_guessed_at(self):
        self.assertEqual(content_type_for('mystery'), 'application/octet-stream')

    def test_a_workbook_is_never_labelled_gzip(self):
        """The regression itself: this is the assertion that was false."""
        self.assertNotEqual(content_type_for('Acme-backup.xlsx'), 'application/gzip')
