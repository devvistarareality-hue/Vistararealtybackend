"""The assistant's tools, and the scoping that keeps them honest.

The model picks which tool to call; these tests cover what happens then. The
Claude call itself is mocked throughout — the value here is not that an HTTP
request is made, it is that the numbers handed back are the asking user's own
and nobody else's.

The scoping tests are the point of the file. An assistant that answers
"how many site visits last month" with another company's total is worse than no
assistant, and it would look perfectly plausible on screen.
"""
import json
from datetime import date, timedelta
from unittest import mock

from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import Booking, Lead, LeadSource, Project, SiteVisit
from sales.tests import auth

from ai.tools import booking_stats, lead_stats, run_tool, site_visit_stats

ASK = '/api/ai/ask/'
STATUS = '/api/ai/status/'


def reply(text=None, tool=None, args=None):
    """A Claude response: either a sentence, or a request to call a tool."""
    if tool:
        return {'stop_reason': 'tool_use',
                'content': [{'type': 'tool_use', 'id': 'tu_1', 'name': tool,
                             'input': args or {}}]}
    return {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': text or 'Done.'}]}


class ToolScopingTests(APITestCase):
    """Two companies, same dates, same shaped data. Each must see only its own."""

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='AIA', name='Ours')
        cls.other = Company.objects.create(code='AIB', name='Theirs')
        cls.admin = User.objects.create(email='ai_admin@x.com', company=cls.co,
                                        role='Admin', user_code='A1', name='Admin')
        cls.their_admin = User.objects.create(email='ai_other@x.com', company=cls.other,
                                              role='Admin', user_code='A2', name='Them')
        cls.stm = User.objects.create(email='ai_stm@x.com', company=cls.co, role='Employee',
                                      designation='STM', user_code='A3', name='Rep')
        cls.mate = User.objects.create(email='ai_mate@x.com', company=cls.co, role='Employee',
                                       designation='STM', user_code='A4', name='Other Rep')
        cls.proj = Project.objects.create(company=cls.co, name='Kalrav')
        cls.their_proj = Project.objects.create(company=cls.other, name='Kalrav')
        cls.src = LeadSource.objects.create(company=cls.co, name='Walk-in')

        when = timezone.now() - timedelta(days=10)

        def visit(company, project, owner, n):
            lead = Lead.objects.create(company=company, name=f'C{n}', phone=f'+9198000000{n:02d}',
                                       project=project, stm=owner)
            return SiteVisit.objects.create(lead=lead, project=project, scheduled_at=when,
                                            status='completed')

        cls.ours = [visit(cls.co, cls.proj, cls.stm, i) for i in range(3)]
        cls.mates = [visit(cls.co, cls.proj, cls.mate, 10 + i) for i in range(2)]
        cls.theirs = [visit(cls.other, cls.their_proj, cls.their_admin, 20 + i) for i in range(7)]
        cls.when = when.date()

    def setUp(self):
        cache.clear()

    def _range(self):
        return (self.when - timedelta(days=2)).isoformat(), (self.when + timedelta(days=2)).isoformat()

    def test_an_admin_sees_their_own_companys_visits_only(self):
        a, b = self._range()
        out = site_visit_stats(self.admin, a, b)
        self.assertEqual(out['total'], 5, 'three of the rep\'s plus two of their colleague\'s')

    def test_another_companys_visits_are_never_counted(self):
        # Seven exist next door, on a project with the identical name.
        a, b = self._range()
        self.assertEqual(site_visit_stats(self.their_admin, a, b)['total'], 7)
        self.assertEqual(site_visit_stats(self.admin, a, b)['total'], 5)

    def test_a_rep_sees_only_their_own(self):
        # scope_leads_to_role, same as every screen — the assistant must not become
        # a way around the visibility rules the UI enforces.
        a, b = self._range()
        self.assertEqual(site_visit_stats(self.stm, a, b)['total'], 3)

    def test_a_project_name_cannot_reach_into_another_company(self):
        # Both companies have a project called Kalrav.
        a, b = self._range()
        out = site_visit_stats(self.admin, a, b, project='Kalrav')
        self.assertEqual(out['total'], 5)
        self.assertEqual(out['project'], 'Kalrav')

    def test_an_unknown_project_returns_nothing_rather_than_everything(self):
        # A name that matches nothing must not silently widen to all projects.
        a, b = self._range()
        self.assertEqual(site_visit_stats(self.admin, a, b, project='Nowhere')['total'], 0)

    def test_dates_outside_the_range_are_excluded(self):
        far = (self.when + timedelta(days=40)).isoformat()
        self.assertEqual(site_visit_stats(self.admin, far, far)['total'], 0)


class ToolShapeTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='AIC', name='Shape Co')
        cls.admin = User.objects.create(email='ais@x.com', company=cls.co, role='Admin',
                                        user_code='S1', name='Admin')
        cls.proj = Project.objects.create(company=cls.co, name='Tundav')
        cls.src = LeadSource.objects.create(company=cls.co, name='Walk-in')
        Booking.objects.create(company=cls.co, project=cls.proj, status='sold',
                               client_name='A', phone='9000000001', booking_date=date(2026, 9, 10),
                               final_amount=1000000)
        Booking.objects.create(company=cls.co, project=cls.proj, status='pending',
                               client_name='B', phone='9000000002', booking_date=date(2026, 9, 11),
                               final_amount=9000000)
        Lead.objects.create(company=cls.co, name='L1', phone='9000000003',
                            project=cls.proj, source=cls.src)

    def setUp(self):
        cache.clear()

    def test_bookings_split_by_stage_and_total_only_the_sold(self):
        out = booking_stats(self.admin, '2026-09-01', '2026-09-30')
        self.assertEqual(out['total'], 2)
        self.assertEqual(out['by_stage'], {'sold': 1, 'pending': 1})
        self.assertEqual(out['sold_count'], 1)
        # Adding the pending 90L in would read as revenue and would not be.
        self.assertEqual(out['sold_value'], 1000000)

    def test_leads_are_counted_by_status_source_and_project(self):
        today = timezone.localdate().isoformat()
        out = lead_stats(self.admin, '2020-01-01', today)
        self.assertEqual(out['total'], 1)
        self.assertEqual(out['by_source'], {'Walk-in': 1})
        self.assertEqual(out['by_project'], {'Tundav': 1})

    def test_nothing_personal_is_ever_returned(self):
        """The whole privacy argument rests on this: aggregates only.

        If a client name or phone number can reach the payload, the claim that no
        personal data leaves the building stops being true.
        """
        today = timezone.localdate().isoformat()
        blob = json.dumps([
            booking_stats(self.admin, '2026-09-01', '2026-09-30'),
            lead_stats(self.admin, '2020-01-01', today),
            site_visit_stats(self.admin, '2020-01-01', today),
        ])
        for leak in ('9000000001', '9000000002', '9000000003', '"A"', '"B"', 'L1'):
            self.assertNotIn(leak, blob, f'{leak} reached the model')

    # ------------------------------------------------------- bad input is data

    def test_a_malformed_date_is_reported_not_raised(self):
        out = site_visit_stats(self.admin, 'last month', 'today')
        self.assertIn('error', out)

    def test_an_unknown_tool_is_reported_not_raised(self):
        self.assertIn('error', run_tool('drop_everything', {}, self.admin))

    def test_unexpected_arguments_are_dropped(self):
        # The model invents an argument; it must not reach the ORM.
        out = run_tool('site_visit_stats',
                       {'date_from': '2026-09-01', 'date_to': '2026-09-30',
                        'company_id': 999, 'limit': 'all'},
                       self.admin)
        self.assertNotIn('error', out)


class AskEndpointTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='AID', name='Ask Co')
        cls.admin = User.objects.create(email='aiq@x.com', company=cls.co, role='Admin',
                                        user_code='Q1', name='Admin')

    def setUp(self):
        cache.clear()
        auth(self.client, self.admin)

    def test_a_question_comes_back_answered(self):
        calls = []

        def fake(key, messages, today):
            calls.append(messages)
            if len(calls) == 1:
                return reply(tool='site_visit_stats',
                             args={'date_from': '2026-09-01', 'date_to': '2026-09-30'})
            return reply('There were no site visits in September.')

        with mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}), \
                mock.patch('ai.views.AskView._call', side_effect=fake):
            r = self.client.post(ASK, {'question': 'site visits last month?'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn('no site visits', r.data['answer'])
        self.assertEqual(r.data['used'], ['site_visit_stats'])

    def test_the_tool_result_is_fed_back_to_the_model(self):
        # Without this the model answers from nothing, which is how a confident
        # wrong number gets onto someone's screen.
        seen = {}

        def fake(key, messages, today):
            if len(messages) == 1:
                return reply(tool='booking_stats',
                             args={'date_from': '2026-09-01', 'date_to': '2026-09-30'})
            seen['followup'] = messages[-1]
            return reply('Two bookings.')

        with mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'k'}), \
                mock.patch('ai.views.AskView._call', side_effect=fake):
            self.client.post(ASK, {'question': 'bookings?'}, format='json')
        block = seen['followup']['content'][0]
        self.assertEqual(block['type'], 'tool_result')
        self.assertIn('by_stage', block['content'])

    def test_without_a_key_it_says_so_rather_than_breaking(self):
        with mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': ''}):
            r = self.client.post(ASK, {'question': 'anything'}, format='json')
        self.assertEqual(r.status_code, 503)
        self.assertIn('not configured', r.data['detail'])

    def test_status_tells_the_ui_whether_to_offer_it(self):
        with mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': ''}):
            self.assertFalse(self.client.get(STATUS).data['enabled'])
        with mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'k'}):
            self.assertTrue(self.client.get(STATUS).data['enabled'])

    def test_an_empty_question_is_refused(self):
        with mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'k'}):
            self.assertEqual(self.client.post(ASK, {'question': '  '}, format='json').status_code, 400)

    def test_an_overlong_question_is_refused(self):
        with mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'k'}):
            r = self.client.post(ASK, {'question': 'x' * 501}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_an_endless_tool_loop_is_cut_off(self):
        # A model that keeps asking for tools must not bill in a loop.
        with mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'k'}), \
                mock.patch('ai.views.AskView._call',
                           return_value=reply(tool='lead_stats',
                                              args={'date_from': '2026-09-01',
                                                    'date_to': '2026-09-30'})) as called:
            r = self.client.post(ASK, {'question': 'leads?'}, format='json')
        self.assertEqual(r.status_code, 503)
        self.assertLessEqual(called.call_count, 5)

    def test_an_api_failure_reads_as_unavailable(self):
        with mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'k'}), \
                mock.patch('ai.views.AskView._call', side_effect=RuntimeError('boom')):
            r = self.client.post(ASK, {'question': 'leads?'}, format='json')
        self.assertEqual(r.status_code, 502)

    def test_signing_in_is_required(self):
        self.client.credentials()
        self.assertIn(self.client.post(ASK, {'question': 'x'}, format='json').status_code, (401, 403))
