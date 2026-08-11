from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0011_split_open_merged_auto_refresh'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='PluginUserData',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('plugin_id', models.CharField(max_length=128)),
                ('collection', models.CharField(max_length=64)),
                ('key', models.CharField(max_length=128)),
                ('value', models.JSONField()),
                ('position', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                (
                    'user',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='plugin_user_data',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                'ordering': ['collection', 'position', 'key'],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('user', 'plugin_id', 'collection', 'key'),
                        name='unique_plugin_user_data_key',
                    ),
                ],
            },
        ),
    ]
