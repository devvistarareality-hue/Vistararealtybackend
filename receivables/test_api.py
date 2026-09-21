"""AR API tests — run against local SQLite, never the production database:

    DATABASE_URL= DB_ENGINE=django.db.backends.sqlite3 ./venv/bin/python manage.py test receivables
"""
import io
from datetime import date, timedelta
from decimal import Decimal as D

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Company
from sales.models import Booking, Project
from receivables.models import ARAccount, ARReceipt, ARReceiptAudit


def make_booking(company, project, plot='10', **kw):
    base = dict(
        company=company, project=project, plot_numbers=plot, client_name='Jigar Makwana', phone='8160191851',
        status='sold', accounts_status='approved',
        installments=[
            {'no': 1, 'date': '2025-08-01', 'amt': 100000},
            {'no': 2, 'date': '2025-09-12', 'amt': 4050000},
            {'no': 3, 'date': '2025-09-30', 'amt': 2500000},
            {'no': 4, 'date': '2025-10-12', 'amt': 4875000},
            {'no': 5, 'date': '2025-11-12', 'amt': 4875000},
        ],
        # Legal & Other = total_extra − stamp − reg = 3,16,923 (Legal 40,000 + Maint 2,76,923)
        total_extra=D('316923'), stamp_duty=D('0'), reg_fees=D('0'), final_amount=D('16716923'),
    )
    base.update(kw)
    return Booking.objects.create(**base)


