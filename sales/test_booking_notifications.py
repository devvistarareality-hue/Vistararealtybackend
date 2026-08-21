"""Every booking event reaches the rep, the approver and Accounts & Finance."""
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.models import Notification, User
from companies.models import Company
from sales.models import Booking, Plot, Project
from sales.views import BookingActionView, BookingListCreateView


class BookingNotifications(TestCase):
    def setUp(self):
        self.co = Company.objects.create(code='NT', name='NT Co')
        self.stm = self._user('stm@x.com', 'ST', 'Employee')
        self.approver = self._user('appr@x.com', 'AP', 'Manager')
        self.accounts = self._user('acc@x.com', 'AC', 'Manager',
                                   manager_modules=['Accounts & Finance'])
        self.admin = self._user('adm@x.com', 'AD', 'Admin')
        self.project = Project.objects.create(company=self.co, name='P',
                                              booking_approvers=[self.approver.id])
        self.plot = Plot.objects.create(project=self.project, number='A-1')

    def _user(self, email, code, role, **extra):
        return User.objects.create(name=code, email=email, phone='9' + code + '000000',
                                   user_code=code, role=role, company=self.co, **extra)

    def _who_got(self, ntype=None):
        qs = Notification.objects.all()
        if ntype:
            qs = qs.filter(type=ntype)
        return {n.recipient_id for n in qs}

    def _submit(self, actor=None, **extra):
        payload = {'project': self.project.id, 'plot': self.plot.id,
                   'client_name': 'C', 'phone': '9000000000', 'final_amount': 100000}
        payload.update(extra)
        req = APIRequestFactory().post('/x/', payload, format='json')
        force_authenticate(req, user=actor or self.stm)
        return BookingListCreateView.as_view()(req)

    def _act(self, booking_id, action, actor):
        req = APIRequestFactory().post('/x/', {'action': action}, format='json')
        force_authenticate(req, user=actor)
        return BookingActionView.as_view()(req, pk=booking_id)

    def test_submitting_notifies_the_approver_and_accounts(self):
        Notification.objects.all().delete()
        self._submit()
        got = self._who_got()
        self.assertIn(self.approver.id, got, 'the named approver must be asked')
        self.assertIn(self.accounts.id, got, 'Accounts & Finance must be told')
        self.assertNotIn(self.stm.id, got, 'the submitter does not need telling')

    def test_a_revision_submitted_by_someone_else_still_reaches_the_rep(self):
        res = self._submit()
        bid = res.data['id']
        Notification.objects.all().delete()
        self._submit(actor=self.admin, revision_of=bid)
        got = self._who_got()
        self.assertIn(self.stm.id, got, "the rep who sold the unit must hear about a revision")
        self.assertIn(self.approver.id, got)
        self.assertIn(self.accounts.id, got)

    def test_approval_notifies_the_rep_and_accounts(self):
        bid = self._submit().data['id']
        Notification.objects.all().delete()
        self.assertEqual(self._act(bid, 'approve', self.approver).status_code, 200)
        got = self._who_got()
        self.assertIn(self.stm.id, got, 'the rep must be told their booking cleared')
        self.assertIn(self.accounts.id, got, 'Accounts must be told')

    def test_rejection_notifies_the_rep_and_accounts(self):
        bid = self._submit().data['id']
        Notification.objects.all().delete()
        self.assertEqual(self._act(bid, 'reject', self.approver).status_code, 200)
        got = self._who_got()
        self.assertIn(self.stm.id, got, 'the rep must be told it was rejected')
        self.assertIn(self.accounts.id, got, 'Accounts must be told a deal fell through')

    def test_accounts_is_identified_by_the_module_not_by_name(self):
        """No hardcoded person — whoever holds the module gets it."""
        other = self._user('acc2@x.com', 'A2', 'Manager',
                           manager_modules=['Accounts & Finance'])
        Notification.objects.all().delete()
        self._submit()
        self.assertIn(other.id, self._who_got())

    def test_a_manager_without_the_module_is_not_spammed(self):
        plain = self._user('plain@x.com', 'PL', 'Manager')
        Notification.objects.all().delete()
        self._submit()
        self.assertNotIn(plain.id, self._who_got())
