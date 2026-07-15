from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0042_booking_sale_deed_amount'),
    ]

    operations = [
        migrations.AddField(
            model_name='project',
            name='eoi_unit_types',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
