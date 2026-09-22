"""A booking's payment schedule must add up to its deal (within Rs 10).

Kalrav 2 plot 26 went through Rs 498 short and plot 77 Rs 22 over: the form checked
percentages to ±0.01% and the server never added the installments up."""
from decimal import Decimal as D

from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Company
from sales.models import Booking, Project
from sales.views import _schedule_error, _schedule_gap


def plot26(**kw):
    # Kalrav 2 plot 26 exactly as stored in production.
    d = {'final_amount': '18308734', 'installments': [
        {'no': 1, 'amt': 3006500, 'date': '2027-06-20'},
        {'no': 1, 'amt': 1672850, 'isNsd': True}, {'no': 2, 'amt': 2509275, 'isNsd': True},
        {'no': 3, 'amt': 2509275, 'isNsd': True}, {'no': 4, 'amt': 3531700, 'isNsd': True},
        {'no': 5, 'amt': 3531700, 'isNsd': True}, {'no': 6, 'amt': 896702, 'isNsd': True},
        {'no': 'Extra', 'amt': 650234, 'isExtra': True}]}
    d.update(kw)
    return d


class ScheduleGapTests(SimpleTestCase):
    def test_plot_26_is_498_short_and_refused(self):
        self.assertEqual(_schedule_gap(plot26()), D('-498'))
        self.assertIn('Rs. 498 short of the total deal', _schedule_error(plot26()))

    def test_a_correct_schedule_passes(self):
        d = plot26()
        d['installments'][6]['amt'] = 896702 + 498
        self.assertEqual(_schedule_gap(d), 0)
        self.assertIsNone(_schedule_error(d))

    def test_rupee_rounding_is_allowed_but_22_over_is_not(self):
        d = plot26(final_amount='18308236')             # schedule total, +2 rounding either way
        self.assertIsNone(_schedule_error({**d, 'final_amount': '18308234'}))
        self.assertIn('Rs. 22 more than', _schedule_error({**d, 'final_amount': '18308214'}))

    def test_eoi_token_schedule_is_not_checked(self):
        self.assertIsNone(_schedule_gap({'eoi': True, 'final_amount': '20931220', 'installments': [{'no': 1, 'amt': 100000}]}))
        self.assertIsNone(_schedule_gap({'plot_numbers': 'EOI-19', 'final_amount': '20931220', 'installments': [{'no': 1, 'amt': 100000}]}))

    def test_no_schedule_is_not_checked(self):
        self.assertIsNone(_schedule_gap({'final_amount': '2800000', 'installments': []}))                       # Pratishtha Regular
        self.assertIsNone(_schedule_gap({'final_amount': '765', 'installments': [{'no': 1, 'amt': 0, 'pct': 100}]}))
        # A small deal whose only amount is the Legal & Other row (Kalrav 2 plot 29) is not a schedule either.
        self.assertIsNone(_schedule_gap({'final_amount': '2240', 'installments': [{'no': 1, 'amt': 0, 'pct': 100},
                                                                                 {'no': 'Extra', 'amt': 1500, 'isExtra': True}]}))

    def test_extra_charges_count_when_there_is_no_extra_row(self):
        d = {'final_amount': '1100000', 'total_extra': '100000', 'installments': [{'no': 1, 'amt': 1000000}]}
        self.assertEqual(_schedule_gap(d), 0)

    def test_extra_work_installments_count(self):
        d = {'final_amount': '1200000', 'installments': [{'no': 1, 'amt': 1000000}, {'no': 'Extra', 'amt': 100000}],
             'extra_work_inst': [{'no': 1, 'amt': 100000}]}
        self.assertEqual(_schedule_gap(d), 0)


class ScheduleCheckOnSubmitTests(TestCase):
    def test_submit_is_refused_before_anything_is_saved(self):
        co = Company.objects.create(code='VIS', name='Vistara')
        project = Project.objects.create(company=co, name='Kalrav 2')
        u = User.objects.create_user('stm1@test.local', company=co, user_code='STM1', password='x', name='STM',
                                     role='Admin', modules=['Sales'])
        api = APIClient(); api.force_authenticate(u)
        r = api.post('/api/sales/bookings/', {'project': project.id, 'client_name': 'Vipinchandra Rana', 'phone': '9825141144',
                                              **plot26()}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('498 short', r.json()['detail'])
        self.assertEqual(Booking.objects.count(), 0)
