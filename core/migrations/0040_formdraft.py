# Generated for the entry-form drafts feature.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0039_child_due_date"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FormDraft",
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
                ("form_key", models.CharField(max_length=64, verbose_name="Form")),
                ("data", models.JSONField(default=dict, verbose_name="Form data")),
                (
                    "created",
                    models.DateTimeField(auto_now_add=True, verbose_name="Created"),
                ),
                (
                    "updated",
                    models.DateTimeField(
                        auto_now=True, db_index=True, verbose_name="Updated"
                    ),
                ),
                (
                    "child",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="form_drafts",
                        to="core.child",
                        verbose_name="Child",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="form_drafts",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="User",
                    ),
                ),
            ],
            options={
                "verbose_name": "Form draft",
                "verbose_name_plural": "Form drafts",
                "ordering": ["-updated"],
            },
        ),
        migrations.AddConstraint(
            model_name="formdraft",
            constraint=models.UniqueConstraint(
                fields=("user", "form_key"),
                condition=models.Q(child__isnull=True),
                name="uniq_formdraft_null_child",
            ),
        ),
        migrations.AddConstraint(
            model_name="formdraft",
            constraint=models.UniqueConstraint(
                fields=("user", "form_key", "child"),
                condition=models.Q(child__isnull=False),
                name="uniq_formdraft_with_child",
            ),
        ),
    ]
