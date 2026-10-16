"""A lead that changed hands says so in its own timeline.

Approving a transfer moved lead.stm and wrote nothing else, so opening the lead
showed it sitting with someone new and no sign of when it moved, who asked for
it, or who signed it off. All of that lived only on the Approvals screen, which
is not where anyone looks when reading a lead's story.

These are read off the transfers themselves rather than written at approval
time, the same way follow-up events are. Not only for consistency: every
transfer approved before this existed shows up too, which a write-time row
could only manage with a backfill.
"""
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Company
from sales.models import Lead, LeadTransfer, Project


class TransferHistoryTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='TRH', name='Transfer Co', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@trh.com', company=cls.co, user_code='TRH001', password='p',
            name='The Admin', role='Admin', modules=['Sales'])
        cls.frm = User.objects.create_user(
            'from@trh.com', company=cls.co, user_code='TRH002', password='p',
            name='Chirag Gulabwani', role='Employee', designation='STM', modules=['Sales'])
        cls.to = User.objects.create_user(
            'to@trh.com', company=cls.co, user_code='TRH003', password='p',
            name='Parth Kapadia', role='Employee', designation='STM', modules=['Sales'])
        cls.approver = User.objects.create_user(
            'appr@trh.com', company=cls.co, user_code='TRH004', password='p',
            name='Sachin Mehta', role='Director', modules=['Sales'])
        cls.project = Project.objects.create(company=cls.co, name='Pratishtha')
        cls.project.booking_approvers = [cls.approver.id]
        cls.project.save(update_fields=['booking_approvers'])

    def setUp(self):
        self.lead = Lead.objects.create(
            company=self.co, name='Yogesh Raykwar', phone='+919265011432',
            project=self.project, stm=self.frm, telecaller=self.frm)
        self.api = APIClient()
        self.api.force_authenticate(user=self.admin)

    def _request_transfer(self, reason='This lead was handled by parth kapadia'):
        return LeadTransfer.objects.create(
            company=self.co, lead=self.lead, project=self.project, from_stm=self.frm,
            to_stm=self.to, requested_by=self.frm, reason=reason, status='pending')

    def _events(self):
        r = self.api.get(f'/api/sales/leads/{self.lead.pk}/')
        self.assertEqual(r.status_code, 200, r.data)
        return [e for e in r.data['history'] if str(e['field_changed']).startswith('transfer_')]

    def _decide(self, t, action, user=None, note=''):
        api = APIClient()
        api.force_authenticate(user=user or self.approver)
        return api.post(f'/api/sales/lead-transfers/{t.pk}/action/',
                        {'action': action, 'note': note}, format='json')

    def _by(self, kind):
        return next((e for e in self._events() if e['field_changed'] == kind), None)

    # ------------------------------------------------------------- the request

    def test_the_request_appears_with_who_asked_and_why(self):
        self._request_transfer()
        e = self._by('transfer_requested')
        self.assertIsNotNone(e, 'the transfer request is missing from the timeline')
        self.assertIn('Chirag Gulabwani', e['new_value'])
        self.assertIn('Parth Kapadia', e['new_value'])
        self.assertIn('Requested by Chirag Gulabwani', e['remarks'])
        self.assertIn('handled by parth kapadia', e['remarks'])

    def test_a_request_with_no_reason_still_reads_properly(self):
        self._request_transfer(reason='')
        self.assertIn('Requested by Chirag Gulabwani', self._by('transfer_requested')['remarks'])

    # ------------------------------------------------------------ the decision

    def test_an_approval_names_who_approved_it(self):
        """The half that was missing entirely — the lead moved and nothing said
        on whose authority."""
        t = self._request_transfer()
        self.assertEqual(self._decide(t, 'approve').status_code, 200)
        e = self._by('transfer_approved')
        self.assertIsNotNone(e)
        self.assertEqual(e['remarks'], 'Approved by Sachin Mehta')
        self.assertEqual(e['changed_by_name'], 'Sachin Mehta')
        self.assertEqual(e['old_value'], 'Chirag Gulabwani')
        self.assertEqual(e['new_value'], 'Parth Kapadia')

    def test_a_decision_note_is_carried(self):
        t = self._request_transfer()
        self._decide(t, 'approve', note='agreed on call')
        self.assertIn('agreed on call', self._by('transfer_approved')['remarks'])

    def test_a_rejection_is_shown_too(self):
        """'A transfer was asked for and refused' is part of the story; leaving
        it out reads as though nobody ever asked."""
        t = self._request_transfer()
        self._decide(t, 'reject', note='keep it with Chirag')
        e = self._by('transfer_rejected')
        self.assertIsNotNone(e)
        self.assertIn('Rejected by Sachin Mehta', e['remarks'])
        self.assertIn('keep it with Chirag', e['remarks'])

    def test_a_withdrawn_request_is_shown(self):
        t = self._request_transfer()
        self._decide(t, 'cancel', user=self.frm)
        self.assertIsNotNone(self._by('transfer_cancelled'))

    def test_a_pending_request_shows_no_decision_yet(self):
        self._request_transfer()
        self.assertIsNone(self._by('transfer_approved'))
        self.assertIsNone(self._by('transfer_rejected'))

    # -------------------------------------------------------------- the basics

    def test_the_timeline_matches_what_actually_happened(self):
        t = self._request_transfer()
        self._decide(t, 'approve')
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stm_id, self.to.id, 'the lead did not actually move')
        kinds = [e['field_changed'] for e in self._events()]
        self.assertEqual(kinds, ['transfer_requested', 'transfer_approved'])

    def test_two_transfers_both_appear(self):
        t1 = self._request_transfer()
        self._decide(t1, 'approve')
        t2 = LeadTransfer.objects.create(
            company=self.co, lead=self.lead, project=self.project, from_stm=self.to,
            to_stm=self.frm, requested_by=self.to, status='pending')
        self._decide(t2, 'approve')
        self.assertEqual(len(self._events()), 4)

    def test_a_lead_never_transferred_has_no_transfer_entries(self):
        self.assertEqual(self._events(), [])

    def test_the_entries_carry_a_timestamp_the_timeline_can_sort_by(self):
        # They are merged into a list sorted on created_at; a null would sink
        # the entry to 2000-01-01 and read as the first thing that ever
        # happened to the lead.
        t = self._request_transfer()
        self._decide(t, 'approve')
        for e in self._events():
            self.assertTrue(e['created_at'], e)

    def test_a_transfer_whose_requester_was_deleted_still_reads(self):
        # requested_by is SET_NULL, so this is reachable.
        t = self._request_transfer()
        LeadTransfer.objects.filter(pk=t.pk).update(requested_by=None)
        self.assertIn('Requested by someone', self._by('transfer_requested')['remarks'])

    def test_the_approval_event_also_reaches_the_assigned_caller(self):
        """The history button on a follow-up is one of the places this shows,
        so the widened read must carry these entries too."""
        from django.utils import timezone as tz
        from sales.models import FollowUp
        caller = User.objects.create_user(
            'c@trh.com', company=self.co, user_code='TRH005', password='p',
            name='Covering Caller', role='Employee', designation='STM', modules=['Sales'])
        FollowUp.objects.create(lead=self.lead, assigned_to=caller,
                                scheduled_at=tz.now(), role_context='stm', created_by=self.frm)
        t = self._request_transfer()
        self._decide(t, 'approve')
        api = APIClient()
        api.force_authenticate(user=caller)
        r = api.get(f'/api/sales/leads/{self.lead.pk}/')
        self.assertEqual(r.status_code, 200)
        kinds = [e['field_changed'] for e in r.data['history']]
        self.assertIn('transfer_approved', kinds)
