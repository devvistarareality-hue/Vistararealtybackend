"""Leads and completed Site Visits as Excel: only for people granted it, holding what
they can see with their filters, completed visits only, and logged."""
from io import BytesIO

import openpyxl
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import User
from activity.models import ActivityLog
from companies.models import Company
from sales.models import Lead, LeadSource, Project, SiteVisit


@override_settings(EXPORTS_INLINE=True)
class ListExportTests(TestCase):
    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='LEX', name='Lex Co')
        self.p = Project.objects.create(company=self.co, name='Kalrav')
        meta = LeadSource.objects.create(company=self.co, name='Meta')
        cp = LeadSource.objects.create(company=self.co, name='Channel Partner')
        self.stm = User.objects.create(name='S', email='s@lex.com', phone='9100000011', user_code='LX-S',
                                       role='Employee', designation='STM', company=self.co, modules=['Sales'])
        self.other = User.objects.create(name='O', email='o@lex.com', phone='9100000012', user_code='LX-O',
                                         role='Employee', designation='STM', company=self.co, modules=['Sales'])
        for i, (src, st) in enumerate([(meta, 'completed'), (meta, 'scheduled'), (cp, 'completed')]):
            lead = Lead.objects.create(company=self.co, project=self.p, source=src, name=f'Client {i}',
                                       phone=f'98000000{i:02d}', stm=self.stm)
            SiteVisit.objects.create(lead=lead, project=self.p, stm=self.stm, status=st,
                                     scheduled_at='2026-10-01T10:00:00Z',
                                     visited_at='2026-10-01T10:00:00Z' if st == 'completed' else None)
        Lead.objects.create(company=self.co, project=self.p, source=meta, name='Not mine', phone='9811111111',
                            stm=self.other)

    def _get(self, user, url):
        """Start the export, check its status, fetch the file — as the screen does."""
        c = APIClient(); c.force_authenticate(user)
        res = c.get(url)
        if res.status_code != 202:
            return res
        job = res.json()['job']
        st = c.get(f'/api/sales/exports/{job}/').json()
        assert st['status'] == 'done', st
        other = APIClient(); other.force_authenticate(self.other)
        assert other.get(f'/api/sales/exports/{job}/').status_code == 404   # only its owner
        return c.get(f'/api/sales/exports/{job}/file/')

    def _rows(self, res):
        ws = openpyxl.load_workbook(BytesIO(res.content)).active
        return [r for r in ws.iter_rows(min_row=4, values_only=True)]

    def test_not_granted_is_refused(self):
        self.assertEqual(self._get(self.stm, '/api/sales/leads/?export=xlsx&book=sales').status_code, 403)
        self.assertEqual(self._get(self.stm, '/api/sales/site-visits/?export=xlsx&book=sales').status_code, 403)

    def test_leads_hold_what_the_person_sees_with_the_source_filter(self):
        self.stm.can_export_leads = True; self.stm.save()
        names = sorted(r[0] for r in self._rows(self._get(self.stm, '/api/sales/leads/?export=xlsx&book=all')))
        self.assertEqual(names, ['Client 0', 'Client 1', 'Client 2'])        # not the other STM's
        names = sorted(r[0] for r in self._rows(self._get(self.stm, '/api/sales/leads/?export=xlsx&book=sales')))
        self.assertEqual(names, ['Client 0', 'Client 1'])
        self.assertTrue(ActivityLog.objects.filter(action='downloaded', target_type='lead export').exists())

    def test_site_visits_are_completed_only(self):
        self.stm.can_export_leads = True; self.stm.save()
        names = sorted(r[1] for r in self._rows(self._get(self.stm, '/api/sales/site-visits/?export=xlsx&book=all')))
        self.assertEqual(names, ['Client 0', 'Client 2'])
        names = [r[1] for r in self._rows(self._get(self.stm, '/api/sales/site-visits/?export=xlsx&book=cp'))]
        self.assertEqual(names, ['Client 2'])

    def test_user_management_can_grant_it(self):
        admin = User.objects.create(name='A', email='a@lex.com', phone='9100000019', user_code='LX-A',
                                    role='Admin', company=self.co)
        self.stm.reporting_manager = admin; self.stm.save()
        c = APIClient(); c.force_authenticate(admin)
        res = c.patch(f'/api/auth/users/{self.stm.id}/', {'can_export_leads': True}, format='json')
        self.assertEqual(res.status_code, 200, res.content[:300])
        self.stm.refresh_from_db()
        self.assertTrue(self.stm.can_export_leads)


class LoiAccessTests(TestCase):
    """The Accounts desk reviewing bookings in Approvals can open their LOIs."""

    def test_accounts_employee_can_open_an_loi(self):
        from django.core.files.base import ContentFile
        from sales.models import Booking
        co = Company.objects.create(code='VRL', name='Vistara', loi_enabled=True)
        p = Project.objects.create(company=co, name='Anahata')
        boss = User.objects.create(name='B', email='b@lx.com', phone='9100000201', user_code='LX-B', role='Admin', company=co)
        acc = User.objects.create(name='Acc', email='acc@lx.com', phone='9100000202', user_code='LX-C', role='Employee',
                                  designation='Accountant', company=co, modules=['Accounts & Finance'], reporting_manager=boss)
        sales = User.objects.create(name='Sx', email='sx@lx.com', phone='9100000203', user_code='LX-D', role='Employee',
                                    designation='STM', company=co, modules=['Sales'], reporting_manager=boss)
        b = Booking.objects.create(company=co, project=p, client_name='C', status='sold', accounts_status='pending')
        b.loi_document.save('loi.pdf', ContentFile(b'%PDF-1.4'), save=True)
        c = APIClient()
        c.force_authenticate(acc)
        self.assertEqual(c.get(f'/api/sales/bookings/{b.id}/loi-url/').status_code, 200)
        c.force_authenticate(sales)
        self.assertEqual(c.get(f'/api/sales/bookings/{b.id}/loi-url/').status_code, 403)
