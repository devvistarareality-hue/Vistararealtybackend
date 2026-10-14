"""Lead stages go back to Meta (Conversions API): once per stage, Meta leads only,
nothing while switched off, personal data hashed."""
import json
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from companies.models import Company
from sales import meta_capi
from sales.models import Booking, Lead, MetaCapiEvent, MetaWebhookConfig, Project, SiteVisit


class FakeResp:
    status_code = 200
    text = '{"events_received":1}'


@override_settings(META_CAPI_INLINE=True)
class MetaCapiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='CAP', name='Cap')
        self.cfg = MetaWebhookConfig.objects.create(company=self.co, verify_token='x', capi_dataset_id='1602590401463535',
                                                    capi_token='tok', capi_enabled=True)
        self.p = Project.objects.create(company=self.co, name='Pratishtha')

    def _lead(self, **kw):
        with self.captureOnCommitCallbacks(execute=True):
            return Lead.objects.create(company=self.co, project=self.p, name='Asha', phone='9876543210',
                                       email='Asha@X.com', **kw)

    def test_stages_are_sent_once_with_hashed_data(self):
        with mock.patch('requests.post', return_value=FakeResp()) as post:
            lead = self._lead(meta_form_id='77', meta_leadgen_id='1234567890123456')
            for _ in range(2):                                  # second save: no new event
                lead.telecaller_status = 'warm'
                with self.captureOnCommitCallbacks(execute=True):
                    lead.save()
            with self.captureOnCommitCallbacks(execute=True):
                v = SiteVisit.objects.create(lead=lead, project=self.p, scheduled_at=timezone.now())
            v.status = 'completed'
            with self.captureOnCommitCallbacks(execute=True):
                v.save()
        names = list(MetaCapiEvent.objects.filter(lead=lead).order_by('id').values_list('event_name', 'status'))
        self.assertEqual(names, [('Lead_Received', 'sent'), ('Qualified', 'sent'),
                                 ('Site_Visit_Scheduled', 'sent'), ('Site_Visit_Done', 'sent')])
        body = post.call_args_list[1].kwargs['json']['data'][0]
        self.assertEqual(body['event_name'], 'Qualified')
        self.assertEqual(body['action_source'], 'system_generated')
        self.assertEqual(body['user_data']['lead_id'], 1234567890123456)
        self.assertEqual(body['user_data']['ph'], [meta_capi._sha('919876543210')])
        self.assertEqual(body['user_data']['em'], [meta_capi._sha('asha@x.com')])
        self.assertNotIn('9876543210', json.dumps(body))
        self.assertIn('/1602590401463535/events', post.call_args_list[0].args[0])

    def test_non_meta_leads_and_switched_off_send_nothing(self):
        with mock.patch('requests.post', return_value=FakeResp()) as post:
            self._lead()                                         # walk-in: not a Meta lead
            self.cfg.capi_enabled = False
            self.cfg.save()
            meta_capi.clear_cache(self.co.id)
            self._lead(meta_form_id='77')
        self.assertFalse(post.called)
        self.assertFalse(MetaCapiEvent.objects.exists())

    def test_booking_sold_and_failures_are_kept(self):
        bad = mock.Mock(status_code=400, text='{"error":{"message":"Invalid token"}}')
        with mock.patch('requests.post', return_value=bad):
            lead = self._lead(meta_form_id='77')
        ev = MetaCapiEvent.objects.get(lead=lead, event_name='Lead_Received')
        self.assertEqual((ev.status, ev.attempts), ('failed', 1))
        self.assertEqual(meta_capi.summary(self.co.id)['recent'][0]['error'], 'Invalid token')
        with mock.patch('requests.post', return_value=FakeResp()):
            meta_capi.send(ev.id)                                # the retry command's path
            with self.captureOnCommitCallbacks(execute=True):
                Booking.objects.create(company=self.co, project=self.p, lead=lead, status='sold', client_name='Asha',
                                       accounts_status='approved')
        ev.refresh_from_db()
        self.assertEqual(ev.status, 'sent')
        self.assertTrue(MetaCapiEvent.objects.filter(lead=lead, event_name='Booked', status='sent').exists())


@override_settings(META_CAPI_INLINE=True)
class StmDirectQualifiedTests(MetaCapiTests):
    """Projects with no telecaller: the STM's warm / hot (or a booked visit) sends Qualified, once."""
    def test_stm_marks_warm(self):
        with mock.patch('requests.post', return_value=FakeResp()):
            lead = self._lead(meta_form_id='77')
            lead.stm_status = 'warm'
            with self.captureOnCommitCallbacks(execute=True):
                lead.save()
            lead.stm_status = 'sv_scheduled'
            with self.captureOnCommitCallbacks(execute=True):
                lead.save()
        names = list(MetaCapiEvent.objects.filter(lead=lead).order_by('id').values_list('event_name', flat=True))
        self.assertEqual(names, ['Lead_Received', 'Qualified', 'Site_Visit_Scheduled'])

    def test_visit_booked_directly(self):
        with mock.patch('requests.post', return_value=FakeResp()):
            lead = self._lead(meta_form_id='77')
            with self.captureOnCommitCallbacks(execute=True):
                SiteVisit.objects.create(lead=lead, project=self.p, scheduled_at=timezone.now())
        self.assertTrue(MetaCapiEvent.objects.filter(lead=lead, event_name='Qualified').exists())


@override_settings(META_CAPI_INLINE=True)
class BookedNeedsAccountsTests(MetaCapiTests):
    """Booked goes to Meta only when Sales AND Accounts have approved — not on Sales alone."""
    def test_booked_after_accounts(self):
        with mock.patch('requests.post', return_value=FakeResp()):
            lead = self._lead(meta_form_id='77')
            with self.captureOnCommitCallbacks(execute=True):
                b = Booking.objects.create(company=self.co, project=self.p, lead=lead, status='sold',
                                           accounts_status='pending', client_name='Asha')
            self.assertFalse(MetaCapiEvent.objects.filter(lead=lead, event_name='Booked').exists())
            b.accounts_status = 'approved'
            with self.captureOnCommitCallbacks(execute=True):
                b.save()
        self.assertTrue(MetaCapiEvent.objects.filter(lead=lead, event_name='Booked').exists())
