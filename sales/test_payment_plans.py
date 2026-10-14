"""Project payment plans: saved through the project endpoint, cleaned to a known shape."""
from rest_framework.test import APITestCase

from accounts.models import User
from companies.models import Company
from sales.models import Project
from sales.tests import auth


class PaymentPlansTests(APITestCase):
    def setUp(self):
        self.co = Company.objects.create(name='Plan Co', code='PLN')
        self.admin = User.objects.create(email='pa@x.com', company=self.co, role='Admin', user_code='PA1')
        self.project = Project.objects.create(company=self.co, name='Kalrav')
        auth(self.client, self.admin)

    def test_saves_and_cleans(self):
        body = {'payment_plans': {
            'direct': {
                'extra': [{'pct': '100', 'days': '15', 'stage': 'Within 15 days'}],
                'unit': [{'pct': 10, 'days': 0, 'stage': 'On booking'}, {'pct': 90, 'days': 90, 'stage': ' Plinth '}],
                'legal': {'days': '', 'stage': 'At sale deed'},
            },
            'eoi': {'unit': [{'pct': 150, 'days': -5, 'stage': 'x' * 300}], 'legal': {'days': 30}},
            'bogus': {'unit': [{'pct': 1}]},
        }}
        r = self.client.patch(f'/api/sales/projects/{self.project.id}/', body, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        saved = Project.objects.get(pk=self.project.pk).payment_plans
        self.assertEqual(set(saved), {'direct', 'eoi'})                      # unknown plan dropped
        self.assertEqual(saved['direct']['unit'][1], {'pct': 90.0, 'days': 90, 'stage': 'Plinth'})
        self.assertEqual(saved['direct']['extra'][0]['days'], 15)
        self.assertEqual(saved['direct']['legal'], {'days': None, 'stage': 'At sale deed'})  # blank stays blank
        eoi = saved['eoi']['unit'][0]
        self.assertEqual((eoi['pct'], eoi['days'], len(eoi['stage'])), (100, 0, 100))      # clamped
        self.assertEqual(saved['eoi']['legal'], {'days': 30, 'stage': ''})
        self.assertIn('payment_plans', r.json())

    def test_rejects_non_object(self):
        r = self.client.patch(f'/api/sales/projects/{self.project.id}/', {'payment_plans': [1, 2]}, format='json')
        self.assertEqual(r.status_code, 400)
