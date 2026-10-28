# -*- coding: utf-8 -*-
import secrets
import uuid

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


def generate_secret():
    """
    Create a signing secret for a new key.
    :return: a URL-safe random string
    """
    return secrets.token_urlsafe(32)


#: A secret that a caller chose itself has to be at least this long; one the
#: application generates is always longer.
MIN_SECRET_LENGTH = 16


class WebhookEndpointManager(models.Manager):
    def bulk_create(self, objs, *args, **kwargs):
        """
        ``save()`` gives a new endpoint its first key, but ``bulk_create``
        never calls it; an endpoint made that way would have no key at all
        and send unsigned requests. Give the batch its version 1 keys here.
        """
        instances = super().bulk_create(objs, *args, **kwargs)
        WebhookSecret.objects.bulk_create(
            [
                WebhookSecret(
                    endpoint=instance,
                    version=1,
                    secret=getattr(instance, "_initial_secret", None)
                    or generate_secret(),
                )
                for instance in instances
                if instance.pk
            ]
        )
        return instances


class WebhookEndpoint(models.Model):
    """
    A URL to notify when a record changes.

    The request names the change and carries none of its contents. An endpoint
    learns which record kind changed and when, and nothing else: not the name
    of a child, not how much was fed, not where anyone was. Anything that wants
    the details reads them back through the API with its own credentials.
    """

    name = models.CharField(max_length=255, verbose_name=_("Name"))
    url = models.URLField(
        max_length=1000,
        verbose_name=_("URL"),
        # Django's default also allows ftp and ftps, which are never sent to,
        # so an endpoint using one would only fail at delivery time.
        validators=[URLValidator(schemes=["http", "https"])],
    )
    active = models.BooleanField(default=True, verbose_name=_("Active"))
    created = models.DateTimeField(auto_now_add=True, verbose_name=_("Created"))
    last_delivery = models.DateTimeField(
        blank=True, null=True, verbose_name=_("Last delivery")
    )

    objects = WebhookEndpointManager()

    class Meta:
        ordering = ("name",)
        verbose_name = _("Webhook endpoint")
        verbose_name_plural = _("Webhook endpoints")

    def __init__(self, *args, **kwargs):
        # ``secret`` is no longer a field -- keys live in WebhookSecret -- but
        # a new endpoint is still made "with a secret": anything constructing
        # one (the API when the receiving application issued its own value)
        # hands it in here and it becomes the first key.
        secret = kwargs.pop("secret", None)
        super().__init__(*args, **kwargs)
        if secret is not None:
            self._initial_secret = secret

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # A new endpoint gets its first key in the same save. One constructed
        # with a secret uses that; anything else gets a generated one.
        adding = self._state.adding
        super().save(*args, **kwargs)
        if adding:
            WebhookSecret.objects.create(
                endpoint=self,
                version=1,
                secret=getattr(self, "_initial_secret", None) or generate_secret(),
            )

    @property
    def current_secret(self):
        """The key requests are signed with right now (the row with no cutoff)."""
        return WebhookSecret.objects.get(endpoint=self, expires_at__isnull=True)

    @property
    def secret(self):
        """
        The value of the current key.
        Kept as an attribute so callers that only ever need the one value in
        force -- signing and the create-once API response -- need know nothing
        about versions.
        """
        return self.current_secret.secret

    def valid_secrets(self, now=None):
        """
        Every key a request must be signed with at ``now``.

        The current key comes first, then the retired keys whose cutoff has
        not passed (newest first, since that is the one a receiver is most
        likely to hold). Nothing is cached on the endpoint: keys are read from
        the database at send time, so a long-lived delivery worker crosses a
        cutoff without having to be told.
        :param now: the current time, defaulting to the real one
        :return: a list of WebhookSecret rows, never empty while the endpoint
            exists (it is always created with a current key)
        """
        now = now or timezone.now()
        keys = list(
            WebhookSecret.objects.filter(
                models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now),
                endpoint=self,
            ).order_by("-version")
        )
        # Ordering puts the highest version first, which is the current key
        # whenever there is one -- but pin it explicitly so the contract does
        # not rely on version numbers alone.
        keys.sort(key=lambda key: not key.is_current)
        return keys

    @transaction.atomic
    def rotate_secret(self, new_secret=None, expires_at=None):
        """
        Give the endpoint a new current key.

        With a future ``expires_at`` the old current key keeps signing until
        that cutoff: requests in between carry a signature for every key that
        is still valid, so a receiver that has not been given the new key yet
        can still check one. With ``expires_at=None`` the old current key is
        discarded at once -- the immediate replacement the API's
        ``PATCH secret`` has always been -- and other retired keys keep their
        own cutoffs.

        :param new_secret: the key value, or None to generate one
        :param expires_at: an aware datetime in the future, or None for an
            immediate replacement
        :return: the new WebhookSecret row
        :raises ValidationError: for a short chosen secret or a cutoff that is
            naive or not in the future
        """
        if new_secret is not None and len(new_secret) < MIN_SECRET_LENGTH:
            raise ValidationError(
                _("The secret must be at least %(length)d characters long.")
                % {"length": MIN_SECRET_LENGTH}
            )
        if expires_at is not None:
            if timezone.is_naive(expires_at):
                raise ValidationError(_("The cutoff time must be timezone aware."))
            if expires_at <= timezone.now():
                raise ValidationError(_("The cutoff time must be in the future."))

        locked = WebhookEndpoint.objects.select_for_update().get(pk=self.pk)
        current = (
            WebhookSecret.objects.select_for_update()
            .filter(endpoint=locked, expires_at__isnull=True)
            .first()
        )
        next_version = (
            WebhookSecret.objects.filter(endpoint=locked).aggregate(
                highest=models.Max("version")
            )["highest"]
            or 0
        ) + 1
        if current is not None:
            if expires_at is None:
                current.delete()
            else:
                current.expires_at = expires_at
                current.save(update_fields=["expires_at"])
        return WebhookSecret.objects.create(
            endpoint=locked,
            version=next_version,
            secret=new_secret or generate_secret(),
        )


