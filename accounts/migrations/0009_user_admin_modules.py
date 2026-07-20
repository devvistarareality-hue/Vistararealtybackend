from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0008_otp_code'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='admin_modules',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
