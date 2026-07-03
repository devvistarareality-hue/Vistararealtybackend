from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0035_lead_meta_form_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='booking',
            name='sale_deed_pct',
            field=models.DecimalField(decimal_places=2, default=60, max_digits=5),
        ),
    ]
