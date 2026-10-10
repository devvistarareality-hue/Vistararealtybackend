"""Ask Nexora: the query tool reads only what the asker may see, handles encrypted
fields, and the question loop runs tools then answers (Claude replaced by a fake)."""
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from activity.models import ActivityLog
from companies.models import Company
from sales import assistant
from sales.models import Closure, Lead, LeadSource, Project, SiteVisit


class QueryToolTests(TestCase):
    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='AIQ', name='Aiq Co')
        self.p1 = Project.objects.create(company=self.co, name='Kalrav')
        self.p2 = Project.objects.create(company=self.co, name='Tundav')
        meta = LeadSource.objects.create(company=self.co, name='Meta')
        mk = lambda code, role, boss=None: User.objects.create(
            name=code, email=f'{code}@x.com', phone='9' + code + '00000', user_code=code, role=role,
            designation='STM' if role == 'Employee' else role, company=self.co, modules=['Sales'],
            reporting_manager=boss)
        self.admin = mk('QA', 'Admin')
        self.boss = mk('QB', 'Manager')
        self.stm = mk('QS', 'Employee', self.boss)
        self.other = mk('QO', 'Employee', self.boss)
        now = timezone.now()
        for i, (owner, proj) in enumerate([(self.stm, self.p1), (self.stm, self.p2), (self.other, self.p1)]):
            lead = Lead.objects.create(company=self.co, project=proj, source=meta, name=f'Shah {i}',
                                       phone=f'98000000{i:02d}', stm=owner, meta_campaign_name='Diwali')
            SiteVisit.objects.create(lead=lead, project=proj, stm=owner, status='completed',
                                     outcome='hot' if i == 0 else 'cold', scheduled_at=now, visited_at=now)
            Closure.objects.create(company=self.co, lead=lead, project=proj, stm=owner, client_name=f'Shah {i}',
                                   closure_date=timezone.localdate(), total_amount=1000000 * (i + 1))

    def q(self, user, **args):
        return assistant.run_query(user, None, args)

    def test_counts_are_limited_to_what_the_asker_sees(self):
        self.assertEqual(self.q(self.stm, entity='leads', mode='count')['total'], 2)
        self.assertEqual(self.q(self.admin, entity='leads', mode='count')['total'], 3)

    def test_group_by_and_date_keywords(self):
        out = self.q(self.admin, entity='site_visits', mode='count', group_by=['project'],
                     filters=[{'field': 'visited', 'op': 'eq', 'value': 'today'}])
        self.assertEqual({g['project']: g['count'] for g in out['groups']}, {'Kalrav': 2, 'Tundav': 1})

    def test_encrypted_filter_and_money_sum(self):
        out = self.q(self.admin, entity='leads', mode='count',
                     filters=[{'field': 'name', 'op': 'contains', 'value': 'shah 1'}])
        self.assertEqual(out['total'], 1)
        out = self.q(self.admin, entity='closures', mode='sum', sum_field='total_amount', group_by=['project'])
        self.assertEqual(out['grand_total'], 6000000.0)
        self.assertEqual({g['project']: g['total'] for g in out['groups']}, {'Kalrav': 4000000.0, 'Tundav': 2000000.0})

    def test_list_mode(self):
        out = self.q(self.stm, entity='site_visits', mode='list', fields=['client', 'project', 'outcome'])
        self.assertEqual(out['shown'], 2)
        self.assertTrue(all(set(r) == {'client', 'project', 'outcome'} for r in out['rows']))

    def test_unknown_field_is_an_error(self):
        with self.assertRaises(ValueError):
            self.q(self.admin, entity='leads', mode='count', group_by=['name'])


def _msg(content, stop):
    usage = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0)
    return SimpleNamespace(stop_reason=stop, usage=usage, to_dict=lambda: {'content': content})


@override_settings(AI_INLINE=True)
class AskViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='AIV', name='Aiv Co')
        self.admin = User.objects.create(name='Admin', email='a@aiv.com', phone='9100000099', user_code='AV-A',
                                         role='Admin', company=self.co)
        self.stm = User.objects.create(name='S', email='s@aiv.com', phone='9100000098', user_code='AV-S',
                                       role='Employee', designation='STM', company=self.co, modules=['Sales'],
                                       reporting_manager=self.admin)

    def _ask(self, user, question='How many leads?'):
        c = APIClient(); c.force_authenticate(user)
        res = c.post('/api/sales/ai/ask/', {'question': question}, format='json')
        return c, res

    def test_not_ticked_is_refused(self):
        _, res = self._ask(self.stm)
        self.assertEqual(res.status_code, 403)

    @mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test'})
    def test_tool_then_answer_and_logged(self):
        fake = mock.MagicMock()
        fake.messages.create.side_effect = [
            _msg([{'type': 'tool_use', 'id': 't1', 'name': 'query_records',
                   'input': {'entity': 'leads', 'mode': 'count'}}], 'tool_use'),
            _msg([{'type': 'text', 'text': 'You have 0 leads.'}], 'end_turn'),
        ]
        with mock.patch.object(assistant, '_client', return_value=fake):
            c, res = self._ask(self.admin)
        self.assertEqual(res.status_code, 202, res.content)
        st = c.get(f"/api/sales/ai/ask/{res.json()['job']}/").json()
        self.assertEqual(st['status'], 'done', st)
        self.assertEqual(st['answer'], 'You have 0 leads.')
        # the tool result went back to the model
        sent = fake.messages.create.call_args_list[1].kwargs['messages']
        result = next(m for m in sent if m['role'] == 'user' and isinstance(m['content'], list))
        self.assertEqual(result['content'][0]['type'], 'tool_result')
        self.assertIn('"total": 0', result['content'][0]['content'])
        import json
        log = json.loads(ActivityLog.objects.filter(target_type='ai question').latest('id').details)
        self.assertEqual(log['queries'], 1)
        self.assertGreater(log['approx_cost_inr'], 0)
        # someone else cannot read the answer
        other = APIClient(); other.force_authenticate(self.stm)
        self.assertEqual(other.get(f"/api/sales/ai/ask/{res.json()['job']}/").status_code, 404)

    def test_missing_key_says_so(self):
        with mock.patch.dict('os.environ', {}, clear=False):
            import os
            os.environ.pop('ANTHROPIC_API_KEY', None)
            _, res = self._ask(self.admin)
        self.assertEqual(res.status_code, 503)
