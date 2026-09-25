"""Back up, reset to zero, restore — and be exactly where you started.

The three have to agree or the cycle loses data: the reset deletes what the
backup covers, the restore writes what the reset deleted, and all three read one
registry (SHEETS in backup_excel) so they cannot drift apart.
"""
import os
from datetime import date, timedelta
from io import BytesIO

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Designation, Notification, User
from companies.models import Company
from receivables.models import ARAccount
from sales.backup_excel import build_workbook, parse_workbook, reset_company, restore, restore_order
from sales.models import (Booking, ChannelPartner, Closure, FollowUp, Lead, LeadSource,
                          LeadStatusHistory, LeadTransfer, Plot, Project, SiteVisit,
                          UserProjectAssignment)
from tasks.models import Task, TaskList

RESET_KEY = 'test-reset-key'
INSTALLMENTS = [
    {'label': 'Installment 1', 'amount': '1402303', 'due_date': '2026-10-24'},
    {'label': 'Installment 2', 'amount': '1402302', 'due_date': '2026-11-23'},
]
LOI_PATH = 'DR Project/Plot A-1 - Meera Shah/R1_LOI_PlotA-1_Meera_Shah.pdf'


def _seed():
    co = Company.objects.create(code='CYC', name='Cycle Realty', is_active=True)
    boss = User.objects.create_user('boss@cyc.com', company=co, user_code='CYC001',
                                    password='x', name='Boss', role='Admin',
                                    modules=['Sales', 'AR'])
    rep = User.objects.create_user('rep@cyc.com', company=co, user_code='CYC002',
                                   password='x', name='Rep', role='Employee',
                                   modules=['Sales'], reporting_manager=boss)
    Designation.objects.create(company=co, name='STM', module='Sales')
    proj = Project.objects.create(company=co, name='Cycle Project')
    other = Project.objects.create(company=co, name='Second Project')
    plot = Plot.objects.create(project=proj, number='A-1', status='booked')
    src = LeadSource.objects.create(company=co, name='walkin')
    ChannelPartner.objects.create(company=co, name='Partner One', contact_no='+919000000009')
    # The project mapping: who works which project.
    UserProjectAssignment.objects.create(user=rep, project=proj)
    UserProjectAssignment.objects.create(user=rep, project=other)
    lead = Lead.objects.create(company=co, name='Meera Shah', phone='+919812345678',
                               project=proj, source=src, status='booked', stm=rep)
    sv = SiteVisit.objects.create(lead=lead, project=proj, status='completed')
    FollowUp.objects.create(lead=lead, assigned_to=rep, role_context='stm',
                            scheduled_at=timezone.now(), remarks='Call back')
    LeadStatusHistory.objects.create(lead=lead, field_changed='status', new_value='booked')
    clo = Closure.objects.create(company=co, lead=lead, site_visit=sv, project=proj,
                                 client_name='Meera Shah', closure_date=date(2026, 9, 1),
                                 total_amount='5609211.00')
    bk = Booking.objects.create(company=co, lead=lead, closure=clo, project=proj, plot=plot,
                                stm=rep, client_name='Meera Shah', installments=INSTALLMENTS,
                                final_amount='5609211.00', discount='25000.00',
                                land_rate='3500.00', loi_document=LOI_PATH)
    LeadTransfer.objects.create(company=co, lead=lead, project=proj, from_stm=rep,
                                to_stm=boss, requested_by=rep, status='pending')
    Notification.objects.create(recipient=rep, type='new_lead', title='New lead')
    ARAccount.objects.create(company=co, root_booking=bk, booking=bk)
    tl = TaskList.objects.create(company=co, name='Onboarding', created_by=boss)
    Task.objects.create(company=co, task_list=tl, title='Collect KYC', created_by=boss)
    return co, boss, rep, proj, other, bk


def _census(company):
    return {t.label: t.cls.objects.filter(**{t.scope: company}).count()
            for t in restore_order()}


