"""One deal, one closure, in one book — and the same for site visits.

Three screens answer "how many closures?" and they used to give three different
numbers for the same company: the Sales dashboard's Closures tile read 479, My
Conversions read 512, and Approvals listed 418 approved bookings. Each counted a
different population — the list kept cancelled closures and partner-sourced ones,
and every closure figure counted a revised deal twice, because revising a booking
issues the revision its own closure and leaves the replaced one on the books.
A closure counts once its booking is approved — the same deals Approvals lists,
which is what makes the three figures one figure.

Site visits had the narrower half of the same fault: the Sales list kept the
partner-sourced visits the dashboard left out, so it read 1,623 completed
against the tile's 1,563.

Follow-ups had neither half: the list and the tiles both counted the two books
together, so nothing contradicted anything and the Sales module quietly held
the partner desk's chasing. They are split here too, which makes all five —
leads, visits, closures, bookings, follow-ups — answer to the same line.
"""
from datetime import date

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from django.utils import timezone

from accounts.models import User
from companies.models import Company
from sales.models import (Booking, Closure, FollowUp, Lead, LeadSource, Plot,
                          Project, SiteVisit)


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


    def test_follow_ups_split_into_the_same_two_books(self):
        cache.clear()
        api = self._api()
        # One follow-up per deal, chased by the person who owns the lead.
        for lead in Lead.objects.filter(company=self.co):
            FollowUp.objects.create(lead=lead, assigned_to=self.stm, status='pending',
                                    scheduled_at=timezone.now())
        sales = api.get('/api/sales/follow-ups/').json()
        sales = sales if isinstance(sales, list) else sales.get('results', [])
        cp = api.get('/api/sales/follow-ups/?cp_only=true').json()
        cp = cp if isinstance(cp, list) else cp.get('results', [])
        self.assertEqual(len(cp), 1, "the partner lead's follow-up")
        self.assertEqual(len(sales), Lead.objects.filter(company=self.co).count() - 1,
                         'and the Sales list holds every other one, and not that')
        tile = api.get('/api/sales/stats/').json()['followup_pending_count']
        self.assertEqual(tile, len(sales), 'the tile counts what the list shows')
        cp_tile = api.get('/api/sales/stats/?cp_only=true').json()['followup_pending_count']
        self.assertEqual(cp_tile, len(cp), "and so does Channel Partner's")

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


class ARevisedDealFollowsItsCurrentSource(TestCase):
    """A deal revised from Channel Partner to Reference is a Reference deal now.

    Two of Vistara's closures were filed under Channel Partner because a booking
    that had been superseded named that source, while the booking that stands
    named another — so the closure sat in one book and its live booking in the
    other, and the CP closure figure read 69 against 68 CP bookings.
    """

    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='REV', name='Revised Co')
        self.p = Project.objects.create(company=self.co, name='Tundav')
        self.admin = User.objects.create_user('a@rev.com', company=self.co, user_code='RV-A',
                                              password='x', name='Admin', role='Admin',
                                              modules=['Sales', 'Channel Partner'])
        self.stm = User.objects.create_user('s@rev.com', company=self.co, user_code='RV-S',
                                            password='x', name='Seller', role='Employee',
                                            modules=['Sales'])
        lead = Lead.objects.create(company=self.co, project=self.p, name='Buyer',
                                   phone='9222200000', stm=self.stm)
        self.closure = Closure.objects.create(company=self.co, project=self.p, lead=lead,
                                              stm=self.stm, closure_date=date(2026, 8, 1))
        # Booked through a channel partner, then revised — same closure, same unit.
        for revision_no, source in ((0, 'Channel Partner'), (1, 'Reference')):
            Booking.objects.create(company=self.co, project=self.p, lead=lead, stm=self.stm,
                                   client_name='Buyer', phone='9222200000', plot_numbers='E-1',
                                   revision_no=revision_no, closure=self.closure, source=source,
                                   status='sold', approval_status='APPROVED',
                                   booking_date=date(2026, 8, 1))

    def _api(self):
        api = APIClient()
        api.force_authenticate(User.objects.get(pk=self.admin.pk))
        return api

    def test_the_closure_moves_to_the_sales_book_with_its_booking(self):
        api = self._api()
        self.assertEqual(api.get('/api/sales/closures/?counts_only=true').json()['total'], 1,
                         'Sales keeps the closure, because the booking that stands is a Sales one')
        self.assertEqual(
            api.get('/api/sales/closures/?counts_only=true&cp_only=true').json()['total'], 0,
            'Channel Partner does not, on the strength of a superseded booking')

    def test_the_tile_says_the_same(self):
        cache.clear()
        api = self._api()
        self.assertEqual(api.get('/api/sales/stats/').json()['closures'], 1)
        self.assertEqual(api.get('/api/sales/stats/?cp_only=true').json()['closures'], 0)


