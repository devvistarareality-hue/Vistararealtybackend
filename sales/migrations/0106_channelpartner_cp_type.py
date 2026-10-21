from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0105_reject_cancel_reasons'),
    ]

    operations = [
        migrations.AddField(
            model_name='channelpartner',
            name='cp_type',
            # No explicit default: a CharField fills '' on the existing rows, so
            # every partner already on the directory starts ungraded rather than
            # being assigned a band nobody chose for them.
            field=models.CharField(
                blank=True, max_length=1, verbose_name='Type',
                choices=[('A', 'A (Wholesale)'), ('B', 'B (Retail)'), ('C', 'C (Non CP)')],
            ),
        ),
    ]
