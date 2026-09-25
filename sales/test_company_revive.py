"""Bringing a deleted company back from its backup.

A restore only goes into the company a workbook came from, matched by id, so once
a company is deleted its backups have nowhere to go. Revive recreates it under its
original id and details and restores into it — every row with its own id, so the
links between them survive.
"""
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase

from accounts.models import User
from companies.models import Company
from sales.backup_excel import build_workbook
from sales.models import Booking, Lead, LeadSource, Project


def _workbook_file(company, drop_company_sheet=False):
    wb = build_workbook(company)
    buf = BytesIO()
    wb.save(buf)
    if drop_company_sheet:           # what a backup taken before this change looks like
        import openpyxl
        buf.seek(0)
        full = openpyxl.load_workbook(buf)
        del full['Company']
        buf = BytesIO()
        full.save(buf)
    return SimpleUploadedFile('backup.xlsx', buf.getvalue(),
                              content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


class ReviveTests(APITestCase):
    def setUp(self):
        self.home = Company.objects.create(code='VRL', name='Home')
        self.admin = User.objects.create(email='plat@x.com', company=self.home, role='Admin',
                                         is_staff=True, user_code='P1')
        self.co = Company.objects.create(code='GONE', name='Gone Group', email='hi@gone.co',
                                         loi_enabled=True)
        self.user = User.objects.create(email='g1@x.com', company=self.co, role='Admin',
                                        user_code='G1', name='Gone Admin')
        self.user.set_password('secret123'); self.user.save()
        src = LeadSource.objects.create(company=self.co, name='Meta')
        self.project = Project.objects.create(company=self.co, name='Tower')
        self.lead = Lead.objects.create(company=self.co, name='Buyer', phone='9000000001',
                                        source=src, project=self.project, stm=self.user)
        self.booking = Booking.objects.create(company=self.co, project=self.project, lead=self.lead,
                                              stm=self.user, status='sold', client_name='Buyer')
        self.client.force_authenticate(self.admin)

    def _revive(self, f, **data):
        return self.client.post('/api/sales/backups/revive/', {'file': f, **data}, format='multipart')

    def test_a_deleted_company_comes_back_with_its_ids_and_links(self):
        f = _workbook_file(self.co)
        cid, lead_id, booking_id = self.co.id, self.lead.id, self.booking.id
        self.co.delete()
        self.assertFalse(Lead.objects.filter(pk=lead_id).exists())

        preview = self._revive(f)
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertFalse(preview.data['needs_code'])
        self.assertFalse(Company.objects.filter(pk=cid).exists(), 'a preview writes nothing')

        f.seek(0)
        r = self._revive(f, commit='1')
        self.assertEqual(r.status_code, 200, r.data)
        co = Company.objects.get(pk=cid)
        self.assertEqual((co.code, co.name, co.email, co.loi_enabled),
                         ('GONE', 'Gone Group', 'hi@gone.co', True))
        lead = Lead.objects.get(pk=lead_id)
        self.assertEqual(lead.company_id, cid)
        self.assertEqual(Booking.objects.get(pk=booking_id).lead_id, lead_id)
        self.assertTrue(User.objects.get(user_code='G1', company=co).check_password('secret123'))

    def test_an_older_backup_needs_the_code_typed(self):
        f = _workbook_file(self.co, drop_company_sheet=True)
        cid = self.co.id
        self.co.delete()
        preview = self._revive(f)
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertTrue(preview.data['needs_code'])
        self.assertEqual(preview.data['company']['name'], 'Gone Group')
        f.seek(0)
        self.assertEqual(self._revive(f, commit='1').status_code, 400)
        f.seek(0)
        r = self._revive(f, commit='1', code='gone')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Company.objects.get(pk=cid).code, 'GONE')

    def test_a_living_company_is_refused(self):
        r = self._revive(_workbook_file(self.co))
        self.assertEqual(r.status_code, 409)

    def test_a_code_taken_since_is_refused(self):
        f = _workbook_file(self.co)
        self.co.delete()
        Company.objects.create(code='GONE', name='Someone new')
        self.assertEqual(self._revive(f).status_code, 409)

    def test_only_a_platform_admin(self):
        f = _workbook_file(self.co)
        self.co.delete()
        plain = User.objects.create(email='emp@x.com', company=self.home, role='Employee', user_code='E1')
        self.client.force_authenticate(plain)
        self.assertEqual(self._revive(f).status_code, 403)
