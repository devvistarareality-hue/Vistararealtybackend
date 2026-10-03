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


BANK_MODULE = 'Bank Master'


def has_bank_master_access(user):
    """Bank Master is its own module, ticked per person in User Management (under
    Accounts & Finance). Opening it, reading a bank's statement and adding or editing
    banks need it. Company and platform admins always have it."""
    if not (user and user.is_authenticated):
        return False
    return bool(
        user.is_staff or is_platform_admin(user) or getattr(user, 'role', '') == 'Admin'
        or BANK_MODULE in (user.modules or [])
        or BANK_MODULE in (user.manager_modules or [])
        or BANK_MODULE in (user.admin_modules or [])
    )


def has_bank_list_access(user):
    """The bank list itself: Bank Master, and AR too — Record Payment (Loan) and
    cancellation refunds pick a bank and show its balance, so AR users need the list
    without being able to open Bank Master."""
    return has_bank_master_access(user) or has_ar_access(user)


def can_manage_banks(user):
    from accounts.capabilities import user_can
    return has_bank_master_access(user) and user_can(user, 'ar.bank.manage')
