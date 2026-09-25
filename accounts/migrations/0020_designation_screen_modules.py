from django.db import migrations, models


def seed_screen_modules(apps, schema_editor):
    """Record what each saved menu already speaks for.

    Until now a saved menu governed every module at once, so a designation
    configured in Accounts & Finance emptied its people's Sales and Land menus
    too. Writing down the modules it was actually configured for lets the others
    keep their defaults — which is the point of the field.
    """
    from accounts.capabilities import SCREEN_MODULE, modules_of
    Designation = apps.get_model('accounts', 'Designation')
    for d in Designation.objects.filter(screens_set=True):
        named = sorted({SCREEN_MODULE[k] for k in (d.screens or []) if k in SCREEN_MODULE}
                       | set(modules_of(d.module)))
        if named != (d.screens_modules or []):
            d.screens_modules = named
            d.save(update_fields=['screens_modules'])


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0019_channel_partner_module'),
    ]

    operations = [
        migrations.AddField(
            model_name='designation',
            name='screens_modules',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RunPython(seed_screen_modules, migrations.RunPython.noop),
    ]
