"""The booking workbook carries what the LOI carries.

The export used to be one flat sheet of the Booking row. That left out everything
the LOI prints as a *list* against a booking — its payment schedules, its custom
terms — and everything a Pratishtha flat or shop quotes, which is a different
price model entirely (box price, token, bank loan) rather than the land and
construction split the columns are built around.

So the workbook is four sheets. One flat row per booking as before, and one sheet
each for the things that are one-to-many and cannot be columns: a booking with
twelve instalments and one with two would otherwise need twelve pairs of columns,
mostly blank.
"""
import io

import openpyxl
from django.core.cache import cache
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import Booking, Plot, Project
from sales.tests import auth

URL = '/api/sales/bookings/export/'


def book(wb_bytes):
    return openpyxl.load_workbook(io.BytesIO(wb_bytes))


def rows_of(ws):
    """Data rows only — past the title/subtitle/blank/header, minus the total."""
    out = []
    for r in ws.iter_rows(min_row=5, values_only=True):
        if r[0] == 'GRAND TOTAL':
            continue
        if any(v not in (None, '') for v in r):
            out.append(r)
    return out


class BookingExportLoiFieldTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='BXP', name='Export Co')
        cls.admin = User.objects.create(
            email='bxp_admin@x.com', company=cls.co, role='Admin', designation='Admin',
            user_code='X0', name='Admin', is_staff=True)
        cls.stm = User.objects.create(
            email='bxp_stm@x.com', company=cls.co, role='Employee', designation='STM',
            user_code='X1', name='Rep')
        cls.project = Project.objects.create(company=cls.co, name='Kalrav')

    def setUp(self):
        cache.clear()
        auth(self.client, self.admin)

    def _booking(self, **kw):
        plot = kw.pop('plot', None) or Plot.objects.create(
            project=self.project, number=kw.pop('number', '12'), status='sold')
        defaults = dict(
            company=self.co, project=self.project, plot=plot, plot_ids=[plot.id],
            stm=self.stm, status='sold', client_name='Asha Shah', phone='9000000400',
            final_amount=2500000, booking_date='2026-01-15')
        defaults.update(kw)
        return Booking.objects.create(**defaults)

    def _wb(self):
        r = self.client.get(URL)
        self.assertEqual(r.status_code, 200)
        return book(r.content)

    # ------------------------------------------------------------ the schedules

    def test_the_payment_schedule_reaches_the_workbook(self):
        # The LOI devotes a whole section to this and the sheet had none of it —
        # a booking's payment plan was readable only by opening its PDF.
        self._booking(installments=[
            {'no': 1, 'amt': 500000, 'pct': 20, 'date': '2026-02-01'},
            {'no': 2, 'amt': 2000000, 'pct': 80, 'date': '2026-06-01'},
        ])
        ws = self._wb()['Payment Schedule']
        rows = rows_of(ws)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r[4] for r in rows], [1, 2], 'instalment numbers')
        self.assertEqual([r[5] for r in rows], ['2026-02-01', '2026-06-01'])
        self.assertEqual([r[7] for r in rows], [500000, 2000000])
        self.assertTrue(all(r[3] == 'Unit Price' for r in rows))
        # It must identify its booking, or the sheet is unreadable on its own.
        self.assertEqual(rows[0][0], 'Kalrav')
        self.assertEqual(rows[0][2], 'Asha Shah')

    def test_the_extra_work_schedule_is_kept_separate(self):
        # Two sections of the LOI, with their own dates — collapsing them would
        # misstate when money is actually due.
        self._booking(
            installments=[{'no': 1, 'amt': 100, 'pct': 100, 'date': '2026-02-01'}],
            extra_work_inst=[{'no': 1, 'amt': 50, 'pct': 100, 'date': '2026-09-01',
                              'isExtraWork': True}])
        rows = rows_of(self._wb()['Payment Schedule'])
        self.assertEqual({r[3] for r in rows}, {'Unit Price', 'Extra Work'})

    def test_a_booking_with_no_schedule_adds_no_rows(self):
        self._booking(installments=[])
        wb = self._wb()
        self.assertNotIn('Payment Schedule', wb.sheetnames,
                         'an empty sheet is worse than no sheet')

    def test_a_schedule_entry_with_junk_numbers_does_not_break_the_sheet(self):
        # These come back from a JSON column, so '' and None are both real.
        self._booking(installments=[
            {'no': 1, 'amt': '', 'pct': None, 'date': ''},
            {'no': 2, 'amt': 'n/a', 'pct': '30', 'date': '2026-06-01'},
        ])
        rows = rows_of(self._wb()['Payment Schedule'])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][7], 0, 'blank amount becomes a zero Excel can sum')
        self.assertEqual(rows[1][6], 30.0, 'a numeric string is still a number')

    # ---------------------------------------------------------------- the terms

    def test_the_custom_terms_reach_the_workbook(self):
        # Sometimes the only record of what was actually agreed.
        self._booking(extra_terms=[
            {'title': 'Construction', 'desc': 'Charged after 1.5 years; 18% GST extra.'}])
        rows = rows_of(self._wb()['Additional Terms'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], 'Construction')
        self.assertIn('18% GST', rows[0][4])

    # ------------------------------------------------------- flat/shop pricing

    def test_a_flat_carries_its_own_price_breakdown(self):
        # A Pratishtha LOI quotes an all-inclusive box price, not the land and
        # construction split the main columns are built around.
        plot = Plot.objects.create(
            project=self.project, number='101', status='sold',
            price_book={'kind': 'flat', 'unit': '101', 'flat_area': 84, 'terrace_area': 21,
                        'facing': 'garden', 'dastavej_value': 2711502,
                        'stamp_duty_reg': 162690, 'gst': 27115, 'bank_processing': 11000,
                        'token': 136193, 'bank_loan': 3026500, 'total': 3037500})
        self._booking(plot=plot, plot_ids=[plot.id])
        rows = rows_of(self._wb()['Flat and Shop Pricing'])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r[3], 'Flat')
        self.assertEqual(r[4], 84)
        self.assertEqual(r[6], 'Garden')
        self.assertEqual(r[7], 2711502, 'final unit price')
        self.assertEqual(r[11], 136193, 'token')
        self.assertEqual(r[12], 3026500, 'bank loan')
        self.assertEqual(r[13], 3037500, 'total')

    def test_a_unit_is_priced_once_even_though_plot_id_repeats_in_plot_ids(self):
        # plot_id is normally also inside plot_ids. Listing both priced every flat
        # twice and doubled the sheet's grand total.
        plot = Plot.objects.create(
            project=self.project, number='102', status='sold',
            price_book={'kind': 'flat', 'unit': '102', 'total': 3000000})
        self._booking(plot=plot, plot_ids=[plot.id])
        self.assertEqual(len(rows_of(self._wb()['Flat and Shop Pricing'])), 1)

    def test_several_units_on_one_booking_are_each_priced(self):
        a = Plot.objects.create(project=self.project, number='201', status='sold',
                                price_book={'kind': 'flat', 'unit': '201', 'total': 1})
        b = Plot.objects.create(project=self.project, number='202', status='sold',
                                price_book={'kind': 'shop', 'unit': '202', 'total': 2})
        self._booking(plot=a, plot_ids=[a.id, b.id], plot_numbers='201, 202')
        rows = rows_of(self._wb()['Flat and Shop Pricing'])
        self.assertEqual({r[1] for r in rows}, {'201', '202'})

    def test_a_plain_plot_booking_adds_no_pricing_rows(self):
        self._booking()
        self.assertNotIn('Flat and Shop Pricing', self._wb().sheetnames)

    # ------------------------------------------------------------- the workbook

    def test_the_flat_sheet_still_leads_and_still_sums(self):
        self._booking(installments=[{'no': 1, 'amt': 10, 'pct': 100, 'date': '2026-02-01'}])
        wb = self._wb()
        self.assertEqual(wb.sheetnames[0], 'Approved Bookings',
                         'the booking list stays the sheet that opens')
        ws = wb['Approved Bookings']
        last = [c.value for c in ws[ws.max_row]]
        self.assertEqual(last[0], 'GRAND TOTAL')
        self.assertTrue(any(str(v).startswith('=SUM(') for v in last if v))

    def test_the_flat_sheet_points_at_the_schedule(self):
        self._booking(installments=[
            {'no': 1, 'amt': 10, 'pct': 50, 'date': '2026-02-01'},
            {'no': 2, 'amt': 10, 'pct': 50, 'date': '2026-03-01'}])
        ws = self._wb()['Approved Bookings']
        headers = [c.value for c in ws[4]]
        self.assertIn('Instalments', headers)
        self.assertEqual(rows_of(ws)[0][headers.index('Instalments')], 2)

    def test_sheet_names_are_legal_for_excel(self):
        # Excel rejects / \ ? * [ ] : in a sheet title — openpyxl raises on save,
        # which would have failed the whole download rather than one sheet.
        plot = Plot.objects.create(project=self.project, number='301', status='sold',
                                   price_book={'kind': 'flat', 'unit': '301', 'total': 1})
        self._booking(plot=plot, plot_ids=[plot.id],
                      installments=[{'no': 1, 'amt': 1, 'pct': 100, 'date': '2026-02-01'}],
                      extra_terms=[{'title': 'T', 'desc': 'D'}])
        for name in self._wb().sheetnames:
            self.assertFalse(set(name) & set(r'/\?*[]:'), f'illegal sheet name {name!r}')

    # ------------------------------------------------------ every stage, not one

    def test_a_booking_at_any_stage_is_included(self):
        """The export is downloaded from Approvals, so it is scoped to Approvals.

        It once listed only Accounts-approved deals, which suited the Bookings
        screen it used to live on. From a screen full of pending ones, a download
        that quietly omitted them would be worse than no download at all.
        """
        self._booking(client_name='Signed Off', accounts_status='approved')
        self._booking(client_name='Still Checking', number='13', accounts_status='pending')
        self._booking(client_name='Turned Down', number='14', accounts_status='rejected')
        self._booking(client_name='Not Yet Sales', number='15', status='pending')
        self._booking(client_name='Half Typed', number='16', status='draft')
        names = {r[3] for r in rows_of(self._wb()['Approved Bookings'])}
        self.assertEqual(names, {'Signed Off', 'Still Checking', 'Turned Down',
                                 'Not Yet Sales', 'Half Typed'})

    def test_each_row_says_which_stage_it_is_at(self):
        # Spanning every stage is only useful if the sheet distinguishes them —
        # otherwise a draft and a confirmed sale read identically.
        self._booking(client_name='Sold One', status='sold', accounts_status='approved')
        self._booking(client_name='Pending One', number='13', status='pending',
                      accounts_status='pending')
        ws = self._wb()['Approved Bookings']
        headers = [c.value for c in ws[4]]
        self.assertIn('Booking Status', headers)
        by_name = {r[3]: r for r in rows_of(ws)}
        self.assertEqual(by_name['Sold One'][headers.index('Booking Status')], 'Sold')
        self.assertEqual(by_name['Pending One'][headers.index('Booking Status')],
                         'Pending Approval')
        self.assertFalse(by_name['Pending One'][headers.index('Accounts Status')],
                         'still with Sales, so no Accounts stage to report')

    def test_accounts_status_is_blank_for_a_deal_accounts_never_saw(self):
        """The column defaults to 'approved' on every new booking.

        While this sheet was sold-only that never showed; spanning every stage it
        would have labelled 135 of VRL's drafts and Sales-rejected deals as
        "Approved" by Accounts, for a stage they never reached.
        """
        self._booking(client_name='Half Typed', status='draft')
        self._booking(client_name='Sales Said No', number='13', status='rejected')
        self._booking(client_name='Real Sale', number='14', status='sold',
                      accounts_status='approved')
        ws = self._wb()['Approved Bookings']
        headers = [c.value for c in ws[4]]
        ai = headers.index('Accounts Status')
        by_name = {r[3]: r for r in rows_of(ws)}
        self.assertFalse(by_name['Half Typed'][ai], 'a draft never reached Accounts')
        self.assertFalse(by_name['Sales Said No'][ai], 'Sales stopped it before Accounts')
        self.assertEqual(by_name['Real Sale'][ai], 'Approved')

    def test_a_recorded_accounts_decision_shows_even_if_sales_later_rejected(self):
        # Accounts did see it, so the column should say so.
        from django.utils import timezone
        self._booking(client_name='Decided Then Pulled', status='rejected',
                      accounts_status='rejected', accounts_rejected_at=timezone.now())
        ws = self._wb()['Approved Bookings']
        headers = [c.value for c in ws[4]]
        self.assertEqual(rows_of(ws)[0][headers.index('Accounts Status')], 'Rejected')

    def test_the_schedule_sheet_spans_every_stage_too(self):
        # The extra sheets are built from the same rows, so they follow the scope.
        self._booking(client_name='Still Checking', accounts_status='pending',
                      installments=[{'no': 1, 'amt': 999, 'pct': 100, 'date': '2026-02-01'}])
        rows = rows_of(self._wb()['Payment Schedule'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], 'Still Checking')

    def test_the_grand_total_spans_every_stage(self):
        # Which is why it is read alongside Booking Status, not as money earned.
        self._booking(client_name='Approved', final_amount=1000000,
                      accounts_status='approved')
        self._booking(client_name='Pending', number='14', final_amount=9000000,
                      accounts_status='pending')
        ws = self._wb()['Approved Bookings']
        self.assertEqual(len(rows_of(ws)), 2)
        headers = [c.value for c in ws[4]]
        amounts = {r[3]: r[headers.index('Final Amount')] for r in rows_of(ws)}
        self.assertEqual(amounts, {'Approved': 1000000, 'Pending': 9000000})

    # --------------------------------------------------------------- the log

    def test_a_download_leaves_a_log_entry(self):
        """Taking every approved booking out of the system — client names, phone
        numbers, the whole price breakdown — is an act in its own right, and this
        row is the only record that it happened.

        It is also the one GET the activity log watches: a read changes nothing, so
        logging them generally would bury the log, but a copy leaving the building
        is not an ordinary read.
        """
        from activity.models import ActivityLog
        self._booking(client_name='Counted')
        before = ActivityLog.objects.count()

        r = self.client.get(URL)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(ActivityLog.objects.count(), before + 1)

        row = ActivityLog.objects.order_by('-id').first()
        self.assertEqual(row.actor_id, self.admin.id)
        self.assertEqual(row.action, 'downloaded')
        self.assertIn('Downloaded booking Excel', row.summary)
        self.assertIn('All Projects', row.summary)
        self.assertIn('1 booking', row.summary, 'how much left, not just that something did')
        self.assertEqual(row.method, 'GET')
        self.assertEqual(row.status_code, 200)
        self.assertEqual(row.company_id, self.co.id)
        # Accounts & Finance, not Sales: the Log someone opens to retrace this is
        # the one on the module whose screen carries the button. By path alone it
        # would file under Sales, where nobody would look for it.
        self.assertEqual(row.module, 'Accounts & Finance')

    def test_the_entry_names_the_project_when_one_is_chosen(self):
        from activity.models import ActivityLog
        self._booking()
        self.client.get(f'{URL}?project={self.project.id}')
        row = ActivityLog.objects.order_by('-id').first()
        self.assertIn('Kalrav', row.summary)
        self.assertEqual(row.target_id, str(self.project.id))

    def test_a_refused_download_is_not_logged_as_one(self):
        # A 403 took nothing, and a log full of attempts that failed makes the
        # entries that matter harder to find.
        from activity.models import ActivityLog
        nobody = User.objects.create(
            email='bxp_nolog@x.com', company=self.co, role='Employee', designation='STM',
            user_code='X8', name='No Access')
        auth(self.client, nobody)
        before = ActivityLog.objects.count()
        self.assertEqual(self.client.get(URL).status_code, 403)
        self.assertEqual(ActivityLog.objects.count(), before)

    def test_ordinary_reads_are_still_not_logged(self):
        # The exception is this one path, not GETs in general.
        from activity.models import ActivityLog
        self._booking()
        before = ActivityLog.objects.count()
        self.client.get('/api/sales/bookings/all/')
        self.client.get('/api/sales/projects/')
        self.assertEqual(ActivityLog.objects.count(), before)

    def test_the_export_is_still_refused_without_permission(self):
        nobody = User.objects.create(
            email='bxp_none@x.com', company=self.co, role='Employee', designation='STM',
            user_code='X9', name='No Access')
        auth(self.client, nobody)
        self.assertEqual(self.client.get(URL).status_code, 403)