class BackupResetRestoreCycle(APITestCase):

    def setUp(self):
        self.co, self.boss, self.rep, self.proj, self.other, self.bk = _seed()
        self.before = _census(self.co)
        buf = BytesIO()
        build_workbook(self.co).save(buf)
        buf.seek(0)
        self.workbook = buf

    def test_reset_empties_everything_but_the_admin_who_ran_it(self):
        reset_company(self.co, keep_user_id=self.boss.id)
        after = _census(self.co)
        self.assertEqual(after['Users'], 1, 'the admin running the reset must survive')
        for label, count in after.items():
            if label != 'Users':
                self.assertEqual(count, 0, f'{label} still has rows after a reset')

    def test_restoring_after_a_reset_puts_everything_back(self):
        reset_company(self.co, keep_user_id=self.boss.id)
        result = restore(self.co, parse_workbook(self.workbook), commit=True)
        self.assertTrue(result['ok'], result.get('detail'))

        after = _census(self.co)
        for label, want in self.before.items():
            self.assertEqual(after[label], want, f'{label}: {after[label]} back, expected {want}')

    def test_the_project_mapping_comes_back(self):
        reset_company(self.co, keep_user_id=self.boss.id)
        restore(self.co, parse_workbook(self.workbook), commit=True)

        pairs = set(UserProjectAssignment.objects
                    .filter(user__company=self.co)
                    .values_list('user_id', 'project_id'))
        self.assertEqual(pairs, {(self.rep.id, self.proj.id), (self.rep.id, self.other.id)})
        # And the records that hang off a project still point at the right one.
        self.assertEqual(Lead.objects.get(company=self.co).project_id, self.proj.id)
        self.assertEqual(Booking.objects.get(company=self.co).project_id, self.proj.id)
        self.assertEqual(Plot.objects.get(project__company=self.co).project_id, self.proj.id)

    def test_the_booking_survives_the_round_trip_intact(self):
        reset_company(self.co, keep_user_id=self.boss.id)
        restore(self.co, parse_workbook(self.workbook), commit=True)

        b = Booking.objects.get(company=self.co)
        self.assertEqual(b.id, self.bk.id)
        self.assertEqual(b.client_name, 'Meera Shah')
        self.assertEqual(str(b.final_amount), '5609211.00')
        self.assertEqual(str(b.discount), '25000.00')
        self.assertEqual(b.installments, INSTALLMENTS)
        self.assertEqual(b.loi_document.name, LOI_PATH)
        self.assertEqual(b.lead_id, self.bk.lead_id)
        self.assertEqual(b.closure_id, self.bk.closure_id)
        self.assertEqual(b.plot_id, self.bk.plot_id)


