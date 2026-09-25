"""The Excel backup must not silently miss anything.

New columns look after themselves — the export reads `model._meta.fields`, so a
field added tomorrow is in tomorrow's backup with no code change. New *models*
do not: SHEETS is a written list, and a module added without touching it would
be quietly absent from every backup taken afterwards. That has already happened
once here — the scheduled JSON dump's BACKUP_APPS never gained `receivables` or
`tasks`, so two whole modules were missing from backups and nobody noticed.

These tests are the tripwire. Add a model, and one of them fails telling you
where to put it.
"""
from django.apps import apps
from django.test import SimpleTestCase

from sales.backup_excel import ALL_TABLES, RESTORABLE, _columns, restore_order

# The apps that hold a company's own records.
LOCAL_APPS = {'sales', 'club1000', 'receivables', 'tasks', 'attendance',
              'accounts', 'companies', 'activity'}

# Deliberately outside a company backup. Each needs a reason, not just a name.
EXCLUDED = {
    'companies.Company':    'the backup is OF a company — this row is the container, not content',
    'accounts.OtpCode':     'short-lived login codes: a liability in a downloaded file, no value in a backup',
    'sales.BackupStamp':    'proof a backup was taken, used to gate a reset — about the backup, not in it',
    'sales.BackupSchedule': 'when to take a backup — configuration of the backup, not data in it',
}


def _company_models():
    for model in apps.get_models():
        if model._meta.app_label not in LOCAL_APPS or model._meta.auto_created:
            continue
        yield model


class EveryModelIsAccountedFor(SimpleTestCase):

    def test_every_model_is_backed_up_or_deliberately_excluded(self):
        covered = {t.model for t in ALL_TABLES}
        missing = [m._meta.label for m in _company_models()
                   if m._meta.label not in covered and m._meta.label not in EXCLUDED]
        self.assertEqual(missing, [], (
            'These models are in no backup sheet and not excluded, so a backup '
            'would silently leave them out: ' + ', '.join(missing) +
            '. Add each to SHEETS in sales/backup_excel.py with the ORM path from '
            'it to companies.Company, or to EXCLUDED here with a reason.'))

    def test_nothing_is_listed_twice(self):
        labels = [t.model for t in ALL_TABLES]
        dupes = sorted({l for l in labels if labels.count(l) > 1})
        self.assertEqual(dupes, [], f'listed in more than one sheet: {dupes}')

    def test_table_labels_are_unique(self):
        # Restore finds a table by the label in column A, so two tables sharing
        # one would make the rows of the second land in the first.
        labels = [t.label for t in ALL_TABLES]
        dupes = sorted({l for l in labels if labels.count(l) > 1})
        self.assertEqual(dupes, [], f'duplicate table labels: {dupes}')

    def test_every_scope_actually_reaches_a_company(self):
        """A typo in a scope path would only surface as an empty sheet."""
        for table in ALL_TABLES:
            with self.subTest(table=table.label):
                model, path = table.cls, table.scope
                for i, part in enumerate(path.split('__')):
                    field = model._meta.get_field(part)     # raises if the path is wrong
                    model = field.related_model
                self.assertEqual(model._meta.label, 'companies.Company',
                                 f'{table.label}: scope {path!r} ends at {model._meta.label}')


class EverythingCanBeRestored(SimpleTestCase):
    """The workbook has to rebuild a company from nothing, not just undo a reset."""

    def test_every_exported_table_is_also_restorable(self):
        missing = [t.label for t in ALL_TABLES if t not in RESTORABLE]
        self.assertEqual(missing, [], (
            'Exported but never written back, so a wiped company could not be '
            'rebuilt from its own backup: ' + ', '.join(missing)))

    def test_restore_order_puts_parents_first(self):
        """Restore follows restore_order(); a parent must come before its child.

        Worked out from the models rather than the order the sheets happen to be
        in — one wrong position is an insert that fails part-way through a
        restore. Closure.site_visit was exactly that: closures were written
        before the site visits they name.
        """
        order = {t.model: i for i, t in enumerate(restore_order())}
        self.assertEqual(len(order), len(ALL_TABLES), 'restore_order() dropped a table')
        for table in ALL_TABLES:
            for f in table.cls._meta.fields:
                if not (f.is_relation and f.related_model):
                    continue
                target = f.related_model._meta.label
                if target in order and target != table.model:
                    with self.subTest(table=table.label, fk=f.name):
                        self.assertLess(
                            order[target], order[table.model],
                            f'{table.label}.{f.name} points at {target}, which is '
                            f'restored later.')

    def test_live_session_tokens_never_reach_the_file(self):
        """Password hashes are in, by choice — a restored account has to be
        signable-into. Session tokens are not: they are live credentials, they
        rotate, and nothing about a restore needs them."""
        user_cols = [h for h, _, _ in _columns(apps.get_model('accounts.User'))]
        self.assertIn('password', user_cols)
        for leaked in ('session_token_web', 'session_token_app', 'email_key'):
            self.assertNotIn(leaked, user_cols)
