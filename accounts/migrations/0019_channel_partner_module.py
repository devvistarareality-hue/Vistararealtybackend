from django.db import migrations

# Channel Partner used to live inside Sales: you reached it from the Sales menu,
# and a CP designation was a Sales designation. It is a module of its own now,
# like AR and Club 1000 — granted in User Management, with its own designations,
# menu and dashboards. This moves the existing data across so the people who
# work in it keep working on the day it ships.
CP = 'Channel Partner'
CP_SCREENS = ['cp.screen.dashboard', 'cp.screen.leads', 'cp.screen.sitevisits',
              'cp.screen.followups', 'cp.screen.closures', 'cp.screen.booking',
              'cp.screen.approvals', 'cp.screen.myteam']


def is_cp_title(name):
    t = (name or '').strip().lower()
    return t.startswith('cp') or 'channel partner' in t


def forwards(apps, schema_editor):
    Designation = apps.get_model('accounts', 'Designation')
    User = apps.get_model('accounts', 'User')

    # 1. A CP designation belongs to the Channel Partner module now.
    for row in Designation.objects.filter(module='Sales'):
        if not is_cp_title(row.name):
            continue
        row.module = CP
        if row.screens_set:
            # Its menu is the module's own tabs; the Sales ones no longer apply.
            keys = {k for k in (row.screens or []) if k.startswith('cp.screen.')}
            row.screens = sorted(keys or CP_SCREENS)
        row.save(update_fields=['module', 'screens'])

    # 2. Whoever works in it moves across: Channel Partner replaces Sales for
    #    them, the way it does in User Management. They worked the partner desk,
    #    not the Sales floor, so carrying both would hand them a module they were
    #    never meant to have.
    for user in User.objects.all():
        if not is_cp_title(user.designation):
            continue
        changed = []
        for field in ('modules', 'manager_modules', 'admin_modules'):
            mods = list(getattr(user, field, None) or [])
            if 'Sales' not in mods and CP not in mods:
                continue
            mods = [m for m in mods if m != 'Sales']
            if CP not in mods:
                mods.append(CP)
            setattr(user, field, mods)
            changed.append(field)
        # Someone with a CP title and no modules at all still gets the module.
        if not changed:
            user.modules = list(user.modules or []) + [CP]
            changed = ['modules']
        user.save(update_fields=changed)

    # 3. Nobody's Sales menu offers Channel Partner any more.
    for row in Designation.objects.filter(screens_set=True):
        keys = [k for k in (row.screens or []) if k != 'sales.screen.cp']
        if len(keys) != len(row.screens or []):
            row.screens = keys
            row.save(update_fields=['screens'])


def backwards(apps, schema_editor):
    Designation = apps.get_model('accounts', 'Designation')
    Designation.objects.filter(module=CP).update(module='Sales')


class Migration(migrations.Migration):

    dependencies = [('accounts', '0018_cp_menu_for_saved_designations')]

    operations = [migrations.RunPython(forwards, backwards)]
