from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0037_plot_construction_area'),
    ]

    operations = [
        migrations.AddField(
            model_name='project',
            name='logo_url',
            field=models.CharField(blank=True, max_length=500),
        ),
    ]
