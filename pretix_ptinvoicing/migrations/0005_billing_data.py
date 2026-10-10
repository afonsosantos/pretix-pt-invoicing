from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pretix_ptinvoicing", "0004_email_sent"),
    ]

    operations = [
        migrations.AddField(
            model_name="issuedinvoice",
            name="billing_data",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
