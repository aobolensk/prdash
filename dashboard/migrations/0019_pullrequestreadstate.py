from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0018_browseraccount'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='PullRequestReadState',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('repo_full_name', models.CharField(max_length=511)),
                ('pr_number', models.PositiveIntegerField()),
                ('first_seen_updated_at', models.DateTimeField()),
                ('read_updated_at', models.DateTimeField(blank=True, null=True)),
                (
                    'user',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='pr_read_states',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name='pullrequestreadstate',
            constraint=models.UniqueConstraint(
                fields=('user', 'repo_full_name', 'pr_number'),
                name='unique_user_pr_read_state',
            ),
        ),
    ]
