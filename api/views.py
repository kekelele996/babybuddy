# -*- coding: utf-8 -*-
import hashlib
import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.urls import reverse

from rest_framework import mixins, permissions, status, viewsets, views
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.schemas.openapi import AutoSchema

from core import models
from babybuddy import models as babybuddy_models
from webhooks import models as webhooks_models
from webhooks import signals as webhooks_signals

from api import models as api_models

from . import serializers, filters


class BMIViewSet(viewsets.ModelViewSet):
    queryset = models.BMI.objects.all()
    serializer_class = serializers.BMISerializer
    filterset_fields = ("child", "date")
    ordering_fields = ("child", "date")
    ordering = "-date"

    def get_view_name(self):
        """
        Gets the view name without changing the case of the model verbose name.
        """
        name = models.BMI._meta.verbose_name
        if self.suffix:
            name += " " + self.suffix
        return name


class ChildViewSet(viewsets.ModelViewSet):
    queryset = models.Child.objects.all()
    serializer_class = serializers.ChildSerializer
    lookup_field = "slug"
    filterset_fields = (
        "id",
        "first_name",
        "last_name",
        "slug",
        "birth_date",
        "birth_time",
    )
    ordering_fields = ("birth_date", "birth_time", "first_name", "last_name", "slug")
    ordering = ["-birth_date", "-birth_time"]


class DiaperChangeViewSet(viewsets.ModelViewSet):
    queryset = models.DiaperChange.objects.all()
    serializer_class = serializers.DiaperChangeSerializer
    filterset_class = filters.DiaperChangeFilter
    ordering_fields = ("amount", "time")
    ordering = "-time"


class FeedingViewSet(viewsets.ModelViewSet):
    queryset = models.Feeding.objects.all()
    serializer_class = serializers.FeedingSerializer
    filterset_class = filters.FeedingFilter
    ordering_fields = ("amount", "duration", "end", "start")
    ordering = "-end"


class HeadCircumferenceViewSet(viewsets.ModelViewSet):
    queryset = models.HeadCircumference.objects.all()
    serializer_class = serializers.HeadCircumferenceSerializer
    filterset_fields = ("child", "date")
    ordering_fields = ("date", "head_circumference")
    ordering = "-date"


class HeightViewSet(viewsets.ModelViewSet):
    queryset = models.Height.objects.all()
    serializer_class = serializers.HeightSerializer
    filterset_fields = ("child", "date")
    ordering_fields = ("date", "height")
    ordering = "-date"


class MedicationViewSet(viewsets.ModelViewSet):
    queryset = models.Medication.objects.all()
    serializer_class = serializers.MedicationSerializer
    filterset_class = filters.MedicationFilter
    ordering_fields = ("time", "name", "dosage")
    ordering = "-time"

    def get_view_name(self):
        # Use model's verbose_name for consistency with user-facing strings
        name = self.queryset.model._meta.verbose_name
        suffix = getattr(self, "suffix", None)
        if suffix:
            name = f"{name} {suffix}"
        return name


class NoteViewSet(viewsets.ModelViewSet):
    queryset = models.Note.objects.all()
    serializer_class = serializers.NoteSerializer
    filterset_class = filters.NoteFilter
    ordering_fields = "time"
    ordering = "-time"


class PumpingViewSet(viewsets.ModelViewSet):
    queryset = models.Pumping.objects.all()
    serializer_class = serializers.PumpingSerializer
    filterset_class = filters.PumpingFilter
    ordering_fields = ("amount", "duration", "end", "start")
    ordering = "-end"


class SleepViewSet(viewsets.ModelViewSet):
    queryset = models.Sleep.objects.all()
    serializer_class = serializers.SleepSerializer
    filterset_class = filters.SleepFilter
    ordering_fields = ("duration", "end", "start")
    ordering = "-end"


