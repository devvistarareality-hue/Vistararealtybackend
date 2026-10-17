"""Importing investors from Excel: checked row by row, saved as pending (Approvals)."""
import io
from datetime import date
from decimal import Decimal

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from club1000.models import Investor, Scheme
from companies.models import Company


class InvestorImportTests(TestCase):
    def setUp(self):
        self.co = Company.objects.create(code='IMP', name='Imp Co', is_active=True)
        self.admin = User.objects.create_user('a@imp.com', company=self.co, user_code='IMP1', password='p',
                                              name='Admin', role='Admin', modules=['Club 1000'])
        self.scheme = Scheme.objects.create(company=self.co, name='RISE', tenure_months=12, min_ticket_size=100000,
                                            interest_payout_options=['quarterly', 'maturity'],
                                            payout_rates={'quarterly': '15', 'maturity': '15'})
        self.api = APIClient(); self.api.force_authenticate(self.admin)

    def _file(self, rows, headers=None):
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = 'Investors'
        ws.append(headers or ['Investor Name', 'Mobile', 'Scheme', 'Amount Invested', 'Investment Date', 'Interest Payout', 'Reference Name', 'Reference Mobile'])
        for r in rows:
            ws.append(r)
        buf = io.BytesIO(); wb.save(buf)
        return SimpleUploadedFile('inv.xlsx', buf.getvalue())

    def test_template_downloads(self):
        r = self.api.get('/api/club1000/investors/import-template/')
        self.assertEqual(r.status_code, 200)
        wb = openpyxl.load_workbook(io.BytesIO(r.content))
        self.assertIn('Investors', wb.sheetnames)
        self.assertEqual(wb['Investors'].cell(row=1, column=1).value, 'Investor Name *')   # must-fill marked
        self.assertIn('Times Reinvested', [c.value for c in wb['Investors'][1]])

    def test_reinvested_money_is_one_row_with_its_count(self):
        f = self._file([['Old One *', '9800000009', 'RISE', 700000, '01-04-2025', 'maturity', 3, '01-04-2019'],
                        ['No Count', '9800000008', 'RISE', 700000, '01-04-2025', 'maturity', 'two', '']],
                       headers=['Investor Name *', 'Mobile *', 'Scheme *', 'Amount Invested *', 'Investment Date *',
                                'Interest Payout', 'Times Reinvested', 'First Invested On'])
        r = self.api.post('/api/club1000/investors/import/', {'file': f, 'commit': 'true'}, format='multipart')
        self.assertEqual((r.data['created'], r.data['failed']), (1, 1), r.data)
        self.assertTrue(any('Times Reinvested' in e for e in r.data['rows'][1]['errors']))
        inv = Investor.objects.get()
        self.assertEqual((inv.renewal_count, inv.first_invested_on, inv.investment_date), (3, date(2019, 4, 1), date(2025, 4, 1)))

    def test_row_missing_a_must_fill_column_is_not_imported(self):
        f = self._file([['Has All', '9800000007', 'RISE', 500000, '01-04-2025', '', '', ''],
                        ['No Amount', '9800000006', 'RISE', '', '01-04-2025', '', '', '']])
        r = self.api.post('/api/club1000/investors/import/', {'file': f, 'commit': 'true'}, format='multipart')
        self.assertEqual((r.data['created'], r.data['failed']), (1, 1))
        self.assertEqual([i.name for i in Investor.objects.all()], ['Has All'])

    def test_check_reports_errors_and_saves_nothing(self):
        f = self._file([['A One', '9800000001', 'RISE', 500000, '01-04-2025', 'quarterly', 'Ref', '9811111111'],
                        ['', '98', 'NOPE', 'abc', '2099-01-01', 'weekly', '', '']])
        r = self.api.post('/api/club1000/investors/import/', {'file': f}, format='multipart')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data['ok'], r.data['failed'], r.data['created']), (1, 1, 0))
        bad = r.data['rows'][1]
        self.assertEqual(bad['row'], 3)
        self.assertTrue(any('Scheme' in e for e in bad['errors']))
        self.assertEqual(Investor.objects.count(), 0)

    def test_commit_saves_pending_investors(self):
        f = self._file([['A One', '9800000001', 'RISE', 500000, '01-04-2025', 'quarterly', 'Ref', '9811111111'],
                        ['B Two', '9800000002', 'rise', '2,00,000', '15/08/2025', '', '', '']])
        r = self.api.post('/api/club1000/investors/import/', {'file': f, 'commit': 'true'}, format='multipart')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['created'], 2)
        by = {i.name: i for i in Investor.objects.all()}   # names are encrypted — match in Python
        a = by['A One']
        self.assertEqual((a.approval_status, a.interest_payout, a.amount_invested, a.investment_date, a.maturity_date),
                         ('pending', 'quarterly', Decimal('500000'), date(2025, 4, 1), date(2026, 4, 1)))
        self.assertEqual(a.payouts.count(), 0)          # schedule waits for approval
        b = by['B Two']
        self.assertEqual((b.interest_payout, b.amount_invested), ('quarterly', Decimal('200000')))

    def test_same_investment_twice_is_refused(self):
        row = ['A One', '9800000001', 'RISE', 500000, '01-04-2025', 'quarterly', '', '']
        self.api.post('/api/club1000/investors/import/', {'file': self._file([row]), 'commit': 'true'}, format='multipart')
        r = self.api.post('/api/club1000/investors/import/', {'file': self._file([row])}, format='multipart')
        self.assertEqual(r.data['failed'], 1)
        self.assertTrue(any('Already in Nexora' in e for e in r.data['rows'][0]['errors']))

    def test_staff_cannot_import(self):
        staff = User.objects.create_user('s@imp.com', company=self.co, user_code='IMP2', password='p',
                                         name='Staff', role='Employee', modules=['Club 1000'])
        api = APIClient(); api.force_authenticate(staff)
        self.assertEqual(api.get('/api/club1000/investors/import-template/').status_code, 403)
