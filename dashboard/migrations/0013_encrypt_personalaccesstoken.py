from django.db import migrations
import dashboard.fields


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0012_pluginuserdata'),
    ]

    operations = [
        migrations.AlterField(
            model_name='personalaccesstoken',
            name='token',
            field=dashboard.fields.EncryptedCharField(
                max_length=512,
                help_text='GitHub fine-grained or classic token',
            ),
        ),
    ]
