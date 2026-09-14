"""Only the latest revision of a deal is listed — however the chain was formed."""
from datetime import date

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APITestCase

from accounts.models import User
from companies.models import Company
from sales.models import Booking, Closure, Lead, Plot, Project
from sales.views import _drop_superseded_revisions

from sales.tests import auth


class RevisionChains(TestCase):
    def setUp(self):
        self.co = Company.objects.create(code='RV', name='Rev Co')
        self.p = Project.objects.create(company=self.co, name='P')
        self.stm = User.objects.create(name='S', email='s@x.com', phone='9000000001',
                                       user_code='S1', role='Employee', company=self.co)
        self.n = 0

    def _closure(self):
        self.n += 1
        return Closure.objects.create(company=self.co, project=self.p, stm=self.stm,
                                      closure_date=date(2026, 8, 1))

    def _bk(self, unit, rev=0, closure=None, phone='9999900000', status='sold'):
        return Booking.objects.create(company=self.co, project=self.p, stm=self.stm,
                                      client_name='C', phone=phone, plot_numbers=unit,
                                      revision_no=rev, closure=closure, status=status,
                                      approval_status='APPROVED')

    def _kept(self):
        return sorted(b.id for b in _drop_superseded_revisions(Booking.objects.all()))

    def test_plain_revision_same_unit_new_closure(self):
        """Revising issues a new closure but keeps the unit."""
        a = self._bk('EOI-4', 0, self._closure())
        b = self._bk('EOI-4', 1, self._closure())
        c = self._bk('EOI-4', 2, self._closure())
        self.assertEqual(self._kept(), [c.id], 'only the newest revision should remain')
        self.assertNotIn(a.id, self._kept())
        self.assertNotIn(b.id, self._kept())

    def test_eoi_converted_to_loi_keeps_closure_but_renumbers_the_unit(self):
        cl = self._closure()
        eoi = self._bk('EOI-2', 0, cl)
        loi = self._bk('80000', 1, cl)          # converted: unit changes, closure does not
        self.assertEqual(self._kept(), [loi.id], 'the superseded EOI must drop out')

    def test_revised_then_converted_resolves_to_one_deal(self):
        """The transitive case: unit links the first two, closure links the last."""
        cl = self._closure()
        a = self._bk('EOI-7', 0, cl)
        b = self._bk('EOI-7', 1, self._closure())
        cl_b = Booking.objects.get(pk=b.id).closure
        c = self._bk('5000', 2, cl_b)           # converted from b
        self.assertEqual(self._kept(), [c.id])

    def test_separate_deals_for_one_client_all_survive(self):
        """Four EOIs for the same phone are four deals, not a chain."""
        ids = [self._bk(f'EOI-{i}', 0, self._closure()).id for i in (1, 6, 28, 29)]
        self.assertEqual(self._kept(), sorted(ids))

    def test_only_one_of_them_being_revised_leaves_the_others_alone(self):
        cl1 = self._closure()
        first = self._bk('EOI-1', 0, cl1)
        others = [self._bk(f'EOI-{i}', 0, self._closure()).id for i in (6, 28)]
        rev = self._bk('5000', 1, cl1)          # revises EOI-1 only
        self.assertEqual(self._kept(), sorted(others + [rev.id]))
        self.assertNotIn(first.id, self._kept())

    def test_two_ordinary_bookings_sharing_a_unit_are_not_collapsed(self):
        a = self._bk('39', 0, self._closure())
        b = self._bk('39', 0, self._closure())
        self.assertEqual(self._kept(), sorted([a.id, b.id]), 'no revision, no collapse')

    def test_rejected_rows_are_left_out_of_chains(self):
        cl = self._closure()
        rej = self._bk('EOI-9', 0, cl, status='rejected')
        live = self._bk('EOI-9', 1, cl)
        kept = self._kept()
        self.assertIn(live.id, kept)
        self.assertIn(rej.id, kept, 'a rejected row belongs in the Rejected tab, untouched')


