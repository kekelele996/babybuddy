# Generated for uploaded-image lifecycle management.

import core.images
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0039_child_due_date"),
    ]

    operations = [
        migrations.AlterField(
            model_name="child",
            name="picture",
            field=models.ImageField(
                blank=True,
                null=True,
                upload_to="child/picture/",
                validators=[core.images.validate_uploaded_image],
                verbose_name="Picture",
            ),
        ),
        migrations.AlterField(
            model_name="note",
            name="image",
            field=models.ImageField(
                blank=True,
                null=True,
                upload_to="notes/images/",
                validators=[core.images.validate_uploaded_image],
                verbose_name="Image",
            ),
        ),
        migrations.CreateModel(
            name="PendingFileDeletion",
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
                    "path",
                    models.CharField(
                        max_length=512, unique=True, verbose_name="Storage path"
                    ),
                ),
                (
                    "attempts",
                    models.PositiveIntegerField(default=0, verbose_name="Attempts"),
                ),
                (
                    "last_error",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=1000,
                        verbose_name="Last error",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Created at"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="Updated at"),
                ),
            ],
            options={
                "verbose_name": "Pending file deletion",
                "verbose_name_plural": "Pending file deletions",
                "ordering": ["-created_at"],
                "default_permissions": ("view", "add", "change", "delete"),
            },
        ),
    ]
