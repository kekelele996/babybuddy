# -*- coding: utf-8 -*-
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0039_child_due_date"),
    ]

    operations = [
        migrations.CreateModel(
            name="MergedChild",
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
                    "slug",
                    models.SlugField(
                        allow_unicode=True,
                        editable=False,
                        max_length=100,
                        unique=True,
                        verbose_name="Former slug",
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        editable=False, max_length=511, verbose_name="Former name"
                    ),
                ),
                (
                    "merged_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Merged at"),
                ),
                (
                    "child",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="merged_aliases",
                        to="core.child",
                        verbose_name="Surviving child",
                    ),
                ),
            ],
            options={
                "verbose_name": "Merged child",
                "verbose_name_plural": "Merged children",
                "ordering": ["-merged_at"],
                "default_permissions": ("view",),
            },
        ),
    ]
