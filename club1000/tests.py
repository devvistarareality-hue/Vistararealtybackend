from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.tests import auth
from club1000.models import Lead


class Club1000FacetsTests(APITestCase):
    """?facets=1 lists only the values the viewer's leads / investors hold, so
    the pages' pickers never offer a choice that returns nothing."""

    def test_lead_facets_only_list_what_is_there(self):
        co = Company.objects.create(code='CFA', name='Facets Co')
        other = Company.objects.create(code='CFB', name='Other Co')
        admin = User.objects.create(email='cf_admin@x.com', company=co, role='Admin', user_code='CF1')
        Lead.objects.create(company=co, name='A', phone='+919000000111', source='walk_in', status='new', assigned_to=admin)
        Lead.objects.create(company=other, name='B', phone='+919000000112', source='other', status='lost')

        auth(self.client, admin)
        res = self.client.get('/api/club1000/leads/?facets=1')
        self.assertEqual(res.status_code, 200)
        f = res.json()
        self.assertEqual(f['statuses'], ['new'])
        self.assertEqual(f['sources'], ['walk_in'])
        self.assertEqual(f['assignee_ids'], [str(admin.id)])
        self.assertEqual(f['scheme_ids'], [])

        res = self.client.get('/api/club1000/investors/?facets=1')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {'scheme_ids': [], 'statuses': []})
