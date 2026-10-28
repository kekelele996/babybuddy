# -*- coding: utf-8 -*-
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, Client as HttpClient
from django.utils import timezone

from core import forms, merge, models


class MergeTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("migrate", verbosity=0)
        cls.now = timezone.now()
        cls.admin = get_user_model().objects.create_superuser(
            username="merge-admin", password="password", email="admin@example.com"
        )

    def setUp(self):
        # A fresh authenticated client per test: TestCase replaces `client`
        # with an anonymous one for every test method.
        self.user = self.admin
        self.client = HttpClient()
        self.client.force_login(self.user)
        self.source = models.Child.objects.create(
            first_name="Ada", last_name="Twin", birth_date="2024-01-01"
        )
        self.target = models.Child.objects.create(
            first_name="Aida", last_name="Twin", birth_date="2024-01-01"
        )

    # -- preview ---------------------------------------------------------

    def test_preview_counts_entries(self):
        models.Feeding.objects.create(
            child=self.source,
            start=self.now - timezone.timedelta(hours=2),
            end=self.now - timezone.timedelta(hours=1),
            type="formula",
            method="bottle",
        )
        models.Weight.objects.create(
            child=self.source, weight=3.5, date=timezone.localdate()
        )
        models.Note.objects.create(child=self.source, note="hello", time=self.now)

        preview = merge.build_merge_preview(self.source, self.target)
        by_name = {s["model_name"]: s for s in preview["sections"]}
        self.assertEqual(by_name["feeding"]["count"], 1)
        self.assertEqual(by_name["weight"]["count"], 1)
        self.assertEqual(by_name["note"]["count"], 1)
        self.assertEqual(by_name["sleep"]["count"], 0)
        self.assertEqual(preview["entry_total"], 3)
        self.assertEqual(preview["conflict_total"], 0)

    def test_preview_detects_overlapping_periods(self):
        models.Feeding.objects.create(
            child=self.source,
            start=self.now - timezone.timedelta(hours=2),
            end=self.now - timezone.timedelta(hours=1),
            type="formula",
            method="bottle",
        )
        models.Feeding.objects.create(
            child=self.target,
            start=self.now - timezone.timedelta(hours=90),
            end=self.now - timezone.timedelta(minutes=30),
            type="formula",
            method="bottle",
        )

        preview = merge.build_merge_preview(self.source, self.target)
        feeding = next(s for s in preview["sections"] if s["model_name"] == "feeding")
        self.assertEqual(len(feeding["conflicts"]), 1)
        self.assertIn("source_url", feeding["conflicts"][0])

    def test_preview_detects_same_date_and_time(self):
        day = timezone.localdate()
        models.Weight.objects.create(child=self.source, weight=3.4, date=day)
        models.Weight.objects.create(child=self.target, weight=3.6, date=day)
        models.Temperature.objects.create(
            child=self.source, temperature=36.8, time=self.now
        )
        models.Temperature.objects.create(
            child=self.target, temperature=37.0, time=self.now
        )

        preview = merge.build_merge_preview(self.source, self.target)
        by_name = {s["model_name"]: s for s in preview["sections"]}
        self.assertEqual(len(by_name["weight"]["conflicts"]), 1)
        self.assertEqual(len(by_name["temperature"]["conflicts"]), 1)

    def test_preview_conflict_links_resolve_for_every_type(self):
        # HeadCircumference's URL name uses hyphens, unlike its model_name;
        # building the preview must not crash on NoReverseMatch.
        day = timezone.localdate()
        models.HeadCircumference.objects.create(
            child=self.source, head_circumference=34.0, date=day
        )
        models.HeadCircumference.objects.create(
            child=self.target, head_circumference=34.5, date=day
        )

        preview = merge.build_merge_preview(self.source, self.target)
        head = next(
            s for s in preview["sections"] if s["model_name"] == "head_circumference"
        )
        self.assertEqual(len(head["conflicts"]), 1)
        self.assertIn("/head-circumference/", head["conflicts"][0]["source_url"])

    def test_preview_reports_timers_and_picture(self):
        models.Timer.objects.create(child=self.source, user=self.user, start=self.now)
        models.Timer.objects.create(
            child=self.source,
            user=self.user,
            start=self.now - timezone.timedelta(hours=5),
            active=False,
        )
        models.Child.objects.filter(pk=self.source.pk).update(
            picture="child/picture/source.jpg"
        )
        self.source.refresh_from_db()

        preview = merge.build_merge_preview(self.source, self.target)
        self.assertEqual(preview["timer_total"], 2)
        self.assertEqual(preview["timer_active_total"], 1)
        self.assertTrue(preview["picture_will_copy"])

    # -- execution -------------------------------------------------------

    def test_merge_moves_entries_tags_timers_picture(self):
        feeding = models.Feeding.objects.create(
            child=self.source,
            start=self.now - timezone.timedelta(hours=2),
            end=self.now - timezone.timedelta(hours=1),
            type="formula",
            method="bottle",
        )
        note = models.Note.objects.create(
            child=self.source, note="tagged", time=self.now
        )
        note.tags.add("twin-note")
        active_timer = models.Timer.objects.create(
            child=self.source, user=self.user, start=self.now
        )
        models.Child.objects.filter(pk=self.source.pk).update(
            picture="child/picture/source.jpg"
        )
        old_slug, old_name = self.source.slug, str(self.source)

        merge.merge_children(self.source, self.target)

        feeding.refresh_from_db()
        note.refresh_from_db()
        active_timer.refresh_from_db()
        self.assertEqual(feeding.child_id, self.target.pk)
        self.assertEqual(note.child_id, self.target.pk)
        # Tags live on the entry, so they survive the reassignment untouched.
        self.assertIn("twin-note", list(note.tags.names()))
        self.assertEqual(active_timer.child_id, self.target.pk)
        self.assertTrue(active_timer.active)
        self.target.refresh_from_db()
        self.assertEqual(self.target.picture.name, "child/picture/source.jpg")
        self.assertFalse(models.Child.objects.filter(slug=old_slug).exists())
        alias = models.MergedChild.objects.get(slug=old_slug)
        self.assertEqual(alias.name, old_name)
        self.assertEqual(alias.child_id, self.target.pk)

    def test_merge_keeps_existing_target_picture(self):
        models.Child.objects.filter(pk=self.source.pk).update(
            picture="child/picture/source.jpg"
        )
        models.Child.objects.filter(pk=self.target.pk).update(
            picture="child/picture/target.jpg"
        )
        merge.merge_children(self.source, self.target)
        self.target.refresh_from_db()
        self.assertEqual(self.target.picture.name, "child/picture/target.jpg")

    def test_merge_cannot_target_itself(self):
        with self.assertRaises(ValueError):
            merge.merge_children(self.source, self.source)

    def test_failure_rolls_back_everything(self):
        models.Feeding.objects.create(
            child=self.source,
            start=self.now - timezone.timedelta(hours=2),
            end=self.now - timezone.timedelta(hours=1),
            type="formula",
            method="bottle",
        )
        active_timer = models.Timer.objects.create(
            child=self.source, user=self.user, start=self.now
        )
        source_id, target_id = self.source.pk, self.target.pk

        with patch.object(
            models.MergedChild.objects,
            "create",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                merge.merge_children(self.source, self.target)

        # Both profiles and all of their records are untouched.
        self.assertTrue(models.Child.objects.filter(pk=source_id).exists())
        self.assertTrue(models.Child.objects.filter(pk=target_id).exists())
        self.assertTrue(models.Feeding.objects.filter(child_id=source_id).exists())
        active_timer.refresh_from_db()
        self.assertEqual(active_timer.child_id, source_id)
        self.assertFalse(models.MergedChild.objects.exists())

    def test_existing_aliases_are_repointed(self):
        older = models.MergedChild.objects.create(
            slug="older-slug", name="Older Name", child=self.source
        )
        merge.merge_children(self.source, self.target)
        older.refresh_from_db()
        self.assertEqual(older.child_id, self.target.pk)
        response = self.client.get("/children/older-slug/")
        self.assertRedirects(
            response,
            "/children/{}/".format(self.target.slug),
            status_code=301,
            fetch_redirect_response=False,
        )

    # -- redirects -------------------------------------------------------

    def test_old_links_redirect_to_surviving_child(self):
        old_slug = self.source.slug
        merge.merge_children(self.source, self.target)

        profile = self.client.get("/children/{}/".format(old_slug))
        self.assertRedirects(
            profile,
            "/children/{}/".format(self.target.slug),
            status_code=301,
            fetch_redirect_response=False,
        )
        # The redirect target itself renders.
        self.assertEqual(
            self.client.get("/children/{}/".format(self.target.slug)).status_code,
            200,
        )
        # Child-scoped sub-pages and query strings are preserved.
        reports = self.client.get(
            "/children/{}/reports/weight/weight/".format(old_slug)
        )
        self.assertRedirects(
            reports,
            "/children/{}/reports/weight/weight/".format(self.target.slug),
            status_code=301,
            fetch_redirect_response=False,
        )
        dashboard = self.client.get("/children/{}/dashboard/?foo=bar".format(old_slug))
        self.assertEqual(
            dashboard.headers["Location"],
            "/children/{}/dashboard/?foo=bar".format(self.target.slug),
        )

    def test_unknown_slug_still_404s(self):
        response = self.client.get("/children/never-existed/")
        self.assertEqual(response.status_code, 404)

    def test_alias_redirect_api_and_safe_methods_only(self):
        old_slug = self.source.slug
        merge.merge_children(self.source, self.target)

        # The API uses the same slug namespace; GETs follow to the new profile.
        api = self.client.get("/api/children/{}/".format(old_slug))
        self.assertEqual(api.status_code, 301)
        self.assertEqual(
            api.headers["Location"],
            "/api/children/{}/".format(self.target.slug),
        )
        # Non-safe methods are never rewritten by the redirect middleware.
        post = self.client.post("/children/{}/merge/".format(old_slug), {})
        self.assertEqual(post.status_code, 404)

    # -- form ------------------------------------------------------------

    def test_form_accepts_correct_confirmation(self):
        form = forms.ChildMergeForm(
            source=self.source,
            data={"target": self.target.pk, "confirm_name": str(self.source)},
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_form_rejects_wrong_name_and_self_target(self):
        form = forms.ChildMergeForm(
            source=self.source,
            data={"target": self.target.pk, "confirm_name": "Wrong Name"},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("confirm_name", form.errors)

        form = forms.ChildMergeForm(
            source=self.source,
            data={"target": self.source.pk, "confirm_name": str(self.source)},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("target", form.errors)

    # -- views -----------------------------------------------------------

    def test_merge_page_preview(self):
        page = self.client.get("/children/{}/merge/".format(self.source.slug))
        self.assertEqual(page.status_code, 200)
        self.assertIn("preview", page.context)

    def test_merge_page_requires_permission(self):
        plain = get_user_model().objects.create_user(username="plain")
        client = HttpClient()
        client.force_login(plain)
        page = client.get("/children/{}/merge/".format(self.source.slug))
        self.assertEqual(page.status_code, 403)

    def test_merge_post_completes_merge(self):
        response = self.client.post(
            "/children/{}/merge/".format(self.source.slug),
            {"target": self.target.pk, "confirm_name": str(self.source)},
        )
        self.assertRedirects(
            response,
            "/children/{}/".format(self.target.slug),
            fetch_redirect_response=False,
        )
        self.assertFalse(models.Child.objects.filter(pk=self.source.pk).exists())
        self.assertTrue(
            models.MergedChild.objects.filter(
                slug=self.source.slug, child=self.target
            ).exists()
        )

    def test_merge_post_bad_confirmation_keeps_both(self):
        response = self.client.post(
            "/children/{}/merge/".format(self.source.slug),
            {"target": self.target.pk, "confirm_name": "Nope"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(models.Child.objects.filter(pk=self.source.pk).exists())
        self.assertTrue(models.Child.objects.filter(pk=self.target.pk).exists())

    def test_merge_page_redirects_without_another_child(self):
        self.target.delete()
        response = self.client.get("/children/{}/merge/".format(self.source.slug))
        self.assertRedirects(response, "/children/", fetch_redirect_response=False)
