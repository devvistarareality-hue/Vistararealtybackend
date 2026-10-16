"""Every approved investor stands behind a Club 1000 lead.

An investor typed straight into Add Investor used to have none — the funnel read
42 investors against 0 conversions, because conversion only ever flipped a lead
that someone had entered first. The money arrived the same way either way, so the
lead is found or made at approval and marked converted.

Found by phone, not created blindly: if the person enquired before they invested,
that is one lead with one history, not two rows splitting it.

Approval and not creation, deliberately — creation leaves an investor 'pending',
and a lead marked converted for an investment that is later rejected would
overstate the funnel and leave a lie behind. The existing rule here is that a
rejected investor leaves no trace; these tests pin that it still holds.
"""
from datetime import date, timedelta

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from club1000.models import Investor, Lead, LeadStatusHistory, Scheme
from companies.models import Company
from sales.fields import phone_blind_index


class InvestorLeadTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='ILD', name='Lead Co', is_active=True)
        cls.other_co = Company.objects.create(code='ILE', name='Elsewhere', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@ild.com', company=cls.co, user_code='ILD001', password='p',
            name='Club Admin', role='Admin', modules=['Club 1000'])
        cls.staff = User.objects.create_user(
            'staff@ild.com', company=cls.co, user_code='ILD002', password='p',
            name='Club Staff', role='Employee', modules=['Club 1000'])
        cls.scheme = Scheme.objects.create(
            company=cls.co, name='RISE', tenure_months=12, min_ticket_size=100000,
            interest_payout_options=['maturity'])

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(user=self.admin)

    # ---------------------------------------------------------------- helpers

    def _investor(self, name='Direct Entry', phone='+919812300001', lead=None, company=None):
        co = company or self.co
        return Investor.objects.create(
            company=co, scheme=self.scheme, added_by=self.staff, name=name, phone=phone,
            amount_invested=500000, investment_date=date.today(),
            maturity_date=date.today() + timedelta(days=365),
            total_return_pct=18, interest_payout='maturity',
            status='active', approval_status='pending', lead=lead)

    def _lead(self, name='Enquired Earlier', phone='+919812300001', company=None, **over):
        fields = dict(company=company or self.co, name=name, phone=phone, status='new')
        fields.update(over)
        return Lead.objects.create(**fields)

    def _act(self, investor, action='approve'):
        return self.api.post(f'/api/club1000/investors/{investor.pk}/action/',
                             {'action': action}, format='json')

    # ------------------------------------------------- a direct investor entry

    def test_approving_a_direct_investor_gives_it_a_converted_lead(self):
        inv = self._investor()
        r = self._act(inv)
        self.assertEqual(r.status_code, 200, r.data)
        inv.refresh_from_db()
        self.assertIsNotNone(inv.lead_id, 'the investor was left with no lead')
        self.assertEqual(inv.lead.status, 'converted')

    def test_the_new_lead_carries_the_investment_over(self):
        """Not a husk with a name on it — the figures that make it readable as
        the enquiry it stands for."""
        inv = self._investor(name='Carried Over', phone='+919812300077')
        self._act(inv)
        lead = Investor.objects.get(pk=inv.pk).lead
        self.assertEqual(lead.name, 'Carried Over')
        self.assertEqual(lead.phone, '+919812300077')
        self.assertEqual(lead.company_id, self.co.id)
        self.assertEqual(lead.scheme_interest_id, self.scheme.id)
        self.assertEqual(lead.amount_interested, inv.amount_invested)
        self.assertEqual(lead.assigned_to_id, self.staff.id, 'whoever added the investor owns it')
        self.assertEqual(lead.lead_date, inv.investment_date)

    def test_the_new_lead_is_findable_by_phone(self):
        # phone is encrypted, so the blind index is the only way the list and
        # any later match can find it. A lead saved without one is invisible.
        inv = self._investor(phone='+919812300055')
        self._act(inv)
        lead = Investor.objects.get(pk=inv.pk).lead
        self.assertEqual(lead.phone_key, phone_blind_index('+919812300055'))

    def test_the_conversion_is_written_into_the_lead_history(self):
        inv = self._investor()
        self._act(inv)
        lead = Investor.objects.get(pk=inv.pk).lead
        h = LeadStatusHistory.objects.filter(lead=lead, field_changed='status').last()
        self.assertIsNotNone(h)
        self.assertEqual(h.new_value, 'converted')

    # ------------------------------------------------- an enquiry already there

    def test_an_existing_lead_on_the_same_phone_is_reused_not_duplicated(self):
        existing = self._lead(phone='+919812300002')
        inv = self._investor(name='Same Person', phone='+919812300002')
        self._act(inv)
        self.assertEqual(Lead.objects.filter(company=self.co).count(), 1,
                         'a second lead was created for a phone already on file')
        inv.refresh_from_db()
        self.assertEqual(inv.lead_id, existing.pk)
        existing.refresh_from_db()
        self.assertEqual(existing.status, 'converted')

    def test_reuse_matches_on_the_digits_not_the_formatting(self):
        # The blind index is the last 10 digits, so a lead saved as 9812300003
        # and an investor typed as +91 98123 00003 are the same person.
        existing = self._lead(phone='9812300003')
        inv = self._investor(phone='+91 98123 00003')
        self._act(inv)
        inv.refresh_from_db()
        self.assertEqual(inv.lead_id, existing.pk)

    def test_an_existing_lead_keeps_its_own_history(self):
        existing = self._lead(phone='+919812300004', status='follow_up')
        inv = self._investor(phone='+919812300004')
        self._act(inv)
        h = LeadStatusHistory.objects.get(lead=existing, field_changed='status')
        self.assertEqual(h.old_value, 'follow_up')
        self.assertEqual(h.new_value, 'converted')

    def test_an_already_converted_lead_gets_no_second_history_row(self):
        existing = self._lead(phone='+919812300005', status='converted')
        inv = self._investor(phone='+919812300005')
        self._act(inv)
        self.assertEqual(
            LeadStatusHistory.objects.filter(lead=existing, field_changed='status').count(), 0)

    def test_a_lead_at_another_company_is_not_borrowed(self):
        """The whole directory is per company; a shared phone number across two
        companies is two different people's business."""
        theirs = self._lead(phone='+919812300006', company=self.other_co)
        inv = self._investor(phone='+919812300006')
        self._act(inv)
        inv.refresh_from_db()
        self.assertNotEqual(inv.lead_id, theirs.pk)
        self.assertEqual(inv.lead.company_id, self.co.id)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, 'new', "another company's lead was touched")

    def test_the_oldest_matching_lead_wins(self):
        # Duplicates exist in live data. Converting the first enquiry keeps the
        # funnel's dates honest rather than crediting the newest row.
        first = self._lead(name='First Enquiry', phone='+919812300007')
        self._lead(name='Duplicate Enquiry', phone='+919812300007')
        inv = self._investor(phone='+919812300007')
        self._act(inv)
        inv.refresh_from_db()
        self.assertEqual(inv.lead_id, first.pk)

    # ------------------------------------------------------ what must not change

    def test_an_investor_that_came_from_a_lead_keeps_that_lead(self):
        source = self._lead(name='Original Lead', phone='+919812300008')
        inv = self._investor(name='From A Lead', phone='+919812300099', lead=source)
        self._act(inv)
        inv.refresh_from_db()
        self.assertEqual(inv.lead_id, source.pk, 'the linked lead was swapped for a phone match')
        self.assertEqual(Lead.objects.filter(company=self.co).count(), 1)
        source.refresh_from_db()
        self.assertEqual(source.status, 'converted')

    def test_a_pending_investor_has_no_lead_yet(self):
        """Creation does not convert anything — approval does. A lead marked
        converted for money that may still be rejected overstates the funnel."""
        self._investor()
        self.assertEqual(Lead.objects.filter(company=self.co).count(), 0)

    def test_a_rejected_investor_leaves_no_lead_behind(self):
        inv = self._investor()
        r = self._act(inv, 'reject')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Lead.objects.filter(company=self.co).count(), 0)
        inv.refresh_from_db()
        self.assertIsNone(inv.lead_id)

    def test_approving_twice_creates_nothing_further(self):
        inv = self._investor()
        self._act(inv)
        self.assertEqual(self._act(inv).status_code, 400)
        self.assertEqual(Lead.objects.filter(company=self.co).count(), 1)

    def test_an_investor_with_no_phone_is_approved_anyway(self):
        """Nothing to match or create a lead from, but the approval itself —
        payout schedule, referral reward — must not fall over."""
        inv = self._investor(phone='')
        r = self._act(inv)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Lead.objects.filter(company=self.co).count(), 0)
        inv.refresh_from_db()
        self.assertEqual(inv.approval_status, 'approved')


