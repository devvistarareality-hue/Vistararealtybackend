from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0036_booking_sale_deed_pct'),
    ]

    operations = [
        migrations.AddField(
            model_name='plot',
            name='construction_area',
            field=models.CharField(blank=True, max_length=100),
        ),
    ]
