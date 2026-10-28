# -*- coding: utf-8 -*-
"""
Lifecycle management for user-uploaded images.

Uploaded images live in the configured file storage (local disk or S3) and are
only referenced from the database by their storage-relative name. The database
does not own the files, so simply deleting or replacing a row leaves the old
file behind. The helpers here close that gap:

* :func:`validate_uploaded_image` makes sure a replacement image is fully
  readable *before* the database row is pointed at it;
* :func:`release_image_file` deletes a file once nothing references it any
  more, checking *all* image fields (so a file still used elsewhere is never
  deleted early);
* deletions that fail (e.g. a transient storage outage) are recorded with
  :class:`core.models.PendingFileDeletion` and can be retried with
  :func:`retry_pending_deletions` or the ``cleanup_images`` management command;
* :func:`clear_dangling_references` repairs rows that point at files missing
  from storage (e.g. after a data restore), and :func:`find_orphan_files` /
  :func:`delete_orphan_files` remove files no row points at.
"""

import logging

from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)

#: (app_label, model_name, field_name) for every model field that stores an
#: uploaded image. Used for reference counting, so every image field must be
#: listed here.
IMAGE_FIELDS = (
    ("core", "Child", "picture"),
    ("core", "Note", "image"),
)

#: Returned by :func:`release_image_file`.
RELEASED = "released"
STILL_IN_USE = "still_in_use"
DEFERRED = "deferred"


def validate_uploaded_image(value):
    """
    Confirm that an uploaded image can actually be decoded, not merely that it
    has an image content type.

    Django's default ``ImageField`` validation only runs ``Image.verify()``,
    which checks the file header but never decodes the pixel data; a truncated
    upload can therefore pass validation and only fail later, when a thumbnail
    is generated. ``load()`` decodes the whole file, so a replacement image is
    known to be usable before the old one is touched.
    """
    if not value:
        return
    try:
        from PIL import Image

        image = Image.open(value)
        image.verify()
        # ``verify()`` leaves the image object unusable, so reopen it before
        # fully decoding it.
        value.seek(0)
        image = Image.open(value)
        image.load()
    except Exception:
        raise ValidationError(
            _(
                "Upload a valid image. The file you uploaded was either not "
                "an image or a corrupted image."
            ),
            code="invalid_image",
        )
    finally:
        # Other consumers (and Django's own validators) expect the stream at
        # its start.
        try:
            value.seek(0)
        except (AttributeError, ValueError, OSError):
            pass


def iter_image_fields():
    """Yield ``(model, field)`` pairs for every registered image field."""
    for app_label, model_name, field_name in IMAGE_FIELDS:
        model = apps.get_model(app_label, model_name)
        yield model, model._meta.get_field(field_name)


def is_image_referenced(name):
    """
    Return True if any image field still stores ``name``.

    The check spans every image-bearing model, so two rows sharing one stored
    file (possible after a restore or a direct copy) keep each other's file
    alive until the last reference goes away.
    """
    if not name:
        return False
    for model, field in iter_image_fields():
        if model.objects.filter(**{field.name: name}).exists():
            return True
    return False


def _delete_file_if_present(name):
    """Delete ``name`` from storage. A missing file counts as success."""
    if not default_storage.exists(name):
        return
    try:
        default_storage.delete(name)
    except FileNotFoundError:
        # Another process may have removed it between the check and the
        # delete. The desired end state (no file) already holds.
        pass


def release_image_file(name, reason=""):
    """
    Delete an uploaded image once it is no longer referenced.

    Intended to run *after* the triggering transaction has committed, usually
    through ``transaction.on_commit``: that ordering guarantees that a replaced
    or deleted row no longer references the file when the reference count is
    taken.

    A deletion failure never reaches the caller as an exception. The path is
    persisted in :class:`core.models.PendingFileDeletion` instead, so it can be
    retried later.
    """
    if not name:
        return STILL_IN_USE
    pending_model = apps.get_model("core", "PendingFileDeletion")
    if is_image_referenced(name):
        logger.debug("Keeping uploaded image %s in use.", name)
        return STILL_IN_USE
    try:
        _delete_file_if_present(name)
    except Exception as exc:
        logger.exception("Failed to delete uploaded image %s.", name)
        pending, _created = pending_model.objects.get_or_create(
            path=name, defaults={"last_error": str(exc)[:1000]}
        )
        if not _created:
            pending.last_error = str(exc)[:1000]
            pending.save(update_fields=["last_error", "updated_at"])
        return DEFERRED
    # A previous attempt may have recorded the path; it is now gone.
    pending_model.objects.filter(path=name).delete()
    logger.debug("Deleted uploaded image %s (%s).", name, reason or "released")
    return RELEASED


