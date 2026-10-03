"""Follow-ups and site visits scheduled with a channel partner themselves.

The CP module already tracks these against a partner's *leads*. This covers the
partner directory's own: calling a partner to stay in touch, and driving one out
to a site — repeatedly, over the life of the relationship.

Three things are worth pinning down, because each of them was a real decision:

1. There is deliberately **no hot/warm/cold outcome**. A partner is a continuing
   relationship, not a lead being qualified, so the field simply does not exist
   and a client sending one must not have it quietly stored.
2. **Many per partner.** No uniqueness anywhere — the same partner can be called
   a dozen times and taken to the same project twice in a week.
3. These models are separate from `FollowUp`/`SiteVisit` on purpose. `FollowUp.lead`
   is NOT NULL and company scoping runs through `lead__company` in dozens of
   query sites; making it nullable to fit partners in would have dropped every
   partner row out of all of those scopes silently. So the scoping here runs
   through `channel_partner__company`, and that is what the isolation tests below
   are actually checking.
"""
from django.core.cache import cache
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User

from sales.models import ChannelPartner, PartnerFollowUp, PartnerSiteVisit, Project
from sales.tests import auth

FU = '/api/sales/partner-follow-ups/'
SV = '/api/sales/partner-site-visits/'


class PartnerActivityTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='PAC', name='Partner Activity Co')
        cls.other_co = Company.objects.create(code='PAX', name='Other Co')

        cls.admin = User.objects.create(email='pa_admin@x.com', company=cls.co,
                                        role='Admin', designation='Admin',
                                        user_code='P0', name='Admin Person')
        cls.cp_exec = User.objects.create(email='pa_exec@x.com', company=cls.co,
                                          role='Employee', designation='CP EXECUTIVE',
                                          user_code='P1', name='CP Exec')
        # Sales, with no CP designation — the module is not theirs.
        cls.stm = User.objects.create(email='pa_stm@x.com', company=cls.co,
                                      role='Employee', designation='STM',
                                      user_code='P2', name='Sales STM')
        cls.foreign_exec = User.objects.create(email='pa_foreign@x.com', company=cls.other_co,
                                               role='Employee', designation='CP EXECUTIVE',
                                               user_code='P3', name='Foreign Exec')

        cls.partner = ChannelPartner.objects.create(company=cls.co, name='Ravi Realty',
                                                    firm_name='Ravi Realty LLP',
                                                    contact_no='+919800111222')
        cls.partner2 = ChannelPartner.objects.create(company=cls.co, name='Second Partner',
                                                     contact_no='+919800111333')
        cls.foreign_partner = ChannelPartner.objects.create(
            company=cls.other_co, name='Outside Partner', contact_no='+919800999888')

        cls.project = Project.objects.create(company=cls.co, name='Greenfield Phase 1')
        cls.foreign_project = Project.objects.create(company=cls.other_co, name='Not Ours')

    def setUp(self):
        cache.clear()

    # ---------------------------------------------------------------- creating

    def test_a_follow_up_can_be_scheduled_with_a_partner(self):
        auth(self.client, self.cp_exec)
        r = self.client.post(FU, {
            'channel_partner': self.partner.id,
            'scheduled_at': '2026-11-02T10:30:00Z',
            'remarks': 'Check in on the Greenfield inventory he was asking about',
        }, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['partner_name'], 'Ravi Realty')
        self.assertEqual(r.data['partner_firm'], 'Ravi Realty LLP')
        self.assertEqual(r.data['status'], 'pending')
        # Unassigned means "mine" — the person scheduling it is the one calling.
        self.assertEqual(r.data['assigned_to'], self.cp_exec.id)
        self.assertEqual(r.data['created_by'], self.cp_exec.id)

    def test_remarks_survive_the_round_trip_through_encryption(self):
        # remarks is an EncryptedTextField; a column that cannot be filtered is
        # easy to break without noticing, because writes keep succeeding.
        auth(self.client, self.cp_exec)
        note = 'Wants 2 units held till Friday — do not promise pricing'
        r = self.client.post(FU, {'channel_partner': self.partner.id,
                                  'scheduled_at': '2026-11-03T09:00:00Z',
                                  'remarks': note}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(PartnerFollowUp.objects.get(pk=r.data['id']).remarks, note)

    def test_a_site_visit_names_a_project(self):
        auth(self.client, self.cp_exec)
        r = self.client.post(SV, {
            'channel_partner': self.partner.id,
            'project': self.project.id,
            'scheduled_at': '2026-11-05T07:00:00Z',
        }, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['project_name'], 'Greenfield Phase 1')
        self.assertEqual(r.data['status'], 'scheduled')
        self.assertEqual(r.data['host'], self.cp_exec.id)

    def test_a_partner_is_required(self):
        auth(self.client, self.cp_exec)
        r = self.client.post(FU, {'scheduled_at': '2026-11-02T10:30:00Z'}, format='json')
        self.assertEqual(r.status_code, 400)

    # ------------------------------------------------- many per partner, no status

    def test_one_partner_takes_many_follow_ups_and_many_visits(self):
        auth(self.client, self.cp_exec)
        for day in ('02', '09', '16', '23'):
            r = self.client.post(FU, {'channel_partner': self.partner.id,
                                      'scheduled_at': f'2026-11-{day}T10:00:00Z'}, format='json')
            self.assertEqual(r.status_code, 201, r.data)
        # Same partner, same project, twice in one week — nothing stands in the way.
        for day in ('05', '07'):
            r = self.client.post(SV, {'channel_partner': self.partner.id,
                                      'project': self.project.id,
                                      'scheduled_at': f'2026-11-{day}T07:00:00Z'}, format='json')
            self.assertEqual(r.status_code, 201, r.data)

        self.assertEqual(PartnerFollowUp.objects.filter(channel_partner=self.partner).count(), 4)
        self.assertEqual(PartnerSiteVisit.objects.filter(channel_partner=self.partner).count(), 2)

    def test_there_is_no_hot_warm_cold_outcome_on_a_partner_visit(self):
        # Deliberate: a partner is not a lead being qualified. If someone adds a
        # `visit_status`/`interest` field later, this is the test that should make
        # them justify it rather than inherit it from SiteVisit by habit.
        names = {f.name for f in PartnerSiteVisit._meta.get_fields()}
        for leadish in ('visit_status', 'interest', 'interest_level', 'temperature'):
            self.assertNotIn(leadish, names)

        auth(self.client, self.cp_exec)
        r = self.client.post(SV, {'channel_partner': self.partner.id,
                                  'project': self.project.id,
                                  'scheduled_at': '2026-11-05T07:00:00Z',
                                  'visit_status': 'hot'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertNotIn('visit_status', r.data)

    # ------------------------------------------------------------------ listing

    def test_the_list_filters_down_to_one_partner(self):
        auth(self.client, self.cp_exec)
        PartnerFollowUp.objects.create(channel_partner=self.partner, assigned_to=self.cp_exec,
                                       scheduled_at='2026-11-02T10:00:00Z')
        PartnerFollowUp.objects.create(channel_partner=self.partner2, assigned_to=self.cp_exec,
                                       scheduled_at='2026-11-03T10:00:00Z')

        both = self.client.get(FU)
        self.assertEqual(both.status_code, 200)
        self.assertEqual(len(both.data), 2)

        just_one = self.client.get(f'{FU}?channel_partner_id={self.partner.id}')
        self.assertEqual(len(just_one.data), 1)
        self.assertEqual(just_one.data[0]['partner_name'], 'Ravi Realty')

    def test_a_junk_partner_id_is_ignored_rather_than_crashing(self):
        # The partner page builds this URL from a route param; an empty or
        # non-numeric one must not 500 the whole screen.
        auth(self.client, self.cp_exec)
        PartnerFollowUp.objects.create(channel_partner=self.partner, assigned_to=self.cp_exec,
                                       scheduled_at='2026-11-02T10:00:00Z')
        for bad in ('', 'undefined', 'null'):
            r = self.client.get(f'{FU}?channel_partner_id={bad}')
            self.assertEqual(r.status_code, 200, bad)
            self.assertEqual(len(r.data), 1, bad)

    def test_visits_filter_by_project_and_status(self):
        auth(self.client, self.cp_exec)
        PartnerSiteVisit.objects.create(channel_partner=self.partner, project=self.project,
                                        scheduled_at='2026-11-05T07:00:00Z', status='scheduled')
        PartnerSiteVisit.objects.create(channel_partner=self.partner, project=self.project,
                                        scheduled_at='2026-11-06T07:00:00Z', status='completed')
        self.assertEqual(len(self.client.get(f'{SV}?project_id={self.project.id}').data), 2)
        self.assertEqual(len(self.client.get(f'{SV}?status=completed').data), 1)
        self.assertEqual(len(self.client.get(f'{SV}?project_id={self.foreign_project.id}').data), 0)

    # ---------------------------------------------------------------- completing

    def test_completing_a_follow_up_stamps_the_time(self):
        auth(self.client, self.cp_exec)
        fu = PartnerFollowUp.objects.create(channel_partner=self.partner,
                                            assigned_to=self.cp_exec,
                                            scheduled_at='2026-11-02T10:00:00Z')
        r = self.client.patch(f'{FU}{fu.id}/', {'status': 'completed',
                                                'outcome': 'Spoke, sending the price list'},
                              format='json')
        self.assertEqual(r.status_code, 200, r.data)
        fu.refresh_from_db()
        self.assertEqual(fu.status, 'completed')
        self.assertIsNotNone(fu.completed_at, 'completed with no time is the drift we are avoiding')

    def test_completing_a_visit_stamps_the_visit_time(self):
        auth(self.client, self.cp_exec)
        sv = PartnerSiteVisit.objects.create(channel_partner=self.partner, project=self.project,
                                             scheduled_at='2026-11-05T07:00:00Z')
        r = self.client.patch(f'{SV}{sv.id}/', {'status': 'completed'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        sv.refresh_from_db()
        self.assertIsNotNone(sv.visited_at)

    def test_one_can_be_removed(self):
        auth(self.client, self.cp_exec)
        fu = PartnerFollowUp.objects.create(channel_partner=self.partner,
                                            assigned_to=self.cp_exec,
                                            scheduled_at='2026-11-02T10:00:00Z')
        self.assertEqual(self.client.delete(f'{FU}{fu.id}/').status_code, 204)
        self.assertFalse(PartnerFollowUp.objects.filter(pk=fu.id).exists())

    # ------------------------------------------------------- company isolation

    def test_another_companys_rows_are_invisible(self):
        PartnerFollowUp.objects.create(channel_partner=self.foreign_partner,
                                       assigned_to=self.foreign_exec,
                                       scheduled_at='2026-11-02T10:00:00Z')
        PartnerSiteVisit.objects.create(channel_partner=self.foreign_partner,
                                        project=self.foreign_project,
                                        scheduled_at='2026-11-05T07:00:00Z')
        auth(self.client, self.cp_exec)
        self.assertEqual(self.client.get(FU).data, [])
        self.assertEqual(self.client.get(SV).data, [])

    def test_a_row_cannot_be_hung_off_another_companys_partner(self):
        auth(self.client, self.cp_exec)
        r = self.client.post(FU, {'channel_partner': self.foreign_partner.id,
                                  'scheduled_at': '2026-11-02T10:00:00Z'}, format='json')
        self.assertEqual(r.status_code, 400, r.data)
        self.assertFalse(PartnerFollowUp.objects.filter(channel_partner=self.foreign_partner).exists())

    def test_a_visit_cannot_point_at_another_companys_project(self):
        auth(self.client, self.cp_exec)
        r = self.client.post(SV, {'channel_partner': self.partner.id,
                                  'project': self.foreign_project.id,
                                  'scheduled_at': '2026-11-05T07:00:00Z'}, format='json')
        self.assertEqual(r.status_code, 403, r.data)
        self.assertFalse(PartnerSiteVisit.objects.filter(project=self.foreign_project,
                                                         channel_partner=self.partner).exists())

    def test_another_companys_row_cannot_be_patched_or_deleted(self):
        fu = PartnerFollowUp.objects.create(channel_partner=self.foreign_partner,
                                            assigned_to=self.foreign_exec,
                                            scheduled_at='2026-11-02T10:00:00Z')
        auth(self.client, self.cp_exec)
        self.assertEqual(self.client.patch(f'{FU}{fu.id}/', {'status': 'completed'},
                                           format='json').status_code, 404)
        self.assertEqual(self.client.delete(f'{FU}{fu.id}/').status_code, 404)
        self.assertTrue(PartnerFollowUp.objects.filter(pk=fu.id).exists())

    # --------------------------------------------------------------- module gate

    def test_sales_staff_without_a_cp_designation_are_kept_out(self):
        auth(self.client, self.stm)
        self.assertEqual(self.client.get(FU).status_code, 403)
        self.assertEqual(self.client.get(SV).status_code, 403)
        self.assertEqual(self.client.post(FU, {'channel_partner': self.partner.id,
                                               'scheduled_at': '2026-11-02T10:00:00Z'},
                                          format='json').status_code, 403)

    def test_an_admin_sees_the_whole_company(self):
        PartnerFollowUp.objects.create(channel_partner=self.partner, assigned_to=self.cp_exec,
                                       scheduled_at='2026-11-02T10:00:00Z')
        PartnerFollowUp.objects.create(channel_partner=self.partner2, assigned_to=self.cp_exec,
                                       scheduled_at='2026-11-03T10:00:00Z')
        auth(self.client, self.admin)
        self.assertEqual(len(self.client.get(FU).data), 2)

    def test_an_anonymous_caller_gets_nothing(self):
        self.client.credentials()
        self.assertIn(self.client.get(FU).status_code, (401, 403))

    # --------------------------------------------- the statuses the UI offers

    def test_every_status_the_ui_offers_is_accepted(self):
        """The two status lists genuinely differ, and the UI pickers mirror them.

        This exists because they were first written as one shared list with
        'cancelled' in both — which FOLLOWUP_STATUS does not have, so the
        follow-up Cancel button 400'd while the site-visit one worked. Anything
        a picker can send has to land, or a button is dead on arrival.
        """
        auth(self.client, self.cp_exec)

        # Mirrors FU_STATUS / DROP_STATUS.fu in the web and app components.
        for value in ('pending', 'completed', 'missed', 'rescheduled'):
            fu = PartnerFollowUp.objects.create(channel_partner=self.partner,
                                                assigned_to=self.cp_exec,
                                                scheduled_at='2026-11-02T10:00:00Z')
            r = self.client.patch(f'{FU}{fu.id}/', {'status': value}, format='json')
            self.assertEqual(r.status_code, 200, f'{value}: {r.data}')
            self.assertEqual(r.data['status'], value)

        # Mirrors SV_STATUS / DROP_STATUS.sv.
        for value in ('scheduled', 'completed', 'cancelled', 'no_show'):
            sv = PartnerSiteVisit.objects.create(channel_partner=self.partner,
                                                 project=self.project,
                                                 scheduled_at='2026-11-05T07:00:00Z')
            r = self.client.patch(f'{SV}{sv.id}/', {'status': value}, format='json')
            self.assertEqual(r.status_code, 200, f'{value}: {r.data}')
            self.assertEqual(r.data['status'], value)

    def test_a_status_outside_the_list_is_refused(self):
        auth(self.client, self.cp_exec)
        fu = PartnerFollowUp.objects.create(channel_partner=self.partner,
                                            assigned_to=self.cp_exec,
                                            scheduled_at='2026-11-02T10:00:00Z')
        # 'cancelled' is a site-visit status, not a follow-up one — the mix-up
        # this pair of tests is here to catch.
        self.assertEqual(self.client.patch(f'{FU}{fu.id}/', {'status': 'cancelled'},
                                           format='json').status_code, 400)

    # ------------------------------------------------------- directory counts

    def test_the_directory_counts_each_kind_without_multiplying_them(self):
        # Three joins in one annotate multiply each other's rows. Without
        # distinct=True a partner with 3 leads and 2 visits reports 6 of each,
        # which reads as plausible and is wrong — hence a test with all three
        # non-zero and mutually prime-ish counts, where any leak shows up.
        from sales.models import Lead, LeadSource
        src = LeadSource.objects.create(company=self.co, name='Channel Partner')
        for i in range(3):
            Lead.objects.create(company=self.co, name=f'Ref {i}', phone=f'+9198001100{i}0',
                                source=src, channel_partner=self.partner)
        for i in range(4):
            PartnerFollowUp.objects.create(channel_partner=self.partner, assigned_to=self.cp_exec,
                                           scheduled_at=f'2026-11-0{i + 1}T10:00:00Z')
        for i in range(2):
            PartnerSiteVisit.objects.create(channel_partner=self.partner, project=self.project,
                                            scheduled_at=f'2026-11-0{i + 5}T07:00:00Z')

        auth(self.client, self.cp_exec)
        r = self.client.get('/api/sales/channel-partners/')
        self.assertEqual(r.status_code, 200)
        row = next(c for c in r.data if c['id'] == self.partner.id)
        self.assertEqual(row['lead_count'], 3)
        self.assertEqual(row['follow_up_count'], 4)
        self.assertEqual(row['site_visit_count'], 2)

        # A partner with nothing against them reports zeroes, not nulls — the UI
        # reads these straight into a badge.
        bare = next(c for c in r.data if c['id'] == self.partner2.id)
        self.assertEqual((bare['lead_count'], bare['follow_up_count'], bare['site_visit_count']), (0, 0, 0))
