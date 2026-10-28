# -*- coding: utf-8 -*-
"""Server-side support for recoverable add-form drafts.

Drafts hold raw widget values for a not-yet-submitted entry form. They are
scoped by (user, form, child), expire after FORM_DRAFT_TTL, and are written and
read through a single small JSON endpoint below. The draft table is the only
thing these functions touch: no care-entry instance is ever built from a draft
and no signal fires, so drafts can neither overwrite records nor announce them.
"""

import json

from django.http import JsonResponse
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.generic import View

from babybuddy.mixins import LoginRequiredMixin
from core import forms, models

#: Maximum size of a draft payload. A real form is a few short fields; this is
#: only here to stop oversized writes.
MAX_DRAFT_BYTES = 65536

#: The forms that may hold drafts, mapped to the model they create and the form
#: class defining their fields. Extending draft support to another entry type is
#: one line here plus a ``draft_key`` on its add view.
DRAFT_FORMS = {
    "feeding": (models.Feeding, forms.FeedingForm),
    "bottle-feeding": (models.Feeding, forms.BottleFeedingForm),
    "sleep": (models.Sleep, forms.SleepForm),
    "pumping": (models.Pumping, forms.PumpingForm),
    "tummytime": (models.TummyTime, forms.TummyTimeForm),
}


class DraftValidationError(ValueError):
    pass


class DraftPermissionError(Exception):
    pass


def draft_field_names(form_class):
    """Names that draft data is allowed to contain for a form class."""
    return set(form_class.Meta.fields)


def normalize_child_id(value):
    """Turn a wire child value into either None or an int pk.

    Missing or empty values mean "no child chosen yet". Anything else must be
    an int-shaped value.
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise DraftValidationError("Invalid child.")


def is_empty_data(data):
    """Whether a draft carries nothing worth storing."""
    return all(value in (None, "", False, [], {}) for value in data.values())


def resolve_slot(user, form_key, child_value):
    """
    Validate form key, permission and child; return (model, form_class, child_id).

    Raises DraftValidationError (400) or DraftPermissionError (403).
    """
    if form_key not in DRAFT_FORMS:
        raise DraftValidationError("Unknown form.")
    model, form_class = DRAFT_FORMS[form_key]
    if not user.has_perm("core.add_{}".format(model.model_name)):
        raise DraftPermissionError()
    child_id = normalize_child_id(child_value)
    if child_id is not None and not models.Child.objects.filter(pk=child_id).exists():
        raise DraftValidationError("Unknown child.")
    return model, form_class, child_id


def validate_data(form_class, data):
    """Check the draft data object, its keys and its individual values."""
    if not isinstance(data, dict):
        raise DraftValidationError("Draft data must be an object.")
    # Drop framework bookkeeping rather than reject it: Django renders hidden
    # ``initial-<field>`` inputs alongside fields with dynamic defaults.
    for bookkeeping in [name for name in data if name.startswith("initial-")]:
        data.pop(bookkeeping)
    allowed = draft_field_names(form_class)
    for name, value in data.items():
        if name not in allowed:
            raise DraftValidationError("Unknown form field.")
        if value is None or isinstance(value, (bool, str, int, float)):
            continue
        raise DraftValidationError("Unsupported field value.")
    try:
        encoded_size = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        raise DraftValidationError("Draft data is not serializable.")
    if encoded_size > MAX_DRAFT_BYTES:
        raise DraftValidationError("Draft is too large.")


@method_decorator(never_cache, name="dispatch")
class DraftEndpoint(LoginRequiredMixin, View):
    http_method_names = ["get", "post", "delete", "options"]

    def error(self, message, status):
        return JsonResponse({"error": message}, status=status)

    def resolve(self, user, form_key, child_value):
        """Resolve a slot or return a ready-made error response as the 4th item."""
        try:
            return (*resolve_slot(user, form_key, child_value), None)
        except DraftPermissionError:
            return None, None, None, self.error("Forbidden", 403)
        except DraftValidationError as error:
            return None, None, None, self.error(str(error), 400)

    def get(self, request, *args, **kwargs):
        _, _, child_id, error_response = self.resolve(
            request.user,
            request.GET.get("form_key"),
            request.GET.get("child"),
        )
        if error_response:
            return error_response
        draft = models.FormDraft.objects.get_valid(
            request.user, request.GET["form_key"], child_id
        )
        if draft is None:
            return self.error("No draft.", 404)
        return JsonResponse(
            {
                "form_key": draft.form_key,
                "child": draft.child_id,
                "data": draft.data,
                "updated": draft.updated.isoformat(),
            }
        )

    def post(self, request, *args, **kwargs):
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self.error("Invalid JSON.", 400)
        if not isinstance(payload, dict):
            return self.error("Invalid payload.", 400)

        form_key = payload.get("form_key")
        _, form_class, child_id, error_response = self.resolve(
            request.user, form_key, payload.get("child")
        )
        if error_response:
            return error_response

        data = payload.get("data", {})
        try:
            validate_data(form_class, data)
        except DraftValidationError as error:
            return self.error(str(error), 400)

        # An untouched, empty form must not leave a draft behind. Explicitly
        # clearing a draft goes through DELETE.
        if is_empty_data(data):
            return JsonResponse({"saved": False}, status=200)

        draft = models.FormDraft.objects.upsert(request.user, form_key, child_id, data)
        return JsonResponse(
            {
                "saved": True,
                "child": draft.child_id,
                "updated": draft.updated.isoformat(),
            },
            status=200,
        )

    def delete(self, request, *args, **kwargs):
        form_key = request.GET.get("form_key")
        _, _, child_id, error_response = self.resolve(
            request.user, form_key, request.GET.get("child")
        )
        if error_response:
            return error_response
        models.FormDraft.objects.discard(request.user, form_key, child_id)
        return JsonResponse({}, status=204)


def draft_endpoint_url():
    return reverse("core:draft-endpoint")
