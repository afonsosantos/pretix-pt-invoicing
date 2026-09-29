import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pretix_ptinvoicing", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="issuedinvoice",
            name="kind",
            field=models.CharField(
                choices=[("invoice", "Invoice"), ("credit_note", "Credit note")],
                default="invoice",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="issuedinvoice",
            name="credits",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="credit_notes",
                to="pretix_ptinvoicing.issuedinvoice",
            ),
        ),
    ]
