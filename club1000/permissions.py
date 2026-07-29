from accounts.permissions import is_platform_admin


def is_club1000_manager(user):
    """Manager-level Club 1000 access: platform admins, company Admins, or a
    user explicitly granted Club 1000 in their manager_modules or admin_modules."""
    return bool(
        user.is_staff or is_platform_admin(user) or user.role == 'Admin'
        or 'Club 1000' in (user.manager_modules or [])
        or 'Club 1000' in (user.admin_modules or [])
    )


def has_club1000_access(user):
    """Any Club 1000 access at all: manager-level, or plain module access."""
    return is_club1000_manager(user) or 'Club 1000' in (user.modules or [])


def scope_leads_to_role(qs, user):
    """Restrict a Club 1000 Lead queryset by org hierarchy: a user sees leads
    assigned to themselves or to anyone reporting to them, transitively.
    Managers (is_club1000_manager) see all company leads — mirrors
    sales.views.scope_leads_to_role, reusing its generic hierarchy walk."""
    if is_club1000_manager(user):
        return qs
    from sales.views import _visible_user_ids  # generic org-tree helper, no Sales coupling
    return qs.filter(assigned_to__in=_visible_user_ids(user))


def _scheme_approver_ids(user, company):
    """Scheme ids where `user` is a configured investor-approver — mirrors
    sales.views._approver_project_ids exactly, one level down (Scheme instead
    of Project)."""
    from .models import Scheme
    return [
        s.id for s in Scheme.objects.filter(company=company).only('id', 'investor_approvers')
        if user.id in (s.investor_approvers or [])
    ]
