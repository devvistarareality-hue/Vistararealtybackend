"""Ask Nexora's club_approvals / club_schemes answer what the Approvals screen shows."""
from datetime import date
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from club1000.models import Investor, Scheme
from companies.models import Company
from sales import assistant


class AskNexoraApprovalsTests(TestCase):
    def setUp(self):
        self.co = Company.objects.create(code='ANA', name='Approvals Co', is_active=True)
        self.admin = User.objects.create_user('a@ana.com', company=self.co, user_code='ANA1', password='p',
                                              name='Admin', role='Admin', modules=['Club 1000'])
        self.l1 = User.objects.create_user('l1@ana.com', company=self.co, user_code='ANA2', password='p', name='Sachin',
                                           role='Manager', modules=['Club 1000'], manager_modules=['Club 1000'])
        self.l2 = User.objects.create_user('l2@ana.com', company=self.co, user_code='ANA3', password='p', name='Rajesh',
                                           role='Manager', modules=['Club 1000'], manager_modules=['Club 1000'])
        self.scheme = Scheme.objects.create(company=self.co, name='RISE', tenure_months=24, min_ticket_size=1,
                                            investor_approvers=[self.l1.id], investor_approvers_l2=[self.l2.id],
                                            payout_rates={'maturity': '16'})
        self.one = Scheme.objects.create(company=self.co, name='EQUITY', tenure_months=12, min_ticket_size=1)
        self.api = APIClient(); self.api.force_authenticate(self.admin)

    def _inv(self, loi, scheme=None, **kw):
        d = date.today() - relativedelta(months=3)
        return Investor.objects.create(company=self.co, scheme=scheme or self.scheme, added_by=self.admin, name=f'Inv {loi}',
                                       phone='98000000' + loi[-2:], loi_no=loi, amount_invested=Decimal('100000'),
                                       investment_date=d, maturity_date=d + relativedelta(months=24),
                                       total_return_pct=Decimal('16'), interest_payout='maturity', status='active',
                                       **{'approval_status': 'pending', **kw})

    def q(self, **a):
        return assistant.run_query(self.admin, self.co.id, {'mode': 'list', 'limit': 50, **a}, mask=False)

    def screen(self, tab):
        return {r['loi_no'] for r in self.api.get('/api/club1000/investors/', {'approval_status': tab}).data}

    def test_pending_levels_rejections_match_the_screen(self):
        new = self._inv('RISE/01')
        self._inv('EQ/01', scheme=self.one)
        renew = self._inv('RISE/02', pending_revision={'amount_invested': '150000'}, pending_revision_type='renew', revision_no=1)
        undo = self._inv('RISE/03', approval_status='approved', renewal_count=1,
                         pending_action={'type': 'undo_renewal', 'by_name': 'Admin', 'reason': 'typed wrong', 'at': '2026-10-18T10:00:00'})
        self._inv('RISE/04', approval_status='rejected')
        self._inv('RISE/05', approval_status='approved',
                  rejections=[{'kind': 'renewal', 'no': 1, 'reason': 'rate wrong', 'by': 'Rajesh', 'at': '2026-10-17T09:00:00', 'amount': '200000'}])

        # Level 1 approves the new one through the real flow → it waits for level 2.
        c = APIClient(); c.force_authenticate(self.l1)
        self.assertEqual(c.post(f'/api/club1000/investors/{new.pk}/action/', {'action': 'approve'}, format='json').status_code, 200)

        pend = self.q(entity='club_approvals', filters=[{'field': 'status', 'op': 'eq', 'value': 'pending'}],
                      fields=['loi_no', 'kind', 'stage', 'level1_by', 'by', 'reason'])['rows']
        by = {r['loi_no']: r for r in pend}
        self.assertEqual(set(by), self.screen('pending'))
        self.assertEqual((by['RISE/01']['stage'], by['RISE/01']['level1_by']), ('waiting for level 2', 'Sachin'))
        self.assertEqual(by['EQ/01']['stage'], 'waiting for approval')            # one-level scheme
        self.assertEqual((by['RISE/02']['kind'], by['RISE/02']['stage']), ('renewal', 'waiting for level 1'))
        self.assertEqual((by['RISE/03']['kind'], by['RISE/03']['reason']), ('undo renewal request', 'typed wrong'))
        self.assertEqual(by['RISE/01']['kind'], 'new investment')

        rej = self.q(entity='club_approvals', filters=[{'field': 'status', 'op': 'eq', 'value': 'rejected'}],
                     fields=['loi_no', 'kind', 'stage', 'by', 'reason', 'proposed_amount'])['rows']
        self.assertEqual({r['loi_no'] for r in rej}, self.screen('rejected'))
        r5 = next(r for r in rej if r['loi_no'] == 'RISE/05')
        self.assertEqual((r5['kind'], r5['stage'], r5['by'], r5['reason']), ('renewal', 'R1 rejected', 'Rajesh', 'rate wrong'))
        self.assertEqual(Decimal(str(r5['proposed_amount']).replace('₹', '').replace(',', '')), Decimal('200000'))

        waiting = assistant.run_query(self.admin, self.co.id, {'entity': 'club_approvals', 'mode': 'count', 'filters': [
            {'field': 'stage', 'op': 'eq', 'value': 'waiting for level 2'}]}, mask=False)
        self.assertEqual(waiting['total'], 1)

    def test_scheme_approvers(self):
        rows = {r['scheme']: r for r in self.q(entity='club_schemes', fields=[
            'scheme', 'level1_approvers', 'level2_approvers', 'approval_levels'])['rows']}
        self.assertEqual((rows['RISE']['level1_approvers'], rows['RISE']['level2_approvers'], rows['RISE']['approval_levels']),
                         ('Sachin', 'Rajesh', 2))
        self.assertEqual(rows['EQUITY']['approval_levels'], 1)


class ApproverPickerTests(TestCase):
    def test_picker_lists_club_managers_and_every_director(self):
        co = Company.objects.create(code='APK', name='Picker Co', is_active=True)
        admin = User.objects.create_user('a@apk.com', company=co, user_code='APK1', password='p', name='Admin',
                                         role='Admin', modules=['Club 1000'])
        User.objects.create_user('d@apk.com', company=co, user_code='APK2', password='p', name='Bare Director',
                                 role='Director', modules=[])
        User.objects.create_user('m@apk.com', company=co, user_code='APK3', password='p', name='Club Manager',
                                 role='Manager', manager_modules=['Club 1000'])
        User.objects.create_user('s@apk.com', company=co, user_code='APK4', password='p', name='Plain Staff',
                                 role='Employee', modules=['Club 1000'])
        api = APIClient(); api.force_authenticate(admin)
        got = {u['name']: u for u in api.get('/api/club1000/users/', {'approvers': '1'}).data}
        self.assertEqual(set(got), {'Admin', 'Bare Director', 'Club Manager'})
        self.assertEqual((got['Bare Director']['director'], got['Bare Director']['has_access']), (True, False))
        # the ordinary list (lead assignment) is unchanged — only people with Club 1000 access
        plain = {u['name'] for u in api.get('/api/club1000/users/').data}
        self.assertEqual(plain, {'Admin', 'Club Manager', 'Plain Staff'})