class WebhookSecret(models.Model):
    """
    One signing key for one endpoint, at one point in its rotation.

    The row with no ``expires_at`` is the current key, and every request is
    signed with it. A row with an ``expires_at`` is a retired key that keeps
    signing until that moment, so a receiver still holding it can check a
    request during the handover. At the cutoff it stops signing and is removed
    on the next delivery run. All of this lives in the database, which is what
    lets a worker be restarted, or replaced, in the middle of a rotation.
    """

    endpoint = models.ForeignKey(
        WebhookEndpoint, on_delete=models.CASCADE, related_name="secrets"
    )
    secret = models.CharField(max_length=255, verbose_name=_("Secret"))
    version = models.PositiveIntegerField(
        verbose_name=_("Version"),
        help_text=_("Where this key sits in the endpoint's rotation, starting at 1."),
    )
    created = models.DateTimeField(auto_now_add=True, verbose_name=_("Created"))
    expires_at = models.DateTimeField(
        blank=True,
        db_index=True,
        null=True,
        verbose_name=_("Expires at"),
        help_text=_(
            "Empty while this is the current key. Once set, the key still "
            "signs requests until this moment and is removed when it passes."
        ),
    )

    class Meta:
        ordering = ("-version",)
        constraints = [
            models.UniqueConstraint(
                fields=["endpoint", "version"],
                name="webhooksecret_unique_version_per_endpoint",
            ),
            models.UniqueConstraint(
                fields=["endpoint"],
                condition=models.Q(expires_at__isnull=True),
                name="webhooksecret_one_current_per_endpoint",
            ),
        ]
        verbose_name = _("Webhook signing key")
        verbose_name_plural = _("Webhook signing keys")

    def __str__(self):
        return "{} v{}".format(self.endpoint_id, self.version)

    @property
    def is_current(self):
        return self.expires_at is None

    def is_valid(self, now=None):
        """Whether this key still signs a request at ``now``."""
        return self.is_current or self.expires_at > (now or timezone.now())

    @classmethod
    def purge_expired(cls, now=None):
        """
        Delete keys whose cutoff has passed.
        :param now: the current time, defaulting to the real one
        :return: whatever the delete returns, including the row count
        """
        now = now or timezone.now()
        return cls.objects.filter(expires_at__lte=now).delete()


class WebhookEvent(models.Model):
    """
    One change, waiting to go to one endpoint.

    The row is written while the change is being saved, and where that save is
    part of a larger transaction the two commit together or not at all. Outside
    one -- a bare ``.save()`` in autocommit -- the entry commits first and the
    events follow, so a process that stops in between keeps the entry and loses
    its announcement. The entry is the important half of that pair: an
    announcement can be missed, a feeding cannot be allowed to vanish.

    Once the change has been sent to this endpoint the row is deleted, so what
    the table holds is what still has to go out or has been given up on. There
    is no history of what an endpoint was told.
    """

    endpoint = models.ForeignKey(
        WebhookEndpoint, on_delete=models.CASCADE, related_name="events"
    )
    event_id = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        verbose_name=_("Event ID"),
        help_text=_(
            "Sent with every attempt so the receiving application can discard "
            "a repeat. It is in the body too, and that copy is the one covered "
            "by the signature."
        ),
    )
    type = models.CharField(
        max_length=255,
        verbose_name=_("Type"),
        help_text=_('The kind of change, e.g. "feeding.created".'),
    )
    object_id = models.CharField(max_length=255, verbose_name=_("Object ID"))
    created = models.DateTimeField(auto_now_add=True, verbose_name=_("Created"))
    attempts = models.PositiveIntegerField(default=0, verbose_name=_("Attempts"))
    next_attempt = models.DateTimeField(
        default=timezone.now,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Next attempt"),
        help_text=_(
            "Empty once the attempts are used up: the event stays as a record "
            "and is not tried again."
        ),
    )
    last_error = models.CharField(
        max_length=255, blank=True, verbose_name=_("Last error")
    )

    class Meta:
        ordering = ("-created",)
        verbose_name = _("Webhook event")
        verbose_name_plural = _("Webhook events")

    def __str__(self):
        return "{} {}".format(self.type, self.object_id)
