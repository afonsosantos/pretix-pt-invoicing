import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pretix_ptinvoicing", "0005_billing_data"),
        ("pretixbase", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ItemSettings",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("reference", models.CharField(blank=True, default="", max_length=50)),
                ("data", models.JSONField(blank=True, default=dict)),
                (
                    "item",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ptinvoicing_settings",
                        to="pretixbase.item",
                    ),
                ),
            ],
        ),
    ]
