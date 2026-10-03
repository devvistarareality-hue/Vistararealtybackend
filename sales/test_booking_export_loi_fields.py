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

    # ------------------------------------------------ only what Accounts approved

    def test_a_booking_still_pending_with_accounts_is_left_out(self):
        """Sales approving a deal is not the same as Accounts accepting the figures.

        A booking is status='sold' from the moment Sales approves it, with Accounts
        still to check it — and those are the ones most likely to move. Totalling
        money Accounts has not accepted overstates what has actually been sold.
        """
        self._booking(client_name='Signed Off', accounts_status='approved')
        self._booking(client_name='Still Checking', number='13', accounts_status='pending')
        names = {r[3] for r in rows_of(self._wb()['Approved Bookings'])}
        self.assertIn('Signed Off', names)
        self.assertNotIn('Still Checking', names)

    def test_a_booking_accounts_rejected_is_left_out(self):
        self._booking(client_name='Turned Down', accounts_status='rejected')
        self.assertEqual(rows_of(self._wb()['Approved Bookings']), [])

    def test_the_schedule_sheet_follows_the_same_filter(self):
        # The extra sheets are built from the same rows, so a pending booking must
        # not leak its instalments in through the side door.
        self._booking(client_name='Still Checking', accounts_status='pending',
                      installments=[{'no': 1, 'amt': 999, 'pct': 100, 'date': '2026-02-01'}])
        wb = self._wb()
        self.assertNotIn('Payment Schedule', wb.sheetnames)

    def test_the_grand_total_counts_only_accepted_money(self):
        self._booking(client_name='Counted', final_amount=1000000, accounts_status='approved')
        self._booking(client_name='Not Counted', number='14', final_amount=9000000,
                      accounts_status='pending')
        rows = rows_of(self._wb()['Approved Bookings'])
        self.assertEqual(len(rows), 1)
        headers = [c.value for c in self._wb()['Approved Bookings'][4]]
        self.assertEqual(rows[0][headers.index('Final Amount')], 1000000)

    def test_the_export_is_still_refused_without_permission(self):
        nobody = User.objects.create(
            email='bxp_none@x.com', company=self.co, role='Employee', designation='STM',
            user_code='X9', name='No Access')
        auth(self.client, nobody)
        self.assertEqual(self.client.get(URL).status_code, 403)
