import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0005_notification'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='session_token',
            field=models.UUIDField(default=uuid.uuid4),
        ),
    ]
