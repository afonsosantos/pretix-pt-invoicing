import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("pretixbase", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="FactptInvoice",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Processing"),
                            ("success", "Issued"),
                            ("error", "Error"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("identifier_id", models.CharField(max_length=50, unique=True)),
                (
                    "factpt_document_id",
                    models.CharField(blank=True, max_length=64, null=True),
                ),
                (
                    "factpt_link",
                    models.CharField(blank=True, max_length=255, null=True),
                ),
                (
                    "permanent_url",
                    models.CharField(blank=True, max_length=255, null=True),
                ),
                ("error_message", models.TextField(blank=True, null=True)),
                ("error_detail", models.JSONField(blank=True, null=True)),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("modified", models.DateTimeField(auto_now=True)),
                (
                    "order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="factpt_invoices",
                        to="pretixbase.order",
                    ),
                ),
            ],
            options={
                "ordering": ["-created"],
            },
        ),
    ]
