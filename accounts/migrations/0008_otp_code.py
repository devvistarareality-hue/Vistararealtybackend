from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0007_split_session_tokens'),
    ]

    operations = [
        migrations.CreateModel(
            name='OtpCode',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('token', models.UUIDField(default=uuid.uuid4, unique=True)),
                ('code', models.CharField(max_length=6)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('is_used', models.BooleanField(default=False)),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='otp_codes',
                    to='accounts.user',
                )),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
