"""Task Allocation API.

Visibility and assignment are company-wide by design — see models.py and
permissions.py. Every view below scopes by company only, never by
reporting-tree, and the assignee picker (`TaskAssigneesView`) lists every
active user in the company with no module/role gate.
"""
from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.permissions import is_platform_admin
from notifications import notify, notify_many

from .models import Task, TaskChecklistItem, TaskComment, TaskList, TaskTag
from .permissions import request_company, tasks_qs


def _log(request, summary, action=None, target_id=None):
    try:
        from activity.recorder import note
        note(request, summary, action=action, target_type='task', target_id=target_id, module='Task Allocation')
    except Exception:
        pass


def _task_list_qs(request):
    return tasks_qs(request, TaskList.objects.all())


def _task_qs(request):
    return tasks_qs(request, Task.objects.all()).select_related('task_list', 'created_by') \
        .prefetch_related('assignees', 'tags', 'checklist_items')


def serialize_task_list(tl):
    return {
        'id': tl.id, 'name': tl.name, 'description': tl.description or '',
        'color': tl.color or '', 'archived': tl.archived,
        'created_by': tl.created_by.name if tl.created_by_id else '',
        'created_at': tl.created_at.isoformat() if tl.created_at else None,
    }


def serialize_task(t, detail=False):
    items = list(t.checklist_items.all())
    out = {
        'id': t.id, 'title': t.title, 'status': t.status, 'priority': t.priority,
        'task_list': t.task_list_id, 'task_list_name': t.task_list.name if t.task_list_id else '',
        'due_date': t.due_date.isoformat() if t.due_date else None,
        'start_date': t.start_date.isoformat() if t.start_date else None,
        'position': t.position, 'archived': t.archived,
        'assignees': [{'id': u.id, 'name': u.name} for u in t.assignees.all()],
        'tags': [{'id': g.id, 'name': g.name, 'color': g.color} for g in t.tags.all()],
        'created_by': {'id': t.created_by_id, 'name': t.created_by.name} if t.created_by_id else None,
        'created_at': t.created_at.isoformat() if t.created_at else None,
        'updated_at': t.updated_at.isoformat() if t.updated_at else None,
        'completed_at': t.completed_at.isoformat() if t.completed_at else None,
        'checklist_total': len(items), 'checklist_done': sum(1 for i in items if i.is_done),
    }
    if detail:
        out['description'] = t.description or ''
        out['checklist_items'] = [{'id': i.id, 'text': i.text, 'is_done': i.is_done, 'order': i.order} for i in items]
    return out


def serialize_comment(c):
    return {
        'id': c.id, 'body': c.body,
        'author': {'id': c.author_id, 'name': c.author.name} if c.author_id else None,
        'created_at': c.created_at.isoformat() if c.created_at else None,
    }


class TaskListsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = _task_list_qs(request)
        if request.query_params.get('include_archived') != 'true':
            qs = qs.filter(archived=False)
        return Response({'results': [serialize_task_list(tl) for tl in qs]})

    def post(self, request):
        name = (request.data.get('name') or '').strip()
        if not name:
            return Response({'detail': 'Name is required.'}, status=status.HTTP_400_BAD_REQUEST)
        company = request_company(request)
        if not company:
            return Response({'detail': 'No company on this account.'}, status=status.HTTP_400_BAD_REQUEST)
        tl = TaskList.objects.create(
            company=company, name=name, description=(request.data.get('description') or '').strip(),
            color=(request.data.get('color') or '').strip(), created_by=request.user,
        )
        _log(request, f'Created task list "{tl.name}"', action='created', target_id=tl.id)
        return Response(serialize_task_list(tl), status=status.HTTP_201_CREATED)


class TaskListDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get(self, request, pk):
        return _task_list_qs(request).filter(pk=pk).first()

    def patch(self, request, pk):
        tl = self._get(request, pk)
        if not tl:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        d = request.data
        if 'name' in d:
            name = (d.get('name') or '').strip()
            if not name:
                return Response({'detail': 'Name cannot be blank.'}, status=status.HTTP_400_BAD_REQUEST)
            tl.name = name
        if 'description' in d:
            tl.description = (d.get('description') or '').strip()
        if 'color' in d:
            tl.color = (d.get('color') or '').strip()
        if 'archived' in d:
            tl.archived = bool(d.get('archived'))
        tl.save()
        _log(request, f'Updated task list "{tl.name}"', action='updated', target_id=tl.id)
        return Response(serialize_task_list(tl))

    def delete(self, request, pk):
        tl = self._get(request, pk)
        if not tl:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if tl.tasks.exists():
            return Response({'detail': 'Archive this list instead — it still has tasks.'}, status=status.HTTP_400_BAD_REQUEST)
        name = tl.name
        tl.delete()
        _log(request, f'Deleted task list "{name}"', action='deleted')
        return Response(status=status.HTTP_204_NO_CONTENT)


class TasksView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = _task_qs(request)
        p = request.query_params
        if p.get('include_archived') != 'true':
            qs = qs.filter(archived=False)
        if p.get('task_list_id'):
            qs = qs.filter(task_list_id=p['task_list_id'])
        if p.get('status'):
            qs = qs.filter(status=p['status'])
        if p.get('priority'):
            qs = qs.filter(priority=p['priority'])
        if p.get('assignee_id'):
            qs = qs.filter(assignees__id=p['assignee_id'])
        if p.get('my_tasks') == 'true':
            qs = qs.filter(assignees=request.user)
        if p.get('assigned_by_me') == 'true':
            qs = qs.filter(created_by=request.user)
        if p.get('overdue') == 'true':
            qs = qs.filter(due_date__lt=timezone.localdate()).exclude(status='done')
        search = (p.get('search') or '').strip()
        if search:
            qs = qs.filter(title__icontains=search)
        ordering = p.get('ordering') or 'position'
        allowed_order = {'position', '-position', 'due_date', '-due_date', 'created_at', '-created_at', 'priority', '-priority'}
        if ordering not in allowed_order:
            ordering = 'position'
        qs = qs.order_by(ordering, '-created_at') if ordering == 'position' else qs.order_by(ordering)
        qs = qs.distinct()[:500]
        return Response({'results': [serialize_task(t) for t in qs]})

    def post(self, request):
        d = request.data
        title = (d.get('title') or '').strip()
        if not title:
            return Response({'detail': 'Title is required.'}, status=status.HTTP_400_BAD_REQUEST)
        company = request_company(request)
        if not company:
            return Response({'detail': 'No company on this account.'}, status=status.HTTP_400_BAD_REQUEST)
        task_list = TaskList.objects.filter(pk=d.get('task_list'), company=company).first()
        if not task_list:
            return Response({'detail': 'Invalid task list.'}, status=status.HTTP_400_BAD_REQUEST)
        status_val = d.get('status') or 'todo'
        if status_val not in dict(Task.STATUS):
            status_val = 'todo'
        priority_val = d.get('priority') or 'normal'
        if priority_val not in dict(Task.PRIORITY):
            priority_val = 'normal'
        task = Task.objects.create(
            company=company, task_list=task_list, title=title,
            description=(d.get('description') or '').strip(),
            status=status_val, priority=priority_val,
            due_date=d.get('due_date') or None, start_date=d.get('start_date') or None,
            created_by=request.user,
        )
        assignee_ids = [int(x) for x in (d.get('assignee_ids') or []) if str(x).isdigit()]
        if assignee_ids:
            valid = list(User.objects.filter(id__in=assignee_ids, company=company, is_active=True))
            task.assignees.set(valid)
            for u in valid:
                if u.id != request.user.id:
                    notify(u, 'task_assigned', 'New Task', task.title, {'task_id': task.id})
        tag_ids = [int(x) for x in (d.get('tag_ids') or []) if str(x).isdigit()]
        if tag_ids:
            task.tags.set(TaskTag.objects.filter(id__in=tag_ids, company=company))
        _log(request, f'Created task "{task.title}"', action='created', target_id=task.id)
        task = _task_qs(request).get(pk=task.pk)
        return Response(serialize_task(task, detail=True), status=status.HTTP_201_CREATED)


class TaskDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get(self, request, pk):
        return _task_qs(request).filter(pk=pk).first()

    def get(self, request, pk):
        task = self._get(request, pk)
        if not task:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(serialize_task(task, detail=True))

    def patch(self, request, pk):
        task = self._get(request, pk)
        if not task:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        d = request.data
        company = task.company
        if 'title' in d:
            title = (d.get('title') or '').strip()
            if not title:
                return Response({'detail': 'Title cannot be blank.'}, status=status.HTTP_400_BAD_REQUEST)
            task.title = title
        if 'description' in d:
            task.description = (d.get('description') or '').strip()
        old_status = task.status
        if 'status' in d and d['status'] in dict(Task.STATUS):
            task.status = d['status']
            if task.status == 'done' and old_status != 'done':
                task.completed_at = timezone.now()
            elif task.status != 'done':
                task.completed_at = None
        if 'priority' in d and d['priority'] in dict(Task.PRIORITY):
            task.priority = d['priority']
        if 'due_date' in d:
            task.due_date = d.get('due_date') or None
        if 'start_date' in d:
            task.start_date = d.get('start_date') or None
        if 'task_list' in d:
            new_list = TaskList.objects.filter(pk=d['task_list'], company=company).first()
            if new_list:
                task.task_list = new_list
        if 'position' in d:
            try:
                task.position = int(d['position'])
            except (TypeError, ValueError):
                pass
        if 'archived' in d:
            task.archived = bool(d.get('archived'))
        task.save()

        if old_status != task.status:
            _log(request, 'Status of "%s": %s → %s' % (
                task.title, dict(Task.STATUS).get(old_status, old_status), dict(Task.STATUS).get(task.status, task.status)),
                action='status_change', target_id=task.id)

        if 'assignee_ids' in d:
            new_ids = set(int(x) for x in (d.get('assignee_ids') or []) if str(x).isdigit())
            old_ids = set(task.assignees.values_list('id', flat=True))
            valid = list(User.objects.filter(id__in=new_ids, company=company, is_active=True))
            task.assignees.set(valid)
            added = [u for u in valid if u.id not in old_ids]
            for u in added:
                if u.id != request.user.id:
                    notify(u, 'task_assigned', 'New Task', task.title, {'task_id': task.id})
            if valid:
                names = ', '.join(u.name for u in valid)
                _log(request, f'Assigned "{task.title}" to {names}', action='assigned', target_id=task.id)

        if 'tag_ids' in d:
            tag_ids = [int(x) for x in (d.get('tag_ids') or []) if str(x).isdigit()]
            task.tags.set(TaskTag.objects.filter(id__in=tag_ids, company=company))

        task = _task_qs(request).get(pk=task.pk)
        return Response(serialize_task(task, detail=True))

    def delete(self, request, pk):
        task = self._get(request, pk)
        if not task:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        title = task.title
        task.delete()
        _log(request, f'Deleted task "{title}"', action='deleted')
        return Response(status=status.HTTP_204_NO_CONTENT)


class TaskChecklistView(APIView):
    permission_classes = [IsAuthenticated]

    def _task(self, request, pk):
        return _task_qs(request).filter(pk=pk).first()

    def get(self, request, pk):
        task = self._task(request, pk)
        if not task:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        items = task.checklist_items.all()
        return Response({'results': [{'id': i.id, 'text': i.text, 'is_done': i.is_done, 'order': i.order} for i in items]})

    def post(self, request, pk):
        task = self._task(request, pk)
        if not task:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        text = (request.data.get('text') or '').strip()
        if not text:
            return Response({'detail': 'Text is required.'}, status=status.HTTP_400_BAD_REQUEST)
        order = task.checklist_items.count()
        item = TaskChecklistItem.objects.create(task=task, text=text, order=order)
        return Response({'id': item.id, 'text': item.text, 'is_done': item.is_done, 'order': item.order}, status=status.HTTP_201_CREATED)


