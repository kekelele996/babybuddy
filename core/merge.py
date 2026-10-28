# -*- coding: utf-8 -*-
"""
Merge one Child profile into another.

A merge moves every care entry (feedings, sleep, measurements, notes, ...),
timers and (when needed) the profile picture from a "source" child to a
"target" child, records the source's former slug as a `MergedChild` alias and
then deletes the source. Tags and note images live on the entries themselves,
so they follow the entries without extra work.

Everything happens in a single transaction: if any step fails, both profiles
are left exactly as they were.
"""

from django.db import transaction
from django.urls import reverse
from django.utils import formats

from core import models
from core.models import _format_dt

#: Entries with a start/end range; two ranges of the same type may overlap.
PERIOD_MODELS = (
    models.Feeding,
    models.Sleep,
    models.TummyTime,
    models.Pumping,
)

#: Entries described by a single point in time.
TIME_MODELS = (
    models.DiaperChange,
    models.Note,
    models.Temperature,
    models.Medication,
)

#: Measurements described by a single date.
DATE_MODELS = (
    models.Weight,
    models.Height,
    models.BMI,
    models.HeadCircumference,
)

ENTRY_MODELS = PERIOD_MODELS + TIME_MODELS + DATE_MODELS

#: URL update-view names where they differ from `model_name`.
UPDATE_URL_NAMES = {
    models.HeadCircumference: "head-circumference-update",
}

PERIOD_KIND = "period"
TIME_KIND = "time"
DATE_KIND = "date"


def _entry_kind(model):
    if model in PERIOD_MODELS:
        return PERIOD_KIND
    if model in TIME_MODELS:
        return TIME_KIND
    return DATE_KIND


def _entry_label(entry, kind):
    if kind == PERIOD_KIND:
        return "{} - {}".format(_format_dt(entry.start), _format_dt(entry.end))
    if kind == TIME_KIND:
        return _format_dt(entry.time)
    return formats.date_format(entry.date, "SHORT_DATE_FORMAT")


def _entry_update_url(model, entry):
    url_name = UPDATE_URL_NAMES.get(model, "{}-update".format(model.model_name))
    return reverse("core:{}".format(url_name), args=[entry.pk])


def _find_conflict(model, kind, entry, target):
    """Return an existing entry of `target` colliding with `entry`, if any."""
    target_entries = model.objects.filter(child=target)
    if kind == PERIOD_KIND:
        # Mirror `validate_unique_period`: touching at an endpoint is allowed.
        return target_entries.filter(start__lt=entry.end, end__gt=entry.start).first()
    if kind == TIME_KIND:
        return target_entries.filter(time=entry.time).first()
    return target_entries.filter(date=entry.date).first()


def build_merge_preview(source, target):
    """
    Build the data shown on the merge confirmation page: per entry type, how
    many source entries will move and which of them collide (same type, same
    date/time or overlapping period) with entries already on the target.
    """
    sections = []
    for model in ENTRY_MODELS:
        kind = _entry_kind(model)
        source_entries = list(model.objects.filter(child=source))
        conflicts = []
        for entry in source_entries:
            conflicting = _find_conflict(model, kind, entry, target)
            if conflicting:
                conflicts.append(
                    {
                        "source_label": _entry_label(entry, kind),
                        "target_label": _entry_label(conflicting, kind),
                        "source_url": _entry_update_url(model, entry),
                        "target_url": _entry_update_url(model, conflicting),
                    }
                )
        sections.append(
            {
                "model_name": model.model_name,
                "verbose_name": model._meta.verbose_name,
                "kind": kind,
                "count": len(source_entries),
                "conflicts": conflicts,
            }
        )

    timers = models.Timer.objects.filter(child=source)
    return {
        "source": source,
        "target": target,
        "sections": sections,
        "entry_total": sum(section["count"] for section in sections),
        "conflict_total": sum(len(section["conflicts"]) for section in sections),
        "timer_total": timers.count(),
        "timer_active_total": timers.filter(active=True).count(),
        "picture_will_copy": bool(source.picture and not target.picture),
    }


def merge_children(source, target):
    """
    Move all entries, timers and (if needed) the picture from `source` to
    `target`, leave a slug alias for old links, and delete `source`.

    Raises ValueError if both arguments are the same child. Any database
    failure rolls the whole operation back.
    :return: the surviving target Child.
    """
    if source.pk == target.pk:
        raise ValueError("A child cannot be merged into itself.")

    with transaction.atomic():
        # Lock both rows so a concurrent edit/delete cannot invalidate the
        # merge while it is running, and reload them so fields changed on
        # other instances (e.g. the picture) are current.
        locked = list(
            models.Child.objects.select_for_update().filter(
                pk__in=[source.pk, target.pk]
            )
        )
        if len(locked) != 2:
            raise models.Child.DoesNotExist("Both children must exist when merging.")
        source = next(child for child in locked if child.pk == source.pk)
        target = next(child for child in locked if child.pk == target.pk)

        # QuerySet.update bypasses save()/signals, so entries (and their tags
        # and images, which reference the entry rows) are simply reassigned
        # without a storm of webhook events.
        for model in ENTRY_MODELS:
            model.objects.filter(child=source).update(child=target)
        models.Timer.objects.filter(child=source).update(child=target)

        # Reuse the source's picture file for the target if it has none. Row
        # deletion never removes the file from storage, so the target keeps
        # working after the source is deleted.
        if source.picture and not target.picture:
            target.picture = source.picture.name
            target.save(update_fields=["picture"])

        # Aliases that previously pointed at the source now resolve one hop
        # further, to the new surviving child.
        models.MergedChild.objects.filter(child=source).update(child=target)
        models.MergedChild.objects.create(
            slug=source.slug, name=str(source), child=target
        )

        source.delete()

    return target
