# -*- coding: utf-8 -*-
import datetime

from django import forms
from django.forms import widgets
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from taggit.forms import TagField, TagWidgetMixin

from babybuddy.widgets import DateInput, DateTimeInput, TimeInput
from core import models
from core.models import Timer
from core.widgets import TagsEditor, ChildRadioSelect, PillRadioSelect


def set_initial_values(kwargs, form_type):
    """
    Sets initial value for add forms based on provided kwargs.

    :param kwargs: Keyword arguments.
    :param form_type: Class of the type of form being initialized.
    :return: Keyword arguments with updated "initial" values.
    """

    # Never update initial values for existing instance (e.g. edit operation).
    if kwargs.get("instance", None):
        kwargs.pop("child", None)
        kwargs.pop("timer", None)
        return kwargs

    # Add the "initial" kwarg if it does not already exist.
    if not kwargs.get("initial"):
        kwargs.update(initial={})

    # Set Child based on `child` kwarg or single Chile database.
    child_slug = kwargs.get("child", None)
    if child_slug:
        kwargs["initial"].update(
            {
                "child": models.Child.objects.filter(slug=child_slug).first(),
            }
        )
    elif models.Child.count() == 1:
        kwargs["initial"].update({"child": models.Child.objects.first()})

    # Set start and end time based on Timer from `timer` kwarg.
    timer_id = kwargs.get("timer", None)
    if timer_id:
        try:
            timer = models.Timer.objects.get(id=timer_id)
            kwargs["initial"].update(
                {"timer": timer, "start": timer.start, "end": timezone.now()}
            )
        except (Timer.DoesNotExist, ValueError, TypeError, OverflowError):
            pass

    # Set type and method values for Feeding instance based on last feed.
    if form_type == FeedingForm and "child" in kwargs["initial"]:
        last_feeding = (
            models.Feeding.objects.filter(child=kwargs["initial"]["child"])
            .order_by("end")
            .last()
        )
        if last_feeding:
            last_method = last_feeding.method
            last_feed_args = {"type": last_feeding.type}
            if last_method not in ["left breast", "right breast"]:
                last_feed_args["method"] = last_method
            kwargs["initial"].update(last_feed_args)

    # Set default "nap" value for Sleep instances.
    if form_type == SleepForm and "nap" not in kwargs["initial"]:
        try:
            start = timezone.localtime(kwargs["initial"]["start"]).time()
        except KeyError:
            start = timezone.localtime().time()
        nap = (
            models.Sleep.settings.nap_start_min
            <= start
            <= models.Sleep.settings.nap_start_max
        )
        kwargs["initial"].update({"nap": nap})

    # Remove custom kwargs, so they do not interfere with `super` calls.
    for key in ["child", "timer"]:
        try:
            kwargs.pop(key)
        except KeyError:
            pass

    return kwargs


class CoreModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", getattr(self, "user", None))
        # Set `timer_id` so the Timer can be consumed only after a successful save.
        self.timer_id = kwargs.get("timer", None)
        kwargs = set_initial_values(kwargs, type(self))
        super(CoreModelForm, self).__init__(*args, **kwargs)
        self.add_timer_field()

    def add_timer_field(self):
        """
        Add a read-only field with the name of the Timer being stopped.

        The form is usually opened from a Timer, so showing which Timer the
        entry belongs to makes it possible to identify the timer after the
        fact. The Timer is only a source of initial values, so the field is
        disabled and not used when the form is saved.
        """
        if not self.timer_id:
            return

        try:
            timer = models.Timer.objects.filter(id=self.timer_id).first()
        except (ValueError, TypeError, OverflowError):
            return

        if not timer:
            return

        self.fields["timer"] = forms.CharField(
            label=_("Timer"),
            required=False,
            disabled=True,
        )
        self.initial["timer"] = timer.title_with_child
        self.fields = self.move_after(self.fields, "timer", "child")

        if hasattr(self, "fieldsets"):
            self.fieldsets = [
                {
                    **fieldset,
                    "fields": self.move_after(fieldset["fields"], "timer", "child"),
                }
                for fieldset in self.fieldsets
            ]

    @staticmethod
    def move_after(fields, item, anchor):
        """Return the fields with `item` placed directly after `anchor`."""
        if item == anchor or anchor not in fields:
            return fields

        if isinstance(fields, dict):
            if item not in fields:
                return fields

            items = list(fields.items())
            entry = items.pop([key for key, _ in items].index(item))
            items.insert([key for key, _ in items].index(anchor) + 1, entry)
            return dict(items)

        fields = list(fields)
        if item in fields:
            fields.remove(item)
        fields.insert(fields.index(anchor) + 1, item)
        return fields

    def clean(self):
        cleaned_data = super().clean()
        if self.timer_id is not None:
            try:
                timer = models.Timer.objects.get(pk=self.timer_id)
            except (Timer.DoesNotExist, ValueError, TypeError, OverflowError):
                raise forms.ValidationError(_("This timer does not exist."))
            if self.user is None or not timer.can_be_consumed_by(self.user):
                raise PermissionDenied(
                    _("You do not have permission to consume timers.")
                )
        return cleaned_data

    @transaction.atomic
    def save(self, commit=True):
        instance = super().save(commit=False)
        if commit:
            timer = None
            if self.timer_id is not None:
                # Lock until the entry and its tags have been saved successfully.
                timer = (
                    models.Timer.objects.select_for_update()
                    .filter(pk=self.timer_id)
                    .first()
                )
                if timer is None:
                    raise forms.ValidationError(_("This timer no longer exists."))
                # The timer may have changed owner since validation.
                if self.user is None or not timer.can_be_consumed_by(self.user):
                    raise PermissionDenied(
                        _("You do not have permission to consume timers.")
                    )
            instance.save()
            self.save_m2m()
            if timer is not None:
                timer.stop()
        return instance

    @property
    def hydrated_fielsets(self):
        # for some reason self.fields returns defintions and not bound fields
        # so until i figure out a better way we can just create a dict here
        # https://github.com/django/django/blob/main/django/forms/forms.py#L52

        bound_field_dict = {}
        for field in self:
            bound_field_dict[field.name] = field

        hydrated_fieldsets = []

        for fieldset in self.fieldsets:
            hyrdrated_fieldset = {
                "layout": fieldset.get("layout", "default"),
                "layout_attrs": fieldset.get("layout_attrs", {}),
                "fields": [],
            }
            for field_name in fieldset["fields"]:
                hyrdrated_fieldset["fields"].append(bound_field_dict[field_name])

            hydrated_fieldsets.append(hyrdrated_fieldset)

        return hydrated_fieldsets


class HiddenTagWidget(TagWidgetMixin, forms.HiddenInput):
    """Hidden tag input that still renders tag names instead of object reprs."""


