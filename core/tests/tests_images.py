# -*- coding: utf-8 -*-
import io
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, Client, override_settings
from django.utils import timezone
from django.utils.formats import get_format, reset_format_cache
from PIL import Image

from core import forms as core_forms
from core import images, models


def png_upload(name="test.png", color="blue"):
    file = io.BytesIO()
    Image.new("RGB", (10, 10), color).save(file, format="PNG")
    return SimpleUploadedFile(name, file.getvalue(), content_type="image/png")


def truncated_upload(name="truncated.png"):
    # A valid PNG header followed by garbage: the header-only check
    # (Image.verify on a truncated stream is not always enough -- the
    # validator fully decodes the file) can let files like this through,
    # which is exactly what the replacement guard must reject.
    return SimpleUploadedFile(
        name,
        b"\x89PNG\r\n\x1a\n" + b"not really a png" * 10,
        content_type="image/png",
    )


class ImageStorageTestCase(TestCase):
    """All tests run against an isolated MEDIA_ROOT."""

    @classmethod
    def setUpClass(cls):
        cls._media_tmp = tempfile.TemporaryDirectory()
        cls._media_override = override_settings(MEDIA_ROOT=cls._media_tmp.name)
        cls._media_override.enable()
        super().setUpClass()
        # The custom `migrate` command seeds an admin/admin superuser.
        cls.user = get_user_model().objects.get(username="admin")
        cls.child = models.Child.objects.create(
            first_name="Child", last_name="One", birth_date=timezone.localdate()
        )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._media_override.disable()
        cls._media_tmp.cleanup()

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _note(self, upload=None, child=None):
        if upload is None:
            upload = png_upload()
        return models.Note.objects.create(
            child=child or self.child,
            note="note with image",
            time=timezone.localtime(),
            image=upload,
        )

    # -- validation ---------------------------------------------------------

    def test_validator_accepts_valid_image(self):
        upload = png_upload()
        images.validate_uploaded_image(upload)
        self.assertEqual(upload.tell(), 0)

    def test_validator_rejects_corrupted_image(self):
        with self.assertRaises(Exception):
            images.validate_uploaded_image(truncated_upload())

    def test_form_rejects_corrupted_replacement_and_keeps_old_image(self):
        note = self._note(png_upload("old.png"))
        old_name = note.image.name
        self.assertTrue(default_storage.exists(old_name))

        reset_format_cache()
        time_format = get_format("DATETIME_INPUT_FORMATS")[0]
        data = {
            "child": self.child.id,
            "note": note.note,
            "time": timezone.localtime(note.time).strftime(time_format),
        }
        form = core_forms.NoteForm(
            data=data,
            files={"image": truncated_upload()},
            instance=note,
            user=self.user,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("image", form.errors)

        note.refresh_from_db()
        self.assertEqual(note.image.name, old_name)
        self.assertTrue(default_storage.exists(old_name))
        # The rejected upload was never persisted to storage.
        self.assertEqual(
            [n for n in images.find_orphan_files() if "truncated" in n], []
        )

    # -- deletion -----------------------------------------------------------

    def test_delete_note_deletes_its_image(self):
        note = self._note()
        name = note.image.name

        with self.captureOnCommitCallbacks(execute=True):
            note.delete()

        self.assertFalse(default_storage.exists(name))
        self.assertFalse(models.PendingFileDeletion.objects.exists())

    def test_delete_child_deletes_picture_and_all_note_images(self):
        deleted_child = models.Child.objects.create(
            first_name="Child",
            last_name="Two",
            birth_date=timezone.localdate(),
            picture=png_upload("face.png"),
        )
        note_one = self._note(png_upload("one.png"), child=deleted_child)
        note_two = self._note(png_upload("two.png"), child=deleted_child)
        # A note belonging to another child must be left alone.
        surviving_note = self._note(png_upload("surviving.png"))
        names = [deleted_child.picture.name, note_one.image.name, note_two.image.name]
        for name in names:
            self.assertTrue(default_storage.exists(name))

        with self.captureOnCommitCallbacks(execute=True):
            deleted_child.delete()

        for name in names:
            self.assertFalse(default_storage.exists(name))
        self.assertTrue(default_storage.exists(surviving_note.image.name))
        self.assertFalse(models.PendingFileDeletion.objects.exists())

    def test_delete_note_without_image_is_a_noop(self):
        note = models.Note.objects.create(
            child=self.child, note="no image", time=timezone.localtime()
        )
        with self.captureOnCommitCallbacks(execute=True):
            note.delete()
        self.assertFalse(models.PendingFileDeletion.objects.exists())

    # -- replacement --------------------------------------------------------

    def test_replace_image_removes_old_file_and_keeps_new(self):
        note = self._note(png_upload("first.png"))
        old_name = note.image.name

        with self.captureOnCommitCallbacks(execute=True):
            note.image = png_upload("second.png", color="red")
            note.save()

        note.refresh_from_db()
        self.assertNotEqual(note.image.name, old_name)
        self.assertFalse(default_storage.exists(old_name))
        self.assertTrue(default_storage.exists(note.image.name))

    def test_clear_image_removes_old_file(self):
        note = self._note(png_upload("clear.png"))
        name = note.image.name

        with self.captureOnCommitCallbacks(execute=True):
            note.image = None
            note.save()

        note.refresh_from_db()
        self.assertFalse(note.image)
        self.assertFalse(default_storage.exists(name))

    def test_replace_child_picture_removes_every_previous_file(self):
        child = models.Child.objects.create(
            first_name="Face", last_name="Kid", birth_date=timezone.localdate()
        )
        previous_names = []
        for index, color in enumerate(("blue", "red", "green")):
            with self.captureOnCommitCallbacks(execute=True):
                child.picture = png_upload(f"face-{index}.png", color=color)
                child.save()
            child.refresh_from_db()
            if previous_names:
                self.assertFalse(default_storage.exists(previous_names[-1]))
            previous_names.append(child.picture.name)
            self.assertTrue(default_storage.exists(child.picture.name))

        with self.captureOnCommitCallbacks(execute=True):
            child.delete()
        for name in previous_names:
            self.assertFalse(default_storage.exists(name))

    def test_file_shared_by_two_rows_survives_first_delete(self):
        first = self._note(png_upload("shared.png"))
        shared_name = first.image.name
        second = models.Note(
            child=self.child, note="reuses the file", time=timezone.localtime()
        )
        # Point a second row at the same stored file without re-uploading.
        second.image = shared_name
        second.save()

        with self.captureOnCommitCallbacks(execute=True):
            first.delete()
        self.assertTrue(default_storage.exists(shared_name))
        self.assertFalse(models.PendingFileDeletion.objects.exists())

        with self.captureOnCommitCallbacks(execute=True):
            second.delete()
        self.assertFalse(default_storage.exists(shared_name))

    # -- failure / retry ----------------------------------------------------

    def test_failed_cleanup_is_persisted_and_retried(self):
        note = self._note(png_upload("fail.png"))
        name = note.image.name

        with mock.patch(
            "django.core.files.storage.FileSystemStorage.delete",
            side_effect=OSError("storage offline"),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                note.delete()

        pending = models.PendingFileDeletion.objects.get(path=name)
        self.assertEqual(pending.attempts, 0)
        self.assertIn("storage offline", pending.last_error)
        self.assertTrue(default_storage.exists(name))

        released, deferred, kept = images.retry_pending_deletions()
        self.assertEqual((released, deferred, kept), (1, 0, 0))
        self.assertFalse(default_storage.exists(name))
        self.assertFalse(models.PendingFileDeletion.objects.filter(path=name).exists())

    def test_retry_records_further_attempts(self):
        note = self._note(png_upload("fail2.png"))
        name = note.image.name
        with mock.patch(
            "django.core.files.storage.FileSystemStorage.delete",
            side_effect=OSError("still offline"),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                note.delete()

        with mock.patch(
            "django.core.files.storage.FileSystemStorage.delete",
            side_effect=OSError("still offline"),
        ):
            released, deferred, kept = images.retry_pending_deletions()
        self.assertEqual((released, deferred, kept), (0, 1, 0))
        pending = models.PendingFileDeletion.objects.get(path=name)
        self.assertEqual(pending.attempts, 1)

    def test_pending_deletion_of_missing_file_is_cleared(self):
        models.PendingFileDeletion.objects.create(path="notes/images/already-gone.png")
        released, _deferred, _kept = images.retry_pending_deletions()
        self.assertEqual(released, 1)
        self.assertFalse(models.PendingFileDeletion.objects.exists())

    def test_pending_deletion_is_dropped_when_file_is_referenced_again(self):
        note = self._note(png_upload("restored.png"))
        name = note.image.name
        models.PendingFileDeletion.objects.create(path=name)

        released, _deferred, kept = images.retry_pending_deletions()
        self.assertEqual(kept, 1)
        self.assertTrue(default_storage.exists(name))
        self.assertFalse(models.PendingFileDeletion.objects.filter(path=name).exists())

    # -- dangling references / orphans --------------------------------------

    def test_has_image_reflects_storage_existence(self):
        note = self._note()
        self.assertTrue(note.has_image())

        dangling = models.Note(
            child=self.child, note="restored", time=timezone.localtime()
        )
        dangling.image = "notes/images/never-restored.png"
        dangling.save()
        dangling = models.Note.objects.get(pk=dangling.pk)
        self.assertFalse(dangling.has_image())

    def test_note_list_renders_with_dangling_reference(self):
        dangling = models.Note(
            child=self.child, note="restored", time=timezone.localtime()
        )
        dangling.image = "notes/images/never-restored.png"
        dangling.save()

        response = self.client.get("/notes/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "never-restored.png")

    def test_child_list_renders_placeholder_with_dangling_picture(self):
        child = models.Child(
            first_name="Ghost", last_name="Kid", birth_date=timezone.localdate()
        )
        child.picture = "child/picture/never-restored.png"
        child.save()

        response = self.client.get("/children/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "never-restored.png")

    def test_scan_clears_dangling_references(self):
        note = models.Note(child=self.child, note="restored", time=timezone.localtime())
        note.image = "notes/images/never-restored.png"
        note.save()
        child = models.Child(
            first_name="Ghost", last_name="Kid", birth_date=timezone.localdate()
        )
        child.picture = "child/picture/never-restored.png"
        child.save()

        dangling = images.list_dangling_references()
        self.assertEqual(len(dangling), 2)

        repaired = images.clear_dangling_references()
        self.assertEqual(repaired, 2)
        note.refresh_from_db()
        child.refresh_from_db()
        self.assertFalse(note.image.name)
        self.assertFalse(child.picture.name)

    def test_scan_deletes_orphan_files(self):
        # Earlier tests leave physical files behind when their transactions
        # roll back, so compare against the pre-test orphan set.
        before = set(images.find_orphan_files())
        orphan = default_storage.save(
            "notes/images/orphan.png", ContentFile(png_upload().read())
        )
        self.assertTrue(default_storage.exists(orphan))
        self.assertEqual(set(images.find_orphan_files()) - before, {orphan})

        released, deferred, skipped = images.delete_orphan_files(dry_run=True)
        self.assertGreaterEqual(skipped, 1)
        self.assertTrue(default_storage.exists(orphan))

        images.delete_orphan_files()
        self.assertFalse(default_storage.exists(orphan))

    def test_referenced_files_are_not_orphans(self):
        note = self._note()
        self.assertNotIn(note.image.name, images.find_orphan_files())

    def test_pending_files_are_not_reported_as_orphans(self):
        models.PendingFileDeletion.objects.create(path="notes/images/pending.png")
        self.assertNotIn("notes/images/pending.png", images.find_orphan_files())

    # -- management command -------------------------------------------------

    def test_command_retries_pending_deletions(self):
        models.PendingFileDeletion.objects.create(path="notes/images/already-gone.png")
        call_command("cleanup_images", verbosity=0)
        self.assertFalse(models.PendingFileDeletion.objects.exists())

    def test_command_scan_repairs_and_cleans(self):
        dangling_note = models.Note(
            child=self.child, note="restored", time=timezone.localtime()
        )
        dangling_note.image = "notes/images/never-restored.png"
        dangling_note.save()
        orphan = default_storage.save(
            "notes/images/orphan.png", ContentFile(png_upload().read())
        )

        call_command("cleanup_images", "--scan", verbosity=0)

        dangling_note.refresh_from_db()
        self.assertFalse(dangling_note.image.name)
        self.assertFalse(default_storage.exists(orphan))
