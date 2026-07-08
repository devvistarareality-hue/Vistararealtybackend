from django.db import migrations
import sales.fields


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0041_fix_leadsource_names'),
    ]

    operations = [
        migrations.AddField(
            model_name='booking',
            name='sale_deed_amount',
            field=sales.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=16),
        ),
    ]