class AClosureCountsOnceItsBookingIsApproved(TestCase):
    """A deal the sales team has recorded but nobody has signed off is not a sale
    anyone can count yet, so the closure figures wait for the booking."""

    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='PND', name='Pending Co')
        self.p = Project.objects.create(company=self.co, name='Tundav')
        self.admin = User.objects.create_user('a@pnd.com', company=self.co, user_code='PN-A',
                                              password='x', name='Admin', role='Admin',
                                              modules=['Sales'])
        self.stm = User.objects.create_user('s@pnd.com', company=self.co, user_code='PN-S',
                                            password='x', name='Seller', role='Employee',
                                            modules=['Sales'])
        for i, status in enumerate(('sold', 'pending')):
            lead = Lead.objects.create(company=self.co, project=self.p, name=f'B{i}',
                                       phone=f'933330000{i}', stm=self.stm)
            closure = Closure.objects.create(company=self.co, project=self.p, lead=lead,
                                             stm=self.stm, closure_date=date(2026, 8, 1))
            Booking.objects.create(company=self.co, project=self.p, lead=lead, stm=self.stm,
                                   client_name=f'B{i}', phone=lead.phone, plot_numbers=f'F-{i}',
                                   closure=closure, status=status, booking_date=date(2026, 8, 1),
                                   approval_status='APPROVED' if status == 'sold' else 'PENDING')

    def _api(self):
        api = APIClient()
        api.force_authenticate(User.objects.get(pk=self.admin.pk))
        return api

    def test_only_the_approved_deal_counts(self):
        cache.clear()
        api = self._api()
        approved = api.get('/api/sales/bookings/?status=sold&source=sales').json()
        self.assertEqual(len(approved), 1, 'Approvals')
        self.assertEqual(api.get('/api/sales/stats/').json()['closures'], 1, 'the tile')
        self.assertEqual(api.get('/api/sales/closures/?counts_only=true').json()['total'], 1,
                         'My Conversions')


