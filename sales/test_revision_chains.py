"""Only the latest revision of a deal is listed — however the chain was formed."""
from datetime import date

from django.test import TestCase

from accounts.models import User
from companies.models import Company
from sales.models import Booking, Closure, Lead, Plot, Project
from sales.views import _drop_superseded_revisions


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
