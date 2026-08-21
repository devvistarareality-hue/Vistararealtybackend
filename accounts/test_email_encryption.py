"""User.email is encrypted at rest without breaking login or user management."""
from django.contrib.auth import authenticate, get_user_model
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from rest_framework.test import APIRequestFactory

from companies.models import Company
from sales.fields import text_blind_index

User = get_user_model()


def raw(col, pk):
    with connection.cursor() as c:
        c.execute(f'SELECT {col} FROM accounts_user WHERE id = %s', [pk])
        return c.fetchone()[0]


class EmailEncryption(TestCase):
    def setUp(self):
        self.co = Company.objects.create(code='EM', name='Em Co')
        self.u = User.objects.create(name='Prince', email='Prince.Soni@Vistara.com',
                                     phone='9000000001', user_code='VRL001',
                                     role='Admin', company=self.co)
        self.u.set_password('secret123'); self.u.save()

    def test_email_is_ciphertext_at_rest(self):
        self.assertTrue(str(raw('email', self.u.id)).startswith('gAAAAA'))

    def test_it_reads_back_exactly_as_entered(self):
        self.assertEqual(User.objects.get(pk=self.u.pk).email, 'Prince.Soni@Vistara.com')

    def test_the_key_is_set_and_case_insensitive(self):
        self.assertEqual(self.u.email_key, text_blind_index('prince.soni@vistara.com'))
        self.assertEqual(text_blind_index(' PRINCE.SONI@VISTARA.COM '),
                         text_blind_index('prince.soni@vistara.com'))
        self.assertNotIn('vistara', self.u.email_key)

    def test_login_by_user_code_still_works(self):
        """Login resolves by user_code, which is untouched."""
        found = User.objects.get(company=self.co, user_code='VRL001', is_active=True)
        self.assertTrue(found.check_password('secret123'))

    def test_django_natural_key_lookup_still_resolves(self):
        """createsuperuser / admin / authenticate() go through this."""
        self.assertEqual(User.objects.get_by_natural_key('prince.soni@vistara.com').id, self.u.id)
        self.assertEqual(User.objects.get_by_natural_key('PRINCE.SONI@VISTARA.COM').id, self.u.id)
        with self.assertRaises(User.DoesNotExist):
            User.objects.get_by_natural_key('nobody@nowhere.com')

    def test_authenticate_still_works(self):
        self.assertIsNotNone(authenticate(username='prince.soni@vistara.com',
                                          password='secret123'))
        self.assertIsNone(authenticate(username='prince.soni@vistara.com', password='wrong'))

    def test_duplicate_email_is_still_refused(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                User.objects.create(name='Clash', email='prince.soni@vistara.com',
                                    phone='9000000002', user_code='VRL002',
                                    company=self.co)

    def test_changing_the_email_moves_the_key(self):
        self.u.email = 'new.address@vistara.com'
        self.u.save()
        fresh = User.objects.get(pk=self.u.pk)
        self.assertEqual(fresh.email, 'new.address@vistara.com')
        self.assertEqual(fresh.email_key, text_blind_index('new.address@vistara.com'))
        with self.assertRaises(User.DoesNotExist):
            User.objects.get_by_natural_key('prince.soni@vistara.com')

    def test_create_user_helper_still_works(self):
        u = User.objects.create_user(email='helper@vistara.com', password='x',
                                     user_code='VRL003', company=self.co)
        self.assertEqual(User.objects.get_by_natural_key('helper@vistara.com').id, u.id)

    def test_backfill_encrypts_a_legacy_row_and_fills_the_key(self):
        with connection.cursor() as c:
            c.execute('UPDATE accounts_user SET email=%s, email_key=NULL WHERE id=%s',
                      ['legacy@vistara.com', self.u.id])
        self.assertEqual(raw('email', self.u.id), 'legacy@vistara.com')
        call_command('encrypt_existing_pii', verbosity=0)
        self.assertTrue(str(raw('email', self.u.id)).startswith('gAAAAA'))
        fresh = User.objects.get(pk=self.u.pk)
        self.assertEqual(fresh.email, 'legacy@vistara.com')
        self.assertEqual(fresh.email_key, text_blind_index('legacy@vistara.com'))
        self.assertEqual(User.objects.get_by_natural_key('legacy@vistara.com').id, self.u.id)
