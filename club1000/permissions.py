from accounts.permissions import is_platform_admin


def is_club1000_manager(user):
    """Manager-level Club 1000 access: platform admins, company Admins, or a
    user explicitly granted Club 1000 in their manager_modules or admin_modules."""
    return bool(
        user.is_staff or is_platform_admin(user) or user.role == 'Admin'
        or 'Club 1000' in (user.manager_modules or [])
        or 'Club 1000' in (user.admin_modules or [])
    )


def can_delete_investors(user):
    """Who may delete an investment: by ROLE — Manager, General Manager, Director, or a
    real admin. Manager-level Club 1000 access given to an Employee is not enough
    (the owner, 19 Oct 2026: "only role manager will have delete access"). Anyone may
    still delete their own drafts — that check is in the views."""
    return bool(_is_hard_admin(user) or user.role in ('Director', 'General Manager', 'Manager'))


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


def can_configure_scheme_approvers(user):
    """Who may pick a scheme's investor_approvers — narrower than
    is_club1000_manager (which just gets someone onto the Approvals page):
    only Directors and real admins decide who approves each scheme, same as
    Sales restricts its "Booking Approvers — by project" panel to isAdmin
    rather than every booking-approving manager. Enforced here, not merely by
    hiding the picker in the UI — otherwise any Club 1000 manager could set
    themselves (or anyone) as an approver straight through the API."""
    return bool(_is_hard_admin(user) or user.role == 'Director')


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


def is_level2_approver(user, scheme):
    """A real admin, or someone named in the scheme's level 2 approvers."""
    if _is_hard_admin(user):
        return True
    return bool(scheme) and user.id in (scheme.investor_approvers_l2 or [])


def approval_gate(request, investor, what):
    """Two-level approval. Returns (final, response): final=True lets the caller carry
    out the approval; otherwise `response` is what to send back — the level 1 approval
    recorded and passed to level 2, or a refusal."""
    from rest_framework.response import Response
    from rest_framework import status
    from django.utils import timezone
    scheme = investor.scheme
    l2 = (scheme.investor_approvers_l2 or []) if scheme else []
    user = request.user
    if not l2:
        if not can_approve_investor(user, investor.scheme_id, user.company):
            return False, Response({'detail': 'You are not a configured approver for this scheme.'}, status=status.HTTP_403_FORBIDDEN)
        return True, None
    if not investor.approval_step:
        if not can_approve_investor(user, investor.scheme_id, user.company):
            return False, Response({'detail': 'Level 1 approval is needed first.'}, status=status.HTTP_403_FORBIDDEN)
        investor.approval_step = {'by': user.id, 'by_name': user.name or '', 'at': timezone.now().isoformat()}
        investor.save(update_fields=['approval_step'])
        try:
            from notifications import notify
            from accounts.models import User
            for u in User.objects.filter(id__in=l2, company=investor.company, is_active=True):
                notify(u, 'investor_approval', 'Level 2 approval needed',
                       f'{what} — {investor.name} · {scheme.name}: approved at level 1 by {user.name}', {'investor_id': investor.id})
        except Exception:
            pass
        from activity.recorder import note
        note(request, f'Level 1 approved — {what} · {investor.name}{f" · LOI {investor.loi_no}" if investor.loi_no else ""}; waiting for level 2',
             action='approve-l1', target_type='investor', target_id=str(investor.pk), module='Club 1000')
        return False, Response({'level': 1, 'detail': 'Approved at level 1 — sent to the level 2 approvers.'})
    if not is_level2_approver(user, scheme):
        return False, Response({'detail': 'Waiting for a level 2 approver.'}, status=status.HTTP_403_FORBIDDEN)
    return True, None
