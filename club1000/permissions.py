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
    """Restrict a Club 1000 Lead queryset: a plain (non-manager) user sees only
    leads assigned to themselves — reporting-chain visibility does NOT apply
    here, unlike Sales. Managers (is_club1000_manager) see all company leads."""
    if is_club1000_manager(user):
        return qs
    return qs.filter(assigned_to=user)


def _scheme_approver_ids(user, company):
    """Scheme ids where `user` is a configured investor-approver — mirrors
    sales.views._approver_project_ids exactly, one level down (Scheme instead
    of Project)."""
    from .models import Scheme
    return [
        s.id for s in Scheme.objects.filter(company=company).only('id', 'investor_approvers')
        if user.id in (s.investor_approvers or [])
    ]


def _is_hard_admin(user):
    """A real company/platform administrator, as opposed to someone who merely
    reaches manager-level Club 1000 access via manager_modules/admin_modules —
    mirrors sales.views._is_hard_admin exactly. Only these bypass per-scheme
    approver scoping entirely."""
    return bool(user.is_staff or is_platform_admin(user) or user.role == 'Admin')


def can_approve_investor(user, scheme_id, company):
    """Whether `user` may approve/reject an investor under `scheme_id` — mirrors
    sales.views._can_approve_project exactly, one level down (Scheme instead of
    Project). Being a Club 1000 manager (is_club1000_manager — broadly granted
    module access) is NOT itself approval authority; only a real admin, or a
    manager specifically named in that scheme's investor_approvers list, may
    approve/reject. A scheme naming nobody is approvable only by a real admin,
    not by every manager — same rule sales' _can_approve_project uses for an
    unconfigured project's booking_approvers."""
    if _is_hard_admin(user):
        return True
    if not scheme_id:
        return False
    return scheme_id in _scheme_approver_ids(user, company)