class TaggableModelForm(forms.ModelForm):
    tags = TagField(
        label=_("Tags"),
        widget=TagsEditor,
        required=False,
        strip=True,
        help_text=_(
            "Click on the tags to add (+) or remove (-) tags or use the text editor to create new tags."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.user is None or not self.user.has_perm("core.change_tag"):
            # Without `change_tag` the field stays in the form (the layout and
            # the preserved value depend on it) but is only a carrier for the
            # current tag names, which `clean_tags` accepts unchanged.
            self.fields["tags"].widget = HiddenTagWidget()
            self.fields["tags"].help_text = None

    def clean_tags(self):
        current = list(self.instance.tags.names()) if self.instance.pk else []
        if self.add_prefix("tags") not in self.data:
            return current
        tags = self.cleaned_data["tags"]
        models.Tag.check_assignment_permissions(self.user, tags, current)
        return tags


class BMIForm(CoreModelForm, TaggableModelForm):
    fieldsets = [
        {
            "fields": ["child", "bmi", "date"],
            "layout": "required",
        },
        {"fields": ["notes", "tags"], "layout": "advanced"},
    ]

    class Meta:
        model = models.BMI
        fields = ["child", "bmi", "date", "notes", "tags"]
        widgets = {
            "child": ChildRadioSelect,
            "date": DateInput(),
            "notes": forms.Textarea(attrs={"rows": 5}),
        }


class BottleFeedingForm(CoreModelForm, TaggableModelForm):
    fieldsets = [
        {"fields": ["child", "type", "start", "amount"], "layout": "required"},
        {"fields": ["notes", "tags"], "layout": "advanced"},
    ]

    def clean(self):
        cleaned_data = super().clean()
        if "start" in cleaned_data:
            self.instance.end = cleaned_data["start"]
        return cleaned_data

    def save(self, commit=True):
        self.instance.method = "bottle"
        self.instance.end = self.instance.start
        return super().save(commit=commit)

    class Meta:
        model = models.Feeding
        fields = ["child", "start", "type", "amount", "notes", "tags"]
        widgets = {
            "child": ChildRadioSelect,
            "start": DateTimeInput(),
            "type": PillRadioSelect(),
            "notes": forms.Textarea(attrs={"rows": 5}),
        }


class ChildForm(forms.ModelForm):
    class Meta:
        model = models.Child
        fields = ["first_name", "last_name", "birth_date", "birth_time", "due_date"]
        if settings.BABY_BUDDY["ALLOW_UPLOADS"]:
            fields.append("picture")
        widgets = {
            "birth_date": DateInput(),
            "birth_time": TimeInput(),
            "due_date": DateInput(),
        }


class ChildDeleteForm(forms.ModelForm):
    confirm_name = forms.CharField(max_length=511)

    class Meta:
        model = models.Child
        fields = []

    def clean_confirm_name(self):
        confirm_name = self.cleaned_data["confirm_name"]
        if confirm_name != str(self.instance):
            raise forms.ValidationError(
                _("Name does not match child name."), code="confirm_mismatch"
            )
        return confirm_name

    def save(self, commit=True):
        instance = self.instance
        self.instance.delete()
        return instance


class DiaperChangeForm(CoreModelForm, TaggableModelForm):
    fieldsets = [
        {"fields": ["child", "time"], "layout": "required"},
        {
            "fields": ["wet", "solid"],
            "layout": "choices",
            "layout_attrs": {"label": "Contents"},
        },
        {"fields": ["color", "amount"]},
        {"layout": "advanced", "fields": ["notes", "tags"]},
    ]

    class Meta:
        model = models.DiaperChange
        fields = ["child", "time", "wet", "solid", "color", "amount", "notes", "tags"]
        widgets = {
            "child": ChildRadioSelect(),
            "color": PillRadioSelect(),
            "time": DateTimeInput(),
            "notes": forms.Textarea(attrs={"rows": 5}),
        }


class FeedingForm(CoreModelForm, TaggableModelForm):
    fieldsets = [
        {"fields": ["child", "start", "end", "type", "method"], "layout": "required"},
        {"fields": ["amount"]},
        {"fields": ["notes", "tags"], "layout": "advanced"},
    ]

    class Meta:
        model = models.Feeding
        fields = ["child", "start", "end", "type", "method", "amount", "notes", "tags"]
        widgets = {
            "child": ChildRadioSelect,
            "start": DateTimeInput(),
            "end": DateTimeInput(),
            "type": PillRadioSelect(),
            "method": PillRadioSelect(),
            "notes": forms.Textarea(attrs={"rows": 5}),
        }


class HeadCircumferenceForm(CoreModelForm, TaggableModelForm):
    fieldsets = [
        {
            "fields": ["child", "head_circumference", "date"],
            "layout": "required",
        },
        {"fields": ["notes", "tags"], "layout": "advanced"},
    ]

    class Meta:
        model = models.HeadCircumference
        fields = ["child", "head_circumference", "date", "notes", "tags"]
        widgets = {
            "child": ChildRadioSelect,
            "date": DateInput(),
            "notes": forms.Textarea(attrs={"rows": 5}),
        }


class HeightForm(CoreModelForm, TaggableModelForm):
    fieldsets = [
        {
            "fields": ["child", "height", "date"],
            "layout": "required",
        },
        {"fields": ["notes", "tags"], "layout": "advanced"},
    ]

    class Meta:
        model = models.Height
        fields = ["child", "height", "date", "notes", "tags"]
        widgets = {
            "child": ChildRadioSelect,
            "date": DateInput(),
            "notes": forms.Textarea(attrs={"rows": 5}),
        }


class MedicationForm(CoreModelForm, TaggableModelForm):
    next_dose_interval = forms.DecimalField(
        label=_("Time Until Next Dosage"),
        required=False,
        min_value=0,
        initial=0,
        help_text=_("Optional: Hours until next dose can be given"),
    )

    fieldsets = [
        {
            "fields": [
                "child",
                "time",
                "next_dose_interval",
                "name",
                "dosage",
                "dosage_unit",
            ],
            "layout": "required",
        },
        {"fields": ["notes", "tags"], "layout": "advanced"},
    ]

    class Meta:
        model = models.Medication
        fields = [
            "child",
            "name",
            "dosage",
            "dosage_unit",
            "time",
            "next_dose_interval",
            "notes",
            "tags",
        ]
        widgets = {
            "child": ChildRadioSelect,
            "dosage_unit": PillRadioSelect(),
            "time": DateTimeInput(),
            "notes": forms.Textarea(attrs={"rows": 5}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Convert existing timedelta to hours for display
        if self.instance and self.instance.next_dose_interval:
            total_seconds = self.instance.next_dose_interval.total_seconds()
            self.initial["next_dose_interval"] = total_seconds / 3600

    def clean_next_dose_interval(self):
        hours = self.cleaned_data.get("next_dose_interval")
        if hours is not None and hours > 0:
            return timezone.timedelta(hours=float(hours))
        return None


class PumpingForm(CoreModelForm, TaggableModelForm):
    fieldsets = [
        {"fields": ["child", "start", "end"], "layout": "required"},
        {"fields": ["amount"]},
        {"fields": ["notes", "tags"], "layout": "advanced"},
    ]

    class Meta:
        model = models.Pumping
        fields = ["child", "start", "end", "amount", "notes", "tags"]
        widgets = {
            "child": ChildRadioSelect,
            "start": DateTimeInput(),
            "end": DateTimeInput(),
            "notes": forms.Textarea(attrs={"rows": 5}),
        }


class NoteForm(CoreModelForm, TaggableModelForm):
    class Meta:
        model = models.Note
        fields = ["child", "note", "time", "tags"]
        if settings.BABY_BUDDY["ALLOW_UPLOADS"]:
            fields.insert(2, "image")
        widgets = {
            "child": ChildRadioSelect,
            "time": DateTimeInput(),
        }


class SleepForm(CoreModelForm, TaggableModelForm):
    fieldsets = [
        {
            "fields": ["child", "start", "end", "nap"],
            "layout": "required",
        },
        {"fields": ["notes", "tags"], "layout": "advanced"},
    ]

    class Meta:
        model = models.Sleep
        fields = ["child", "start", "end", "nap", "notes", "tags"]
        widgets = {
            "child": ChildRadioSelect,
            "start": DateTimeInput(),
            "end": DateTimeInput(),
            "notes": forms.Textarea(attrs={"rows": 5}),
        }


class TagAdminForm(CoreModelForm):
    fieldsets = [
        {
            "fields": ["name", "color"],
            "layout": "required",
        }
    ]

    class Meta:
        model = models.Tag
        fields = ["name", "color"]
        readonly_fields = ["slug"]
        widgets = {
            "color": widgets.TextInput(
                attrs={"type": "color", "class": "form-control-color"}
            )
        }


class TemperatureForm(CoreModelForm, TaggableModelForm):
    fieldsets = [
        {
            "fields": ["child", "temperature", "time"],
            "layout": "required",
        },
        {"fields": ["notes", "tags"], "layout": "advanced"},
    ]

    class Meta:
        model = models.Temperature
        fields = ["child", "temperature", "time", "notes", "tags"]
        widgets = {
            "child": ChildRadioSelect,
            "time": DateTimeInput(),
            "notes": forms.Textarea(attrs={"rows": 5}),
        }


class TimerForm(CoreModelForm):
    class Meta:
        model = models.Timer
        fields = ["child", "name", "start"]
        widgets = {
            "child": ChildRadioSelect,
            "start": DateTimeInput(),
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user")
        super(TimerForm, self).__init__(*args, **kwargs)

    def save(self, commit=True):
        instance = super(TimerForm, self).save(commit=False)
        if instance.user_id is None:
            instance.user = self.user
            instance.save()
        else:
            # Editing a timer does not change its owner, including an owner
            # changed by someone else while the form was open.
            instance.save(update_fields=self._meta.fields)
        return instance


class TummyTimeForm(CoreModelForm, TaggableModelForm):
    fieldsets = [
        {"fields": ["child", "start", "end"], "layout": "required"},
        {"fields": ["milestone"]},
        {"fields": ["notes", "tags"], "layout": "advanced"},
    ]

    class Meta:
        model = models.TummyTime
        fields = ["child", "start", "end", "milestone", "notes", "tags"]
        widgets = {
            "child": ChildRadioSelect,
            "start": DateTimeInput(),
            "end": DateTimeInput(),
            "notes": forms.Textarea(attrs={"rows": 5}),
        }


class WeightForm(CoreModelForm, TaggableModelForm):
    fieldsets = [
        {
            "fields": ["child", "weight", "date"],
            "layout": "required",
        },
        {"fields": ["notes", "tags"], "layout": "advanced"},
    ]

    class Meta:
        model = models.Weight
        fields = ["child", "weight", "date", "notes", "tags"]
        widgets = {
            "child": ChildRadioSelect,
            "date": DateInput(),
            "notes": forms.Textarea(attrs={"rows": 5}),
        }


#: Entry kinds with a start/end period supported by the bulk time adjustment.
#: Overlap validation is per kind, so each kind is planned independently.
#: Tuple fields: choice key, model, the permission required to move the kind.
TIME_SHIFT_ENTRIES = (
    ("feeding", models.Feeding, "core.change_feeding"),
    ("sleep", models.Sleep, "core.change_sleep"),
    ("pumping", models.Pumping, "core.change_pumping"),
    ("tummytime", models.TummyTime, "core.change_tummytime"),
)


class TimeShiftForm(forms.Form):
    """
    Parameters for moving a child's period-based entries by a fixed number of
    hours. The form itself never saves; ``plan`` previews the move and
    ``execute`` applies it atomically after re-checking the preview.
    """

    child = forms.ModelChoiceField(
        label=_("Child"),
        queryset=models.Child.objects.all(),
        widget=ChildRadioSelect,
        empty_label=None,
    )
    start_date = forms.DateField(label=_("Start date"), widget=DateInput())
    end_date = forms.DateField(label=_("End date"), widget=DateInput())
    hours = forms.IntegerField(
        label=_("Shift (hours)"),
        min_value=-23,
        max_value=23,
        widget=forms.NumberInput(),
        help_text=_(
            "Entries are moved by this many hours. Use a positive value to "
            "move entries later and a negative value to move them earlier."
        ),
    )
    entry_types = forms.MultipleChoiceField(
        label=_("Entry types"),
        widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
        choices=(),
    )

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        # Only kinds the user may actually change are offered.
        self.allowed = {
            key: model
            for key, model, permission in TIME_SHIFT_ENTRIES
            if user is None or user.has_perm(permission)
        }
        self.fields["entry_types"].choices = [
            (key, model._meta.verbose_name) for key, model in self.allowed.items()
        ]
        # Everything the user may adjust is selected by default.
        if not self.is_bound:
            self.initial["entry_types"] = list(self.allowed)

    def clean(self):
        cleaned_data = super().clean()
        start = cleaned_data.get("start_date")
        end = cleaned_data.get("end_date")
        if start and end and end < start:
            self.add_error(
                "end_date",
                forms.ValidationError(
                    _("End date must be on or after the start date."),
                    code="date_order",
                ),
            )
        if cleaned_data.get("hours") == 0:
            self.add_error(
                "hours",
                forms.ValidationError(
                    _("A shift of zero hours changes nothing."),
                    code="zero_shift",
                ),
            )
        selected = cleaned_data.get("entry_types")
        if selected and not set(selected).issubset(self.allowed):
            self.add_error(
                "entry_types",
                forms.ValidationError(
                    _("You may not adjust this entry type."),
                    code="forbidden_type",
                ),
            )
        return cleaned_data

    def shift_delta(self):
        return datetime.timedelta(hours=self.cleaned_data["hours"])

    def range_bounds(self):
        """Return the aware lower/upper datetimes of the selected dates."""
        current_timezone = timezone.get_current_timezone()
        lower = timezone.make_aware(
            datetime.datetime.combine(
                self.cleaned_data["start_date"], datetime.time.min
            ),
            current_timezone,
        )
        upper = timezone.make_aware(
            datetime.datetime.combine(
                self.cleaned_data["end_date"], datetime.time.max
            ),
            current_timezone,
        )
        return lower, upper

    def selected_keys(self):
        return [
            key
            for key, _model, _permission in TIME_SHIFT_ENTRIES
            if key in self.cleaned_data.get("entry_types", [])
        ]

    def plan(self):
        """
        Build a preview of the shift: the entries that would move, any entry
        whose new period would land in the future, and every overlap the move
        would create against an entry outside the selection.

        Entries moving together never collide with each other: a uniform
        translation preserves their relative intervals.
        """
        child = self.cleaned_data["child"]
        delta = self.shift_delta()
        lower, upper = self.range_bounds()
        now = timezone.now()

        groups = []
        conflicts = []
        future = []

        for key in self.selected_keys():
            model = self.allowed[key]
            label = model._meta.verbose_name
            entries = list(
                model.objects.filter(
                    child=child, start__gte=lower, start__lte=upper
                ).order_by("start")
            )
            # Everything that keeps its current position is a potential
            # obstacle, including entries just outside the date range.
            stationary = model.objects.filter(child=child).exclude(
                pk__in=[entry.pk for entry in entries]
            )
            rows = []
            for entry in entries:
                new_start = entry.start + delta
                new_end = entry.end + delta
                row = {
                    "entry": entry,
                    "new_start": new_start,
                    "new_end": new_end,
                }
                rows.append(row)
                if new_start > now or new_end > now:
                    future.append({"label": label, **row})
                obstacle = (
                    stationary.filter(start__lt=new_end, end__gt=new_start)
                    .order_by("start")
                    .first()
                )
                if obstacle:
                    conflicts.append(
                        {
                            "label": label,
                            "moving": entry,
                            "new_start": new_start,
                            "new_end": new_end,
                            "moving_url": reverse(
                                f"core:{model.model_name}-update", args=[entry.pk]
                            ),
                            "obstacle": obstacle,
                            "obstacle_url": reverse(
                                f"core:{model.model_name}-update",
                                args=[obstacle.pk],
                            ),
                        }
                    )
            groups.append({"key": key, "label": label, "rows": rows})

        return {
            "child": child,
            "delta": delta,
            "lower": lower,
            "upper": upper,
            "hours": self.cleaned_data["hours"],
            "groups": groups,
            "conflicts": conflicts,
            "future": future,
            "total": sum(len(group["rows"]) for group in groups),
        }

    def lock_rows(self):
        """
        Lock every selected-kind row of the child for the duration of the
        transaction, so a concurrent edit cannot invalidate the preview
        between the check and the write.
        """
        child = self.cleaned_data["child"]
        for key in self.selected_keys():
            model = self.allowed[key]
            list(
                model.objects.select_for_update()
                .filter(child=child)
                .order_by("pk")
            )

    def apply(self, plan):
        """Shift the planned rows. Caller is responsible for locking and
        re-checking; save per row so durations refresh and update webhooks
        fire."""
        moved = 0
        for group in plan["groups"]:
            model = self.allowed[group["key"]]
            for row in group["rows"]:
                entry = model.objects.select_for_update().get(pk=row["entry"].pk)
                entry.start = row["new_start"]
                entry.end = row["new_end"]
                entry.save(update_fields=["start", "end", "duration"])
                moved += 1
        return moved

    def execute(self):
        """
        Re-plan against the current database state and apply the shift in one
        transaction. Any future entry or overlap rolls back every row.
        """
        with transaction.atomic():
            self.lock_rows()
            plan = self.plan()
            if plan["total"] == 0:
                raise forms.ValidationError(
                    _(
                        "No entries of the selected types start in this date "
                        "range."
                    ),
                    code="empty_selection",
                )
            if plan["future"]:
                raise forms.ValidationError(
                    _(
                        "The shift would move one or more entries into the "
                        "future. Adjust the shift and try again."
                    ),
                    code="future_period",
                )
            if plan["conflicts"]:
                raise forms.ValidationError(
                    _(
                        "The shift would overlap existing entries. Resolve the "
                        "conflicts listed below and try again."
                    ),
                    code="period_intersection",
                )
            self.apply(plan)
        return plan
