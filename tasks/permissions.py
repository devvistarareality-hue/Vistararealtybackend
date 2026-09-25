"""Task Allocation is company-wide, not tree-scoped: every active user in a
company can see every list/task in that company and assign work to any other
active user in the same company. No `_visible_user_ids`/manager-tree
restriction applies here — that is a deliberate difference from Sales."""
from accounts.permissions import is_platform_admin, scope_to_company


def tasks_qs(request, qs):
    """Company-scope a Task/TaskList/etc. queryset, honouring a platform
    admin's `?company_id=` override the same way AR/Sales do."""
    qs = scope_to_company(qs, request.user)
    cid = request.query_params.get('company_id')
    if cid and is_platform_admin(request.user):
        qs = qs.filter(company_id=cid)
    return qs


def request_company(request):
    """The company new records should be created under."""
    company = getattr(request.user, 'company', None)
    cid = request.data.get('company_id') if hasattr(request, 'data') else None
    if cid and is_platform_admin(request.user):
        from companies.models import Company
        company = Company.objects.filter(pk=cid).first() or company
    return company
