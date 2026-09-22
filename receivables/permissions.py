from accounts.permissions import is_platform_admin

AR_MODULE = 'AR'


def has_ar_access(user):
    """Granted from User Management: anyone with the AR module (plain, manager or
    admin level) can view accounts and record, edit or delete receipts. Company and
    platform admins always have it."""
    if not (user and user.is_authenticated):
        return False
    return bool(
        user.is_staff or is_platform_admin(user) or getattr(user, 'role', '') == 'Admin'
        or AR_MODULE in (user.modules or [])
        or AR_MODULE in (user.manager_modules or [])
        or AR_MODULE in (user.admin_modules or [])
    )
