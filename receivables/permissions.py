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


def ar_can(user, key):
    """Inside AR, what this person may do — their company's designation settings
    decide (accounts/capabilities.py). Module access is still the gate: without the
    AR module, nothing here applies."""
    from accounts.capabilities import user_can
    return has_ar_access(user) and user_can(user, key)


FINANCE_MODULE = 'Accounts & Finance'


def has_finance_access(user):
    """Bank Master belongs to the Accounts & Finance department — shared by AR now
    and Accounts Payable next — so the Accounts & Finance module opens it as well
    as AR does."""
    if has_ar_access(user):
        return True
    if not (user and user.is_authenticated):
        return False
    return bool(FINANCE_MODULE in (user.modules or []) or FINANCE_MODULE in (user.manager_modules or [])
                or FINANCE_MODULE in (user.admin_modules or []))


def can_manage_banks(user):
    from accounts.capabilities import user_can
    return has_finance_access(user) and user_can(user, 'ar.bank.manage')

