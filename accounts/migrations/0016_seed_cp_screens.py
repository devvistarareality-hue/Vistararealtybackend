from django.db import migrations

# The Channel Partner module used to ride on one menu key (sales.screen.cp): its
# own eight sub-pages were never switchable, so an admin who unticked everything
# still saw the whole module. They are separate keys now — which means a
# designation someone already configured has none of them ticked, and its menu
# would empty out on deploy. Give those rows the CP items their ticks already
# implied, so nothing changes until an admin decides otherwise.
CP_SCREENS = [
    'cp.screen.dashboard', 'cp.screen.leads', 'cp.screen.sitevisits',
    'cp.screen.followups', 'cp.screen.closures', 'cp.screen.booking',
    'cp.screen.approvals',
]
MANAGER_ONLY = 'cp.screen.myteam'


def seed(apps, schema_editor):
    Designation = apps.get_model('accounts', 'Designation')
    for row in Designation.objects.filter(screens_set=True):
        screens = set(row.screens or [])
        if 'sales.screen.cp' not in screens:
            continue
        screens |= set(CP_SCREENS)
        # My Team is a manager's page, and it followed the Sales one.
        if 'sales.screen.myteam' in screens:
            screens.add(MANAGER_ONLY)
        row.screens = sorted(screens)
        row.save(update_fields=['screens'])


def unseed(apps, schema_editor):
    Designation = apps.get_model('accounts', 'Designation')
    for row in Designation.objects.filter(screens_set=True):
        screens = [s for s in (row.screens or []) if not s.startswith('cp.screen.')]
        if len(screens) != len(row.screens or []):
            row.screens = screens
            row.save(update_fields=['screens'])


class Migration(migrations.Migration):

    dependencies = [('accounts', '0015_designation_screens')]

    operations = [migrations.RunPython(seed, unseed)]