class TagViewSet(viewsets.ModelViewSet):
    queryset = models.Tag.objects.all()
    serializer_class = serializers.TagSerializer
    lookup_field = "slug"
    filterset_fields = ("last_used", "name")
    ordering_fields = ("last_used", "name", "slug")
    ordering = "name"


class TemperatureViewSet(viewsets.ModelViewSet):
    queryset = models.Temperature.objects.all()
    serializer_class = serializers.TemperatureSerializer
    filterset_class = filters.TemperatureFilter
    ordering_fields = ("temperature", "time")
    ordering = "-time"


class TimerViewSet(viewsets.ModelViewSet):
    queryset = models.Timer.objects.all()
    serializer_class = serializers.TimerSerializer
    filterset_class = filters.TimerFilter
    ordering_fields = ("duration", "end", "start")
    ordering = "-start"

    @action(detail=True, methods=["patch"])
    def restart(self, request, pk=None):
        timer = self.get_object()
        timer.restart()
        return Response(self.serializer_class(timer).data)


class TummyTimeViewSet(viewsets.ModelViewSet):
    queryset = models.TummyTime.objects.all()
    serializer_class = serializers.TummyTimeSerializer
    filterset_class = filters.TummyTimeFilter
    ordering_fields = ("duration", "end", "start")
    ordering = "-start"


class WeightViewSet(viewsets.ModelViewSet):
    queryset = models.Weight.objects.all()
    serializer_class = serializers.WeightSerializer
    filterset_fields = ("child", "date")
    ordering_fields = ("date", "weight")
    ordering = "-date"


