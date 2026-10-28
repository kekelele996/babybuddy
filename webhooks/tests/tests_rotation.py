# -*- coding: utf-8 -*-
import datetime

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection, transaction
from django.db.utils import DatabaseError, IntegrityError
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from webhooks import delivery
from webhooks.models import WebhookEndpoint, WebhookSecret


class WebhookKeyRotationTestCase(TestCase):
    def setUp(self):
        call_command("migrate", verbosity=0)
        self.endpoint = WebhookEndpoint.objects.create(
            name="Home",
            url="http://home.test/hook",
            secret="v1-secret-0123456789abcdef",
        )

    def test_a_new_endpoint_starts_with_key_version_1(self):
        key = self.endpoint.current_secret
        self.assertEqual(key.version, 1)
        self.assertIsNone(key.expires_at)
        self.assertEqual(self.endpoint.secret, "v1-secret-0123456789abcdef")
        self.assertTrue(key.is_current)

    def test_rotation_makes_a_new_current_key_and_retires_the_old(self):
        cutoff = timezone.now() + datetime.timedelta(hours=24)
        new_key = self.endpoint.rotate_secret(expires_at=cutoff)

        self.assertEqual(new_key.version, 2)
        self.assertIsNone(new_key.expires_at)
        old_key = WebhookSecret.objects.get(endpoint=self.endpoint, version=1)
        self.assertEqual(old_key.expires_at, cutoff)
        self.assertEqual(self.endpoint.current_secret, new_key)
        self.assertEqual(self.endpoint.secret, new_key.secret)

    def test_the_new_secret_is_generated_when_none_is_given(self):
        new_key = self.endpoint.rotate_secret(
            expires_at=timezone.now() + datetime.timedelta(hours=1)
        )
        self.assertGreaterEqual(len(new_key.secret), 16)
        self.assertNotEqual(new_key.secret, "v1-secret-0123456789abcdef")

    def test_a_chosen_new_secret_is_kept(self):
        cutoff = timezone.now() + datetime.timedelta(hours=1)
        new_key = self.endpoint.rotate_secret(
            new_secret="the-replacement-secret-1234567890", expires_at=cutoff
        )
        self.assertEqual(new_key.secret, "the-replacement-secret-1234567890")
        self.assertEqual(self.endpoint.secret, "the-replacement-secret-1234567890")

    def test_a_short_chosen_secret_is_refused_and_nothing_changes(self):
        with self.assertRaises(ValidationError):
            self.endpoint.rotate_secret(
                new_secret="short",
                expires_at=timezone.now() + datetime.timedelta(hours=1),
            )
        self.assertEqual(WebhookSecret.objects.count(), 1)
        self.assertEqual(self.endpoint.current_secret.version, 1)

    def test_a_past_cutoff_is_refused(self):
        with self.assertRaises(ValidationError):
            self.endpoint.rotate_secret(
                expires_at=timezone.now() - datetime.timedelta(minutes=1)
            )
        self.assertEqual(self.endpoint.current_secret.version, 1)

    def test_a_naive_cutoff_is_refused(self):
        with self.assertRaises(ValidationError):
            self.endpoint.rotate_secret(
                expires_at=datetime.datetime.now()  # noqa: DTZ005
            )

    def test_an_immediate_rotation_discards_the_old_key(self):
        # The API's "PATCH secret" path: no overlap, old key gone at once.
        new_key = self.endpoint.rotate_secret(new_secret="brand-new-secret-0123456789")
        self.assertEqual(new_key.version, 2)
        self.assertEqual(
            list(
                WebhookSecret.objects.filter(endpoint=self.endpoint).values_list(
                    "version", flat=True
                )
            ),
            [2],
        )
        self.assertEqual(self.endpoint.secret, "brand-new-secret-0123456789")

    def test_version_numbers_keep_climbing_after_an_immediate_rotation(self):
        self.endpoint.rotate_secret()
        self.endpoint.rotate_secret()
        self.assertEqual(self.endpoint.current_secret.version, 3)

    def test_chained_rotations_keep_each_own_cutoff(self):
        first_cutoff = timezone.now() + datetime.timedelta(hours=1)
        second_cutoff = timezone.now() + datetime.timedelta(hours=2)
        self.endpoint.rotate_secret(expires_at=first_cutoff)
        self.endpoint.rotate_secret(expires_at=second_cutoff)

        keys = {
            key.version: key
            for key in WebhookSecret.objects.filter(endpoint=self.endpoint)
        }
        self.assertEqual(set(keys), {1, 2, 3})
        self.assertEqual(keys[1].expires_at, first_cutoff)
        self.assertEqual(keys[2].expires_at, second_cutoff)
        self.assertIsNone(keys[3].expires_at)

    def test_an_immediate_rotation_leaves_other_retired_keys_alone(self):
        first_cutoff = timezone.now() + datetime.timedelta(hours=1)
        second_cutoff = timezone.now() + datetime.timedelta(hours=2)
        self.endpoint.rotate_secret(expires_at=first_cutoff)
        self.endpoint.rotate_secret(expires_at=second_cutoff)
        self.endpoint.rotate_secret()  # v3 replaced at once

        versions = set(
            WebhookSecret.objects.filter(endpoint=self.endpoint).values_list(
                "version", flat=True
            )
        )
        self.assertEqual(versions, {1, 2, 4})

    def test_valid_secrets_has_the_current_key_first_then_unexpired_old_ones(self):
        now = timezone.now()
        self.endpoint.rotate_secret(expires_at=now + datetime.timedelta(hours=1))
        self.endpoint.rotate_secret(expires_at=now + datetime.timedelta(hours=2))

        valid = self.endpoint.valid_secrets(now=now)
        self.assertEqual([key.version for key in valid], [3, 2, 1])

    def test_valid_secrets_excludes_keys_at_and_after_their_cutoff(self):
        cutoff = timezone.now() + datetime.timedelta(hours=1)
        self.endpoint.rotate_secret(expires_at=cutoff)
        self.assertEqual(
            [key.version for key in self.endpoint.valid_secrets(now=cutoff)], [2]
        )
        self.assertEqual(
            [
                key.version
                for key in self.endpoint.valid_secrets(
                    now=cutoff + datetime.timedelta(seconds=1)
                )
            ],
            [2],
        )

    def test_is_valid_follows_the_cutoff(self):
        cutoff = timezone.now() + datetime.timedelta(hours=1)
        self.endpoint.rotate_secret(expires_at=cutoff)
        old_key = WebhookSecret.objects.get(version=1)
        self.assertTrue(old_key.is_valid(now=cutoff - datetime.timedelta(seconds=1)))
        self.assertFalse(old_key.is_valid(now=cutoff))

    def test_purge_deletes_keys_at_and_before_the_cutoff_only(self):
        now = timezone.now()
        # A past-cutoff key and a future-cutoff key around the current one.
        WebhookSecret.objects.filter(endpoint=self.endpoint, version=1).update(
            expires_at=now - datetime.timedelta(minutes=1)
        )
        WebhookSecret.objects.create(
            endpoint=self.endpoint,
            version=2,
            secret="retired-but-valid-123456",
            expires_at=now + datetime.timedelta(hours=1),
        )
        WebhookSecret.objects.create(
            endpoint=self.endpoint,
            version=3,
            secret="the-current-key-123456789",
        )
        # A key whose cutoff is exactly now is already due to go.
        boundary = WebhookSecret.objects.create(
            endpoint=self.endpoint,
            version=4,
            secret="boundary-cutoff-key-12345",
            expires_at=now,
        )

        deleted = WebhookSecret.purge_expired(now=now)
        self.assertEqual(deleted[0], 2)
        remaining = set(
            WebhookSecret.objects.filter(endpoint=self.endpoint).values_list(
                "version", flat=True
            )
        )
        self.assertEqual(remaining, {2, 3})
        self.assertFalse(WebhookSecret.objects.filter(pk=boundary.pk).exists())

    def test_version_is_unique_per_endpoint_but_shared_numbers_are_fine(self):
        other = WebhookEndpoint.objects.create(
            name="Other", url="http://other.test/hook", secret="other-v1-1234567890"
        )
        # Both endpoints independently carry a version 1.
        self.assertEqual(other.current_secret.version, 1)
        # Two current keys on one endpoint are impossible.
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                WebhookSecret.objects.create(
                    endpoint=self.endpoint, version=99, secret="x" * 16
                )

    def test_bulk_created_endpoints_get_their_version_1_keys(self):
        created = WebhookEndpoint.objects.bulk_create(
            [
                WebhookEndpoint(name="One", url="http://one.test/hook"),
                WebhookEndpoint(
                    name="Two",
                    url="http://two.test/hook",
                    secret="chosen-bulk-secret-1234567",
                ),
            ]
        )
        self.assertEqual(
            [endpoint.current_secret.version for endpoint in created], [1, 1]
        )
        self.assertEqual(created[1].secret, "chosen-bulk-secret-1234567")
        self.assertGreaterEqual(len(created[0].secret), 16)
        # Such an endpoint can sign, rather than send an unsigned request.
        keys = created[0].valid_secrets()
        self.assertEqual(len(keys), 1)
        self.assertTrue(delivery.signature_header(keys, 1000, "{}").startswith("v1="))

    def test_deleting_an_endpoint_takes_its_keys_with_it(self):
        self.endpoint.rotate_secret(
            expires_at=timezone.now() + datetime.timedelta(hours=1)
        )
        self.assertEqual(WebhookSecret.objects.count(), 2)
        self.endpoint.delete()
        self.assertEqual(WebhookSecret.objects.count(), 0)