class ARApiTests(TestCase):
    def setUp(self):
        self.co = Company.objects.create(code='VIS', name='Vistara')
        self.other = Company.objects.create(code='OTH', name='Other')
        self.project = Project.objects.create(company=self.co, name='Kalrav 2')
        self.user = User.objects.create_user('ar1@test.local', company=self.co, user_code='AR1', password='x', name='AR User',
                                             role='Employee', modules=['AR'])
        self.nobody = User.objects.create_user('no1@test.local', company=self.co, user_code='NO1', password='x', name='No AR',
                                               role='Employee', modules=['Sales'])
        self.booking = make_booking(self.co, self.project)
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def acct(self):
        rows = self.api.get('/api/ar/accounts/').json()['results']
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_approved_booking_becomes_an_account_with_its_plan(self):
        a = self.acct()
        self.assertEqual(a['collectable'], 16716923)
        self.assertEqual(a['plan_mismatch'], 0)
        ledger = self.api.get(f"/api/ar/accounts/{a['id']}/").json()
        self.assertEqual([p['no'] for p in ledger['plan']], ['1', '2', '3', '4', '5', 'L'])
        self.assertIsNone(ledger['plan'][-1]['due'])  # legal line has no date until set

    def test_not_yet_accounts_approved_is_excluded(self):
        self.booking.accounts_status = 'pending'
        self.booking.save()
        self.assertEqual(self.api.get('/api/ar/accounts/').json()['results'], [])

    def test_access_is_module_gated_and_company_scoped(self):
        c = APIClient()
        c.force_authenticate(self.nobody)
        self.assertEqual(c.get('/api/ar/accounts/').status_code, 403)
        a = self.acct()
        outsider = User.objects.create_user('ot1@test.local', company=self.other, user_code='OT1', password='x', name='O',
                                            role='Employee', modules=['AR'])
        c.force_authenticate(outsider)
        self.assertEqual(c.get(f"/api/ar/accounts/{a['id']}/").status_code, 404)

    def test_receipt_lifecycle_is_audited(self):
        a = self.acct()
        r = self.api.post(f"/api/ar/accounts/{a['id']}/receipts/",
                          {'paid_on': '2025-08-01', 'amount': '100000', 'mode': 'nbfc', 'remarks': 'first'}, format='json')
        self.assertEqual(r.status_code, 201)
        rid = r.json()['id']
        self.assertEqual(self.api.get(f"/api/ar/accounts/{a['id']}/").json()['received'], 100000)

        self.api.patch(f'/api/ar/receipts/{rid}/', {'amount': '90000'}, format='json')
        self.api.delete(f'/api/ar/receipts/{rid}/')
        self.assertEqual(self.api.get(f"/api/ar/accounts/{a['id']}/").json()['received'], 0)
        self.assertTrue(ARReceipt.objects.get(pk=rid).is_deleted)  # soft delete
        audit = self.api.get(f'/api/ar/receipts/{rid}/audit/').json()
        self.assertEqual([x['action'] for x in audit], ['delete', 'update', 'create'])
        self.assertEqual(audit[1]['before']['amount'], '100000')
        self.assertEqual(audit[1]['after']['amount'], '90000')

    def test_receipt_validation(self):
        a = self.acct()
        future = (timezone.localdate() + timedelta(days=2)).isoformat()
        r = self.api.post(f"/api/ar/accounts/{a['id']}/receipts/", {'paid_on': future, 'amount': '0', 'mode': 'upi'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(set(r.json()), {'paid_on', 'amount', 'mode'})

    def test_cancellation_freezes_the_account(self):
        a = self.acct()
        ARReceipt.objects.create(account_id=a['id'], paid_on=date(2025, 8, 1), amount=D('100000'), mode='bank')
        self.booking.cancelled_at = timezone.now()
        self.booking.save()
        a = self.acct()
        self.assertEqual(a['status'], 'frozen')
        self.assertEqual(a['received'], 100000)  # history kept
        r = self.api.post(f"/api/ar/accounts/{a['id']}/receipts/", {'paid_on': '2025-09-01', 'amount': '5', 'mode': 'bank'}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_revision_replaces_plan_receipts_stay(self):
        a = self.acct()
        ARReceipt.objects.create(account_id=a['id'], paid_on=date(2025, 8, 1), amount=D('100000'), mode='bank')
        rev = make_booking(self.co, self.project, revision_of=self.booking, revision_no=1,
                           installments=[{'no': 1, 'date': '2025-08-01', 'amt': 16400000}])
        a2 = self.acct()
        self.assertEqual(a2['id'], a['id'])
        self.assertEqual(a2['booking_id'], rev.id)
        self.assertEqual(a2['received'], 100000)
        ledger = self.api.get(f"/api/ar/accounts/{a['id']}/").json()
        self.assertEqual([p['no'] for p in ledger['plan']], ['1', 'L'])

    def test_set_legal_due_date(self):
        a = self.acct()
        r = self.api.patch(f"/api/ar/accounts/{a['id']}/", {'legal_due_date': '2025-12-12'}, format='json').json()
        self.assertEqual(r['plan'][-1]['due'], '2025-12-12')

    def test_plot10_through_the_api(self):
        """The workbook's Plot 10, end to end: receipts in, ledger out."""
        a = self.acct()
        self.api.patch(f"/api/ar/accounts/{a['id']}/", {'legal_due_date': '2025-12-12'}, format='json')
        for d, amt, mode in [('2025-08-01', 100000, 'nbfc'), ('2025-09-12', 4050000, 'nbfc'), ('2025-09-28', 3000000, 'nbfc'),
                             ('2025-11-25', 2000000, 'nbfc'), ('2025-12-20', 1500000, 'nbfc'), ('2025-12-26', 2000000, 'bank'),
                             ('2026-01-03', 500000, 'bank'), ('2026-02-01', 1000000, 'nbfc'), ('2026-03-31', 1000000, 'nbfc'),
                             ('2026-07-16', 1567000, 'nbfc')]:
            ARReceipt.objects.create(account_id=a['id'], paid_on=date.fromisoformat(d), amount=D(amt), mode=mode)
        led = self.api.get(f"/api/ar/accounts/{a['id']}/?as_of=2026-09-21").json()
        self.assertEqual((led['received'], led['outstanding'], led['net_interest'], led['os_with_interest']),
                         (16717000, -77, 615052, 614975))

    def _tracker_xlsx(self):
        """Mimics the old workbook's Payment Tracker: one row per allocation, with
        Carry-In rows whose Paid is blank — those must NOT become receipts."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Payment Tracker'
        ws.append(['Project', 'Plot No', 'Installment No', 'Due Date', 'Budgeted', 'Carry-In', 'Net Due',
                   'Paid', 'Paid Dates', 'Mode', 'Remarks'])
        ws.append(['K2', 10, 1, '01/08/25', 100000, None, 100000, 100000, date(2025, 8, 1), 'NBFC', ''])
        ws.append(['K2', 10, 1, '01/08/25', None, None, None, 4050000, date(2025, 9, 12), 'NBFC', ''])
        ws.append(['K2', 10, 2, '12/09/25', 4050000, 4050000, None, None, date(2025, 9, 12), 'NBFC', '-'])  # carry-in
        ws.append(['K2', 10, 4, '12/10/25', 875000, None, 875000, 2000000, date(2025, 12, 26), 'BANK', 'TD-CHQ'])
        ws.append(['K2', 99, 1, '01/08/25', 1, None, 1, 50000, date(2025, 8, 1), 'BANK', 'unknown plot'])
        buf = io.BytesIO()
        wb.save(buf)
        return SimpleUploadedFile('ar.xlsx', buf.getvalue(),
                                  content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    def test_import_takes_only_real_receipts_and_is_idempotent(self):
        self.acct()
        preview = self.api.post('/api/ar/import/', {'file': self._tracker_xlsx(), 'project_id': self.project.id}, format='multipart').json()
        self.assertFalse(preview['committed'])
        self.assertEqual(preview['ready'], 3)                     # carry-in row ignored
        self.assertEqual(preview['total_amount'], 6150000)
        self.assertEqual([s['plot'] for s in preview['skipped_rows']], ['99'])
        self.assertEqual(ARReceipt.objects.count(), 0)            # preview writes nothing

        done = self.api.post('/api/ar/import/', {'file': self._tracker_xlsx(), 'project_id': self.project.id, 'commit': '1'}, format='multipart').json()
        self.assertTrue(done['committed'])
        self.assertEqual(ARReceipt.objects.filter(source='import').count(), 3)
        self.assertEqual(ARReceiptAudit.objects.filter(action='create').count(), 3)

        again = self.api.post('/api/ar/import/', {'file': self._tracker_xlsx(), 'project_id': self.project.id, 'commit': '1'}, format='multipart').json()
        self.assertEqual(again['ready'], 0)                       # already recorded → skipped
        self.assertEqual(ARReceipt.objects.count(), 3)

    def test_dashboard(self):
        a = self.acct()
        ARReceipt.objects.create(account_id=a['id'], paid_on=date(2025, 8, 1), amount=D('100000'), mode='bank')
        d = self.api.get('/api/ar/dashboard/?as_of=2026-09-21').json()
        self.assertEqual(d['accounts'], 1)
        self.assertEqual(d['totals']['received'], 100000)
        self.assertEqual(d['top_over_180'][0]['client'], 'Jigar Makwana')


class ARScheduleAndReportsTests(TestCase):
    """Bookings with no installments (every Pratishtha flat), the AR-entered
    schedule, the issue flags, the dashboard and the printable statement."""

    def setUp(self):
        self.co = Company.objects.create(code='VIS', name='Vistara Realty')
        self.project = Project.objects.create(company=self.co, name='Pratishtha')
        self.user = User.objects.create_user('ar2@test.local', company=self.co, user_code='AR2', password='x', name='AR User',
                                             role='Employee', modules=['AR'])
        # A flat: 28,00,000 deal, no installments; 1,50,000 extra of which stamp 40,000 + reg 10,000.
        self.flat = make_booking(self.co, self.project, plot='1003', client_name='Asha Patel', installments=[],
                                 total_extra=D('150000'), stamp_duty=D('40000'), reg_fees=D('10000'),
                                 final_amount=D('2800000'))
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.id = self.api.get('/api/ar/accounts/').json()['results'][0]['id']

    def ledger(self, as_of='2026-09-21'):
        return self.api.get(f'/api/ar/accounts/{self.id}/?as_of={as_of}').json()

    def test_no_schedule_still_totals_the_whole_deal(self):
        d = self.ledger()
        self.assertTrue(d['no_schedule'])
        self.assertEqual(d['collectable'], 2750000)                    # deal − stamp − reg
        self.assertEqual(d['plan_mismatch'], 0)
        self.assertEqual([p['kind'] for p in d['plan']], ['legal', 'balance'])
        self.assertEqual(d['plan'][-1]['amount'], 2650000)             # 27,50,000 − legal 1,00,000
        self.assertEqual(d['net_interest'], 0)
        self.assertTrue(d['schedule_editable'])
        self.assertEqual(d['schedule_target'], 2650000)

    def test_ar_schedule_must_add_up_then_drives_interest(self):
        url = f'/api/ar/accounts/{self.id}/'
        bad = self.api.patch(url, {'schedule': [{'date': '2026-01-01', 'amount': 1000000}]}, format='json')
        self.assertEqual(bad.status_code, 400)
        self.assertIn('26,50,000', bad.json()['detail'])
        ok = self.api.patch(url, {'schedule': [{'date': '2026-06-01', 'amount': 1650000},
                                               {'date': '2026-01-01', 'amount': 1000000}]}, format='json')
        self.assertEqual(ok.status_code, 200)
        d = self.ledger()
        self.assertFalse(d['no_schedule'])
        self.assertTrue(d['ar_schedule'])
        self.assertEqual([(p['no'], p['due']) for p in d['plan'][:2]], [('1', '2026-01-01'), ('2', '2026-06-01')])
        self.assertNotIn('balance', [p['kind'] for p in d['plan']])
        self.assertEqual(d['overdue'], 2650000)
        self.assertGreater(d['net_interest'], 0)
        self.assertEqual(d['schedule_by'], 'AR User')
        # Clearing puts the balance line back.
        self.api.patch(url, {'schedule': []}, format='json')
        self.assertTrue(self.ledger()['no_schedule'])

    def test_a_booking_with_its_own_schedule_cannot_be_scheduled_in_ar(self):
        own = make_booking(self.co, self.project, plot='10')
        rows = self.api.get('/api/ar/accounts/').json()['results']
        aid = next(r['id'] for r in rows if r['booking_id'] == own.id)
        r = self.api.patch(f'/api/ar/accounts/{aid}/', {'schedule': [{'date': '2026-01-01', 'amount': 1}]}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.api.get(f'/api/ar/accounts/{aid}/').json()['schedule_editable'])

    def test_suspect_amount_flag(self):
        make_booking(self.co, self.project, plot='51', final_amount=D('765'), total_extra=D('0'), installments=[])
        rows = {r['plots']: r for r in self.api.get('/api/ar/accounts/').json()['results']}
        self.assertTrue(rows['51']['suspect_amount'])
        self.assertFalse(rows['1003']['suspect_amount'])

    def test_dashboard(self):
        self.api.post(f'/api/ar/accounts/{self.id}/receipts/', {'paid_on': '2026-01-05', 'amount': '500000', 'mode': 'bank'}, format='json')
        self.api.patch(f'/api/ar/accounts/{self.id}/', {'schedule': [{'date': '2026-01-01', 'amount': 2650000}]}, format='json')
        d = self.api.get('/api/ar/dashboard/?as_of=2026-09-21').json()
        self.assertEqual(d['accounts'], 1)
        self.assertEqual(d['totals']['received'], 500000)
        self.assertEqual(d['totals']['overdue'], 2150000)
        self.assertEqual(d['overdue_accounts'], 1)
        self.assertEqual(d['top_overdue'][0]['id'], self.id)
        self.assertEqual(d['top_over_180'][0]['amount'], 2150000)
        self.assertEqual(d['projects'], [{'id': self.project.id, 'name': 'Pratishtha'}])
        self.assertEqual(d['issues'], {'no_schedule': 0, 'plan_mismatch': 0, 'suspect_amount': 0})

    def test_statement_is_print_ready_html(self):
        self.api.post(f'/api/ar/accounts/{self.id}/receipts/', {'paid_on': '2026-01-05', 'amount': '1234567', 'mode': 'cheque',
                                                                'remarks': 'HDFC <b>'}, format='json')
        r = self.api.get(f'/api/ar/accounts/{self.id}/statement/?as_of=2026-09-21')
        self.assertEqual(r.status_code, 200)
        self.assertIn('text/html', r['Content-Type'])
        html = r.content.decode()
        self.assertIn('Statement of Account', html)
        self.assertIn('Asha Patel', html)
        self.assertIn('₹ 12,34,567', html)            # Indian grouping
        self.assertIn('HDFC &lt;b&gt;', html)         # remarks are escaped
        self.assertIn('Vistara Realty', html)
