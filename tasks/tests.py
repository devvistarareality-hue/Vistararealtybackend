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
