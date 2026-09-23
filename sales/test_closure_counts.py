"""One deal, one closure, in one book — and the same for site visits.

Three screens answer "how many closures?" and they used to give three different
numbers for the same company: the Sales dashboard's Closures tile read 479, My
Conversions read 512, and Approvals listed 418 approved bookings. Each counted a
different population — the list kept cancelled closures and partner-sourced ones,
and every closure figure counted a revised deal twice, because revising a booking
issues the revision its own closure and leaves the replaced one on the books.

Site visits had the narrower half of the same fault: the Sales list kept the
partner-sourced visits the dashboard left out, so it read 1,623 completed
against the tile's 1,563.
"""
from datetime import date

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from django.utils import timezone

from accounts.models import User
from companies.models import Company
from sales.models import Booking, Closure, Lead, LeadSource, Project, SiteVisit


class ClosureCountsAgree(TestCase):
    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='CLC', name='Closure Co')
        self.p = Project.objects.create(company=self.co, name='Tundav')
        self.meta = LeadSource.objects.create(company=self.co, name='Meta')
        self.cp = LeadSource.objects.create(company=self.co, name='Channel Partner')
        self.admin = User.objects.create_user('a@clc.com', company=self.co, user_code='CL-A',
                                              password='x', name='Admin', role='Admin',
                                              modules=['Sales', 'Channel Partner'])
        # The deals belong to someone else: an admin sees the whole company, but
        # only work that is their own escapes the Sales/CP split (a partner lead
        # handed to a Sales person counts as that person's).
        self.stm = User.objects.create_user('s@clc.com', company=self.co, user_code='CL-S',
                                            password='x', name='Seller', role='Employee',
                                            modules=['Sales'])
        self.n = 0

        # Three ordinary Sales deals.
        for i in range(3):
            self._deal(f'S{i}', self.meta, unit=f'A-{i}')
        # One partner-sourced deal — the Channel Partner book's.
        self._deal('CP1', self.cp, unit='B-1')
        # One cancelled deal — off the books entirely, on both screens.
        cl = self._deal('X1', self.meta, unit='C-1', status='rejected',
                        approval_status='CANCELLED')
        cl.status = 'cancelled'
        cl.save(update_fields=['status'])
        # One Sales deal that was revised: two bookings, two closures, one deal.
        # A revision keeps the buyer and the unit, which is how the chain is found.
        self.superseded = self._deal('R1', self.meta, unit='D-1', phone='9111100000')
        self.revised = self._deal('R1', self.meta, unit='D-1', phone='9111100000',
                                  revision_no=1)

    def _deal(self, name, source, unit, phone=None, revision_no=0, status='sold',
              approval_status='APPROVED'):
        self.n += 1
        phone = phone or f'90000000{self.n:02d}'
        lead = Lead.objects.create(company=self.co, project=self.p, source=source,
                                   name=name, phone=phone, stm=self.stm)
        SiteVisit.objects.create(lead=lead, project=self.p, stm=self.stm,
                                 status='completed', visited_at=timezone.now())
        closure = Closure.objects.create(company=self.co, project=self.p, lead=lead,
                                         stm=self.stm, closure_date=date(2026, 8, 1))
        Booking.objects.create(company=self.co, project=self.p, lead=lead, stm=self.stm,
                               client_name=name, phone=phone, plot_numbers=unit,
                               revision_no=revision_no, closure=closure, status=status,
                               approval_status=approval_status, booking_date=date(2026, 8, 1))
        return closure

    def _api(self):
        api = APIClient()
        api.force_authenticate(User.objects.get(pk=self.admin.pk))
        return api

    def test_the_three_sales_figures_agree(self):
        cache.clear()
        api = self._api()
        tile = api.get('/api/sales/stats/').json()['closures']
        listed = api.get('/api/sales/closures/?counts_only=true').json()['total']
        approved = api.get('/api/sales/bookings/?status=sold&source=sales').json()
        # Three ordinary deals plus the revised one, counted once.
        self.assertEqual(tile, 4, 'the dashboard tile')
        self.assertEqual(listed, 4, 'My Conversions')
        self.assertEqual(len(approved), 4, 'Approvals')

    def test_the_partner_deal_is_counted_in_channel_partner(self):
        cache.clear()
        api = self._api()
        self.assertEqual(api.get('/api/sales/stats/?cp_only=true').json()['closures'], 1)
        self.assertEqual(
            api.get('/api/sales/closures/?counts_only=true&cp_only=true').json()['total'], 1)

    def test_a_cancelled_closure_is_not_a_conversion(self):
        listed = self._api().get('/api/sales/closures/?counts_only=true').json()['total']
        self.assertEqual(listed, 4, 'the cancelled deal must not be listed')

    def test_a_revised_deal_is_listed_once(self):
        rows = self._api().get('/api/sales/closures/').json()
        rows = rows if isinstance(rows, list) else rows.get('results', [])
        ids = {r['id'] for r in rows}
        self.assertIn(self.revised.id, ids, 'the revision stands')
        self.assertNotIn(self.superseded.id, ids, 'the closure it replaced does not')


    def test_site_visits_split_into_the_same_two_books(self):
        cache.clear()
        api = self._api()
        tile = api.get('/api/sales/stats/').json()['sv_done']
        listed = api.get('/api/sales/site-visits/?counts_only=true').json()
        # Seven visits were recorded, one of them the partner's. Unlike closures,
        # a visit is not folded into a revision chain — each is its own visit.
        self.assertEqual(tile, 6, 'the dashboard tile')
        self.assertEqual(listed.get('completed'), 6, 'the Site Visits list')

        cp_tile = api.get('/api/sales/stats/?cp_only=true').json()['sv_done']
        cp_listed = api.get('/api/sales/site-visits/?counts_only=true&cp_only=true').json()
        self.assertEqual(cp_tile, 1, "Channel Partner's tile")
        self.assertEqual(cp_listed.get('completed'), 1, "Channel Partner's list")