class RevisionInheritsItsParent(TestCase):
    """A revision must never lose the unit it is a revision of."""

    def setUp(self):
        from sales.views import BookingListCreateView
        self.View = BookingListCreateView
        self.co = Company.objects.create(code='RI', name='RI Co')
        self.u = User.objects.create(name='S', email='s2@x.com', phone='9000000002',
                                     user_code='S2', role='Admin', company=self.co)
        # a project with no units mapped — the case the earlier guard cannot cover
        self.area_project = Project.objects.create(company=self.co, name='By Area',
                                                   formula_set='industrial')

    def _post(self, payload):
        from rest_framework.test import APIRequestFactory, force_authenticate
        req = APIRequestFactory().post('/x/', payload, format='json')
        force_authenticate(req, user=self.u)
        return self.View.as_view()(req)

    def test_revising_an_eoi_keeps_the_eoi_number(self):
        """The exact defect: the revision came out blank and showed its area."""
        eoi = Booking.objects.create(company=self.co, project=self.area_project,
                                     stm=self.u, client_name='C', phone='9825387696',
                                     plot_numbers='EOI-2', area='80000', status='sold',
                                     approval_status='APPROVED')
        res = self._post({'project': self.area_project.id, 'revision_of': eoi.id,
                          'client_name': 'C', 'phone': '9825387696'})
        self.assertIn(res.status_code, (200, 201), res.data)
        rev = Booking.objects.get(pk=res.data['id'])
        self.assertEqual(rev.plot_numbers, 'EOI-2', 'the revision lost its EOI number')
        self.assertEqual(rev.area, '80000')
        self.assertEqual(rev.revision_no, 1)

    def test_the_parent_link_is_recorded(self):
        eoi = Booking.objects.create(company=self.co, project=self.area_project,
                                     stm=self.u, client_name='C', phone='9000000009',
                                     plot_numbers='EOI-9', area='5000', status='sold')
        res = self._post({'project': self.area_project.id, 'revision_of': eoi.id,
                          'client_name': 'C', 'phone': '9000000009'})
        rev = Booking.objects.get(pk=res.data['id'])
        self.assertEqual(rev.revision_of_id, eoi.id)

    def test_an_explicit_unit_still_wins_over_the_inherited_one(self):
        """Converting to a real unit must not be overridden by inheritance."""
        p = Project.objects.create(company=self.co, name='Mapped')
        plot = Plot.objects.create(project=p, number='A-7')
        eoi = Booking.objects.create(company=self.co, project=p, stm=self.u,
                                     client_name='C', phone='9000000010',
                                     plot_numbers='EOI-3', area='585', status='sold')
        res = self._post({'project': p.id, 'revision_of': eoi.id, 'plot': plot.id,
                          'client_name': 'C', 'phone': '9000000010'})
        rev = Booking.objects.get(pk=res.data['id'])
        self.assertEqual(rev.plot_numbers, 'A-7', 'the chosen unit should win')
        self.assertEqual(rev.revision_of_id, eoi.id)

    def test_chain_collapses_via_the_recorded_link_even_when_the_unit_changes(self):
        p = Project.objects.create(company=self.co, name='Mapped2')
        plot = Plot.objects.create(project=p, number='B-4')
        eoi = Booking.objects.create(company=self.co, project=p, stm=self.u,
                                     client_name='C', phone='9000000011',
                                     plot_numbers='EOI-5', area='585', status='sold',
                                     approval_status='APPROVED')
        res = self._post({'project': p.id, 'revision_of': eoi.id, 'plot': plot.id,
                          'client_name': 'C', 'phone': '9000000011'})
        rev_id = res.data['id']
        kept = {b.id for b in _drop_superseded_revisions(Booking.objects.filter(project=p))}
        self.assertEqual(kept, {rev_id}, 'the superseded EOI should drop out')


