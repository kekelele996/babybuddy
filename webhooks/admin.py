# -*- coding: utf-8 -*-
from django.contrib import admin
from django.core.exceptions import (
    PermissionDenied,
    ValidationError as DjangoValidationError,
)
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, render
from django.urls import path, reverse
from django.utils import formats, timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from webhooks import models
from webhooks.forms import SecretRotationForm


class WebhookSecretInline(admin.TabularInline):
    """
    The endpoint's keys, read-only. Keys change only through a rotation:
    adding or editing a row by hand would make a version that never went
    through the handover, and deleting one would quietly take away the
    signature a receiver still needs.
    """

    model = models.WebhookSecret
    fields = ("version", "created", "status")
    readonly_fields = ("version", "created", "status")
    ordering = ("-version",)
    extra = 0
    can_delete = False

    @admin.display(description=_("Status"))
    def status(self, key):
        if key.is_current:
            return _("Current")
        when = formats.date_format(
            timezone.localtime(key.expires_at), "DATETIME_FORMAT"
        )
        if key.expires_at > timezone.now():
            return _("Stops working at %(when)s") % {"when": when}
        return _("Expired at %(when)s") % {"when": when}

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(models.WebhookEndpoint)
class WebhookEndpointAdmin(admin.ModelAdmin):
    list_display = ("name", "url", "signing_key", "active", "created", "last_delivery")
    list_filter = ("active",)
    search_fields = ("name", "url")
    readonly_fields = ("created",)
    inlines = (WebhookSecretInline,)
    change_form_template = "admin/webhooks/webhookendpoint/change_form.html"

    def get_queryset(self, request):
        # The signing-key column reads the endpoint's rows, so prefetch them
        # once for the whole list instead of one query per endpoint.
        return (
            super()
            .get_queryset(request)
            .prefetch_related(
                Prefetch("secrets", queryset=models.WebhookSecret.objects.all())
            )
        )

    @admin.display(description=_("Signing key"))
    def signing_key(self, endpoint):
        keys = list(endpoint.secrets.all())
        current = next((key for key in keys if key.is_current), None)
        if current is None:
            return "–"
        now = timezone.now()
        retiring = [
            key for key in keys if key.expires_at is not None and key.expires_at > now
        ]
        label = _("v%(version)s") % {"version": current.version}
        if not retiring:
            return label
        lines = [format_html("<strong>{}</strong>", label)]
        for key in sorted(retiring, key=lambda row: row.version, reverse=True):
            when = formats.date_format(
                timezone.localtime(key.expires_at), "DATETIME_FORMAT"
            )
            lines.append(
                _("Old key v%(version)s stops working at %(when)s")
                % {"version": key.version, "when": when}
            )
        return format_html("<br>".join(lines))

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<path:object_id>/rotate-secret/",
                self.admin_site.admin_view(self.rotate_secret_view),
                name="webhooks_webhookendpoint_rotate_secret",
            ),
        ]
        return custom + urls

    def rotate_secret_view(self, request, object_id):
        endpoint = get_object_or_404(models.WebhookEndpoint, pk=object_id)
        if not self.has_change_permission(request, endpoint):
            raise PermissionDenied

        new_key = None
        cutoff = None
        form = SecretRotationForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            cutoff = form.cleaned_data["expires_at"]
            try:
                new_key = endpoint.rotate_secret(
                    new_secret=form.cleaned_data["new_secret"] or None,
                    expires_at=cutoff,
                )
            except DjangoValidationError as error:
                # The cutoff can be overtaken by the clock between form
                # validation and the locked write. Report it as a form
                # problem instead of a 500; nothing was changed.
                form.add_error(None, error)
                cutoff = None

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": _("Rotate signing key"),
            "endpoint": endpoint,
            "form": form,
            "new_key": new_key,
            "old_key_version": new_key.version - 1 if new_key else None,
            "cutoff": cutoff,
            "change_url": reverse(
                "admin:webhooks_webhookendpoint_change", args=[endpoint.pk]
            ),
            "changelist_url": reverse("admin:webhooks_webhookendpoint_changelist"),
            "preserved_filters": self.get_preserved_filters(request),
        }
        return render(
            request,
            "admin/webhooks/webhookendpoint/rotate_secret.html",
            context,
        )


@admin.register(models.WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = (
        "type",
        "object_id",
        "endpoint",
        "created",
        "attempts",
        "next_attempt",
    )
    list_filter = ("type", "endpoint")
    search_fields = ("type", "object_id", "last_error")
    readonly_fields = (
        "endpoint",
        "event_id",
        "type",
        "object_id",
        "created",
        "attempts",
        "next_attempt",
        "last_error",
    )

    def has_add_permission(self, request):
        # Events come from changes to records. Adding one by hand would be a
        # notification about something that never happened.
        return False
