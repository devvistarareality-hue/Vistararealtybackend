"""A project's standard unit types — EOI and LOI — are saved and read back."""
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Company
from sales.models import Project


class UnitTypesTests(TestCase):
    def test_loi_unit_types_round_trip(self):
        co = Company.objects.create(code='UTT', name='Utt')
        admin = User.objects.create(name='A', email='a@utt.com', phone='9400000001', user_code='UT-A',
                                    role='Admin', company=co)
        p = Project.objects.create(company=co, name='Kalrav 3')
        api = APIClient(); api.force_authenticate(admin)
        types = [{'type': '2 BHK', 'plot_area': '585', 'const_area': '167'}]
        r = api.patch(f'/api/sales/projects/{p.id}/', {'loi_unit_types': types, 'eoi_unit_types': types}, format='json')
        self.assertLess(r.status_code, 400, r.content)
        got = api.get(f'/api/sales/projects/{p.id}/').json()
        self.assertEqual(got['loi_unit_types'], types)
        self.assertEqual(got['eoi_unit_types'], types)
