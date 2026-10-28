# -*- coding: utf-8 -*-
import secrets
from copy import deepcopy
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from taggit.serializers import TagListSerializerField, TaggitSerializer

from core import models
from babybuddy import models as babybuddy_models
from webhooks import models as webhooks_models


class CoreModelSerializer(serializers.HyperlinkedModelSerializer):
    """
    Provide the child link (used by most core models) and run model clean()
    methods during POST operations.
    """

    child = serializers.PrimaryKeyRelatedField(queryset=models.Child.objects.all())

    def validate(self, attrs):
        # Ensure that all instance data is available for partial updates to
        # support clean methods that compare multiple fields.
        if self.partial:
            new_instance = deepcopy(self.instance)
            for attr, value in attrs.items():
                setattr(new_instance, attr, value)
        else:
            new_instance = self.Meta.model(**attrs)
        new_instance.clean()
        return attrs


class CoreModelWithDurationSerializer(CoreModelSerializer):
    """
    Specific serializer base for models with a "start" and "end" field.
    """

    child = serializers.PrimaryKeyRelatedField(
        allow_null=True,
        help_text="Required unless a Timer value is provided.",
        queryset=models.Child.objects.all(),
        required=False,
    )

    timer = serializers.PrimaryKeyRelatedField(
        allow_null=True,
        help_text="May be used in place of the Start, End, and/or Child values.",
        queryset=models.Timer.objects.all(),
        required=False,
        write_only=True,
    )

    class Meta:
        abstract = True
        extra_kwargs = {
            "start": {
                "help_text": "Required unless a Timer value is provided.",
                "required": False,
            },
            "end": {
                "help_text": "Required unless a Timer value is provided.",
                "required": False,
            },
        }

    def validate(self, attrs):
        # Check for a special "timer" data argument that can be used in place
        # of "start" and "end" fields as well as "child" if it is set on the
        # Timer entry.
        timer = None
        if "timer" in attrs:
            # Remove the "timer" attribute (super validation would fail as it
            # is not a true field on the model).
            timer = attrs.pop("timer")
            if timer is None:
                raise ValidationError({"timer": "This field may not be null."})
            if not timer.can_be_consumed_by(self.context["request"].user):
                raise PermissionDenied("You do not have permission to consume timers.")

            if timer.child:
                attrs["child"] = timer.child

            # Overwrites values provided directly!
            attrs["start"] = timer.start
            attrs["end"] = timezone.now()

        # The "child", "start", and "end" field should all be set at this
        # point. If one is not, model validation will fail because they are
        # required fields at the model level.
        if not self.partial:
            errors = {}
            for field in ["child", "start", "end"]:
                if field not in attrs or not attrs[field]:
                    errors[field] = "This field is required."
            if len(errors) > 0:
                raise ValidationError(errors)

        attrs = super().validate(attrs)

        self.timer = timer
        return attrs

    @transaction.atomic
    def save(self, **kwargs):
        timer = getattr(self, "timer", None)
        if timer is not None:
            try:
                timer = models.Timer.objects.select_for_update().get(pk=timer.pk)
            except models.Timer.DoesNotExist:
                raise ValidationError({"timer": "This timer no longer exists."})
            # The timer may have changed owner since validation.
            if not timer.can_be_consumed_by(self.context["request"].user):
                raise PermissionDenied("You do not have permission to consume timers.")
        instance = super().save(**kwargs)
        if timer is not None:
            timer.stop()
        return instance


class TaggableSerializer(TaggitSerializer, serializers.HyperlinkedModelSerializer):
    tags = TagListSerializerField(required=False)

    def validate_tags(self, tags):
        current = self.instance.tags.names() if self.instance else ()
        models.Tag.check_assignment_permissions(
            self.context["request"].user, tags, current
        )
        return tags


class BMISerializer(CoreModelSerializer, TaggableSerializer):
    class Meta:
        model = models.BMI
        fields = ("id", "child", "bmi", "date", "notes", "tags")
        extra_kwargs = {
            "core.BMI.bmi": {"label": "BMI"},
        }


class PumpingSerializer(CoreModelWithDurationSerializer, TaggableSerializer):
    class Meta(CoreModelWithDurationSerializer.Meta):
        model = models.Pumping
        fields = (
            "id",
            "child",
            "amount",
            "start",
            "end",
            "duration",
            "notes",
            "tags",
            "timer",
        )


class ChildSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.Child
        fields = (
            "id",
            "first_name",
            "last_name",
            "birth_date",
            "birth_time",
            "due_date",
            "slug",
            "picture",
        )
        lookup_field = "slug"


class DiaperChangeSerializer(CoreModelSerializer, TaggableSerializer):
    class Meta:
        model = models.DiaperChange
        fields = (
            "id",
            "child",
            "time",
            "wet",
            "solid",
            "color",
            "amount",
            "notes",
            "tags",
        )


class FeedingSerializer(CoreModelWithDurationSerializer, TaggableSerializer):
    class Meta(CoreModelWithDurationSerializer.Meta):
        model = models.Feeding
        fields = (
            "id",
            "child",
            "start",
            "end",
            "timer",
            "duration",
            "type",
            "method",
            "amount",
            "notes",
            "tags",
        )


class HeadCircumferenceSerializer(CoreModelSerializer, TaggableSerializer):
    class Meta:
        model = models.HeadCircumference
        fields = ("id", "child", "head_circumference", "date", "notes", "tags")


class HeightSerializer(CoreModelSerializer, TaggableSerializer):
    class Meta:
        model = models.Height
        fields = ("id", "child", "height", "date", "notes", "tags")


class MedicationSerializer(CoreModelSerializer, TaggableSerializer):
    class Meta:
        model = models.Medication
        fields = (
            "id",
            "child",
            "name",
            "dosage",
            "dosage_unit",
            "time",
            "next_dose_interval",
            "notes",
            "tags",
        )


class NoteSerializer(CoreModelSerializer, TaggableSerializer):
    class Meta:
        model = models.Note
        fields = ("id", "child", "note", "image", "time", "tags")


class SleepSerializer(CoreModelWithDurationSerializer, TaggableSerializer):
    nap = serializers.BooleanField(allow_null=True, default=None, required=False)

    class Meta(CoreModelWithDurationSerializer.Meta):
        model = models.Sleep
        fields = (
            "id",
            "child",
            "start",
            "end",
            "timer",
            "duration",
            "nap",
            "notes",
            "tags",
        )


class BatchEnvelopeSerializer(serializers.Serializer):
    """
    The shape of a batch request, checked before anything else.

    Only the envelope is validated on a retry, so that resending a batch
    whose timer was already consumed returns the stored result instead of
    failing on the -- now gone -- timer. The entries themselves are checked
    afterwards, by :class:`BatchSerializer`, and only when the key is new.
    """

    #: The largest batch accepted, so one request cannot hold an arbitrarily
    #: long validation-and-write transaction open.
    MAX_ENTRIES = 100

    idempotency_key = serializers.CharField(
        max_length=255,
        help_text=(
            "A key the client chooses. Sending the batch again with the same "
            "key returns the result of the first submission."
        ),
    )
    entries = serializers.ListField(
        child=serializers.DictField(),
        allow_empty=False,
        help_text=(
            'The entries to write. Each is an object with a "kind" of '
            '"feeding" or "sleep" and the fields of that record.'
        ),
    )

    def validate(self, attrs):
        if len(attrs["entries"]) > self.MAX_ENTRIES:
            raise ValidationError(
                {
                    "entries": [
                        "A batch may contain at most {} entries.".format(
                            self.MAX_ENTRIES
                        )
                    ]
                }
            )
        return attrs


