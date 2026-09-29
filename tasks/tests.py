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
