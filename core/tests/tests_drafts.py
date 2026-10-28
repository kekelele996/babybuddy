# -*- coding: utf-8 -*-
"""Tests for recoverable entry-form drafts.

Covers the FormDraft model, the /drafts/ endpoint and the CoreAddView
integration (clear on success, retain on validation failure, strict scope).
"""

import datetime
import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.test import Client, TestCase
from django.utils import timezone

from core import forms, models
from webhooks.models import WebhookEndpoint, WebhookEvent

User = get_user_model()
ENDPOINT = "/drafts/"


class DraftModelTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="drafter")
        self.other_user = User.objects.create_user(username="someone-else")
        self.child = models.Child.objects.create(
            first_name="A", last_name="Child", birth_date=datetime.date(2023, 1, 1)
        )
        self.other_child = models.Child.objects.create(
            first_name="B", last_name="Child", birth_date=datetime.date(2023, 2, 1)
        )

    def backdate(self, draft, age):
        models.FormDraft.objects.filter(pk=draft.pk).update(
            updated=timezone.now() - age
        )
        draft.refresh_from_db()

    def test_upsert_keeps_one_row_per_slot_and_replaces_data(self):
        draft = models.FormDraft.objects.upsert(
            self.user, "feeding", self.child.pk, {"notes": "first"}
        )
        old_updated = draft.updated
        self.backdate(draft, datetime.timedelta(hours=1))

        again = models.FormDraft.objects.upsert(
            self.user, "feeding", self.child.pk, {"notes": "second"}
        )

        self.assertEqual(again.pk, draft.pk)
        self.assertEqual(models.FormDraft.objects.count(), 1)
        self.assertEqual(again.data, {"notes": "second"})
        self.assertGreater(again.updated, old_updated)

    def test_slots_are_isolated_by_child_form_and_user(self):
        a = models.FormDraft.objects.upsert(
            self.user, "feeding", self.child.pk, {"notes": "A"}
        )
        b = models.FormDraft.objects.upsert(
            self.user, "feeding", self.other_child.pk, {"notes": "B"}
        )
        sleep = models.FormDraft.objects.upsert(
            self.user, "sleep", self.child.pk, {"notes": "sleep"}
        )
        other_user = models.FormDraft.objects.upsert(
            self.other_user, "feeding", self.child.pk, {"notes": "other"}
        )

        self.assertEqual(models.FormDraft.objects.count(), 4)
        self.assertEqual(
            len({a.pk, b.pk, sleep.pk, other_user.pk}),
            4,
        )
        self.assertEqual(
            models.FormDraft.objects.get_valid(
                self.user, "feeding", self.child.pk
            ).data,
            {"notes": "A"},
        )
        self.assertIsNone(
            models.FormDraft.objects.get_valid(self.other_user, "sleep", self.child.pk)
        )

    def test_null_child_slot_is_unique(self):
        first = models.FormDraft.objects.upsert(
            self.user, "feeding", None, {"notes": "no child yet"}
        )
        second = models.FormDraft.objects.upsert(
            self.user, "feeding", "", {"notes": "still no child"}
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(models.FormDraft.objects.count(), 1)
        self.assertIsNone(
            models.FormDraft.objects.get_valid(self.user, "feeding", None).child_id
        )

    def test_get_valid_enforces_ttl_and_lazily_deletes(self):
        fresh = models.FormDraft.objects.upsert(self.user, "sleep", self.child.pk, {})
        expired = models.FormDraft.objects.upsert(
            self.user, "pumping", self.child.pk, {}
        )
        self.backdate(fresh, datetime.timedelta(days=6, hours=23))
        self.backdate(expired, datetime.timedelta(days=7, minutes=1))

        self.assertIsNotNone(
            models.FormDraft.objects.get_valid(self.user, "sleep", self.child.pk)
        )
        self.assertIsNone(
            models.FormDraft.objects.get_valid(self.user, "pumping", self.child.pk)
        )
        self.assertFalse(models.FormDraft.objects.filter(pk=expired.pk).exists())

    def test_clear_expired_only_removes_old_drafts(self):
        keep = models.FormDraft.objects.upsert(self.user, "feeding", self.child.pk, {})
        drop = models.FormDraft.objects.upsert(self.user, "sleep", self.child.pk, {})
        self.backdate(drop, models.FORM_DRAFT_TTL + datetime.timedelta(minutes=1))

        deleted = models.FormDraft.objects.clear_expired()

        self.assertEqual(deleted, 1)
        self.assertTrue(models.FormDraft.objects.filter(pk=keep.pk).exists())
        self.assertFalse(models.FormDraft.objects.filter(pk=drop.pk).exists())

    def test_drafts_are_cascaded_with_user_and_child(self):
        draft = models.FormDraft.objects.upsert(self.user, "feeding", self.child.pk, {})
        null_draft = models.FormDraft.objects.upsert(
            self.other_user, "feeding", None, {}
        )

        self.child.delete()
        self.assertFalse(models.FormDraft.objects.filter(pk=draft.pk).exists())

        self.other_user.delete()
        self.assertFalse(models.FormDraft.objects.filter(pk=null_draft.pk).exists())

    def test_discard_deletes_only_the_named_slot(self):
        keep = models.FormDraft.objects.upsert(self.user, "feeding", self.child.pk, {})
        target = models.FormDraft.objects.upsert(
            self.user, "feeding", self.other_child.pk, {}
        )

        removed = models.FormDraft.objects.discard(
            self.user, "feeding", self.other_child.pk
        )

        self.assertEqual(removed, 1)
        self.assertTrue(models.FormDraft.objects.filter(pk=keep.pk).exists())
        self.assertFalse(models.FormDraft.objects.filter(pk=target.pk).exists())
        # Discarding an empty slot is a harmless no-op.
        self.assertEqual(
            models.FormDraft.objects.discard(self.user, "feeding", self.other_child.pk),
            0,
        )


class ClearExpiredDraftsCommandTestCase(TestCase):
    def test_command_deletes_expired_drafts_and_reports_count(self):
        user = User.objects.create_user(username="drafter")
        keep = models.FormDraft.objects.upsert(user, "feeding", None, {})
        drop = models.FormDraft.objects.upsert(user, "sleep", None, {})
        models.FormDraft.objects.filter(pk=drop.pk).update(
            updated=timezone.now() - models.FORM_DRAFT_TTL - datetime.timedelta(days=1)
        )

        from io import StringIO

        out = StringIO()
        call_command("clear_expired_drafts", stdout=out)

        self.assertTrue(models.FormDraft.objects.filter(pk=keep.pk).exists())
        self.assertFalse(models.FormDraft.objects.filter(pk=drop.pk).exists())
        self.assertIn("1", out.getvalue())


class DraftEndpointTestCase(TestCase):
    def setUp(self):
        self.child = models.Child.objects.create(
            first_name="A", last_name="Child", birth_date=datetime.date(2023, 1, 1)
        )
        # The default "admin" user is created by the migrate command during
        # test-database setup, so these names must differ.
        self.admin = User.objects.create_superuser(
            username="draft-admin", password="whatever"
        )
        self.caregiver = User.objects.create_user(username="draft-carer")
        self.caregiver.groups.add(
            Group.objects.get(name=settings.BABY_BUDDY["CAREGIVER_GROUP_NAME"])
        )
        # A second *privileged* user: slot isolation must be about ownership,
        # not permission levels.
        self.other = User.objects.create_superuser(
            username="draft-other", password="whatever"
        )
        self.client.force_login(self.admin)

    def payload(self, form_key="feeding", child=None, data=None):
        return json.dumps(
            {
                "form_key": form_key,
                "child": self.child.pk if child is None else child,
                "data": {"notes": "night feed"} if data is None else data,
            }
        )

    def test_anonymous_requests_are_redirected_to_login(self):
        anon = Client()
        checks = (
            anon.get(ENDPOINT, {"form_key": "feeding"}),
            anon.post(ENDPOINT, "{}", content_type="application/json"),
            anon.delete(ENDPOINT + "?form_key=feeding"),
        )
        for response in checks:
            self.assertEqual(response.status_code, 302)
            self.assertIn("/login", response["Location"])

    def test_post_then_get_roundtrip(self):
        response = self.client.post(
            ENDPOINT, self.payload(), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["saved"])
        self.assertEqual(response.json()["child"], self.child.pk)

        response = self.client.get(
            ENDPOINT, {"form_key": "feeding", "child": self.child.pk}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["form_key"], "feeding")
        self.assertEqual(body["child"], self.child.pk)
        self.assertEqual(body["data"], {"notes": "night feed"})
        self.assertIn("updated", body)

    def test_cross_session_same_account_sees_draft(self):
        self.client.post(ENDPOINT, self.payload(), content_type="application/json")

        tablet = Client()
        tablet.force_login(self.admin)
        response = tablet.get(ENDPOINT, {"form_key": "feeding", "child": self.child.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], {"notes": "night feed"})

    def test_other_users_cannot_read_overwrite_or_delete_the_draft(self):
        self.client.post(
            ENDPOINT,
            self.payload(data={"notes": "secret"}),
            content_type="application/json",
        )
        intruder = Client()
        intruder.force_login(self.other)

        self.assertEqual(
            intruder.get(
                ENDPOINT, {"form_key": "feeding", "child": self.child.pk}
            ).status_code,
            404,
        )
        overwrite = intruder.post(
            ENDPOINT,
            self.payload(data={"notes": "hijacked"}),
            content_type="application/json",
        )
        self.assertEqual(overwrite.status_code, 200)
        intruder.delete(ENDPOINT + "?form_key=feeding&child={}".format(self.child.pk))

        # The original slot is untouched (the intruder only changed their own).
        draft = models.FormDraft.objects.get_valid(self.admin, "feeding", self.child.pk)
        self.assertEqual(draft.data, {"notes": "secret"})

    def test_caregiver_permission_matches_add_permission(self):
        carer = Client()
        carer.force_login(self.caregiver)

        # Caregivers may add feedings but not pumping.
        self.assertEqual(
            carer.post(
                ENDPOINT,
                self.payload(form_key="feeding"),
                content_type="application/json",
            ).status_code,
            200,
        )
        self.assertEqual(carer.get(ENDPOINT, {"form_key": "pumping"}).status_code, 403)
        self.assertEqual(
            carer.post(
                ENDPOINT,
                self.payload(form_key="pumping"),
                content_type="application/json",
            ).status_code,
            403,
        )

        self.caregiver.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="core", codename="add_pumping"
            )
        )
        self.assertEqual(
            carer.post(
                ENDPOINT,
                self.payload(form_key="pumping", data={"amount": "90"}),
                content_type="application/json",
            ).status_code,
            200,
        )

    def test_bottle_feeding_key_uses_its_own_slot_and_fields(self):
        response = self.client.post(
            ENDPOINT,
            self.payload(
                form_key="bottle-feeding",
                data={"child": self.child.pk, "type": "formula", "amount": "120"},
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        # Bottle and full feeding forms do not share a slot.
        self.assertIsNone(
            models.FormDraft.objects.get_valid(self.admin, "feeding", self.child.pk)
        )
        draft = models.FormDraft.objects.get_valid(
            self.admin, "bottle-feeding", self.child.pk
        )
        self.assertEqual(draft.data["amount"], "120")
        # `method` exists on FeedingForm but not on BottleFeedingForm.
        bad = self.client.post(
            ENDPOINT,
            self.payload(form_key="bottle-feeding", data={"method": "bottle"}),
            content_type="application/json",
        )
        self.assertEqual(bad.status_code, 400)

    def test_invalid_payloads_are_rejected_without_saving(self):
        base = {"child": self.child.pk}

        def post(body):
            return self.client.post(
                ENDPOINT,
                json.dumps(body),
                content_type="application/json",
            )

        cases = [
            ("unknown form", {**base, "form_key": "diaperchange", "data": {}}),
            ("unknown child", {**base, "form_key": "feeding", "child": 999999}),
            ("non-string child", {**base, "form_key": "feeding", "child": "abc"}),
            ("data not object", {**base, "form_key": "feeding", "data": []}),
            ("unknown field", {**base, "form_key": "sleep", "data": {"hack": "x"}}),
            ("nested value", {**base, "form_key": "sleep", "data": {"notes": {}}}),
            (
                "oversized",
                {**base, "form_key": "sleep", "data": {"notes": "x" * 70000}},
            ),
        ]
        for label, body in cases:
            with self.subTest(case=label):
                response = post(body)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(models.FormDraft.objects.count(), 0)

        self.assertEqual(
            self.client.post(
                ENDPOINT, "not json", content_type="application/json"
            ).status_code,
            400,
        )

    def test_dynamic_initial_bookkeeping_keys_are_stripped(self):
        # Django renders hidden initial-start/initial-end inputs for fields
        # with dynamic defaults; clients may echo them back.
        response = self.client.post(
            ENDPOINT,
            self.payload(
                form_key="feeding",
                data={"notes": "x", "initial-start": "2026-01-01 00:00:00"},
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        draft = models.FormDraft.objects.get_valid(self.admin, "feeding", self.child.pk)
        self.assertEqual(draft.data, {"notes": "x"})

    def test_empty_data_does_not_create_a_draft(self):
        for data in ({}, {"notes": ""}, {"nap": False, "notes": None}):
            response = self.client.post(
                ENDPOINT,
                self.payload(form_key="sleep", data=data),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["saved"])
        self.assertEqual(models.FormDraft.objects.count(), 0)

    def test_null_child_slot_roundtrip(self):
        response = self.client.post(
            ENDPOINT,
            json.dumps({"form_key": "feeding", "child": None, "data": {"notes": "?"}}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["child"])
        self.assertEqual(
            self.client.get(ENDPOINT, {"form_key": "feeding"}).status_code, 200
        )

    def test_delete_removes_draft_and_is_idempotent(self):
        self.client.post(ENDPOINT, self.payload(), content_type="application/json")
        slot_query = "?form_key=feeding&child={}".format(self.child.pk)
        response = self.client.delete(ENDPOINT + slot_query)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            self.client.get(
                ENDPOINT, {"form_key": "feeding", "child": self.child.pk}
            ).status_code,
            404,
        )
        self.assertEqual(self.client.delete(ENDPOINT + slot_query).status_code, 204)

    def test_missing_form_key_is_rejected(self):
        query = "?child={}".format(self.child.pk)
        self.assertEqual(self.client.get(ENDPOINT + query).status_code, 400)
        self.assertEqual(self.client.delete(ENDPOINT + query).status_code, 400)

    def test_csrf_is_enforced(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.admin)
        response = strict.post(
            ENDPOINT, self.payload(), content_type="application/json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(models.FormDraft.objects.count(), 0)

    def test_draft_writes_emit_no_webhook_events(self):
        WebhookEndpoint.objects.create(name="hook", url="https://example.test/hook")
        self.client.post(
            ENDPOINT,
            self.payload(data={"notes": "first"}),
            content_type="application/json",
        )
        self.client.post(
            ENDPOINT,
            self.payload(data={"notes": "second"}),
            content_type="application/json",
        )
        self.client.delete(
            ENDPOINT + "?form_key=feeding&child={}".format(self.child.pk)
        )
        self.assertEqual(WebhookEvent.objects.count(), 0)


class DraftViewIntegrationTestCase(TestCase):
    def setUp(self):
        self.child = models.Child.objects.create(
            first_name="A", last_name="Child", birth_date=datetime.date(2023, 1, 1)
        )
        self.other_child = models.Child.objects.create(
            first_name="B", last_name="Child", birth_date=datetime.date(2023, 2, 1)
        )
        self.user = User.objects.create_superuser(
            username="draft-parent", password="whatever"
        )
        self.client.force_login(self.user)
        # Far enough back that every subtest's shifted start stays non-future.
        self.base = timezone.now() - datetime.timedelta(days=30)

    def entry_data(self, day, **extra):
        start = self.base + datetime.timedelta(days=day, hours=2)
        data = {
            "child": self.child.pk,
            "start": start.isoformat(),
            "end": (start + datetime.timedelta(minutes=10)).isoformat(),
            "notes": "a note",
        }
        data.update(extra)
        return data

    ENABLED_VIEWS = [
        (
            "/feedings/add/",
            "feeding",
            models.Feeding,
            {"type": "formula", "method": "bottle", "amount": "60"},
        ),
        (
            "/feedings/bottle/add/",
            "bottle-feeding",
            models.Feeding,
            {"type": "formula", "amount": "60"},
        ),
        ("/sleep/add/", "sleep", models.Sleep, {"nap": "on"}),
        ("/pumping/add/", "pumping", models.Pumping, {"amount": "50"}),
        (
            "/tummy-time/add/",
            "tummytime",
            models.TummyTime,
            {"milestone": "rolled over"},
        ),
    ]

    def test_successful_submit_clears_only_its_slot(self):
        for index, (url, key, model, extra) in enumerate(self.ENABLED_VIEWS):
            with self.subTest(url=url):
                # Bottle feeding has no end field.
                data = self.entry_data(index * 2, **extra)
                if key == "bottle-feeding":
                    data.pop("end")
                models.FormDraft.objects.upsert(
                    self.user, key, self.child.pk, {"notes": "half written"}
                )
                decoy_child = models.FormDraft.objects.upsert(
                    self.user, key, self.other_child.pk, {"notes": "other child"}
                )
                other_key = "pumping" if key == "sleep" else "sleep"
                decoy_form = models.FormDraft.objects.upsert(
                    self.user, other_key, self.child.pk, {"notes": "other form"}
                )
                before = model.objects.count()

                response = self.client.post(url, data)

                self.assertEqual(response.status_code, 302, url and response.content)
                self.assertEqual(model.objects.count(), before + 1)
                self.assertIsNone(
                    models.FormDraft.objects.get_valid(self.user, key, self.child.pk)
                )
                self.assertTrue(
                    models.FormDraft.objects.filter(pk=decoy_child.pk).exists()
                )
                self.assertTrue(
                    models.FormDraft.objects.filter(pk=decoy_form.pk).exists()
                )

    def test_failed_submit_persists_last_values_without_saving_record(self):
        data = self.entry_data(
            0, type="formula", method="bottle", amount="not-a-number"
        )
        # Also make the duration invalid.
        data["end"] = (self.base + datetime.timedelta(days=0, hours=1)).isoformat()

        response = self.client.post("/feedings/add/", data)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(models.Feeding.objects.count(), 0)
        draft = models.FormDraft.objects.get_valid(self.user, "feeding", self.child.pk)
        self.assertIsNotNone(draft)
        self.assertEqual(draft.data["notes"], "a note")
        self.assertEqual(draft.data["method"], "bottle")
        self.assertNotIn("csrfmiddlewaretoken", draft.data)

        # The last attempt is visible to another session of the same account.
        tablet = Client()
        tablet.force_login(self.user)
        body = tablet.get(
            ENDPOINT, {"form_key": "feeding", "child": self.child.pk}
        ).json()
        self.assertEqual(body["data"]["notes"], "a note")

    def test_draft_context_only_on_enabled_add_views(self):
        for url, key, _, _ in self.ENABLED_VIEWS:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["draft"]["key"], key)
                self.assertEqual(response.context["draft"]["url"], "/drafts/")
                self.assertFalse(response.context["draft"]["bound"])
                html = response.content.decode()
                self.assertIn(f'data-draft-key="{key}"', html)
                self.assertIn('data-draft-url="/drafts/"', html)
                self.assertIn('data-draft-bound="false"', html)
                self.assertIn("data-draft-banner", html)
                self.assertIn("data-draft-discard", html)
                self.assertIn("data-draft-i18n", html)

        feeding = models.Feeding.objects.create(
            child=self.child,
            start=self.base,
            end=self.base + datetime.timedelta(minutes=5),
            type="formula",
            method="bottle",
        )
        disabled = [
            f"/feedings/{feeding.pk}/",
            "/changes/add/",
            "/notes/add/",
            "/timers/add/",
            "/children/add/",
        ]
        for url in disabled:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("draft", response.context)
                self.assertNotIn(b"data-draft-key", response.content)

    def test_context_child_resolution(self):
        response = self.client.get(f"/feedings/add/?child={self.child.slug}")
        self.assertEqual(response.context["draft"]["child"], self.child.pk)

        response = self.client.get("/feedings/add/")
        self.assertEqual(response.context["draft"]["child"], "")

    def test_single_child_library_auto_scopes_draft_context(self):
        # A fresh child-only scenario is provided by deleting the second child.
        self.other_child.delete()
        response = self.client.get("/sleep/add/")
        self.assertEqual(response.context["draft"]["child"], self.child.pk)

    def test_bound_context_on_re_render(self):
        response = self.client.post(
            "/sleep/add/", {"child": self.child.pk, "start": "not-a-time"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["draft"]["bound"])

    def test_save_time_validation_error_keeps_draft(self):
        # A ValidationError raised while saving (e.g. a timer consumed by
        # another device between GET and POST) is turned back into the form by
        # CoreFormMixin; the rejected submit must keep and refresh the draft
        # instead of having it discarded by a 200 "form_valid" response.
        from unittest import mock

        from django.core.exceptions import ValidationError as DjangoValidationError

        data = self.entry_data(0, type="formula", method="bottle", amount="60")
        models.FormDraft.objects.upsert(
            self.user, "feeding", self.child.pk, {"notes": "older draft"}
        )
        with mock.patch.object(
            forms.CoreModelForm,
            "save",
            side_effect=DjangoValidationError("save failed"),
        ):
            response = self.client.post("/feedings/add/", data)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(models.Feeding.objects.count(), 0)
        draft = models.FormDraft.objects.get_valid(self.user, "feeding", self.child.pk)
        self.assertIsNotNone(draft)
        self.assertEqual(draft.data["notes"], "a note")

    def test_timer_entry_coexists_with_draft_lifecycle(self):
        start = timezone.now() - datetime.timedelta(hours=2)
        timer = models.Timer.objects.create(
            user=self.user, child=self.child, start=start
        )
        url = f"/feedings/add/?timer={timer.pk}"
        self.assertEqual(self.client.get(url).status_code, 200)
        models.FormDraft.objects.upsert(
            self.user, "feeding", self.child.pk, {"notes": "timer draft"}
        )

        data = {
            "child": self.child.pk,
            "start": start.isoformat(),
            "end": (start + datetime.timedelta(minutes=10)).isoformat(),
            "type": "formula",
            "method": "bottle",
            "amount": "60",
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(models.Timer.objects.filter(pk=timer.pk).exists())
        self.assertIsNone(
            models.FormDraft.objects.get_valid(self.user, "feeding", self.child.pk)
        )

    def test_timer_permission_failure_saves_nothing_and_leaves_no_draft(self):
        carer_user = User.objects.create_user(username="timerless-carer")
        carer_user.groups.add(
            Group.objects.get(name=settings.BABY_BUDDY["CAREGIVER_GROUP_NAME"])
        )
        other_owner = User.objects.create_user(username="timer-owner-2")
        start = timezone.now() - datetime.timedelta(hours=3)
        timer = models.Timer.objects.create(
            user=other_owner, child=self.child, start=start
        )
        carer = Client()
        carer.force_login(carer_user)
        data = {
            "child": self.child.pk,
            "start": start.isoformat(),
            "end": (start + datetime.timedelta(minutes=10)).isoformat(),
            "type": "formula",
            "method": "bottle",
        }
        response = carer.post(f"/feedings/add/?timer={timer.pk}", data)
        self.assertIn(response.status_code, (200, 400, 403))
        self.assertTrue(models.Timer.objects.filter(pk=timer.pk).exists())
        self.assertEqual(models.Feeding.objects.count(), 0)
        self.assertEqual(models.FormDraft.objects.filter(user=carer_user).count(), 0)

    def test_failed_submit_emits_no_event_but_successful_one_does(self):
        WebhookEndpoint.objects.create(name="hook", url="https://example.test/hook")

        bad = self.entry_data(0, type="formula", method="bottle")
        bad["end"] = (self.base - datetime.timedelta(hours=1)).isoformat()
        self.client.post("/feedings/add/", bad)
        self.assertEqual(WebhookEvent.objects.count(), 0)

        good = self.entry_data(2, type="formula", method="bottle", amount="60")
        self.client.post("/feedings/add/", good)
        events = WebhookEvent.objects.all()
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().type, "feeding.created")
