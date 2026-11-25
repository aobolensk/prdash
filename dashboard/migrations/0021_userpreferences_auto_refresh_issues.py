from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0020_userpreferences_highlight_unread_prs'),
    ]

    operations = [
        migrations.AddField(
            model_name='userpreferences',
            name='auto_refresh_issues',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='userpreferences',
            name='auto_refresh_interval_issues',
            field=models.PositiveIntegerField(
                choices=[
                    (1, '1 minute'),
                    (2, '2 minutes'),
                    (5, '5 minutes'),
                    (10, '10 minutes'),
                    (15, '15 minutes'),
                    (30, '30 minutes'),
                    (60, '1 hour'),
                ],
                default=5,
            ),
        ),
    ]
