from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def create_default_profiles(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split('.'))
    RepositoryProfile = apps.get_model('dashboard', 'RepositoryProfile')
    TrackedRepository = apps.get_model('dashboard', 'TrackedRepository')

    profiles = {
        user.id: RepositoryProfile.objects.create(user_id=user.id, name='Default')
        for user in User.objects.all().only('id')
    }
    for repository in TrackedRepository.objects.all().iterator():
        repository.profile_id = profiles[repository.user_id].id
        repository.save(update_fields=['profile'])


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0016_pluginusersecret'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='RepositoryProfile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='repository_profiles',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['name', 'id'],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('user', 'name'), name='unique_user_repository_profile_name',
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name='trackedrepository',
            name='profile',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='repositories',
                to='dashboard.repositoryprofile',
            ),
        ),
        migrations.RunPython(create_default_profiles, migrations.RunPython.noop),
        migrations.AlterUniqueTogether(
            name='trackedrepository',
            unique_together=set(),
        ),
        migrations.AlterField(
            model_name='trackedrepository',
            name='profile',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='repositories',
                to='dashboard.repositoryprofile',
            ),
        ),
        migrations.AddConstraint(
            model_name='trackedrepository',
            constraint=models.UniqueConstraint(
                fields=('profile', 'owner', 'name'), name='unique_profile_tracked_repository',
            ),
        ),
    ]
