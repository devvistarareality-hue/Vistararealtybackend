"""Shared multi-tenant permission helpers.

A *platform admin* (VRL super admin or Django staff) can see and manage data
across all companies. Every other user is confined to their own company.
"""

VRL_CODE = 'VRL'


def is_platform_admin(user):
    """True for the VRL company Admin or any Django staff/superuser."""
    return bool(
        user and getattr(user, 'is_authenticated', False) and (
            user.is_staff or (
                getattr(user, 'company', None) and
                getattr(user.company, 'code', '').upper() == VRL_CODE and
                getattr(user, 'role', '') == 'Admin'
            )
        )
    )


def scope_to_company(qs, user, field='company'):
    """Restrict a queryset to the user's company unless they are a platform admin.

    `field` is the lookup path from the model to its Company
    (e.g. 'company', 'lead__company', 'project__company', 'user__company').
    """
    if is_platform_admin(user):
        return qs
    return qs.filter(**{field: getattr(user, 'company', None)})