class AccountsHasTheLastWord(TestCase):
    """A sale is a sale when Accounts has signed it off, so that is when the
    closure figures count it — and until then it sits on its own tile.

    The three modules have to reconcile: Accounts' Approved is the Sales and
    Channel Partner dashboards' closures added together, and its queue is their
    "Pending from Accounts" added together.
    """

    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='ACC', name='Accounts Co')
        self.p = Project.objects.create(company=self.co, name='Tundav')
        self.cp_src = LeadSource.objects.create(company=self.co, name='Channel Partner')
        self.admin = User.objects.create_user('a@acc.com', company=self.co, user_code='AC-A',
                                              password='x', name='Admin', role='Admin',
                                              modules=['Sales', 'Channel Partner',
                                                       'Accounts & Finance'])
        self.stm = User.objects.create_user('s@acc.com', company=self.co, user_code='AC-S',
                                            password='x', name='Seller', role='Employee',
                                            modules=['Sales'])
        self.n = 0
        # Sales: two signed off, one still at the Accounts gate.
        self._deal(None, 'approved'); self._deal(None, 'approved'); self._deal(None, 'pending')
        # Channel Partner: one signed off, one waiting.
        self._deal(self.cp_src, 'approved'); self._deal(self.cp_src, 'pending')

    def _deal(self, source, accounts_status):
        self.n += 1
        lead = Lead.objects.create(company=self.co, project=self.p, source=source,
                                   name=f'D{self.n}', phone=f'94444000{self.n:02d}', stm=self.stm)
        closure = Closure.objects.create(company=self.co, project=self.p, lead=lead,
                                         stm=self.stm, closure_date=date(2026, 8, 1))
        Booking.objects.create(company=self.co, project=self.p, lead=lead, stm=self.stm,
                               client_name=f'D{self.n}', phone=lead.phone,
                               plot_numbers=f'G-{self.n}', closure=closure, status='sold',
                               approval_status='APPROVED', accounts_status=accounts_status,
                               booking_date=date(2026, 8, 1))

    def _api(self):
        api = APIClient()
        api.force_authenticate(User.objects.get(pk=self.admin.pk))
        return api

    def test_a_deal_waiting_on_accounts_is_not_a_closure_yet(self):
        cache.clear()
        api = self._api()
        sales = api.get('/api/sales/stats/').json()
        self.assertEqual(sales['closures'], 2)
        self.assertEqual(sales['accounts_pending'], 1)
        self.assertEqual(api.get('/api/sales/closures/?counts_only=true').json()['total'], 2,
                         'My Conversions counts the same deals')

        cp = api.get('/api/sales/stats/?cp_only=true').json()
        self.assertEqual(cp['closures'], 1)
        self.assertEqual(cp['accounts_pending'], 1)

    def test_approvals_can_list_the_ones_waiting(self):
        """The Approvals tab and the dashboard tile ask the same question."""
        cache.clear()
        api = self._api()
        for query, side, expected in (
            ('&source=sales', '', 1),
            ('&cp_only=true', '?cp_only=true', 1),
        ):
            waiting = api.get(f'/api/sales/bookings/?status=sold&accounts_status=pending{query}').json()
            tile = api.get(f'/api/sales/stats/{side}').json()['accounts_pending']
            self.assertEqual(len(waiting), expected, query)
            self.assertEqual(len(waiting), tile, f'the tab and the tile disagree for {query}')

    def test_the_partner_desk_sees_what_it_closed_from_other_sources(self):
        """A CP desk also books deals that did not come from a partner. Those sit
        in the Sales book, so the CP dashboard names them beside its own figures
        rather than letting them go missing."""
        cache.clear()
        cp_head = User.objects.create_user('h@acc.com', company=self.co, user_code='AC-H',
                                           password='x', name='Head', role='Manager',
                                           designation='CP CLUSTER HEAD',
                                           modules=['Channel Partner'])
        self.stm.reporting_manager = cp_head
        self.stm.save(update_fields=['reporting_manager'])
        api = APIClient()
        api.force_authenticate(User.objects.get(pk=cp_head.pk))
        d = api.get('/api/sales/stats/?cp_only=true').json()
        # The partner book: one signed off, one waiting.
        self.assertEqual(d['closures'], 1)
        self.assertEqual(d['accounts_pending'], 1)
        # His team's own work from other sources, which Sales counts, not CP.
        self.assertEqual(d['closures_other_source'], 2)
        self.assertEqual(d['accounts_pending_other_source'], 1)

    def test_a_deal_revised_into_someone_elses_name_leaves_the_desk(self):
        """The figure reads the booking that stands, so a deal revised away from
        this desk goes with it — otherwise the tiles add up to one more than the
        list, which is how 25 + 36 came to face a My Bookings of 60."""
        cache.clear()
        cp_head = User.objects.create_user('r@acc.com', company=self.co, user_code='AC-R',
                                           password='x', name='Head', role='Manager',
                                           designation='CP CLUSTER HEAD',
                                           modules=['Channel Partner'])
        self.stm.reporting_manager = cp_head
        self.stm.save(update_fields=['reporting_manager'])
        outsider = User.objects.create_user('o@acc.com', company=self.co, user_code='AC-O',
                                            password='x', name='Outsider', role='Employee',
                                            modules=['Sales'])
        api = APIClient()
        api.force_authenticate(User.objects.get(pk=cp_head.pk))
        before = api.get('/api/sales/stats/?cp_only=true').json()['closures_other_source']

        # His man's deal is revised, and the revision is booked by someone else.
        deal = Booking.objects.filter(stm=self.stm, lead__source__isnull=True,
                                      accounts_status='approved').first()
        Booking.objects.create(company=self.co, project=self.p, lead=deal.lead, stm=outsider,
                               client_name=deal.client_name, phone=deal.phone,
                               plot_numbers=deal.plot_numbers, closure=deal.closure,
                               revision_no=1, status='sold', approval_status='APPROVED',
                               accounts_status=deal.accounts_status,
                               booking_date=date(2026, 8, 1))
        cache.clear()
        after = api.get('/api/sales/stats/?cp_only=true').json()['closures_other_source']
        self.assertEqual(after, before - 1, 'the closure follows the booking that stands')

    def test_the_other_source_figures_are_channel_partner_only(self):
        cache.clear()
        d = self._api().get('/api/sales/stats/').json()
        self.assertEqual(d['closures_other_source'], 0, 'Sales has no other book to name')
        self.assertEqual(d['accounts_pending_other_source'], 0)

    def test_accounts_reconciles_with_sales_and_channel_partner(self):
        cache.clear()
        api = self._api()
        sales = api.get('/api/sales/stats/').json()
        cp = api.get('/api/sales/stats/?cp_only=true').json()
        acc = api.get('/api/sales/bookings/all/?counts_only=true').json()
        self.assertEqual(acc['approved'], sales['closures'] + cp['closures'])
        self.assertEqual(acc['pending'], sales['accounts_pending'] + cp['accounts_pending'])
        self.assertEqual(acc['total'], 5, 'every deal Sales or CP has approved')