@override_settings(DEBUG=False)
class ResetIsGated(APITestCase):

    def setUp(self):
        self.co, self.boss, self.rep, *_ = _seed()
        self.url = f'/api/sales/backups/reset/?company_id={self.co.id}'
        self.client.force_authenticate(self.boss)

    def _take_backup(self):
        r = self.client.get(f'/api/sales/backups/excel/?company_id={self.co.id}')
        self.assertEqual(r.status_code, 200)

    def test_an_employee_cannot_reset(self):
        self.client.force_authenticate(self.rep)
        r = self.client.post(self.url, {'reset_key': RESET_KEY, 'confirm': 'DELETE'}, format='json')
        self.assertEqual(r.status_code, 403)

    def test_no_reset_without_a_backup(self):
        with self.settings():
            os.environ['DATA_RESET_KEY'] = RESET_KEY
            r = self.client.post(self.url, {'reset_key': RESET_KEY, 'confirm': 'DELETE'},
                                 format='json')
        self.assertEqual(r.status_code, 409)
        self.assertIn('backup', r.json()['detail'].lower())
        self.assertTrue(Lead.objects.filter(company=self.co).exists(), 'nothing may be deleted')

    def test_no_reset_with_the_wrong_key(self):
        self._take_backup()
        os.environ['DATA_RESET_KEY'] = RESET_KEY
        r = self.client.post(self.url, {'reset_key': 'wrong', 'confirm': 'DELETE'}, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertTrue(Lead.objects.filter(company=self.co).exists())

    def test_no_reset_when_the_server_has_no_key(self):
        """Fails closed: a missing key must never mean no protection."""
        self._take_backup()
        os.environ.pop('DATA_RESET_KEY', None)
        r = self.client.post(self.url, {'reset_key': '', 'confirm': 'DELETE'}, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertTrue(Lead.objects.filter(company=self.co).exists())

    def test_no_reset_without_typing_delete(self):
        self._take_backup()
        os.environ['DATA_RESET_KEY'] = RESET_KEY
        r = self.client.post(self.url, {'reset_key': RESET_KEY, 'confirm': 'nope'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertTrue(Lead.objects.filter(company=self.co).exists())

    def test_all_three_gates_passed_resets_the_company(self):
        self._take_backup()
        os.environ['DATA_RESET_KEY'] = RESET_KEY
        r = self.client.post(self.url, {'reset_key': RESET_KEY, 'confirm': 'DELETE'}, format='json')
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertFalse(Lead.objects.filter(company=self.co).exists())
        self.assertTrue(User.objects.filter(pk=self.boss.pk).exists(),
                        'the admin who ran it must still be able to sign in')

    def test_a_company_cannot_reset_another(self):
        other = Company.objects.create(code='OTH', name='Other Co', is_active=True)
        os.environ['DATA_RESET_KEY'] = RESET_KEY
        r = self.client.post(f'/api/sales/backups/reset/?company_id={other.id}',
                             {'reset_key': RESET_KEY, 'confirm': 'DELETE'}, format='json')
        self.assertEqual(r.status_code, 403)


class APartialBackupCannotAuthoriseAReset(APITestCase):
    """Backups cover the whole company now, but stamps from before that could be
    partial — and a reset empties everything, so a partial one must not unlock it."""

    def setUp(self):
        self.co, self.boss, *_ = _seed()
        self.client.force_authenticate(self.boss)
        os.environ['DATA_RESET_KEY'] = RESET_KEY

    def _reset(self):
        return self.client.post(f'/api/sales/backups/reset/?company_id={self.co.id}',
                                {'reset_key': RESET_KEY, 'confirm': 'DELETE'}, format='json')

    def test_an_old_partial_stamp_is_not_enough(self):
        from sales.models import BackupStamp
        BackupStamp.objects.create(company=self.co, modules=['AR'], taken_by=self.boss)
        self.assertEqual(self._reset().status_code, 409)
        self.assertTrue(Lead.objects.filter(company=self.co).exists())

    def test_a_full_backup_unlocks_it(self):
        r = self.client.get(f'/api/sales/backups/excel/?company_id={self.co.id}')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._reset().status_code, 200)
        self.assertFalse(Lead.objects.filter(company=self.co).exists())


class ScheduledBackups(APITestCase):
    """Taking a company's backup on a schedule, and keeping it."""

    def setUp(self):
        self.co, self.boss, self.rep, *_ = _seed()
        self.client.force_authenticate(self.boss)
        self.url = f'/api/sales/backups/schedule/?company_id={self.co.id}'

    def test_a_schedule_starts_off(self):
        body = self.client.get(self.url).json()
        self.assertFalse(body['is_enabled'])
        self.assertEqual(body['history'], [])

    def test_the_schedule_can_be_set(self):
        r = self.client.patch(self.url, {'is_enabled': True, 'frequency': 'daily',
                                         'keep_last': 3}, format='json')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body['is_enabled'])
        self.assertEqual(body['frequency'], 'daily')
        self.assertEqual(body['keep_last'], 3)

    def test_an_employee_cannot_touch_the_schedule(self):
        self.client.force_authenticate(self.rep)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.patch(self.url, {'is_enabled': True}, format='json').status_code, 403)

    def test_due_respects_the_frequency(self):
        from sales.backup_schedule import due
        from sales.models import BackupSchedule, BackupStamp
        sched, _ = BackupSchedule.objects.get_or_create(company=self.co)
        sched.is_enabled = True
        sched.frequency = 'weekly'
        sched.save()
        self.assertTrue(due(sched), 'never backed up, so due now')

        stamp = BackupStamp.objects.create(company=self.co, automatic=True, file_path='x.xlsx')
        self.assertFalse(due(sched), 'just backed up, so not due')

        BackupStamp.objects.filter(pk=stamp.pk).update(taken_at=timezone.now() - timedelta(days=8))
        self.assertTrue(due(sched), 'a week later, due again')

    def test_a_disabled_schedule_is_never_due(self):
        from sales.backup_schedule import due
        from sales.models import BackupSchedule
        sched, _ = BackupSchedule.objects.get_or_create(company=self.co)
        self.assertFalse(due(sched))

    def test_a_stored_backup_belongs_to_its_company(self):
        from sales.models import BackupStamp
        other = Company.objects.create(code='NOPE', name='Other Co', is_active=True)
        theirs = BackupStamp.objects.create(company=other, file_path='theirs.xlsx')
        r = self.client.get(f'/api/sales/backups/stored/{theirs.id}/?company_id={self.co.id}')
        self.assertEqual(r.status_code, 404, "another company's backup is not downloadable")


class SomebodyCanAlwaysSignBackIn(APITestCase):
    """A reset must never leave a company nobody can log into.

    Restoring needs a login, and every account the workbook brings back has no
    password — hashes are deliberately kept out of the file. So at least one
    working account has to survive the reset itself.
    """

    def setUp(self):
        self.co, self.admin, self.rep, *_ = _seed()

    def _login(self, code, user_code, password):
        return self.client.post('/api/auth/login/', {
            'company_code': code, 'user_code': user_code,
            'password': password, 'platform': 'web'}, format='json').status_code

    def test_the_admin_who_resets_keeps_their_password(self):
        reset_company(self.co, keep_user_id=self.admin.id)
        self.assertEqual(self._login('CYC', 'CYC001', 'x'), 200)

    def test_restoring_does_not_clobber_that_password(self):
        buf = BytesIO()
        build_workbook(self.co).save(buf)
        reset_company(self.co, keep_user_id=self.admin.id)
        buf.seek(0)
        restore(self.co, parse_workbook(buf), commit=True)
        self.assertEqual(self._login('CYC', 'CYC001', 'x'), 200,
                         'the surviving account must not be overwritten by the restore')

    def test_restored_colleagues_can_sign_in_with_their_old_password(self):
        """Hashes are in the workbook, so a restore hands people their logins
        back rather than a company of accounts nobody can use."""
        buf = BytesIO()
        build_workbook(self.co).save(buf)
        reset_company(self.co, keep_user_id=self.admin.id)
        buf.seek(0)
        self.assertEqual(self._login('CYC', 'CYC002', 'x'), 401, 'deleted, so no login')
        restore(self.co, parse_workbook(buf), commit=True)
        rep = User.objects.get(company=self.co, user_code='CYC002')
        self.assertTrue(rep.has_usable_password())
        self.assertEqual(self._login('CYC', 'CYC002', 'x'), 200)

    def test_a_restored_user_is_still_findable_by_email(self):
        """The blind index is derived in save(), which bulk_create skips."""
        buf = BytesIO()
        build_workbook(self.co).save(buf)
        reset_company(self.co, keep_user_id=self.admin.id)
        buf.seek(0)
        restore(self.co, parse_workbook(buf), commit=True)
        rep = User.objects.get(company=self.co, user_code='CYC002')
        self.assertTrue(rep.email_key, 'email lookups would miss this user')

    def test_a_platform_admin_resetting_elsewhere_leaves_that_company_a_way_in(self):
        """keep_user_id is in another company, so keeping it would keep nobody here."""
        vrl = Company.objects.create(code='VRL', name='Vistara', is_active=True)
        root = User.objects.create_user('root@vrl.com', company=vrl, user_code='VRL1',
                                        password='x', name='Root', role='Admin',
                                        modules=['Sales'])
        reset_company(self.co, keep_user_id=root.id)
        self.assertTrue(User.objects.filter(company=self.co, role='Admin').exists(),
                        "the company's own admin must survive or nobody can restore it")
        self.assertEqual(self._login('CYC', 'CYC001', 'x'), 200)
