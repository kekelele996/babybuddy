# -*- coding: utf-8 -*-
"""
File storage housekeeping for image-bearing models.

Django never deletes an ImageField file on its own -- replacing the image or
deleting the row leaves the file in storage. These receivers close that gap:

* on replace, the *previous* file is released only after the transaction has
  committed, so a failed save (including a rejected replacement image) leaves
  the original image untouched;
* on delete, the attached file is released after commit. Cascade deletes (e.g.
  deleting a Child and all of its Notes) work because every deleted row sends
  its own signal and all releases run once the cascade has finished;
* ``core.images.release_image_file`` re-checks every image field before
  deleting, so a file another row still references is never removed.

The receivers are connected to concrete senders only and are imported for
their side effects from ``CoreConfig.ready``.
"""

from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from core import images
from core.models import Child, Note

#: sender -> the name of its image field.
IMAGE_FIELD_NAMES = {
    Child: "picture",
    Note: "image",
}

#: Attribute used to stash the pre-save file name on the instance.
PREVIOUS_NAME_ATTR = "_previous_image_name"

#: Per-model attributes cached by ``has_picture``/``has_image``.
EXISTENCE_CACHE_ATTRS = {
    Child: "_picture_exists",
    Note: "_image_exists",
}


def _current_name(instance, field_name):
    return getattr(instance, field_name).name or ""


@receiver(pre_save, sender=Child, dispatch_uid="core.image.remember_previous.child")
@receiver(pre_save, sender=Note, dispatch_uid="core.image.remember_previous.note")
def remember_previous_image(sender, instance, raw=False, **kwargs):
    """Stash the stored image name so post_save can detect a replacement."""
    if raw:
        # Loading a fixture restores names but never moves files; there is no
        # previous value to clean up.
        setattr(instance, PREVIOUS_NAME_ATTR, "")
        return
    field_name = IMAGE_FIELD_NAMES[sender]
    previous = ""
    if instance.pk:
        previous = (
            sender.objects.filter(pk=instance.pk)
            .values_list(field_name, flat=True)
            .first()
            or ""
        )
    setattr(instance, PREVIOUS_NAME_ATTR, previous)


@receiver(post_save, sender=Child, dispatch_uid="core.image.release_replaced.child")
@receiver(post_save, sender=Note, dispatch_uid="core.image.release_replaced.note")
def release_replaced_image(sender, instance, created=False, raw=False, **kwargs):
    """After a replacement commits, release the file the row used to point at."""
    # The cached storage-existence result is stale after a save.
    instance.__dict__.pop(EXISTENCE_CACHE_ATTRS[sender], None)
    if raw or created:
        return
    field_name = IMAGE_FIELD_NAMES[sender]
    previous = getattr(instance, PREVIOUS_NAME_ATTR, "")
    current = _current_name(instance, field_name)
    if previous and previous != current:
        transaction.on_commit(
            lambda: images.release_image_file(previous, reason="replaced")
        )


@receiver(post_delete, sender=Child, dispatch_uid="core.image.release_deleted.child")
@receiver(post_delete, sender=Note, dispatch_uid="core.image.release_deleted.note")
def release_deleted_image(sender, instance, **kwargs):
    """Release the attached file after the row deletion has committed."""
    field_name = IMAGE_FIELD_NAMES[sender]
    name = _current_name(instance, field_name)
    if name:
        transaction.on_commit(lambda: images.release_image_file(name, reason="deleted"))
