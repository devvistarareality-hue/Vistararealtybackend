from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0038_project_logo_url'),
    ]

    operations = [
        migrations.AddField(
            model_name='booking',
            name='apply_page_fee',
            field=models.CharField(default='Yes', max_length=5),
        ),
    ]