class SupersededAcrossVisibilityTests(APITestCase):
    """A replaced booking must stay hidden even from someone who cannot see what
    replaced it.

    Chains used to be detected inside the viewer's own slice, so staleness depended
    on who was looking: a CP cluster head still had booking #478 listed as a live
    approved deal because its replacement was booked by someone outside his module.
    His figure read 108 where the same slice read 107 in Sales, and the extra row
    carried commercial terms that had since been revised.
    """

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='SUP', name='Supersede Co')
        cls.director = User.objects.create(email='sup_dir@x.com', company=cls.co, role='Director',
                                           designation='DIRECTOR', user_code='S0', name='Director')
        cls.cp = User.objects.create(email='sup_cp@x.com', company=cls.co, role='Manager',
                                     designation='CP CLUSTER HEAD', user_code='S1', name='Cp Head',
                                     reporting_manager=cls.director)
        # Books the replacement, and is invisible to the CP head: a sibling branch,
        # and their work is not partner-sourced so the CP pool does not carry it.
        cls.outsider = User.objects.create(email='sup_out@x.com', company=cls.co, role='Employee',
                                           designation='STM', user_code='S2', name='Outsider',
                                           reporting_manager=cls.director)
        cls.project = Project.objects.create(company=cls.co, name='Supersede Tower')
        cls.original = Booking.objects.create(
            company=cls.co, project=cls.project, stm=cls.cp, status='sold',
            source='Channel Partner', client_name='Same Client', phone='9000000080',
            plot_numbers='A-1', revision_no=0)
        cls.revision = Booking.objects.create(
            company=cls.co, project=cls.project, stm=cls.outsider, status='sold',
            source='Reference', client_name='Same Client', phone='9000000080',
            plot_numbers='A-1', revision_no=1, revision_of=cls.original)

    def setUp(self):
        cache.clear()

    def _ids(self, user, url):
        auth(self.client, user)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        return [b['id'] for b in r.data]

    def test_the_replaced_booking_is_hidden_even_when_its_replacement_is_not_visible(self):
        ids = self._ids(self.cp, '/api/sales/bookings/?mine=1&cp_only=true&status=sold')
        self.assertNotIn(self.original.id, ids)
        # And it is not swapped for the replacement either — that booking belongs to
        # someone else's branch and is genuinely not this viewer's to see.
        self.assertNotIn(self.revision.id, ids)

    def test_someone_who_sees_both_gets_the_live_one_only(self):
        ids = self._ids(self.director, '/api/sales/bookings/?mine=1&status=sold')
        self.assertIn(self.revision.id, ids)
        self.assertNotIn(self.original.id, ids)


class RevisionHistoryEndpointTests(APITestCase):
    """GET /api/sales/bookings/<pk>/revisions/ — every version of one deal.

    Only the latest version is listed anywhere, which is right: a deal should appear
    once and at its current terms. But the earlier ones are what was signed at the
    time, and until now nothing in the product could reach them.
    """

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='RHE', name='History Co')
        cls.other_co = Company.objects.create(code='RHX', name='Other Co')
        cls.stm = User.objects.create(email='rh_stm@x.com', company=cls.co, role='Employee',
                                      designation='STM', user_code='H1', name='Stm')
        cls.stranger = User.objects.create(email='rh_str@x.com', company=cls.co, role='Employee',
                                           designation='STM', user_code='H2', name='Stranger')
        cls.project = Project.objects.create(company=cls.co, name='History Tower')
        common = dict(company=cls.co, project=cls.project, stm=cls.stm,
                      client_name='Jignesh Rami', phone='9824818649', plot_numbers='EOI-1')
        cls.r0 = Booking.objects.create(status='sold', revision_no=0, final_amount=4680000, **common)
        # A rejected middle version: excluded from "what is live", but it is exactly
        # what someone opening the history wants to see.
        cls.r1 = Booking.objects.create(status='rejected', revision_no=1, final_amount=4700000,
                                        revision_of=cls.r0, **common)
        cls.r2 = Booking.objects.create(status='sold', revision_no=2, final_amount=4750000,
                                        revision_of=cls.r1, **common)

    def setUp(self):
        cache.clear()

    def _get(self, user, pk):
        auth(self.client, user)
        return self.client.get(f'/api/sales/bookings/{pk}/revisions/')

    def test_every_version_comes_back_newest_first(self):
        # The current terms are what is usually being checked, and the history reads
        # backwards from them: what does this deal say now, and what did it say before.
        r = self._get(self.stm, self.r2.id)
        self.assertEqual(r.status_code, 200)
        self.assertEqual([(b['id'], b['revision_no']) for b in r.data],
                         [(self.r2.id, 2), (self.r1.id, 1), (self.r0.id, 0)])

    def test_a_rejected_version_is_part_of_the_history(self):
        ids = [b['id'] for b in self._get(self.stm, self.r2.id).data]
        self.assertIn(self.r1.id, ids)

    def test_asking_from_any_version_gives_the_same_chain(self):
        frm_r0 = [b['id'] for b in self._get(self.stm, self.r0.id).data]
        frm_r2 = [b['id'] for b in self._get(self.stm, self.r2.id).data]
        self.assertEqual(frm_r0, frm_r2)

    def test_someone_with_no_claim_on_the_booking_gets_nothing(self):
        # 404 rather than 403: a booking you may not see should not be confirmed to
        # exist by the error you get back.
        self.assertEqual(self._get(self.stranger, self.r2.id).status_code, 404)

    def test_a_booking_in_another_company_is_not_reachable(self):
        foreign = User.objects.create(email='rh_foreign@x.com', company=self.other_co,
                                      role='Admin', designation='Admin', user_code='H3',
                                      name='Foreign Admin')
        self.assertEqual(self._get(foreign, self.r2.id).status_code, 404)


