"""Shared multi-tenant permission helpers.

A *platform admin* (VRL super admin or Django staff) can see and manage data
across all companies. Every other user is confined to their own company.
"""

VRL_CODE = 'VRL'


def is_module_admin(user):
    """A departmental / module-scoped admin: role='Admin' restricted to a single
    module (e.g. a Sales Admin). Company-scoped — NOT a platform super-admin, even
    inside the VRL company."""
    return bool(
        user and getattr(user, 'is_authenticated', False)
        and not getattr(user, 'is_staff', False)
        and getattr(user, 'role', '') == 'Admin'
        and len(getattr(user, 'modules', None) or []) == 1
    )


def is_platform_admin(user):
    """True for Django staff/superusers and the VRL company Admin — EXCEPT a VRL
    Admin restricted to a single module (a departmental admin), who stays scoped to
    their own company."""
    if not (user and getattr(user, 'is_authenticated', False)):
        return False
    if getattr(user, 'is_staff', False):
        return True
    if is_module_admin(user):
        return False
    return bool(
        getattr(user, 'company', None) and
        getattr(user.company, 'code', '').upper() == VRL_CODE and
        getattr(user, 'role', '') == 'Admin'
    )


def scope_to_company(qs, user, field='company'):
    """Restrict a queryset to the user's company unless they are a platform admin.

    `field` is the lookup path from the model to its Company
    (e.g. 'company', 'lead__company', 'project__company', 'user__company').
    """
    if is_platform_admin(user):
        return qs
    return qs.filter(**{field: getattr(user, 'company', None)})