class BatchSerializer(BatchEnvelopeSerializer):
    """
    Validate every entry of a batch before any of them is written.

    Each entry names its record kind -- "feeding" or "sleep" -- in a "kind"
    field rather than a "type" field, because a feeding already has a field
    called "type". The entry is then checked with the serializer of that kind,
    so the same field rules apply as to a single POST. Errors come back keyed
    by the entry index, telling the caller which entries cannot be written,
    and nothing is written while any entry fails.

    Two checks run across entries rather than on each one: a timer cannot be
    used by two entries, since the first consumes it, and two entries of the
    same kind for the same child may not overlap in time. The per-entry
    ``clean()`` methods only see the database, not entries still waiting in
    the same batch, so overlaps between batch entries are checked here as
    well, in input order.
    """

    #: Serializers an entry may consist of, keyed by its "kind" value.
    ENTRY_SERIALIZERS = {
        "feeding": FeedingSerializer,
        "sleep": SleepSerializer,
    }

    def validate(self, attrs):
        attrs = super().validate(attrs)
        # Not a validate_<field> method on purpose: that name would make DRF
        # treat it as the field-level validator for "entries".
        attrs["prepared"] = self.check_entries(attrs["entries"])
        return attrs

    def check_entries(self, entries):
        errors = {}
        prepared = []
        for index, entry in enumerate(entries):
            kind = entry.get("kind")
            if kind is None:
                errors[str(index)] = {"kind": "This field is required."}
                continue
            entry_serializer_class = self.ENTRY_SERIALIZERS.get(kind)
            if entry_serializer_class is None:
                errors[str(index)] = {
                    "kind": '"{}" is not a valid choice.'.format(kind)
                }
                continue
            data = {key: value for key, value in entry.items() if key != "kind"}
            entry_serializer = entry_serializer_class(data=data, context=self.context)
            if not entry_serializer.is_valid():
                errors[str(index)] = entry_serializer.errors
            else:
                prepared.append((index, kind, entry_serializer))

        self._validate_timers(prepared, errors)
        self._validate_periods(prepared, errors)

        if errors:
            raise ValidationError({"entries": errors})

        return prepared

    def _validate_timers(self, prepared, errors):
        # The entry that uses a timer consumes (deletes) it, so a timer used
        # by two entries would leave the second with nothing to consume. The
        # later entry is the one reported.
        timers_seen = {}
        for index, kind, entry_serializer in prepared:
            timer = getattr(entry_serializer, "timer", None)
            if timer is None:
                continue
            if timer.pk in timers_seen:
                errors.setdefault(str(index), {})[
                    "timer"
                ] = "This timer is already used by another entry in the batch."
            else:
                timers_seen[timer.pk] = index

    def _validate_periods(self, prepared, errors):
        # validate_unique_period only sees saved rows, so two periods that
        # first meet inside this batch are compared here, kind by kind and
        # child by child, in the order they were submitted.
        periods = []
        for index, kind, entry_serializer in prepared:
            data = entry_serializer.validated_data
            model = entry_serializer.Meta.model
            child = data["child"]
            start = data["start"]
            end = data["end"]
            for other in periods:
                if (
                    model is other[0]
                    and child.pk == other[1]
                    and start < other[3]
                    and end > other[2]
                ):
                    errors.setdefault(str(index), {})["non_field_errors"] = [
                        "Another entry in this batch intersects the specified "
                        "time period."
                    ]
                    break
            periods.append((model, child.pk, start, end))


class TagSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.Tag
        fields = ("slug", "name", "color", "last_used")
        extra_kwargs = {
            "slug": {"required": False, "read_only": True},
            "color": {"required": False},
            "last_used": {"required": False, "read_only": True},
        }


class TemperatureSerializer(CoreModelSerializer, TaggableSerializer):
    class Meta:
        model = models.Temperature
        fields = ("id", "child", "temperature", "time", "notes", "tags")


class TimerSerializer(CoreModelSerializer):
    child = serializers.PrimaryKeyRelatedField(
        allow_null=True,
        allow_empty=True,
        queryset=models.Child.objects.all(),
        required=False,
    )
    user = serializers.PrimaryKeyRelatedField(
        allow_null=True,
        allow_empty=True,
        queryset=get_user_model().objects.all(),
        required=False,
    )
    duration = serializers.DurationField(read_only=True, required=False)

    class Meta:
        model = models.Timer
        fields = ("id", "child", "name", "start", "duration", "user")

    def validate(self, attrs):
        attrs = super(TimerSerializer, self).validate(attrs)
        request_user = self.context["request"].user

        if self.instance is None:
            # Set user to current user if no value is provided.
            if "user" not in attrs or attrs["user"] is None:
                attrs["user"] = request_user
        elif "user" in attrs:
            # The owner may consume the timer, so taking over another user's
            # timer requires the same permission as consuming it.
            attrs["user"] = attrs["user"] or request_user
            if attrs["user"] != self.instance.user and not request_user.has_perm(
                "core.delete_timer"
            ):
                raise PermissionDenied(
                    "You do not have permission to change the user of a timer."
                )

        return attrs

    @transaction.atomic
    def update(self, instance, validated_data):
        # Work on the stored timer, whose owner may have changed since
        # validation, so the check uses it and a stale owner is never saved back.
        instance = models.Timer.objects.select_for_update().get(pk=instance.pk)
        user = validated_data.get("user", instance.user)
        if user != instance.user and not self.context["request"].user.has_perm(
            "core.delete_timer"
        ):
            raise PermissionDenied(
                "You do not have permission to change the user of a timer."
            )
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance


