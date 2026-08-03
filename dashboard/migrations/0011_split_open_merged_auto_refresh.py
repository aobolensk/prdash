from django.db import migrations, models


AUTO_REFRESH_INTERVAL_CHOICES = [
    (1, '1 minute'), (2, '2 minutes'), (5, '5 minutes'), (10, '10 minutes'),
    (15, '15 minutes'), (30, '30 minutes'), (60, '1 hour'),
]


def migrate_my_prs_to_open_and_merged(apps, schema_editor):
    """Copy the shared my_prs auto-refresh settings to the new open/merged fields."""
    UserPreferences = apps.get_model('dashboard', 'UserPreferences')
    for prefs in UserPreferences.objects.all():
        prefs.auto_refresh_open = prefs.auto_refresh_my_prs
        prefs.auto_refresh_merged = prefs.auto_refresh_my_prs
        prefs.auto_refresh_interval_open = prefs.auto_refresh_interval_my_prs
        prefs.auto_refresh_interval_merged = prefs.auto_refresh_interval_my_prs
        prefs.save()


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0010_pluginconfiguration_remove_github_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='userpreferences',
            name='auto_refresh_open',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='userpreferences',
            name='auto_refresh_merged',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='userpreferences',
            name='auto_refresh_interval_open',
            field=models.PositiveIntegerField(choices=AUTO_REFRESH_INTERVAL_CHOICES, default=5),
        ),
        migrations.AddField(
            model_name='userpreferences',
            name='auto_refresh_interval_merged',
            field=models.PositiveIntegerField(choices=AUTO_REFRESH_INTERVAL_CHOICES, default=5),
        ),
        migrations.RunPython(migrate_my_prs_to_open_and_merged, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='userpreferences',
            name='auto_refresh_my_prs',
        ),
        migrations.RemoveField(
            model_name='userpreferences',
            name='auto_refresh_interval_my_prs',
        ),
    ]
