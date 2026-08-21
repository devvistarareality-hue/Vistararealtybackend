"""A booking must name a unit when the project has units mapped."""
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.models import User
from companies.models import Company
from sales.models import Booking, Plot, Project
from sales.views import BookingListCreateView


class BookingRequiresUnit(TestCase):
    def setUp(self):
        self.co = Company.objects.create(code='BU', name='BU Co')
        self.u = User.objects.create(name='S', email='s@x.com', phone='9000000001',
                                     user_code='S1', role='Admin', company=self.co)
        self.mapped = Project.objects.create(company=self.co, name='Mapped')
        self.plot = Plot.objects.create(project=self.mapped, number='A-1')
        self.unmapped = Project.objects.create(company=self.co, name='By Area',
                                               formula_set='industrial')

    def _post(self, payload):
        req = APIRequestFactory().post('/x/', payload, format='json')
        force_authenticate(req, user=self.u)
        return BookingListCreateView.as_view()(req)

    def test_rejected_without_a_unit_when_the_project_has_units(self):
        res = self._post({'project': self.mapped.id, 'client_name': 'C', 'phone': '9000000000'})
        self.assertEqual(res.status_code, 400)
        self.assertIn('Select a unit', res.data['detail'])
        self.assertEqual(Booking.objects.count(), 0)

    def test_accepted_with_a_unit(self):
        res = self._post({'project': self.mapped.id, 'plot': self.plot.id,
                          'client_name': 'C', 'phone': '9000000000'})
        self.assertIn(res.status_code, (200, 201), res.data)

    def test_area_only_project_is_still_allowed(self):
        """Land sold by area has no unit list — refusing would stop those sales."""
        res = self._post({'project': self.unmapped.id, 'client_name': 'C',
                          'phone': '9000000000', 'area': '80000'})
        self.assertIn(res.status_code, (200, 201), res.data)

    def test_eoi_is_exempt(self):
        """An EOI is raised before a unit is chosen."""
        res = self._post({'project': self.mapped.id, 'eoi': True,
                          'client_name': 'C', 'phone': '9000000000'})
        self.assertIn(res.status_code, (200, 201), res.data)

    def test_a_revision_must_also_name_a_unit(self):
        """This is the case that produced 'Unit 80000' — a revision with no plot."""
        res = self._post({'project': self.mapped.id, 'revision_of': 1,
                          'client_name': 'C', 'phone': '9000000000', 'area': '80000'})
        self.assertEqual(res.status_code, 400)
