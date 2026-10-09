from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.tests import auth
from tasks.models import Task, TaskList


class TaskFacetsTests(APITestCase):
    """?facets=1 lists only the values the viewer's tasks hold, so the list page's
    pickers never offer a choice that returns nothing."""

    def test_facets_only_list_what_is_there(self):
        co = Company.objects.create(code='TFA', name='Facets Co')
        other = Company.objects.create(code='TFB', name='Other Co')
        admin = User.objects.create(email='tf_admin@x.com', company=co, role='Admin', user_code='TF1')
        doer = User.objects.create(email='tf_doer@x.com', company=co, role='Employee', user_code='TF2')
        used = TaskList.objects.create(company=co, name='Used')
        TaskList.objects.create(company=co, name='Empty')
        t = Task.objects.create(company=co, task_list=used, title='One', status='todo', priority='high')
        t.assignees.set([doer])
        theirs = TaskList.objects.create(company=other, name='Theirs')
        Task.objects.create(company=other, task_list=theirs, title='Two', status='done', priority='low')

        auth(self.client, admin)
        res = self.client.get('/api/tasks/?facets=1')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {
            'list_ids': [str(used.id)], 'statuses': ['todo'],
            'priorities': ['high'], 'assignee_ids': [str(doer.id)],
        })


class TaskCreatedWithoutAListTests(APITestCase):
    """Creating a task no longer asks which list it belongs to.

    It was a dropdown with a single option on every company that uses this module,
    so choosing it was work that told nobody anything. Task.task_list is still NOT
    NULL, though, so the server has to answer the question the form stopped asking.
    """

    def setUp(self):
        self.co = Company.objects.create(code='TNL', name='No List Co')
        self.other = Company.objects.create(code='TNM', name='Other Co')
        self.admin = User.objects.create(email='tnl_admin@x.com', company=self.co,
                                         role='Admin', user_code='TN1')
        auth(self.client, self.admin)

    def _create(self, **extra):
        body = {'title': 'Fix the lift'}
        body.update(extra)
        return self.client.post('/api/tasks/', body, format='json')

    def test_a_task_lands_on_the_companys_existing_list(self):
        existing = TaskList.objects.create(company=self.co, name='Facility manager')
        r = self._create()
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Task.objects.get(pk=r.data['id']).task_list_id, existing.id)

    def test_the_oldest_open_list_wins_when_there_are_several(self):
        first = TaskList.objects.create(company=self.co, name='Facility manager')
        TaskList.objects.create(company=self.co, name='Later One')
        r = self._create()
        self.assertEqual(Task.objects.get(pk=r.data['id']).task_list_id, first.id)

    def test_an_archived_list_is_not_used(self):
        TaskList.objects.create(company=self.co, name='Retired', archived=True)
        r = self._create()
        self.assertEqual(r.status_code, 201, r.data)
        self.assertFalse(Task.objects.get(pk=r.data['id']).task_list.archived)

    def test_a_company_with_no_lists_gets_one(self):
        # Otherwise the first task a company ever creates is refused, which is a
        # strange way to meet a module.
        self.assertEqual(TaskList.objects.filter(company=self.co).count(), 0)
        r = self._create()
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(TaskList.objects.filter(company=self.co).count(), 1)
        self.assertEqual(Task.objects.get(pk=r.data['id']).task_list.name, 'Tasks')

    def test_a_list_named_explicitly_is_still_honoured(self):
        TaskList.objects.create(company=self.co, name='Default')
        chosen = TaskList.objects.create(company=self.co, name='Chosen')
        r = self._create(task_list=chosen.id)
        self.assertEqual(Task.objects.get(pk=r.data['id']).task_list_id, chosen.id)

    def test_another_companys_list_is_still_refused(self):
        # The picker going away does not make the id safe to trust.
        TaskList.objects.create(company=self.co, name='Ours')
        theirs = TaskList.objects.create(company=self.other, name='Theirs')
        r = self._create(task_list=theirs.id)
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(Task.objects.count(), 0)

    def test_a_title_is_still_required(self):
        TaskList.objects.create(company=self.co, name='Ours')
        self.assertEqual(self._create(title='   ').status_code, 400)


class TaskCodeTests(APITestCase):
    """Every task carries a reference — TSK-001 — so it can be quoted and found.

    Numbered per company, not platform-wide: the numbers stay small, and nobody
    can read another company's volume off their own.
    """

    def setUp(self):
        self.co = Company.objects.create(code='TCD', name='Code Co')
        self.other = Company.objects.create(code='TCE', name='Other Co')
        self.admin = User.objects.create(email='tcd_admin@x.com', company=self.co,
                                         role='Admin', user_code='TC1')
        TaskList.objects.create(company=self.co, name='Ours')
        auth(self.client, self.admin)

    def _create(self, title='Fix the lift'):
        r = self.client.post('/api/tasks/', {'title': title}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def test_a_new_task_is_given_a_reference(self):
        self.assertEqual(self._create()['code'], 'TSK-001')

    def test_references_run_in_sequence(self):
        self.assertEqual([self._create(f'Job {i}')['code'] for i in range(3)],
                         ['TSK-001', 'TSK-002', 'TSK-003'])

    def test_each_company_counts_from_one(self):
        self._create()
        TaskList.objects.create(company=self.other, name='Theirs')
        mate = User.objects.create(email='tce_admin@x.com', company=self.other,
                                   role='Admin', user_code='TC2')
        auth(self.client, mate)
        self.assertEqual(self._create('Their job')['code'], 'TSK-001')

    def test_a_deleted_task_does_not_hand_its_number_on(self):
        # Counting rows would reuse the number of anything removed, and two tasks
        # with one reference is worse than a gap in the sequence.
        first = self._create('One')
        self._create('Two')
        self.client.delete(f"/api/tasks/{first['id']}/")
        self.assertEqual(self._create('Three')['code'], 'TSK-003')

    # ------------------------------------------------------------- searching

    def test_a_task_is_found_by_its_reference(self):
        self._create('One')
        wanted = self._create('Needle')
        r = self.client.get(f"/api/tasks/?search={wanted['code']}")
        self.assertEqual([t['id'] for t in r.json()['results']], [wanted['id']])

    def test_the_padding_and_the_hash_are_optional(self):
        # Nobody types TSK-002 when they mean "number 2".
        self._create('One')
        wanted = self._create('Needle')
        for term in ('TSK-002', 'tsk-2', '002', '2', '#TSK-002'):
            r = self.client.get(f'/api/tasks/?search={term}')
            ids = [t['id'] for t in r.json()['results']]
            self.assertIn(wanted['id'], ids, f'{term!r} did not find it')

    def test_searching_by_title_still_works(self):
        self._create('Repair the roof')
        self._create('Something else')
        r = self.client.get('/api/tasks/?search=roof')
        self.assertEqual([t['title'] for t in r.json()['results']], ['Repair the roof'])

    def test_a_search_matching_nothing_returns_nothing(self):
        self._create('One')
        self.assertEqual(self.client.get('/api/tasks/?search=TSK-999').json()['results'], [])

    def test_the_reference_does_not_leak_across_companies(self):
        self._create('Ours')
        TaskList.objects.create(company=self.other, name='Theirs')
        mate = User.objects.create(email='tce2@x.com', company=self.other,
                                   role='Admin', user_code='TC3')
        auth(self.client, mate)
        theirs = self._create('Theirs')
        # Both are TSK-001; each company sees only its own.
        r = self.client.get('/api/tasks/?search=TSK-001')
        self.assertEqual([t['id'] for t in r.json()['results']], [theirs['id']])