class TaskChecklistItemView(APIView):
    permission_classes = [IsAuthenticated]

    def _item(self, request, iid):
        return TaskChecklistItem.objects.filter(pk=iid, task__in=_task_qs(request)).select_related('task').first()

    def patch(self, request, iid):
        item = self._item(request, iid)
        if not item:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        d = request.data
        if 'text' in d:
            text = (d.get('text') or '').strip()
            if not text:
                return Response({'detail': 'Text cannot be blank.'}, status=status.HTTP_400_BAD_REQUEST)
            item.text = text
        if 'is_done' in d:
            item.is_done = bool(d.get('is_done'))
        if 'order' in d:
            try:
                item.order = int(d['order'])
            except (TypeError, ValueError):
                pass
        item.save()
        return Response({'id': item.id, 'text': item.text, 'is_done': item.is_done, 'order': item.order})

    def delete(self, request, iid):
        item = self._item(request, iid)
        if not item:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        item.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TaskCommentsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        task = _task_qs(request).filter(pk=pk).first()
        if not task:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        comments = task.comments.select_related('author').all()
        return Response({'results': [serialize_comment(c) for c in comments]})

    def post(self, request, pk):
        task = _task_qs(request).filter(pk=pk).first()
        if not task:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        body = (request.data.get('body') or '').strip()
        if not body:
            return Response({'detail': 'Comment cannot be blank.'}, status=status.HTTP_400_BAD_REQUEST)
        comment = TaskComment.objects.create(task=task, author=request.user, body=body)
        recipients = set(task.assignees.exclude(id=request.user.id))
        if task.created_by_id and task.created_by_id != request.user.id:
            recipients.add(task.created_by)
        if recipients:
            notify_many(list(recipients), 'task_comment', f'New comment on "{task.title}"',
                        f'{request.user.name}: {body[:120]}', {'task_id': task.id})
        return Response(serialize_comment(comment), status=status.HTTP_201_CREATED)


class TaskTagsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = tasks_qs(request, TaskTag.objects.all())
        return Response({'results': [{'id': t.id, 'name': t.name, 'color': t.color} for t in qs]})

    def post(self, request):
        name = (request.data.get('name') or '').strip()
        if not name:
            return Response({'detail': 'Name is required.'}, status=status.HTTP_400_BAD_REQUEST)
        company = request_company(request)
        if not company:
            return Response({'detail': 'No company on this account.'}, status=status.HTTP_400_BAD_REQUEST)
        tag, _created = TaskTag.objects.get_or_create(
            company=company, name=name, defaults={'color': (request.data.get('color') or '').strip()},
        )
        return Response({'id': tag.id, 'name': tag.name, 'color': tag.color}, status=status.HTTP_201_CREATED)


class TaskAssigneesView(APIView):
    """Company-wide, on purpose — anyone active in the company can be assigned a
    task by anyone else. No module/role/reporting-tree filter, unlike AR's
    module-gated assignee list."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = getattr(request.user, 'company', None)
        cid = request.query_params.get('company_id')
        if cid and is_platform_admin(request.user):
            from companies.models import Company
            company = Company.objects.filter(pk=cid).first()
        if not company:
            return Response({'results': [{'id': request.user.id, 'name': request.user.name}]})
        users = User.objects.filter(company=company, is_active=True).order_by('name')
        return Response({'results': [{'id': u.id, 'name': u.name} for u in users]})


class TaskStatsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = tasks_qs(request, Task.objects.all()).filter(archived=False)
        today = timezone.localdate()
        my_open = qs.filter(assignees=request.user).exclude(status='done').distinct().count()
        overdue = qs.filter(due_date__lt=today).exclude(status='done').distinct().count()
        due_today = qs.filter(due_date=today).exclude(status='done').distinct().count()
        week_ago = today - timedelta(days=7)
        completed_week = qs.filter(status='done', completed_at__date__gte=week_ago).distinct().count()
        by_status = {key: qs.filter(status=key).distinct().count() for key, _ in Task.STATUS}
        by_priority = {key: qs.filter(priority=key).distinct().count() for key, _ in Task.PRIORITY}
        assigned_by_me = qs.filter(created_by=request.user).distinct().count()
        return Response({
            'my_open_tasks': my_open, 'overdue': overdue, 'due_today': due_today,
            'completed_this_week': completed_week, 'assigned_by_me': assigned_by_me,
            'by_status': by_status, 'by_priority': by_priority,
            'total_open': qs.exclude(status='done').distinct().count(),
        })