class OneLeadPerInvestmentTests(InvestorLeadTests):
    """One number, several investments: each gets its own lead."""

    def test_a_second_investment_on_the_same_number_gets_its_own_lead(self):
        first = self._investor(name='First', phone='+919812300090')
        self._act(first)
        second = self._investor(name='Second', phone='+919812300090')
        self._act(second)
        first.refresh_from_db(); second.refresh_from_db()
        self.assertNotEqual(first.lead_id, second.lead_id)
        self.assertEqual(Lead.objects.filter(phone_key=phone_blind_index('+919812300090')).count(), 2)

    def test_an_enquiry_for_another_scheme_is_not_borrowed(self):
        other = Scheme.objects.create(company=self.co, name='GROWTH', tenure_months=24, min_ticket_size=1,
                                      interest_payout_options=['maturity'])
        enquiry = self._lead(phone='+919812300091', scheme_interest=other)
        inv = self._investor(phone='+919812300091')
        self._act(inv)
        inv.refresh_from_db()
        self.assertNotEqual(inv.lead_id, enquiry.pk)

    def test_the_backfill_splits_a_shared_lead_and_fills_the_gaps(self):
        from django.core.management import call_command
        a = self._investor(name='A', phone='+919812300092'); a.approval_status = 'approved'; a.save()
        b = self._investor(name='B', phone='+919812300092'); b.approval_status = 'approved'; b.save()
        shared = self._lead(phone='+919812300092', scheme_interest=self.scheme)
        Investor.objects.filter(pk__in=[a.pk, b.pk]).update(lead=shared)
        c = self._investor(name='C', phone='+919812300093'); c.approval_status = 'approved'; c.save()
        call_command('link_investor_leads', '--dry-run')
        self.assertIsNone(Investor.objects.get(pk=c.pk).lead_id, 'a dry run wrote something')
        call_command('link_investor_leads')
        a, b, c = (Investor.objects.get(pk=x.pk) for x in (a, b, c))
        self.assertEqual(a.lead_id, shared.pk)
        self.assertNotEqual(b.lead_id, shared.pk)
        self.assertIsNotNone(c.lead_id)
        self.assertTrue(all(Lead.objects.get(pk=x.lead_id).status == 'converted' for x in (a, b, c)))