class WhenAccountsRejects(TestCase):
    """Accounts refusing a deal undoes it, rather than leaving it half-sold.

    The unit goes back on the market, the closure is torn up, the booking reads
    as rejected everywhere, and the figures it was in drop with it. A reason is
    required — the rep has to be told what to fix.
    """

    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='REJ', name='Reject Co')
        self.p = Project.objects.create(company=self.co, name='Tundav')
        self.plot = Plot.objects.create(project=self.p, number='H-1', status='hold')
        self.admin = User.objects.create_user('a@rej.com', company=self.co, user_code='RJ-A',
                                              password='x', name='Admin', role='Admin',
                                              modules=['Sales', 'Accounts & Finance'])
        self.stm = User.objects.create_user('s@rej.com', company=self.co, user_code='RJ-S',
                                            password='x', name='Seller', role='Employee',
                                            modules=['Sales'])
        self.lead = Lead.objects.create(company=self.co, project=self.p, name='Buyer',
                                        phone='95555000001', stm=self.stm)
        self.closure = Closure.objects.create(company=self.co, project=self.p, lead=self.lead,
                                              stm=self.stm, closure_date=date(2026, 8, 1))
        self.booking = Booking.objects.create(
            company=self.co, project=self.p, plot=self.plot, lead=self.lead, stm=self.stm,
            client_name='Buyer', phone=self.lead.phone, closure=self.closure, status='sold',
            approval_status='APPROVED', accounts_status='pending', booking_date=date(2026, 8, 1))

    def _api(self):
        api = APIClient()
        api.force_authenticate(User.objects.get(pk=self.admin.pk))
        return api

    def test_a_reason_is_required(self):
        r = self._api().post(f'/api/sales/bookings/{self.booking.id}/accounts-action/',
                             {'action': 'reject'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.accounts_status, 'pending', 'nothing moved')

    def test_rejecting_undoes_the_sale(self):
        cache.clear()
        api = self._api()
        before = api.get('/api/sales/stats/').json()
        self.assertEqual(before['accounts_pending'], 1)
        self.assertEqual(before['closures'], 0, 'not a closure while Accounts holds it')

        r = api.post(f'/api/sales/bookings/{self.booking.id}/accounts-action/',
                     {'action': 'reject', 'reason': 'Payment plan does not add up'}, format='json')
        self.assertEqual(r.status_code, 200)

        self.booking.refresh_from_db()
        self.assertEqual(self.booking.accounts_status, 'rejected')
        self.assertEqual(self.booking.status, 'rejected', 'the booking itself, not just the stage')
        self.assertEqual(self.booking.accounts_rejected_reason, 'Payment plan does not add up')
        self.assertEqual(self.booking.accounts_rejected_by_id, self.admin.id)
        self.plot.refresh_from_db()
        self.assertEqual(self.plot.status, 'available', 'the unit goes back on the market')
        self.assertFalse(Closure.objects.filter(id=self.closure.id).exists(),
                         'the closure is torn up')

        cache.clear()
        after = api.get('/api/sales/stats/').json()
        self.assertEqual(after['accounts_pending'], 0, 'it leaves the waiting tile')
        self.assertEqual(after['closures'], 0, 'and never reaches Closures')

    def test_it_lands_on_rejected_and_nowhere_else(self):
        api = self._api()
        api.post(f'/api/sales/bookings/{self.booking.id}/accounts-action/',
                 {'action': 'reject', 'reason': 'Wrong rate'}, format='json')
        for tab, expected in (('sold', 0), ('pending', 0), ('rejected', 1)):
            rows = api.get(f'/api/sales/bookings/?status={tab}&source=sales').json()
            self.assertEqual(len(rows), expected, f'the {tab} tab')
        waiting = api.get('/api/sales/bookings/?status=sold&accounts_status=pending&source=sales').json()
        self.assertEqual(len(waiting), 0, 'no longer waiting on Accounts')


class TheClosuresTileOpensAListThatMatchesIt(TestCase):
    """A manager's dashboard counts everything they can see; My Bookings is their
    own desk. The tile therefore opens the list on `scope=visible`, or a Regional
    Head reads 367 closures and lands on 270.
    """

    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='SCP', name='Scope Co')
        self.p = Project.objects.create(company=self.co, name='Tundav')
        self.head = User.objects.create_user('h@scp.com', company=self.co, user_code='SC-H',
                                             password='x', name='Head', role='Manager',
                                             modules=['Sales'])
        self.mine = User.objects.create_user('m@scp.com', company=self.co, user_code='SC-M',
                                             password='x', name='Mine', role='Employee',
                                             modules=['Sales'], reporting_manager=self.head)
        self.theirs = User.objects.create_user('t@scp.com', company=self.co, user_code='SC-T',
                                               password='x', name='Theirs', role='Employee',
                                               modules=['Sales'])
        self.n = 0
        for owner in (self.mine, self.theirs):
            self._deal(owner)

    def _deal(self, owner):
        self.n += 1
        lead = Lead.objects.create(company=self.co, project=self.p, name=f'C{self.n}',
                                   phone=f'96666000{self.n:02d}', stm=owner)
        closure = Closure.objects.create(company=self.co, project=self.p, lead=lead,
                                         stm=owner, closure_date=date(2026, 8, 1))
        Booking.objects.create(company=self.co, project=self.p, lead=lead, stm=owner,
                               client_name=f'C{self.n}', phone=lead.phone,
                               plot_numbers=f'J-{self.n}', closure=closure, status='sold',
                               approval_status='APPROVED', booking_date=date(2026, 8, 1))

    def _api(self):
        api = APIClient()
        api.force_authenticate(User.objects.get(pk=self.head.pk))
        return api

    def _approved(self, query):
        rows = self._api().get(f'/api/sales/bookings/?mine=1&source=sales{query}').json()
        return len([b for b in rows if b['status'] == 'sold'])

    def test_the_tile_matches_the_wider_view_and_not_the_default(self):
        cache.clear()
        tile = self._api().get('/api/sales/stats/').json()['closures']
        self.assertEqual(tile, 2, 'a manager sees the company')
        self.assertEqual(self._approved(''), 1, "My Bookings is still the head's own desk")
        self.assertEqual(self._approved('&scope=visible'), tile,
                         'and the view the tile opens holds exactly what it counted')

    def test_the_wider_view_is_no_wider_than_the_person_may_see(self):
        api = APIClient()
        api.force_authenticate(User.objects.get(pk=self.mine.pk))
        rows = api.get('/api/sales/bookings/?mine=1&source=sales&scope=visible').json()
        self.assertEqual(len(rows), 1, 'an employee sees their own work either way')