class BatchViewSet(
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """
    Write a batch of feeding and sleep entries as one submission.

    Every entry is validated first and the response names any that fail by
    their index; only a batch without an invalid entry is written, and it is
    written inside one transaction, so either all entries land or none do.

    The client supplies an idempotency key. A batch sent again with the same
    key is not written a second time: the stored result of the first
    submission is returned instead. That keeps a retry after a network
    timeout from creating the entries, consuming their timers, and sending
    their announcements twice. A key reused with a different payload is a
    client error (409).

    A successful batch announces itself once per active endpoint
    ("batch.created"), rather than once per entry.
    """

    serializer_class = serializers.BatchSerializer
    permission_classes = [permissions.IsAuthenticated]

    #: Adding an entry of a kind needs the same permission as posting one.
    ADD_PERMISSIONS = {
        "feeding": "core.add_feeding",
        "sleep": "core.add_sleep",
    }

    def get_queryset(self):
        return api_models.Batch.objects.filter(user=self.request.user)

    def create(self, request, *args, **kwargs):
        # The envelope alone is checked first: the idempotency lookup has to
        # precede entry validation, since a retry of a batch that consumed a
        # timer no longer has that timer to validate against.
        envelope = serializers.BatchEnvelopeSerializer(data=request.data)
        envelope.is_valid(raise_exception=True)
        idempotency_key = envelope.validated_data["idempotency_key"]
        fingerprint = self._fingerprint(request.data["entries"])

        with transaction.atomic():
            existing = self._get_existing(idempotency_key)
            if existing is not None:
                return self._replay(existing, fingerprint)

            # The key is new, so now every entry has to prove it is valid.
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            prepared = serializer.validated_data["prepared"]

            for kind in {kind for _, kind, _ in prepared}:
                if not request.user.has_perm(self.ADD_PERMISSIONS[kind]):
                    raise PermissionDenied(
                        "You do not have permission to add {} entries.".format(kind)
                    )

            try:
                # The unique constraint is the real guard against two requests
                # submitting the same key at the same time; select_for_update
                # cannot lock a row that does not exist yet.
                with transaction.atomic():
                    batch = api_models.Batch.objects.create(
                        user=request.user,
                        idempotency_key=idempotency_key,
                        fingerprint=fingerprint,
                    )
            except IntegrityError:
                return self._replay(self._get_existing(idempotency_key), fingerprint)

            results = self._save_entries(prepared)
            batch.result = self._response_body(batch, results)
            batch.save(update_fields=["result"])
            # Queued last and inside the transaction, so the announcement
            # commits exactly when the entries and the stored result do.
            webhooks_signals.record_event(batch, "created")

        return Response(
            batch.result,
            status=status.HTTP_201_CREATED,
            headers={"Location": reverse("api:batch-detail", args=[batch.pk])},
        )

    def retrieve(self, request, *args, **kwargs):
        return Response(self.get_object().result)

    def _get_existing(self, idempotency_key):
        return (
            api_models.Batch.objects.select_for_update()
            .filter(user=self.request.user, idempotency_key=idempotency_key)
            .first()
        )

    def _replay(self, batch, fingerprint):
        """Answer a repeated submission with the result of the first one."""
        if batch is None:
            # The insert lost a race but the winning row is not readable yet.
            return Response(
                {
                    "idempotency_key": [
                        "This key is already being submitted; retry the request."
                    ]
                },
                status=status.HTTP_409_CONFLICT,
            )
        if batch.fingerprint != fingerprint:
            return Response(
                {
                    "idempotency_key": [
                        "This key was already used for a different batch."
                    ]
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response(batch.result, status=status.HTTP_201_CREATED)

    def _save_entries(self, prepared):
        context = self.get_serializer_context()
        results = []
        # The entries are not announced one by one, and a timer they consume
        # is not announced as deleted: the batch event below is the only
        # announcement this submission makes.
        with webhooks_signals.suppress_events():
            for index, kind, entry_serializer in prepared:
                instance = entry_serializer.save()
                data = type(entry_serializer)(instance, context=context).data
                results.append({"kind": kind, **data})
        return results

    @staticmethod
    def _response_body(batch, results):
        return {
            "id": batch.pk,
            "idempotency_key": batch.idempotency_key,
            "results": results,
        }

    @staticmethod
    def _fingerprint(entries):
        body = json.dumps(entries, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


class ProfileView(views.APIView):
    schema = AutoSchema(operation_id_base="CurrentProfile")

    action = "get"
    basename = "profile"

    queryset = babybuddy_models.Settings.objects.all()
    serializer_class = serializers.ProfileSerializer

    def get(self, request):
        settings = get_object_or_404(
            babybuddy_models.Settings.objects, user=request.user
        )
        serializer = self.serializer_class(settings)
        return Response(serializer.data)


class WebhookEndpointViewSet(viewsets.ModelViewSet):
    queryset = webhooks_models.WebhookEndpoint.objects.all()
    serializer_class = serializers.WebhookEndpointSerializer
    filterset_fields = ("active",)
    ordering_fields = ("name", "created", "last_delivery")
    ordering = "name"

    def create(self, request, *args, **kwargs):
        """
        Create an endpoint and return its secret this one time, since the
        secret is never part of a response afterwards.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        endpoint = serializer.save()
        data = dict(serializer.data, secret=endpoint.secret)
        return Response(
            data,
            status=status.HTTP_201_CREATED,
            headers=self.get_success_headers(serializer.data),
        )


class CaregiverViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """
    Caregiver accounts only. Accounts in any other role, and anything with
    staff or superuser status, are not reachable here at all. There is no
    delete; access is withdrawn by setting `is_active` to false, the same way
    the user guide already tells an administrator to withdraw it.
    """

    serializer_class = serializers.CaregiverSerializer
    filterset_fields = ("is_active",)
    ordering_fields = ("username", "date_joined")
    ordering = "username"

    def get_queryset(self):
        return (
            get_user_model()
            .objects.filter(
                groups__name=settings.BABY_BUDDY["CAREGIVER_GROUP_NAME"],
                is_staff=False,
                is_superuser=False,
            )
            .select_related("settings")
        )

    def create(self, request, *args, **kwargs):
        """
        Create a caregiver and return their API key this one time.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        data = dict(serializer.data, api_key=user.settings.api_key().key)
        return Response(
            data,
            status=status.HTTP_201_CREATED,
            headers=self.get_success_headers(serializer.data),
        )
