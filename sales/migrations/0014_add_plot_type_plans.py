from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0013_add_meta_adset_name_to_lead'),
    ]

    operations = [
        migrations.AddField(
            model_name='project',
            name='plot_type_plans',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