class SecretMigrationTestCase(TransactionTestCase):
    """
    The 0002 upgrade turns the single secret column into a version 1 key row
    and then removes the column.
    """

    def test_the_existing_secret_becomes_version_1(self):
        call_command("migrate", "webhooks", "0001", verbosity=0)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO webhooks_webhookendpoint "
                    "(name, url, secret, active, created, last_delivery) "
                    "VALUES (%s, %s, %s, %s, %s, NULL)",
                    [
                        "Old",
                        "http://old.test/hook",
                        "the-original-secret",
                        True,
                        timezone.now(),
                    ],
                )

            call_command("migrate", "webhooks", "0002", verbosity=0)

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT version, secret, expires_at FROM webhooks_webhooksecret"
                )
                self.assertEqual(cursor.fetchall(), [(1, "the-original-secret", None)])
                with self.assertRaises(DatabaseError):
                    cursor.execute("SELECT secret FROM webhooks_webhookendpoint")

            # Going backwards puts the earliest key back on the endpoint.
            call_command("migrate", "webhooks", "0001", verbosity=0)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT secret FROM webhooks_webhookendpoint WHERE name = %s",
                    ["Old"],
                )
                self.assertEqual(cursor.fetchone(), ("the-original-secret",))
        finally:
            call_command("migrate", "webhooks", verbosity=0)
