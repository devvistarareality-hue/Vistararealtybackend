"""Deleting a company takes every module's data with it and leaves nothing to restore
into, so it is gated at least as hard as Data Reset: a recent full backup, the reset
key, the company's code typed out — and never the company you are signed in under."""
import os
from unittest import mock

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from companies.models import Company
from sales.models import BackupStamp, Lead


class DeleteCompanyGuardTests(APITestCase):
    def setUp(self):
        self.home = Company.objects.create(code='HOME', name='Home Co')
        self.target = Company.objects.create(code='GONE', name='Gone Co')
        self.admin = User.objects.create(email='plat@x.com', company=self.home, role='Admin',
                                         is_staff=True, user_code='P1')
        Lead.objects.create(company=self.target, name='A lead', phone='9000000001')
        self.client.force_authenticate(self.admin)
        os.environ['DATA_RESET_KEY'] = 'the-key'

    def tearDown(self):
        os.environ.pop('DATA_RESET_KEY', None)

    def _backup(self, company):
        from sales.backup_excel import MODULES
        BackupStamp.objects.create(company=company, taken_at=timezone.now(), modules=list(MODULES))

    def _delete(self, company, **body):
        return self.client.delete(f'/api/company/{company.id}/', body, format='json')

    def test_no_backup_no_delete(self):
        r = self._delete(self.target, reset_key='the-key', confirm='GONE')
        self.assertEqual(r.status_code, 409)
        self.assertTrue(Company.objects.filter(pk=self.target.pk).exists())

    def test_wrong_key_no_delete(self):
        self._backup(self.target)
        r = self._delete(self.target, reset_key='nope', confirm='GONE')
        self.assertEqual(r.status_code, 403)
        self.assertTrue(Company.objects.filter(pk=self.target.pk).exists())

    def test_wrong_code_no_delete(self):
        self._backup(self.target)
        r = self._delete(self.target, reset_key='the-key', confirm='HOME')
        self.assertEqual(r.status_code, 400)
        self.assertTrue(Company.objects.filter(pk=self.target.pk).exists())

    def test_own_company_is_never_deleted(self):
        self._backup(self.home)
        r = self._delete(self.home, reset_key='the-key', confirm='HOME')
        self.assertEqual(r.status_code, 400)
        self.assertTrue(Company.objects.filter(pk=self.home.pk).exists())

    def test_no_key_on_server_no_delete(self):
        self._backup(self.target)
        os.environ.pop('DATA_RESET_KEY', None)
        r = self._delete(self.target, reset_key='the-key', confirm='GONE')
        self.assertEqual(r.status_code, 403)

    def test_all_gates_met_deletes_it_and_its_data(self):
        # The rows whose PROTECT foreign keys made a plain delete 500 on a real company:
        # an AR account guarding its booking, an investor guarding its scheme.
        from datetime import date
        from decimal import Decimal
        from sales.models import Booking
        from receivables.models import ARAccount
        from club1000.models import Scheme, Investor
        b = Booking.objects.create(company=self.target, status='sold', client_name='C')
        ARAccount.objects.create(company=self.target, root_booking=b, booking=b)
        sch = Scheme.objects.create(company=self.target, name='S', tenure_months=12,
                                    min_ticket_size=Decimal('1'), interest_payout_options=['maturity'])
        Investor.objects.create(company=self.target, scheme=sch, name='I', phone='9000000009',
                                amount_invested=Decimal('100'), maturity_date=date(2030, 1, 1))
        self._backup(self.target)
        r = self._delete(self.target, reset_key='the-key', confirm='gone')   # code is case-insensitive
        self.assertEqual(r.status_code, 204)
        self.assertFalse(Company.objects.filter(pk=self.target.pk).exists())
        self.assertFalse(Lead.objects.filter(name='A lead').exists())
        self.assertFalse(Booking.objects.filter(client_name='C').exists())
        self.assertFalse(Investor.objects.filter(name='I').exists())

    def test_check_only_validates_without_a_backup_and_deletes_nothing(self):
        bad = self._delete(self.target, reset_key='nope', confirm='GONE', check_only=True)
        self.assertEqual(bad.status_code, 403)
        ok = self._delete(self.target, reset_key='the-key', confirm='GONE', check_only=True)
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(Company.objects.filter(pk=self.target.pk).exists())
