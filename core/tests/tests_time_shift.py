# -*- coding: utf-8 -*-
"""Tests for the bulk entry time adjustment (time zone correction) view."""

import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.utils import timezone

from core import forms, models, timeline
from webhooks.models import WebhookEndpoint, WebhookEvent


class TimeShiftTestCase(TestCase):
    def setUp(self):
        self.child = models.Child.objects.create(
            first_name="Shift", last_name="Child", birth_date="2023-01-01"
        )
        self.user = get_user_model().objects.create_superuser(
            username="shift-admin", password="pass"
        )
        self.client.force_login(self.user)

        # A fixed day well in the past; all shifts stay well clear of "now".
        self.day = timezone.localdate() - datetime.timedelta(days=30)
        self.next_day = self.day + datetime.timedelta(days=1)

        self.feeding = self.make(models.Feeding, 8, 30, type="formula", method="bottle")
        self.feeding_night = self.make(
            models.Feeding, 20, 30, type="formula", method="bottle"
        )
        self.sleep = self.make(models.Sleep, 12, 60, nap=True)
        self.pumping = self.make(models.Pumping, 6, 20, amount=42)
        self.tummy = self.make(models.TummyTime, 18, 20)

        self.all_types = ["feeding", "sleep", "pumping", "tummytime"]

    def dt(self, day, hour=0, minute=0):
        naive = datetime.datetime.combine(day, datetime.time(hour, minute))
        return timezone.make_aware(naive, timezone.get_current_timezone())

    def make(self, model, hour, minutes, day=None, **kwargs):
        day = day or self.day
        start = self.dt(day, hour)
        end = start + datetime.timedelta(minutes=minutes)
        if model is models.Feeding:
            kwargs.setdefault("type", "breast milk")
            kwargs.setdefault("method", "both breasts")
        if model is models.Pumping:
            kwargs.setdefault("amount", 10)
        if model is models.Sleep:
            kwargs.setdefault("nap", False)
        return model.objects.create(
            child=self.child, start=start, end=end, **kwargs
        )

    def post_data(self, hours, types=None, day=None, end_day=None, **extra):
        day = day or self.day
        end_day = end_day or day
        data = {
            "child": self.child.pk,
            "start_date": day.isoformat(),
            "end_date": end_day.isoformat(),
            "hours": str(hours),
            "entry_types": types or self.all_types,
        }
        data.update(extra)
        return data

    def originals(self):
        return {
            instance: (instance.start, instance.end)
            for instance in [
                self.feeding,
                self.feeding_night,
                self.sleep,
                self.pumping,
                self.tummy,
            ]
        }

    def assert_unchanged(self, originals):
        for instance, (start, end) in originals.items():
            instance.refresh_from_db()
            self.assertEqual(instance.start, start)
            self.assertEqual(instance.end, end)

    # -- access -----------------------------------------------------------

    def test_get_requires_login(self):
        self.client.logout()
        response = self.client.get("/adjust/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.url)

    def test_get_requires_a_change_permission(self):
        user = get_user_model().objects.create_user(username="limited")
        self.client.force_login(user)
        self.assertEqual(self.client.get("/adjust/").status_code, 403)

    def test_get_renders_form_with_allowed_types_selected(self):
        response = self.client.get("/adjust/")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/time_shift_form.html")
        choices = {
            value for value, _label in response.context["form"].fields["entry_types"].choices
        }
        self.assertEqual(choices, set(self.all_types))
        # All four are pre-selected for a superuser.
        self.assertEqual(
            set(response.context["form"].initial["entry_types"]), set(self.all_types)
        )

    # -- preview ----------------------------------------------------------

    def test_preview_lists_only_entries_starting_in_range(self):
        # Starts before the range and crosses midnight into it; the entry must
        # not move but must remain an obstacle for a feeding shifted onto it.
        border = self.make(
            models.Feeding, 23, 90, day=self.day - datetime.timedelta(days=1)
        )
        response = self.client.post("/adjust/", self.post_data(-8))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/time_shift_preview.html")
        plan = response.context["plan"]
        self.assertEqual(plan["total"], 5)
        self.assertTrue(any(c["obstacle"].pk == border.pk for c in plan["conflicts"]))
        # Preview never writes.
        self.assert_unchanged(self.originals())

    def test_moving_entries_do_not_conflict_with_each_other(self):
        # A uniform translation preserves the gap between entries that move
        # together, so neither feeding can be an obstacle for the other.
        form = forms.TimeShiftForm(
            data=self.post_data(8, types=["feeding"]), user=self.user
        )
        self.assertTrue(form.is_valid(), form.errors)
        plan = form.plan()
        self.assertEqual(
            sum(len(group["rows"]) for group in plan["groups"]), 2
        )
        self.assertEqual(plan["conflicts"], [])

    def test_preview_reports_conflict_with_stationary_entry(self):
        # 20:00 feeding shifted +8h lands at 04:00 next day; put an entry there.
        obstacle = self.make(models.Feeding, 4, 20, day=self.next_day)
        response = self.client.post("/adjust/", self.post_data(8, types=["feeding"]))
        plan = response.context["plan"]
        conflicts = plan["conflicts"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["moving"].pk, self.feeding_night.pk)
        self.assertEqual(conflicts[0]["obstacle"].pk, obstacle.pk)
        self.assertContains(response, b"overlapping time period")
        self.assertNotContains(response, 'name="confirm"')
        self.assert_unchanged(self.originals())

    def test_preview_reports_future_periods(self):
        recent = models.Feeding.objects.create(
            child=self.child,
            start=timezone.now() - datetime.timedelta(hours=1),
            end=timezone.now() - datetime.timedelta(minutes=50),
            type="formula",
            method="bottle",
        )
        today = timezone.localdate()
        response = self.client.post(
            "/adjust/", self.post_data(8, types=["feeding"], day=today)
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            any(row["entry"].pk == recent.pk for row in response.context["plan"]["future"])
        )

    # -- apply ------------------------------------------------------------

    def test_confirm_shifts_all_four_types_and_preserves_durations(self):
        endpoint = WebhookEndpoint.objects.create(
            name="hook", url="https://example.com/hook"
        )
        originals = self.originals()
        durations = {
            instance: instance.duration
            for instance in [self.feeding, self.sleep, self.pumping, self.tummy]
        }
        response = self.client.post(
            "/adjust/", self.post_data(-8, confirm=1)
        )
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(
            response, f"/children/{self.child.slug}/", fetch_redirect_response=False
        )
        delta = datetime.timedelta(hours=-8)
        for instance, duration in durations.items():
            instance.refresh_from_db()
            self.assertEqual(instance.start, originals[instance][0] + delta)
            self.assertEqual(instance.end, originals[instance][1] + delta)
            self.assertEqual(instance.duration, duration)

        # One updated event per moved entry for the active receiver.
        events = WebhookEvent.objects.filter(endpoint=endpoint)
        self.assertEqual(events.count(), 5)
        for model_name, instance in [
            ("feeding", self.feeding),
            ("feeding", self.feeding_night),
            ("sleep", self.sleep),
            ("pumping", self.pumping),
            ("tummytime", self.tummy),
        ]:
            self.assertTrue(
                events.filter(
                    type=f"{model_name}.updated", object_id=str(instance.pk)
                ).exists()
            )

    def test_confirm_with_conflict_changes_nothing(self):
        self.make(models.Feeding, 4, 20, day=self.next_day)
        response = self.client.post(
            "/adjust/", self.post_data(8, types=["feeding"], confirm=1)
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/time_shift_preview.html")
        self.assertTrue(response.context["form"].non_field_errors())
        self.assertEqual(len(response.context["plan"]["conflicts"]), 1)
        self.assert_unchanged(self.originals())
        self.assertFalse(
            WebhookEvent.objects.filter(type="feeding.updated").exists()
        )

    def test_confirm_with_future_period_changes_nothing(self):
        models.Feeding.objects.create(
            child=self.child,
            start=timezone.now() - datetime.timedelta(hours=1),
            end=timezone.now() - datetime.timedelta(minutes=50),
            type="formula",
            method="bottle",
        )
        today = timezone.localdate()
        response = self.client.post(
            "/adjust/",
            self.post_data(8, types=["feeding"], day=today, confirm=1),
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].non_field_errors())
        self.assert_unchanged(self.originals())

    def test_failed_save_rolls_back_every_entry_and_events(self):
        WebhookEndpoint.objects.create(name="hook", url="https://example.com/hook")
        form = forms.TimeShiftForm(
            data=self.post_data(-8, confirm=1), user=self.user
        )
        self.assertTrue(form.is_valid(), form.errors)
        with patch.object(
            models.TummyTime, "save", side_effect=RuntimeError("disk full")
        ):
            with self.assertRaisesMessage(RuntimeError, "disk full"):
                form.execute()
        self.assert_unchanged(self.originals())
        self.assertFalse(WebhookEvent.objects.exists())

    def test_unselected_types_are_not_moved(self):
        originals = self.originals()
        response = self.client.post(
            "/adjust/", self.post_data(-8, types=["feeding"], confirm=1)
        )
        self.assertEqual(response.status_code, 302)
        self.feeding.refresh_from_db()
        self.sleep.refresh_from_db()
        self.assertEqual(
            self.feeding.start,
            originals[self.feeding][0] - datetime.timedelta(hours=8),
        )
        self.assertEqual(self.sleep.start, originals[self.sleep][0])

    # -- navigation consistency ------------------------------------------

    def test_timeline_follows_entries_across_midnight(self):
        self.client.post("/adjust/", self.post_data(8, confirm=1))
        # The 20:00 feeding becomes 04:00 the next day and must show there.
        events = timeline.get_objects(
            self.dt(self.next_day), child=self.child, user=self.user
        )
        self.assertTrue(
            any(
                event["model_name"] == "feeding"
                and event["time"].date() == self.next_day
                for event in events
            )
        )

    # -- validation and permissions --------------------------------------

    def test_zero_hours_rejected(self):
        response = self.client.post("/adjust/", self.post_data(0))
        self.assertEqual(response.status_code, 200)
        self.assertIn("hours", response.context["form"].errors)

    def test_reversed_dates_rejected(self):
        data = self.post_data(
            -8, day=self.next_day, end_day=self.day
        )
        response = self.client.post("/adjust/", data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("end_date", response.context["form"].errors)

    def test_empty_range_reports_non_field_error(self):
        empty_day = self.day - datetime.timedelta(days=200)
        response = self.client.post("/adjust/", self.post_data(-8, day=empty_day))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].non_field_errors())
        self.assert_unchanged(self.originals())

    def test_limited_user_only_sees_permitted_type(self):
        user = get_user_model().objects.create_user(username="feeder")
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="core", codename="change_feeding"
            )
        )
        self.client.force_login(user)
        response = self.client.get("/adjust/")
        choices = {
            value for value, _label in response.context["form"].fields["entry_types"].choices
        }
        self.assertEqual(choices, {"feeding"})

        # A tampered POST asking for sleep is refused and writes nothing.
        response = self.client.post("/adjust/", self.post_data(-8, types=["sleep"]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("entry_types", response.context["form"].errors)
        self.assert_unchanged(self.originals())

        # The permitted type works.
        feeding_original_start = self.feeding.start
        response = self.client.post(
            "/adjust/", self.post_data(-8, types=["feeding"], confirm=1)
        )
        self.assertEqual(response.status_code, 302)
        self.feeding.refresh_from_db()
        self.assertEqual(
            self.feeding.start,
            feeding_original_start - datetime.timedelta(hours=8),
        )
