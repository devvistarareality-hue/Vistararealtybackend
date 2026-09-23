"""Every role, every module, end to end.

One company, one person per job — Admin, Manager, Telecaller, STM, CP Executive,
CP Manager, Accounts, AR and Club 1000 — then each one is walked through the
screens they use, checking they can do their job and cannot do someone else's.

This is the regression net for the per-company permission work: the same run must
pass whether a designation is configured or still falling back to its title.
"""
from datetime import date, timedelta

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Designation, User
from companies.models import Company
from sales.models import Booking, Lead, Plot, Project

TODAY = date.today().isoformat()
SOON = (date.today() + timedelta(days=20)).isoformat()


class FullErpRoleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='FULL', name='Full Co', loi_enabled=True)

        def desig(name, module):
            Designation.objects.create(company=cls.co, name=name, module=module)

        for name, module in [('Telecaller', 'Sales'), ('STM', 'Sales'), ('CP Executive', 'Sales'),
                             ('CP Cluster Head', 'Sales'), ('Accountant', 'Accounts & Finance'),
                             ('AR Officer', 'AR'), ('Investment Manager', 'Club 1000'),
                             ('Sales Cluster Head', 'Sales')]:
            desig(name, module)

        def user(code, name, role, designation, **kw):
            return User.objects.create_user(f'{code.lower()}@full.test', company=cls.co, user_code=code,
                                            password='x', name=name, role=role, designation=designation, **kw)

        cls.admin = user('F0', 'Admin Person', 'Admin', '', is_staff=False)
        cls.manager = user('F1', 'Sales Manager', 'Manager', 'Sales Cluster Head',
                           modules=['Sales'], manager_modules=['Sales'])
        cls.telecaller = user('F2', 'Tele Person', 'Employee', 'Telecaller', modules=['Sales'],
                              reporting_manager=cls.manager)
        cls.stm = user('F3', 'STM Person', 'Employee', 'STM', modules=['Sales'],
                       reporting_manager=cls.manager)
        cls.cp = user('F4', 'CP Person', 'Employee', 'CP Executive', modules=['Sales'],
                      reporting_manager=cls.manager)
        cls.cp_manager = user('F5', 'CP Head', 'Manager', 'CP Cluster Head', modules=['Sales'],
                              manager_modules=['Sales'])
        cls.accounts = user('F6', 'Accounts Person', 'Manager', 'Accountant',
                            modules=['Accounts & Finance'], manager_modules=['Accounts & Finance'])
        cls.ar = user('F7', 'AR Person', 'Employee', 'AR Officer', modules=['AR'])
        cls.club = user('F8', 'Club Person', 'Manager', 'Investment Manager',
                        modules=['Club 1000'], manager_modules=['Club 1000'])

        cls.project = Project.objects.create(company=cls.co, name='Kalrav 2',
                                             booking_approvers=[cls.manager.id],
                                             accounts_booking_approvers=[cls.accounts.id])
        cls.plot = Plot.objects.create(project=cls.project, number='12', status='available')

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    # ── Sales: telecaller and STM ─────────────────────────────────────────
    def test_telecaller_works_their_queue_but_cannot_reassign(self):
        c = self.client_for(self.telecaller)
        r = c.post('/api/sales/leads/', {'name': 'Called Lead', 'phone': '9000000501',
                                         'project': self.project.id}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        lead_id = r.json()['id']
        self.assertEqual(c.patch(f'/api/sales/leads/{lead_id}/',
                                 {'telecaller_status': 'warm', 'telecaller_remarks': 'keen'},
                                 format='json').status_code, 200)
        # Reassigning is not theirs to do: the field is dropped, the owner stays.
        c.patch(f'/api/sales/leads/{lead_id}/', {'stm': self.stm.id}, format='json')
        self.assertIsNone(Lead.objects.get(pk=lead_id).stm_id)
        self.assertEqual(c.get('/api/sales/leads/').status_code, 200)

    def test_stm_sees_only_their_own_leads(self):
        mine = Lead.objects.create(company=self.co, name='Mine', phone='9000000502', stm=self.stm)
        Lead.objects.create(company=self.co, name='Someone else', phone='9000000503', stm=self.cp)
        names = [l['name'] for l in self.client_for(self.stm).get('/api/sales/leads/').json()['results']]
        self.assertIn(mine.name, names)
        self.assertNotIn('Someone else', names)

    def test_manager_sees_the_whole_desk(self):
        Lead.objects.create(company=self.co, name='Team lead', phone='9000000504', stm=self.stm)
        r = self.client_for(self.manager).get('/api/sales/leads/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('Team lead', [l['name'] for l in r.json()['results']])

    # ── Booking: Sales approves, then Accounts ────────────────────────────
    def _submit_booking(self):
        c = self.client_for(self.stm)
        lead = Lead.objects.create(company=self.co, name='Buyer', phone='9000000505', stm=self.stm)
        r = c.post('/api/sales/bookings/', {
            'project': self.project.id, 'plot_ids': [self.plot.id], 'lead': lead.id,
            'client_name': 'Buyer', 'phone': '9000000505', 'booking_date': TODAY,
            'final_amount': 1000000, 'total_extra': 0,
            'installments': [{'no': 1, 'date': SOON, 'amt': 1000000}],
        }, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()['id']

    def test_booking_travels_from_sales_to_accounts_to_ar(self):
        bid = self._submit_booking()
        # Sales approver signs off.
        r = self.client_for(self.manager).post(f'/api/sales/bookings/{bid}/action/',
                                               {'action': 'approve'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Booking.objects.get(pk=bid).status, 'sold')
        # The STM cannot approve their own booking.
        self.assertEqual(self.client_for(self.stm).post(f'/api/sales/bookings/{bid}/action/',
                                                        {'action': 'approve'}, format='json').status_code, 403)
        # Accounts is the second gate.
        r = self.client_for(self.accounts).post(f'/api/sales/bookings/{bid}/accounts-action/',
                                                {'action': 'approve'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Booking.objects.get(pk=bid).accounts_status, 'approved')
        # And now it is an AR account.
        rows = self.client_for(self.ar).get('/api/ar/accounts/').json()['results']
        self.assertEqual([x['client_name'] for x in rows], ['Buyer'])

    def test_a_schedule_that_does_not_add_up_is_refused(self):
        c = self.client_for(self.stm)
        lead = Lead.objects.create(company=self.co, name='Short', phone='9000000506', stm=self.stm)
        plot = Plot.objects.create(project=self.project, number='13', status='available')
        r = c.post('/api/sales/bookings/', {
            'project': self.project.id, 'plot_ids': [plot.id], 'lead': lead.id,
            'client_name': 'Short', 'phone': '9000000506', 'booking_date': TODAY,
            'final_amount': 1000000, 'total_extra': 0,
            'installments': [{'no': 1, 'date': SOON, 'amt': 400000}],
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('payment schedule', str(r.json()).lower())

    def test_an_impossible_installment_year_is_refused(self):
        c = self.client_for(self.stm)
        lead = Lead.objects.create(company=self.co, name='Typo', phone='9000000507', stm=self.stm)
        plot = Plot.objects.create(project=self.project, number='14', status='available')
        r = c.post('/api/sales/bookings/', {
            'project': self.project.id, 'plot_ids': [plot.id], 'lead': lead.id,
            'client_name': 'Typo', 'phone': '9000000507', 'booking_date': TODAY,
            'final_amount': 1000000, 'total_extra': 0,
            'installments': [{'no': 1, 'date': '0026-01-26', 'amt': 1000000}],
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('installment date', str(r.json()))

    # ── AR ────────────────────────────────────────────────────────────────
    def _ar_account(self):
        from receivables.test_api import make_booking
        make_booking(self.co, self.project, plot='77')
        return self.client_for(self.ar).get('/api/ar/accounts/').json()['results'][0]['id']

    def test_ar_person_runs_collections(self):
        acct = self._ar_account()
        c = self.client_for(self.ar)
        self.assertEqual(c.post(f'/api/ar/accounts/{acct}/receipts/',
                                {'paid_on': TODAY, 'amount': 100000, 'mode': 'bank'},
                                format='json').status_code, 201)
        self.assertEqual(c.get('/api/ar/dashboard/').status_code, 200)
        self.assertEqual(c.get('/api/ar/collections/?view=overdue').status_code, 200)
        self.assertEqual(c.post(f'/api/ar/accounts/{acct}/followups/',
                                {'scheduled_at': TODAY + 'T10:00', 'channel': 'call'},
                                format='json').status_code, 201)

    def test_sales_people_cannot_touch_ar(self):
        self._ar_account()
        for person in (self.stm, self.telecaller, self.cp):
            self.assertEqual(self.client_for(person).get('/api/ar/accounts/').status_code, 403,
                             f'{person.designation} should not reach AR')

    def test_a_company_can_take_receipt_entry_away_from_a_designation(self):
        acct = self._ar_account()
        d = Designation.objects.get(company=self.co, name='AR Officer')
        from accounts.capabilities import legacy_capabilities
        d.capabilities = sorted(legacy_capabilities(d.name) - {'ar.receipt.record'})
        d.capabilities_set = True
        d.save(update_fields=['capabilities', 'capabilities_set'])
        c = self.client_for(User.objects.get(pk=self.ar.pk))
        self.assertEqual(c.post(f'/api/ar/accounts/{acct}/receipts/',
                                {'paid_on': TODAY, 'amount': 5000, 'mode': 'bank'},
                                format='json').status_code, 403)
        self.assertEqual(c.get('/api/ar/accounts/').status_code, 200)

    # ── Club 1000 ─────────────────────────────────────────────────────────
    def test_club_person_runs_their_book(self):
        c = self.client_for(self.club)
        r = c.post('/api/club1000/schemes/', {'name': 'Plan A', 'tenure_months': 12,
                                              'min_ticket_size': 100000,
                                              'interest_payout_options': ['monthly'],
                                              'payout_rates': {'monthly': 12}}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(c.get('/api/club1000/stats/').status_code, 200)
        self.assertEqual(self.client_for(self.stm).get('/api/club1000/stats/').status_code, 403)

    # ── Accounts & Finance ────────────────────────────────────────────────
    def test_accounts_sees_the_bookings_ledger_and_others_do_not(self):
        self._submit_booking()
        self.assertEqual(self.client_for(self.accounts).get('/api/sales/bookings/all/').status_code, 200)
        self.assertEqual(self.client_for(self.telecaller).get('/api/sales/bookings/all/').status_code, 403)

    # ── Admin ─────────────────────────────────────────────────────────────
    def test_admin_runs_the_masters_and_sees_the_log(self):
        c = self.client_for(self.admin)
        for path in ('/api/auth/users/', '/api/auth/designations/', '/api/auth/designations/capabilities/',
                     '/api/activity/', '/api/sales/projects/', '/api/sales/reports/'):
            self.assertEqual(c.get(path).status_code, 200, path)

    def test_only_an_admin_changes_permissions(self):
        d = Designation.objects.get(company=self.co, name='Telecaller')
        self.assertEqual(self.client_for(self.manager).patch(
            f'/api/auth/designations/{d.id}/', {'capabilities': []}, format='json').status_code, 403)
        self.assertEqual(self.client_for(self.admin).patch(
            f'/api/auth/designations/{d.id}/',
            {'capabilities': ['sales.pipeline.telecalling', 'sales.lead.assign']}, format='json').status_code, 200)
        # And the telecaller may now reassign, which they could not before.
        from sales.views import can_assign_leads
        self.assertTrue(can_assign_leads(User.objects.get(pk=self.telecaller.pk)))

    # ── Everyone: the log records what they did ───────────────────────────
    def test_every_change_is_logged_with_the_person_and_the_record(self):
        from activity.models import ActivityLog
        c = self.client_for(self.telecaller)
        c.post('/api/sales/leads/', {'name': 'Logged Lead', 'phone': '9000000508'}, format='json')
        row = ActivityLog.objects.latest('id')
        self.assertEqual(row.actor_id, self.telecaller.id)
        self.assertEqual(row.module, 'Sales')
        self.assertIn('Logged Lead', row.summary)

    # ── Channel Partner ───────────────────────────────────────────────────
    def test_cp_people_reach_the_cp_module_and_plain_sales_does_not(self):
        from sales.views import can_access_cp_module
        self.assertTrue(can_access_cp_module(self.cp))
        self.assertTrue(can_access_cp_module(self.cp_manager))
        self.assertTrue(can_access_cp_module(self.admin))
        self.assertFalse(can_access_cp_module(self.stm))
        self.assertFalse(can_access_cp_module(self.telecaller))

    def test_cp_executive_sees_their_own_partner_leads_only(self):
        # A CP Executive works the partner-sourced pool: a lead is theirs only when it
        # came through a channel partner (or carries that source).
        from sales.models import LeadSource
        cp_source = LeadSource.objects.create(company=self.co, name='Channel Partner')
        mine = Lead.objects.create(company=self.co, name='CP mine', phone='9000000509',
                                   stm=self.cp, source=cp_source)
        Lead.objects.create(company=self.co, name='CP someone else', phone='9000000511',
                            stm=self.stm, source=cp_source)
        Lead.objects.create(company=self.co, name='STM lead', phone='9000000510', stm=self.stm)
        names = [l['name'] for l in self.client_for(self.cp).get('/api/sales/leads/').json()['results']]
        self.assertIn(mine.name, names)
        self.assertNotIn('STM lead', names)          # not partner-sourced
        self.assertNotIn('CP someone else', names)   # partner-sourced, but not theirs