def retry_pending_deletions(queryset=None):
    """
    Retry previously deferred file deletions.

    :returns: ``(released, deferred, kept)`` counts. ``kept`` counts entries
    that were dropped because the file had become referenced again (e.g. the
    referencing data was restored before the retry ran).
    """
    pending_model = apps.get_model("core", "PendingFileDeletion")
    pending = queryset if queryset is not None else pending_model.objects.all()
    released = deferred = kept = 0
    for entry in pending.order_by("created_at"):
        if is_image_referenced(entry.path):
            entry.delete()
            kept += 1
            continue
        entry.attempts += 1
        try:
            _delete_file_if_present(entry.path)
        except Exception as exc:
            entry.last_error = str(exc)[:1000]
            entry.save(update_fields=["attempts", "last_error", "updated_at"])
            deferred += 1
        else:
            entry.delete()
            released += 1
    return released, deferred, kept


def list_dangling_references():
    """
    Return ``[(model, field_name, pk, name), ...]`` for rows whose image is
    set but absent from storage (e.g. after restoring a database without the
    media files).
    """
    dangling = []
    for model, field in iter_image_fields():
        qs = model.objects.exclude(**{field.name: ""}).only("pk", field.name)
        for instance in qs:
            file_ref = getattr(instance, field.name)
            if not file_ref.name:
                continue
            try:
                exists = file_ref.storage.exists(file_ref.name)
            except Exception:
                # Storage is unreachable; do not treat this as dangling.
                logger.exception(
                    "Could not check storage for %s %s image.",
                    model.__name__,
                    instance.pk,
                )
                continue
            if not exists:
                dangling.append((model, field.name, instance.pk, file_ref.name))
    return dangling


def clear_dangling_references():
    """
    Blank out image fields whose files do not exist in storage.

    Returns the number of repaired rows. Bulk ``update()`` is used deliberately
    so that no save signals fire for a file that is already gone.
    """
    groups = {}
    for model, field_name, pk, _name in list_dangling_references():
        groups.setdefault((model, field_name), []).append(pk)
    repaired = 0
    for (model, field_name), pks in groups.items():
        model.objects.filter(pk__in=pks).update(**{field_name: ""})
        repaired += len(pks)
    return repaired


def _list_storage_files(prefix):
    """Recursively list storage-relative file names under ``prefix``."""
    names = []
    try:
        directories, files = default_storage.listdir(prefix)
    except FileNotFoundError:
        return names
    except Exception:
        logger.exception("Could not list storage prefix %s.", prefix)
        return names
    for file_name in files:
        names.append(f"{prefix}{file_name}")
    for directory in directories:
        names.extend(_list_storage_files(f"{prefix}{directory}/"))
    return names


def find_orphan_files():
    """
    Return storage-relative names present in an image upload directory that no
    row references any more (leftovers from interrupted deletions or restores).
    """
    referenced = set()
    prefixes = []
    for model, field in iter_image_fields():
        prefixes.append(field.upload_to)
        referenced.update(
            name
            for name in model.objects.exclude(**{field.name: ""}).values_list(
                field.name, flat=True
            )
            if name
        )
    pending_model = apps.get_model("core", "PendingFileDeletion")
    # Files already queued for deletion are not "found" again; the retry
    # queue owns them.
    pending = set(pending_model.objects.values_list("path", flat=True))
    orphans = []
    for prefix in prefixes:
        for name in _list_storage_files(prefix):
            if name not in referenced and name not in pending:
                orphans.append(name)
    return orphans


def delete_orphan_files(dry_run=False):
    """
    Delete files that no row references.

    :returns: ``(released, deferred, skipped)`` where ``skipped`` counts the
    files reported but untouched in dry-run mode.
    """
    orphans = find_orphan_files()
    released = deferred = skipped = 0
    for name in orphans:
        if dry_run:
            skipped += 1
            continue
        status = release_image_file(name, reason="orphan scan")
        if status == RELEASED:
            released += 1
        elif status == DEFERRED:
            deferred += 1
    return released, deferred, skipped
