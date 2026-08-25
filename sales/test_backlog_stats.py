"""The dashboard's backlog tiles: To Call, Follow-ups Pending, Follow-ups Overdue.

These count what is still *waiting*, not what was done, so they are the tiles a rep
plans their day from. Each one has to agree with the screen it links to — the All
Leads "To Call" tab and the Follow-Ups screen's Pending/Overdue chips — or the
dashboard quietly lies about the size of the queue.
"""
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import Lead, FollowUp
from sales.tests import auth


class BacklogStatsTests(APITestCase):
    def setUp(self):
        # StatsView caches per user id, and test rollbacks reuse ids, so a stale
        # payload from another class would otherwise leak in.
        cache.clear()
        self.co = Company.objects.create(code='BKL', name='Backlog Co')
        self.tc = User.objects.create(email='tc@x.com', company=self.co, role='Sales',
                                      user_code='T1', designation='Telecaller')
        self.stm = User.objects.create(email='stm@x.com', company=self.co, role='Sales',
                                       user_code='S1', designation='STM')

    def tearDown(self):
        cache.clear()

    def _lead(self, **kw):
        return Lead.objects.create(company=self.co, name='L', phone='9000000000',
                                   status='new', **kw)

    def _stats(self, user):
        auth(self.client, user)
        res = self.client.get('/api/sales/stats/')
        self.assertEqual(res.status_code, 200)
        return res.data

    # ── To Call ────────────────────────────────────────────────────────────────
    def test_to_call_counts_only_unactioned_leads_for_a_telecaller(self):
        self._lead(telecaller=self.tc)                              # not called yet
        self._lead(telecaller=self.tc)                              # not called yet
        self._lead(telecaller=self.tc, telecaller_status='warm')    # already worked
        d = self._stats(self.tc)
        self.assertEqual(d['to_call_count'], 2)
        self.assertEqual(d['called_count'], 1)

    def test_to_call_uses_the_stm_column_for_an_stm(self):
        """An STM's backlog is leads with no stm_status — their telecaller_status is
        somebody else's column and must not count as worked."""
        self._lead(stm=self.stm, telecaller_status='warm')          # TC done, STM hasn't
        self._lead(stm=self.stm, stm_status='hot')                  # STM has worked it
        d = self._stats(self.stm)
        self.assertEqual(d['to_call_count'], 1)

    def test_to_call_matches_the_all_leads_to_call_tab(self):
        """The tile links to that tab, so the two must never disagree."""
        for _ in range(3):
            self._lead(telecaller=self.tc)
        self._lead(telecaller=self.tc, telecaller_status='cold')

        tile = self._stats(self.tc)['to_call_count']
        res = self.client.get('/api/sales/leads/?work=pending')
        self.assertEqual(res.status_code, 200)
        tab = res.data['count'] if isinstance(res.data, dict) else len(res.data)
        self.assertEqual(tile, tab, 'To Call tile disagrees with the To Call tab')

    def test_another_reps_leads_are_not_in_my_backlog(self):
        other = User.objects.create(email='tc2@x.com', company=self.co, role='Sales',
                                    user_code='T2', designation='Telecaller')
        self._lead(telecaller=other)
        self._lead(telecaller=self.tc)
        self.assertEqual(self._stats(self.tc)['to_call_count'], 1)

    # ── Follow-ups ─────────────────────────────────────────────────────────────
    def _fu(self, user, when, status='pending'):
        return FollowUp.objects.create(lead=self._lead(), assigned_to=user,
                                       role_context='telecaller',
                                       scheduled_at=when, status=status)

    def test_pending_and_overdue_split_on_the_scheduled_time(self):
        now = timezone.now()
        self._fu(self.tc, now - timedelta(days=2))      # overdue
        self._fu(self.tc, now - timedelta(hours=3))     # overdue
        self._fu(self.tc, now + timedelta(days=1))      # pending, not yet due
        d = self._stats(self.tc)
        self.assertEqual(d['followup_pending_count'], 3)
        self.assertEqual(d['followup_overdue_count'], 2)

    def test_overdue_is_a_subset_of_pending(self):
        now = timezone.now()
        for i in range(4):
            self._fu(self.tc, now - timedelta(days=i + 1))
        d = self._stats(self.tc)
        self.assertLessEqual(d['followup_overdue_count'], d['followup_pending_count'])
        self.assertEqual(d['followup_overdue_count'], 4)

    def test_completed_follow_ups_are_neither_pending_nor_overdue(self):
        now = timezone.now()
        fu = self._fu(self.tc, now - timedelta(days=5), status='completed')
        fu.completed_at = now
        fu.save(update_fields=['completed_at'])
        d = self._stats(self.tc)
        self.assertEqual(d['followup_pending_count'], 0)
        self.assertEqual(d['followup_overdue_count'], 0)
        self.assertEqual(d['followup_call_count'], 1)   # it still counts as a call made

    def test_follow_ups_are_scoped_to_the_assignee(self):
        now = timezone.now()
        self._fu(self.stm, now - timedelta(days=1))     # somebody else's
        self._fu(self.tc,  now - timedelta(days=1))
        self.assertEqual(self._stats(self.tc)['followup_overdue_count'], 1)

    def test_backlog_never_crosses_companies(self):
        other_co = Company.objects.create(code='OTH', name='Other Co')
        other_tc = User.objects.create(email='o@x.com', company=other_co, role='Sales',
                                       user_code='O1', designation='Telecaller')
        Lead.objects.create(company=other_co, name='X', phone='9111111111',
                            status='new', telecaller=other_tc)
        FollowUp.objects.create(
            lead=Lead.objects.create(company=other_co, name='Y', phone='9222222222'),
            assigned_to=other_tc, role_context='telecaller',
            scheduled_at=timezone.now() - timedelta(days=1), status='pending')
        self._lead(telecaller=self.tc)

        d = self._stats(self.tc)
        self.assertEqual(d['to_call_count'], 1)
        self.assertEqual(d['followup_pending_count'], 0)
