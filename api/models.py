# -*- coding: utf-8 -*-
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Batch(models.Model):
    """
    One batch submission, stored so that sending the same batch again returns
    the result of the first submission.

    A batch is created under the idempotency key the client chose. The key is
    unique per user, so a retry after a network timeout finds the first
    submission instead of writing the entries a second time. The fingerprint
    holds the identity of the submitted payload: a key reused with a different
    payload is a client mistake and is rejected rather than silently answered
    with an unrelated result. ``result`` is the response of the first, and
    only, successful submission -- the created entries in their input order.
    """

    model_name = "batch"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="batches",
        verbose_name=_("User"),
    )
    idempotency_key = models.CharField(
        max_length=255,
        verbose_name=_("Idempotency key"),
        help_text=_(
            "A key the client chooses for the batch. Sending the same key "
            "again returns the result of the first submission."
        ),
    )
    fingerprint = models.CharField(
        max_length=64,
        verbose_name=_("Fingerprint"),
        help_text=_("Hash of the submitted entries, used to spot a reused key."),
    )
    result = models.JSONField(
        default=dict,
        verbose_name=_("Result"),
        help_text=_("The response of the first successful submission."),
    )
    created = models.DateTimeField(auto_now_add=True, verbose_name=_("Created"))

    objects = models.Manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("user", "idempotency_key"),
                name="api_batch_unique_user_idempotency_key",
            )
        ]
        ordering = ("-created",)
        verbose_name = _("Batch")
        verbose_name_plural = _("Batches")

    def __str__(self):
        return self.idempotency_key