class RevisionHistoryReachTests(APITestCase):
    """Whoever can see a booking in their list can open its history.

    A director saw the booking in My Bookings through the reporting tree and was then
    refused when opening it: the check asked only for ownership, admin, or approver,
    and approving no projects is normal for someone whose visibility comes from the
    tree instead. The card said "Couldn't load the history."
    """

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='RHR', name='Reach Co')
        cls.director = User.objects.create(email='rr_dir@x.com', company=cls.co, role='Director',
                                           designation='DIRECTOR', user_code='K0', name='Director')
        cls.stm = User.objects.create(email='rr_stm@x.com', company=cls.co, role='Employee',
                                      designation='STM', user_code='K1', name='Stm',
                                      reporting_manager=cls.director)
        cls.outsider = User.objects.create(email='rr_out@x.com', company=cls.co, role='Employee',
                                           designation='STM', user_code='K2', name='Outsider')
        cls.project = Project.objects.create(company=cls.co, name='Reach Tower')
        common = dict(company=cls.co, project=cls.project, stm=cls.stm,
                      client_name='Trupti Akshay Patel', phone='9925174692', plot_numbers='EOI-1')
        cls.r0 = Booking.objects.create(status='sold', revision_no=0, final_amount=2500000, **common)
        cls.r1 = Booking.objects.create(status='sold', revision_no=1, final_amount=2600000,
                                        revision_of=cls.r0, **common)
        cls.draft = Booking.objects.create(
            company=cls.co, project=cls.project, stm=cls.stm, status='draft', revision_no=1,
            client_name='Half Done', phone='9925174693', plot_numbers='EOI-9')

    def setUp(self):
        cache.clear()

    def _get(self, user, pk):
        auth(self.client, user)
        return self.client.get(f'/api/sales/bookings/{pk}/revisions/')

    def test_a_manager_can_open_the_history_of_their_reports_booking(self):
        r = self._get(self.director, self.r1.id)
        self.assertEqual(r.status_code, 200)
        self.assertEqual([b['id'] for b in r.data], [self.r1.id, self.r0.id])

    def test_someone_outside_the_tree_still_cannot(self):
        self.assertEqual(self._get(self.outsider, self.r1.id).status_code, 404)

    def test_a_reports_draft_stays_out_of_reach(self):
        # Half-finished commercial terms are deliberately not browsable by every
        # manager; widening the general rule must not quietly undo that.
        self.assertEqual(self._get(self.director, self.draft.id).status_code, 404)


class AccountsRevisionHistoryTests(APITestCase):
    """Accounts & Finance reads every approved booking, so it may read their history.

    Their visibility comes from the module they are in, not from owning the booking,
    approving it, or sitting above anyone — so the narrower checks all miss them.
    """

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='ACR', name='Accounts Co')
        cls.reviewer = User.objects.create(email='ar_acc@x.com', company=cls.co, role='Employee',
                                           designation='ACCOUNTANT', user_code='A1',
                                           name='Reviewer', modules=['Accounts & Finance'])
        cls.stm = User.objects.create(email='ar_stm@x.com', company=cls.co, role='Employee',
                                      designation='STM', user_code='A2', name='Stm')
        cls.project = Project.objects.create(company=cls.co, name='Accounts Tower')
        common = dict(company=cls.co, project=cls.project, stm=cls.stm,
                      client_name='Vidhi Kher', phone='9898060667', plot_numbers='Karuna23')
        cls.r0 = Booking.objects.create(status='sold', revision_no=0, final_amount=21100000, **common)
        cls.r1 = Booking.objects.create(status='sold', revision_no=1, final_amount=21100000,
                                        revision_of=cls.r0, **common)

    def setUp(self):
        cache.clear()

    def test_an_accounts_reviewer_can_open_the_history(self):
        auth(self.client, self.reviewer)
        r = self.client.get(f'/api/sales/bookings/{self.r1.id}/revisions/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual([b['id'] for b in r.data], [self.r1.id, self.r0.id])
