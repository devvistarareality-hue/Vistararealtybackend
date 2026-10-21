"""Adds Channel Partner Stage: New / Hot / Warm / Cold.

New is the default from here on, so a partner added today starts as New without
anybody choosing it. The partners already on the directory must NOT be swept
into New — they are not new arrivals, they simply predate the field — so this
backfills them with blank and leaves the model's real default ('new') to apply
only to rows created afterwards.

That is what `preserve_default=False` does: the '' below fills the existing
rows, then the field's own default takes over.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0107_cp_category_tiers'),
    ]

    operations = [
        migrations.AddField(
            model_name='channelpartner',
            name='stage',
            field=models.CharField(
                default='', blank=True, max_length=10, verbose_name='Stage',
                choices=[('new', 'New'), ('hot', 'Hot'), ('warm', 'Warm'), ('cold', 'Cold')],
            ),
            preserve_default=False,
        ),
        # Now put the real default back on the field. This is DDL only — it does
        # not revisit the rows above, so the existing partners keep their blank
        # while everything created after this starts as 'new'.
        migrations.AlterField(
            model_name='channelpartner',
            name='stage',
            field=models.CharField(
                default='new', blank=True, max_length=10, verbose_name='Stage',
                choices=[('new', 'New'), ('hot', 'Hot'), ('warm', 'Warm'), ('cold', 'Cold')],
            ),
        ),
    ]