class TummyTimeSerializer(CoreModelWithDurationSerializer, TaggableSerializer):
    class Meta(CoreModelWithDurationSerializer.Meta):
        model = models.TummyTime
        fields = (
            "id",
            "child",
            "start",
            "end",
            "timer",
            "duration",
            "milestone",
            "notes",
            "tags",
        )


class WeightSerializer(CoreModelSerializer, TaggableSerializer):
    class Meta:
        model = models.Weight
        fields = ("id", "child", "weight", "date", "notes", "tags")


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = get_user_model()
        fields = (
            "id",
            "username",
            "first_name",
            "last_name",
            "email",
            "is_staff",
        )
        extra_kwargs = {k: {"read_only": True} for k in fields}


class ProfileSerializer(serializers.ModelSerializer):
    user = UserSerializer(many=False)
    api_key = serializers.SerializerMethodField("get_api_key")

    def get_api_key(self, value):
        return self.instance.api_key().key

    class Meta:
        model = babybuddy_models.Settings
        fields = (
            "user",
            "language",
            "timezone",
            "api_key",
        )
        extra_kwargs = {k: {"read_only": True} for k in fields}


class WebhookEndpointSerializer(serializers.ModelSerializer):
    """
    A webhook endpoint as another application sets it up for itself.

    The secret can be written and is never read back. An application that
    chose its own sends it here; one that did not is given the generated
    secret once, in the response to the request that created the endpoint.
    """

    # DRF leaves the model's URLValidator out of a URLField and puts its own
    # in, which also allows ftp. Nothing is ever sent to ftp, so the model's
    # own validators apply here as they do in the admin area.
    url = serializers.URLField(
        max_length=1000,
        validators=webhooks_models.WebhookEndpoint._meta.get_field("url").validators,
    )

    class Meta:
        model = webhooks_models.WebhookEndpoint
        fields = ("id", "name", "url", "secret", "active", "created", "last_delivery")
        read_only_fields = ("created", "last_delivery")
        extra_kwargs = {"secret": {"write_only": True, "min_length": 16}}


class CaregiverSerializer(serializers.ModelSerializer):
    """
    A caregiver account, created and managed without the admin area.

    Only the fields a caregiver account needs are here. The role cannot be
    changed through them: there is no staff or superuser flag and no groups
    field, so an account made here stays a caregiver whatever is sent later.

    The account is used through its API key, which is returned once, when the
    account is created. It has no password unless an email address is given,
    in which case it is created with one nobody knows so that the reset flow
    can reach it and its owner can set a real one.
    """

    access_expires = serializers.DateTimeField(
        source="settings.access_expires", required=False, allow_null=True
    )

    class Meta:
        model = get_user_model()
        fields = (
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "is_active",
            "access_expires",
        )

    def create(self, validated_data):
        expires = validated_data.pop("settings", {}).get("access_expires")
        # Django's reset form skips accounts whose password is unusable, so an
        # address on its own would never receive anything. With one the account
        # gets a usable password that nobody knows; without one it gets none.
        password = secrets.token_urlsafe(32) if validated_data.get("email") else None
        with transaction.atomic():
            user = get_user_model().objects.create_user(
                password=password, **validated_data
            )
            user.groups.add(
                Group.objects.get(name=settings.BABY_BUDDY["CAREGIVER_GROUP_NAME"])
            )
            user.settings.access_expires = expires
            user.settings.save()
        return user

    def update(self, instance, validated_data):
        user_settings = validated_data.pop("settings", {})
        with transaction.atomic():
            user = super().update(instance, validated_data)
            if user.email and not user.has_usable_password():
                # The address is what the reset flow needs, so an account that
                # gains one has to gain a usable password with it.
                user.set_password(secrets.token_urlsafe(32))
                user.save(update_fields=["password"])
            if "access_expires" in user_settings:
                user.settings.access_expires = user_settings["access_expires"]
                user.settings.save()
        return user
