# -*- coding: utf-8 -*-
import datetime
from unittest.mock import patch

from django.conf import settings
from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import formats, timezone

from webhooks import admin as webhook_admin
from webhooks.models import WebhookEndpoint, WebhookSecret


class WebhookEndpointAdminTestCase(TestCase):
    fixtures = ["tests.json"]

    def setUp(self):
        call_command("migrate", verbosity=0)
        self.endpoint = WebhookEndpoint.objects.create(
            name="Home",
            url="https://receiver.example.com/hook",
            secret="the-old-secret-1234567890",
        )
        self.client.login(username="admin", password="admin")
        self.changelist_url = reverse("admin:webhooks_webhookendpoint_changelist")
        self.change_url = reverse(
            "admin:webhooks_webhookendpoint_change", args=[self.endpoint.pk]
        )
        self.rotate_url = reverse(
            "admin:webhooks_webhookendpoint_rotate_secret",
            args=[self.endpoint.pk],
        )

    def test_changelist_shows_the_current_version(self):
        response = self.client.get(self.changelist_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "v1")
        # Secrets never appear on an admin page.
        self.assertNotContains(response, "the-old-secret-1234567890")

    def test_changelist_shows_when_an_old_key_stops_working(self):
        cutoff = timezone.now() + datetime.timedelta(hours=24)
        self.endpoint.rotate_secret(
            new_secret="the-new-secret-0987654321", expires_at=cutoff
        )
        response = self.client.get(self.changelist_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "v2")
        self.assertContains(response, "v1")
        expected = formats.date_format(timezone.localtime(cutoff), "DATETIME_FORMAT")
        self.assertContains(response, expected)
        self.assertNotContains(response, "the-new-secret-0987654321")
        self.assertNotContains(response, "the-old-secret-1234567890")

    def test_change_page_lists_each_key_but_never_a_secret(self):
        cutoff = timezone.now() + datetime.timedelta(hours=24)
        self.endpoint.rotate_secret(
            new_secret="the-new-secret-0987654321", expires_at=cutoff
        )
        response = self.client.get(self.change_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "v1")
        self.assertContains(response, "v2")
        self.assertContains(response, "Current")
        self.assertNotContains(response, "the-old-secret-1234567890")
        self.assertNotContains(response, "the-new-secret-0987654321")
        # The rotation action is reachable from the change page.
        self.assertContains(response, self.rotate_url)

    def test_the_rotation_form_opens_with_a_one_day_cutoff(self):
        before = timezone.now()
        response = self.client.get(self.rotate_url)
        after = timezone.now()
        self.assertEqual(response.status_code, 200)
        initial = response.context["form"]["expires_at"].initial
        self.assertGreaterEqual(initial, before + datetime.timedelta(hours=23))
        self.assertLessEqual(initial, after + datetime.timedelta(hours=25))

    def test_a_cutoff_in_the_past_is_refused(self):
        # Yesterday: past in whichever timezone the admin user has it entered
        # in, not merely one that a UTC offset could carry into the future.
        past = timezone.localtime() - datetime.timedelta(days=1)
        response = self.client.post(
            self.rotate_url,
            {
                "expires_at_0": past.strftime("%Y-%m-%d"),
                "expires_at_1": past.strftime("%H:%M"),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("expires_at", response.context["form"].errors)
        self.assertEqual(WebhookSecret.objects.count(), 1)
        self.assertEqual(self.endpoint.current_secret.version, 1)

    def test_a_short_new_secret_is_refused(self):
        future = timezone.now() + datetime.timedelta(hours=24)
        response = self.client.post(
            self.rotate_url,
            {
                "expires_at_0": future.strftime("%Y-%m-%d"),
                "expires_at_1": future.strftime("%H:%M"),
                "new_secret": "short",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("new_secret", response.context["form"].errors)
        self.assertEqual(WebhookSecret.objects.count(), 1)

    def test_a_valid_rotation_is_done_and_the_secret_is_shown_once(self):
        future = timezone.now() + datetime.timedelta(hours=24)
        response = self.client.post(
            self.rotate_url,
            {
                "expires_at_0": future.strftime("%Y-%m-%d"),
                "expires_at_1": future.strftime("%H:%M"),
                "new_secret": "the-secret-i-typed-in-12345",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.endpoint.refresh_from_db()
        new_key = self.endpoint.current_secret
        self.assertEqual(new_key.version, 2)
        self.assertEqual(new_key.secret, "the-secret-i-typed-in-12345")
        retired = WebhookSecret.objects.get(version=1)
        self.assertIsNotNone(retired.expires_at)
        # The one place the new secret appears.
        self.assertContains(response, "the-secret-i-typed-in-12345")

        # And nowhere else afterwards: not the change page, not the list.
        for url in (self.change_url, self.changelist_url):
            follow_up = self.client.get(url)
            self.assertNotContains(follow_up, "the-secret-i-typed-in-12345")

    def test_a_cutoff_overtaken_by_the_clock_is_a_form_error_not_a_500(self):
        # The form passed, but the locked write loses the race with the clock:
        # the page reports it and no rotation happens.
        future = timezone.now() + datetime.timedelta(hours=24)
        with patch.object(
            WebhookEndpoint,
            "rotate_secret",
            side_effect=ValidationError("The cutoff time must be in the future."),
        ):
            response = self.client.post(
                self.rotate_url,
                {
                    "expires_at_0": future.strftime("%Y-%m-%d"),
                    "expires_at_1": future.strftime("%H:%M"),
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].non_field_errors())
        self.assertIsNone(response.context["new_key"])
        self.assertEqual(WebhookSecret.objects.count(), 1)
        self.assertEqual(self.endpoint.current_secret.version, 1)

    def test_a_rotation_with_a_blank_secret_generates_one(self):
        future = timezone.now() + datetime.timedelta(hours=24)
        response = self.client.post(
            self.rotate_url,
            {
                "expires_at_0": future.strftime("%Y-%m-%d"),
                "expires_at_1": future.strftime("%H:%M"),
                "new_secret": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        new_key = self.endpoint.current_secret
        self.assertEqual(new_key.version, 2)
        self.assertGreaterEqual(len(new_key.secret), 16)
        self.assertContains(response, new_key.secret)

    def test_anonymous_visitors_are_sent_to_login(self):
        self.client.logout()
        response = self.client.get(self.rotate_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def test_caregivers_cannot_rotate(self):
        self.client.logout()
        user = get_user_model().objects.create_user(
            username="caregiver-rotate", password="password"
        )
        user.groups.add(
            Group.objects.get(name=settings.BABY_BUDDY["CAREGIVER_GROUP_NAME"])
        )
        self.client.login(username="caregiver-rotate", password="password")
        response = self.client.get(self.rotate_url)
        self.assertEqual(response.status_code, 302)

    def test_keys_have_no_hand_editing_entry_points(self):
        # The inline is the only place keys show up, and it adds and deletes
        # nothing; the model has no standalone admin section either.
        self.assertNotIn(WebhookSecret, django_admin.site._registry)
        inline = webhook_admin.WebhookSecretInline(WebhookEndpoint, django_admin.site)
        self.assertFalse(inline.has_add_permission(None))
        self.assertFalse(inline.has_delete_permission(None))
