from django.db import migrations, models


AUTO_REFRESH_INTERVAL_CHOICES = [
    (1, '1 minute'), (2, '2 minutes'), (5, '5 minutes'), (10, '10 minutes'),
    (15, '15 minutes'), (30, '30 minutes'), (60, '1 hour'),
]


def migrate_auto_refresh_interval(apps, schema_editor):
    """Copy the global auto_refresh_interval value to all three per-tab fields."""
    UserPreferences = apps.get_model('dashboard', 'UserPreferences')
    for prefs in UserPreferences.objects.all():
        prefs.auto_refresh_interval_my_prs = prefs.auto_refresh_interval
        prefs.auto_refresh_interval_review_requests = prefs.auto_refresh_interval
        prefs.auto_refresh_interval_assigned = prefs.auto_refresh_interval
        prefs.save()


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0006_alter_userpreferences_auto_refresh_interval'),
    ]

    operations = [
        migrations.AddField(
            model_name='userpreferences',
            name='auto_refresh_interval_my_prs',
            field=models.PositiveIntegerField(choices=AUTO_REFRESH_INTERVAL_CHOICES, default=5),
        ),
        migrations.AddField(
            model_name='userpreferences',
            name='auto_refresh_interval_review_requests',
            field=models.PositiveIntegerField(choices=AUTO_REFRESH_INTERVAL_CHOICES, default=5),
        ),
        migrations.AddField(
            model_name='userpreferences',
            name='auto_refresh_interval_assigned',
            field=models.PositiveIntegerField(choices=AUTO_REFRESH_INTERVAL_CHOICES, default=5),
        ),
        migrations.RunPython(migrate_auto_refresh_interval, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='userpreferences',
            name='auto_refresh_interval',
        ),
    ]
