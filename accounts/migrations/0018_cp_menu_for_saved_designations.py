from django.db import migrations

# Channel Partner's tabs got their own menu keys after some designations had
# already been saved. Those rows hold "Channel Partner" in the Sales menu but
# none of the module's own tabs, which leaves a CP person with an empty sidebar.
# 0016 fixed the rows the seeding had touched; this catches the ones an admin
# saved from the permissions screen in between.
CP_KEYS = [
    'cp.screen.dashboard', 'cp.screen.leads', 'cp.screen.sitevisits',
    'cp.screen.followups', 'cp.screen.closures', 'cp.screen.booking',
    'cp.screen.approvals',
]
MANAGER_ONLY = 'cp.screen.myteam'


def fix(apps, schema_editor):
    Designation = apps.get_model('accounts', 'Designation')
    for row in Designation.objects.filter(screens_set=True):
        screens = set(row.screens or [])
        if not screens:
            continue                      # an empty menu is a real answer
        wants_cp = 'sales.screen.cp' in screens or (row.name or '').strip().lower().startswith('cp')
        if not wants_cp or any(k.startswith('cp.screen.') for k in screens):
            continue
        screens |= set(CP_KEYS)
        if 'sales.screen.myteam' in screens:
            screens.add(MANAGER_ONLY)
        row.screens = sorted(screens)
        row.save(update_fields=['screens'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [('accounts', '0017_roledashboard')]

    operations = [migrations.RunPython(fix, noop)]
