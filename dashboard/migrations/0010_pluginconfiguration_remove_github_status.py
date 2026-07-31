from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0009_userpreferences_show_github_status'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='PluginConfiguration',
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
                ('enabled', models.BooleanField(default=False)),
                ('config', models.JSONField(blank=True, default=dict)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                (
                    'user',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='plugin_configurations',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                'ordering': ['plugin_id'],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('user', 'plugin_id'),
                        name='unique_user_plugin_configuration',
                    ),
                ],
            },
        ),
        migrations.RemoveField(
            model_name='userpreferences',
            name='show_github_status',
        ),
    ]
