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

from sales.backup_excel import ALL_TABLES, RESTORABLE

# The apps that hold a company's own records.
LOCAL_APPS = {'sales', 'club1000', 'receivables', 'tasks', 'attendance',
              'accounts', 'companies', 'activity'}

# Deliberately outside a company backup. Each needs a reason, not just a name.
EXCLUDED = {
    'companies.Company':    'the backup is OF a company — this row is the container, not content',
    'accounts.OtpCode':     'short-lived login codes: a liability in a downloaded file, no value in a backup',
    'sales.BackupSettings': "the backup system's own schedule, platform-level rather than a company's data",
    'sales.BackupRecord':   "the backup system's own history, platform-level rather than a company's data",
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


class NothingWipedIsUnrestorable(SimpleTestCase):
    """Anything Data Reset destroys has to be restorable, or a reset is one-way.

    The catch is cascade: SalesDataResetView names nine tables, but deleting a
    Lead takes every CASCADE child with it. LeadTransfer was exactly that —
    wiped by a reset, absent from the restore, gone for good.
    """

    WIPED = {'sales.Lead', 'sales.Closure', 'sales.Booking'}

    def test_cascade_children_of_wiped_tables_are_restorable(self):
        restorable = {t.model for t in RESTORABLE}
        lost = []
        for model in _company_models():
            for f in model._meta.fields:
                if not (f.is_relation and f.related_model):
                    continue
                if f.related_model._meta.label not in self.WIPED:
                    continue
                on_delete = getattr(getattr(f, 'remote_field', None), 'on_delete', None)
                if getattr(on_delete, '__name__', '') == 'CASCADE' \
                        and model._meta.label not in restorable:
                    lost.append(f'{model._meta.label}.{f.name}')
        self.assertEqual(lost, [], (
            'These are CASCADE-deleted when Data Reset wipes leads/closures/bookings '
            'but are not restorable, so a reset would destroy them permanently: '
            + ', '.join(lost) + '. Mark their table restorable=True in SHEETS.'))

    def test_restorable_tables_come_after_what_they_point_at(self):
        """Restore writes in SHEETS order, so a parent must be listed first."""
        order = {t.model: i for i, t in enumerate(RESTORABLE)}
        for table in RESTORABLE:
            for f in table.cls._meta.fields:
                if not (f.is_relation and f.related_model):
                    continue
                target = f.related_model._meta.label
                if target in order and target != table.model:
                    with self.subTest(table=table.label, fk=f.name):
                        self.assertLess(
                            order[target], order[table.model],
                            f'{table.label}.{f.name} points at {target}, which is '
                            f'restored later — move it earlier in SHEETS.')
