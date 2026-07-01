import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0006_user_session_token'),
    ]

    operations = [
        migrations.RenameField(
            model_name='user',
            old_name='session_token',
            new_name='session_token_app',
        ),
        migrations.AddField(
            model_name='user',
            name='session_token_web',
            field=models.UUIDField(default=uuid.uuid4),
        ),
    ]
