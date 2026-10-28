# -*- coding: utf-8 -*-
import django.db.models.deletion
from django.db import migrations, models


def copy_endpoint_secrets_to_keys(apps, schema_editor):
    """
    Each existing endpoint has the key it has always had, as its version 1.
    """
    WebhookEndpoint = apps.get_model("webhooks", "WebhookEndpoint")
    WebhookSecret = apps.get_model("webhooks", "WebhookSecret")
    WebhookSecret.objects.bulk_create(
        WebhookSecret(
            endpoint=endpoint, version=1, secret=endpoint.secret, expires_at=None
        )
        for endpoint in WebhookEndpoint.objects.all()
    )


def copy_keys_back_to_endpoints(apps, schema_editor):
    """
    Put the earliest key back onto the endpoint for the time before this
    migration. Keys created by later rotations have no home there and are left
    for the DeleteModel that follows to remove.
    """
    WebhookEndpoint = apps.get_model("webhooks", "WebhookEndpoint")
    for endpoint in WebhookEndpoint.objects.all():
        first = endpoint.secrets.order_by("version").first()
        if first is not None:
            endpoint.secret = first.secret
            endpoint.save(update_fields=["secret"])


class Migration(migrations.Migration):

    dependencies = [
        ("webhooks", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="WebhookSecret",
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
                ("secret", models.CharField(max_length=255, verbose_name="Secret")),
                (
                    "version",
                    models.PositiveIntegerField(
                        help_text=(
                            "Where this key sits in the endpoint's rotation, "
                            "starting at 1."
                        ),
                        verbose_name="Version",
                    ),
                ),
                (
                    "created",
                    models.DateTimeField(auto_now_add=True, verbose_name="Created"),
                ),
                (
                    "expires_at",
                    models.DateTimeField(
                        blank=True,
                        db_index=True,
                        help_text=(
                            "Empty while this is the current key. Once set, the "
                            "key still signs requests until this moment and is "
                            "removed when it passes."
                        ),
                        null=True,
                        verbose_name="Expires at",
                    ),
                ),
                (
                    "endpoint",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="secrets",
                        to="webhooks.webhookendpoint",
                    ),
                ),
            ],
            options={
                "verbose_name": "Webhook signing key",
                "verbose_name_plural": "Webhook signing keys",
                "ordering": ("-version",),
            },
        ),
        migrations.RunPython(
            copy_endpoint_secrets_to_keys, copy_keys_back_to_endpoints
        ),
        migrations.RemoveField(model_name="webhookendpoint", name="secret"),
        migrations.AddConstraint(
            model_name="webhooksecret",
            constraint=models.UniqueConstraint(
                fields=("endpoint", "version"),
                name="webhooksecret_unique_version_per_endpoint",
            ),
        ),
        migrations.AddConstraint(
            model_name="webhooksecret",
            constraint=models.UniqueConstraint(
                condition=models.Q(("expires_at__isnull", True)),
                fields=("endpoint",),
                name="webhooksecret_one_current_per_endpoint",
            ),
        ),
    ]
