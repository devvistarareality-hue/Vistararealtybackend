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
