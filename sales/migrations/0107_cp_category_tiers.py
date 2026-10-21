"""Channel partner Category becomes Platinum / Gold / Diamond.

The old values (Premium / Normal / Referral) are cleared rather than mapped onto
a new tier. 'normal' was the field's default, so the 697 partners holding it were
not graded as anything — they were simply added — and turning that into "Gold"
would put a grading on record that nobody decided. The 13 'premium' ones are
cleared with them so the tier means one thing: somebody chose it. Partners are
regraded by hand from here.

Not reversible in any honest sense: the old values are gone once this runs, so
the reverse leaves the column as it is rather than inventing them back.
"""
from django.db import migrations, models

LEGACY = ('premium', 'normal', 'referral')


def clear_legacy_tiers(apps, schema_editor):
    ChannelPartner = apps.get_model('sales', 'ChannelPartner')
    # .update() on purpose: this touches one column on every row, and going
    # through model saves would fire the encrypted-field machinery (and the
    # activity log) 710 times for a change no one person made.
    ChannelPartner.objects.filter(category__in=LEGACY).update(category='')


def noop(apps, schema_editor):
    """Reverse: nothing to put back — see the module docstring."""


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0106_channelpartner_cp_type'),
    ]

    operations = [
        migrations.RunPython(clear_legacy_tiers, noop),
        migrations.AlterField(
            model_name='channelpartner',
            name='category',
            field=models.CharField(
                blank=True, max_length=10,
                choices=[('platinum', 'Platinum'), ('gold', 'Gold'), ('diamond', 'Diamond')],
            ),
        ),
    ]
