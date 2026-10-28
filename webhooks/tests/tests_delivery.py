# -*- coding: utf-8 -*-
import datetime
import hashlib
import hmac
import json
import threading
from io import StringIO
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from core import models

from webhooks import delivery
from webhooks.models import WebhookEndpoint, WebhookEvent, WebhookSecret


class RecordingServerTestCase(TestCase):
    """
    Delivers to a real local server.

    Mocking the HTTP layer would prove that the code calls itself. This proves
    what actually arrives, which is the part a receiving application has to
    cope with.
    """

    def setUp(self):
        call_command("migrate", verbosity=0)
        self.received = []
        self.status = 200
        case = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                case.received.append(
                    {
                        "path": self.path,
                        "headers": {key: value for key, value in self.headers.items()},
                        "body": body,
                    }
                )
                self.send_response(case.status)
                self.end_headers()
                self.wfile.write(b"thanks")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.endpoint = WebhookEndpoint.objects.create(
            name="Home",
            url="http://127.0.0.1:{}/hook".format(self.server.server_address[1]),
            secret="the secret of this endpoint",
        )
        self.event = WebhookEvent.objects.create(
            endpoint=self.endpoint,
            type="feeding.created",
            object_id="42",
        )

    def test_what_the_receiver_sees(self):
        delivery.deliver_pending()
        self.assertEqual(len(self.received), 1)
        request = self.received[0]

        self.assertEqual(request["path"], "/hook")
        self.assertEqual(request["headers"]["Content-Type"], "application/json")

        body = json.loads(request["body"])
        self.assertEqual(sorted(body), ["created", "id", "object_id", "type"])
        self.assertEqual(body["type"], "feeding.created")
        self.assertEqual(body["object_id"], "42")
        self.assertEqual(body["id"], str(self.event.event_id))

    def test_nothing_about_the_child_travels(self):
        # The whole point: an endpoint is told that something changed and has
        # to ask the API for the rest, so it can learn nothing from the wire.
        self.event.type = "feeding.created"
        self.event.object_id = "42"
        self.event.save()
        delivery.deliver_pending()
        body = self.received[0]["body"]
        self.assertNotIn("child", body.lower())
        self.assertNotIn("amount", body.lower())
        self.assertNotIn("name", body.lower())

    def test_the_receiver_can_check_the_signature(self):
        delivery.deliver_pending()
        headers = self.received[0]["headers"]
        body = self.received[0]["body"]
        timestamp = headers["X-BabyBuddy-Timestamp"]

        message = "{}.{}".format(timestamp, body).encode()
        expected = hmac.new(
            b"the secret of this endpoint", message, hashlib.sha256
        ).hexdigest()

        self.assertEqual(headers["X-BabyBuddy-Signature"], "v1=" + expected)
        self.assertEqual(headers["X-BabyBuddy-Event-Id"], str(self.event.event_id))
        self.assertEqual(headers["X-BabyBuddy-Event-Type"], "feeding.created")

    def test_each_endpoint_signs_with_its_own_secret(self):
        # Two endpoints on one URL must not be interchangeable: a signature
        # that is good for one has to be worthless for the other.
        other = WebhookEndpoint.objects.create(
            name="Other",
            url=self.endpoint.url,
            secret="a different secret entirely",
        )
        WebhookEvent.objects.create(endpoint=other, type="sleep.created", object_id="7")
        delivery.deliver_pending()
        self.assertEqual(len(self.received), 2)

        secrets_used = set()
        for request in self.received:
            headers = request["headers"]
            timestamp = headers["X-BabyBuddy-Timestamp"]
            message = "{}.{}".format(timestamp, request["body"]).encode()
            for secret in (
                b"the secret of this endpoint",
                b"a different secret entirely",
            ):
                expected = hmac.new(secret, message, hashlib.sha256).hexdigest()
                if headers["X-BabyBuddy-Signature"] == "v1=" + expected:
                    secrets_used.add(secret)
        self.assertEqual(len(secrets_used), 2, "both endpoints used one secret")

    def test_a_tampered_body_is_not_accepted(self):
        delivery.deliver_pending()
        headers = self.received[0]["headers"]
        timestamp = headers["X-BabyBuddy-Timestamp"]

        message = "{}.{}".format(timestamp, "{}").encode()
        expected = hmac.new(
            b"the secret of this endpoint", message, hashlib.sha256
        ).hexdigest()

        self.assertNotEqual(headers["X-BabyBuddy-Signature"], "v1=" + expected)

    def test_the_timestamp_cannot_be_moved(self):
        # What covering the timestamp buys: nobody can take a captured body and
        # give it a new timestamp to make it look fresh.
        secret = "the secret of this endpoint"
        body = "{}"
        self.assertNotEqual(
            delivery.signature(secret, 1000, body),
            delivery.signature(secret, 1001, body),
        )

    def test_a_verbatim_replay_still_verifies(self):
        # The limit, named so nobody assumes otherwise. A request sent again
        # exactly as it was captured is byte for byte the same request and its
        # digest is the same, so it verifies however long ago it was taken.
        # Signing the timestamp does not stop that: refusing a stale timestamp
        # and keeping the event ids already seen is what does, and both are the
        # receiver's to do. See docs/configuration/webhooks.md.
        secret = "the secret of this endpoint"
        body = "{}"
        captured = delivery.signature(secret, 1000, body)
        self.assertEqual(captured, delivery.signature(secret, 1000, body))

    def test_the_command_delivers(self):
        out = StringIO()
        call_command("deliver_webhooks", stdout=out, timeout=5)
        self.assertIn("Delivered 1 event(s).", out.getvalue())
        self.assertEqual(len(self.received), 1)

    def test_the_command_says_so_when_there_is_nothing_to_do(self):
        WebhookEvent.objects.all().delete()
        out = StringIO()
        call_command("deliver_webhooks", stdout=out, timeout=5)
        self.assertIn("Nothing to deliver", out.getvalue())
        self.assertEqual(self.received, [])

    def test_every_keeps_delivering_until_stopped(self):
        # Two runs, then the sleep after the second one is where it stops.
        naps = []

        def sleep(seconds):
            naps.append(seconds)
            if len(naps) == 2:
                raise KeyboardInterrupt
            WebhookEvent.objects.create(endpoint=self.endpoint, type="feeding.updated")

        out = StringIO()
        with patch("webhooks.management.commands.deliver_webhooks.time.sleep", sleep):
            call_command("deliver_webhooks", stdout=out, timeout=5, every=1)
        self.assertEqual(naps, [1.0, 1.0])
        self.assertEqual(len(self.received), 2)
        self.assertEqual(out.getvalue().count("Delivered 1 event(s)."), 2)

    def test_every_writes_each_delivery_out_straight_away(self):
        class Buffered(StringIO):
            flushed = ""

            def flush(self):
                self.flushed = self.getvalue()

        out = Buffered()
        with patch(
            "webhooks.management.commands.deliver_webhooks.time.sleep",
            side_effect=[KeyboardInterrupt],
        ):
            call_command("deliver_webhooks", stdout=out, timeout=5, every=1)
        self.assertIn("Delivered 1 event(s).", out.flushed)

    def test_every_is_quiet_while_there_is_nothing_to_send(self):
        WebhookEvent.objects.all().delete()
        out = StringIO()
        with patch(
            "webhooks.management.commands.deliver_webhooks.time.sleep",
            side_effect=[None, KeyboardInterrupt],
        ):
            call_command("deliver_webhooks", stdout=out, timeout=5, every=1)
        self.assertEqual(out.getvalue(), "")

    def test_a_failed_run_does_not_stop_every(self):
        err = StringIO()
        with patch(
            "webhooks.management.commands.deliver_webhooks.deliver_pending",
            side_effect=[RuntimeError("no such table: webhooks_webhookevent"), 1],
        ), patch(
            "webhooks.management.commands.deliver_webhooks.time.sleep",
            side_effect=[None, KeyboardInterrupt],
        ):
            call_command(
                "deliver_webhooks", stdout=StringIO(), stderr=err, timeout=5, every=1
            )
        self.assertIn("no such table", err.getvalue())

    def test_every_must_be_positive(self):
        for value in ("0", "-1", "soon"):
            with self.assertRaises(CommandError):
                call_command("deliver_webhooks", "--every", value, stdout=StringIO())

    def test_a_delivered_event_is_removed(self):
        delivered = delivery.deliver_pending()
        self.assertEqual(delivered, 1)
        self.assertFalse(WebhookEvent.objects.filter(pk=self.event.pk).exists())
        self.endpoint.refresh_from_db()
        self.assertIsNotNone(self.endpoint.last_delivery)

    def test_a_failed_delivery_is_tried_again_later(self):
        self.status = 500
        now = timezone.now()
        delivery.deliver_pending(now=now)
        self.event.refresh_from_db()
        self.assertEqual(self.event.attempts, 1)
        self.assertEqual(self.event.last_error, "HTTP 500")
        self.assertEqual(self.event.next_attempt, now + datetime.timedelta(minutes=1))
        # And it goes out again once the wait is over.
        self.status = 200
        delivery.deliver_pending(now=now + datetime.timedelta(minutes=2))
        self.assertFalse(WebhookEvent.objects.filter(pk=self.event.pk).exists())

    def test_the_waits_double(self):
        self.status = 500
        now = timezone.now()
        for expected_wait, expected_attempts in [(1, 1), (2, 2), (4, 3), (8, 4)]:
            self.event.next_attempt = now
            self.event.save(update_fields=["next_attempt"])
            delivery.deliver_pending(now=now)
            self.event.refresh_from_db()
            self.assertEqual(self.event.attempts, expected_attempts)
            self.assertEqual(
                self.event.next_attempt,
                now + datetime.timedelta(minutes=expected_wait),
            )

    def test_an_event_gives_up_after_the_last_attempt(self):
        self.status = 500
        now = timezone.now()
        for _ in range(delivery.MAX_ATTEMPTS):
            self.event.next_attempt = now
            self.event.save(update_fields=["next_attempt"])
            delivery.deliver_pending(now=now)
            self.event.refresh_from_db()
        self.assertEqual(self.event.attempts, delivery.MAX_ATTEMPTS)
        self.assertIsNone(self.event.next_attempt)

        # Nothing is sent for an event that has given up.
        before = len(self.received)
        delivery.deliver_pending(now=now + datetime.timedelta(days=1))
        self.assertEqual(len(self.received), before)

    def test_one_endpoint_that_cannot_be_queued_costs_only_its_own_event(self):
        # The write of one event failing must not leave the other endpoints
        # with nothing. Each is written in its own savepoint and each is
        # attempted on its own.
        WebhookEvent.objects.all().delete()
        other = WebhookEndpoint.objects.create(
            name="Other", url="http://127.0.0.1:1/hook", secret="secret"
        )
        feeding = models.Feeding.objects.create(
            child=models.Child.objects.create(
                first_name="First", birth_date=timezone.localdate()
            ),
            start=timezone.localtime(),
            end=timezone.localtime(),
            type="formula",
        )
        WebhookEvent.objects.all().delete()

        original = WebhookEvent.objects.create
        calls = []

        def fragile(**kwargs):
            calls.append(kwargs["endpoint"].name)
            if kwargs["endpoint"].name == "Home":
                raise RuntimeError("no room")
            return original(**kwargs)

        WebhookEvent.objects.create = fragile
        self.addCleanup(setattr, WebhookEvent.objects, "create", original)
        from webhooks import signals as webhook_signals

        with self.assertLogs("webhooks.signals", level="ERROR"):
            webhook_signals.record_event(feeding, "created")

        self.assertEqual(sorted(calls), ["Home", "Other"])
        self.assertEqual(WebhookEvent.objects.count(), 1)
        self.assertEqual(WebhookEvent.objects.get().endpoint, other)

    def test_a_switched_off_endpoint_is_left_out(self):
        # Turning an endpoint off has to stop it being told things, including
        # what was still queued when the switch went off.
        self.endpoint.active = False
        self.endpoint.save()
        self.assertEqual(delivery.deliver_pending(), 0)
        self.assertEqual(self.received, [])

        # And it goes out when the endpoint is wanted again.
        self.endpoint.active = True
        self.endpoint.save()
        self.assertEqual(delivery.deliver_pending(), 1)
        self.assertEqual(len(self.received), 1)

    def test_an_event_that_is_not_due_is_left_alone(self):
        WebhookEvent.objects.all().update(
            next_attempt=timezone.now() + datetime.timedelta(hours=1)
        )
        self.assertEqual(delivery.deliver_pending(), 0)
        self.assertEqual(self.received, [])

    def header_signatures(self, request):
        """The individual ``v1=<hex>`` values a request arrived with."""
        return request["headers"]["X-BabyBuddy-Signature"].split(",")

    def is_signed_with(self, secret, request):
        timestamp = request["headers"]["X-BabyBuddy-Timestamp"]
        message = "{}.{}".format(timestamp, request["body"]).encode()
        digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
        return "v1=" + digest in self.header_signatures(request)

    def rotate(self, cutoff=None, new_secret=None):
        return self.endpoint.rotate_secret(new_secret=new_secret, expires_at=cutoff)

    def test_during_a_rotation_the_request_is_signed_with_both_keys(self):
        # The whole point of the overlap: a receiver still holding the old key
        # and one already given the new key can both check this request.
        self.rotate(
            cutoff=timezone.now() + datetime.timedelta(hours=24),
            new_secret="the new secret of this endpoint!",
        )
        delivery.deliver_pending()
        self.assertEqual(len(self.received), 1)
        request = self.received[0]

        self.assertEqual(len(self.header_signatures(request)), 2)
        self.assertTrue(self.is_signed_with("the secret of this endpoint", request))
        self.assertTrue(
            self.is_signed_with("the new secret of this endpoint!", request)
        )
        # The current key's signature leads, so a receiver that already has it
        # can stop at the first.
        self.assertTrue(self.header_signatures(request)[0].startswith("v1="))
        self.assertEqual(
            self.header_signatures(request)[0],
            "v1="
            + hmac.new(
                b"the new secret of this endpoint!",
                "{}.{}".format(
                    request["headers"]["X-BabyBuddy-Timestamp"], request["body"]
                ).encode(),
                hashlib.sha256,
            ).hexdigest(),
        )

    def test_each_unexpired_key_signs_and_an_expired_one_signs_nothing(self):
        now = timezone.now()
        self.rotate(
            cutoff=now + datetime.timedelta(hours=1),
            new_secret="the middle secret of this end",
        )
        self.rotate(
            cutoff=now + datetime.timedelta(hours=2),
            new_secret="the newest secret of this end",
        )
        # A key from an even older rotation whose cutoff has already passed:
        # purged before anything is sent.
        WebhookSecret.objects.create(
            endpoint=self.endpoint,
            version=99,
            secret="a-key-long-past-its-cutoff-01",
            expires_at=now - datetime.timedelta(hours=1),
        )

        delivery.deliver_pending(now=now)
        request = self.received[0]
        signatures = self.header_signatures(request)
        self.assertEqual(len(signatures), 3)
        self.assertTrue(self.is_signed_with("the secret of this endpoint", request))
        self.assertTrue(self.is_signed_with("the middle secret of this end", request))
        self.assertTrue(self.is_signed_with("the newest secret of this end", request))
        self.assertFalse(self.is_signed_with("a-key-long-past-its-cutoff-01", request))
        self.assertFalse(WebhookSecret.objects.filter(version=99).exists())

    def test_a_retired_key_stops_signing_at_its_cutoff(self):
        now = timezone.now()
        cutoff = now + datetime.timedelta(hours=1)
        self.rotate(cutoff=cutoff, new_secret="the new secret of this endpoint!")

        # One second before the cutoff both keys still sign.
        delivery.deliver_pending(now=cutoff - datetime.timedelta(seconds=1))
        self.assertEqual(len(self.header_signatures(self.received[0])), 2)

        # A second change sent once the cutoff has passed: new key only, and
        # the old row has been removed -- with no scheduler involved.
        WebhookEvent.objects.create(
            endpoint=self.endpoint, type="feeding.updated", object_id="43"
        )
        delivery.deliver_pending(now=cutoff)
        self.assertEqual(len(self.received), 2)
        request = self.received[1]
        self.assertEqual(len(self.header_signatures(request)), 1)
        self.assertTrue(
            self.is_signed_with("the new secret of this endpoint!", request)
        )
        self.assertFalse(self.is_signed_with("the secret of this endpoint", request))
        self.assertFalse(WebhookSecret.objects.filter(version=1).exists())

    def test_an_event_queued_before_a_rotation_is_not_lost(self):
        self.rotate(
            cutoff=timezone.now() + datetime.timedelta(hours=24),
            new_secret="the new secret of this endpoint!",
        )
        self.assertEqual(WebhookEvent.objects.count(), 1)
        self.assertEqual(delivery.deliver_pending(), 1)
        self.assertEqual(len(self.received), 1)
        self.assertFalse(WebhookEvent.objects.filter(pk=self.event.pk).exists())

    def test_a_failing_event_is_rotated_underneath_and_then_delivered(self):
        # Fails first (with one signature), is queued with a backoff, the key
        # is rotated while it waits, and the retry goes out signed with both.
        now = timezone.now()
        self.status = 500
        delivery.deliver_pending(now=now)
        self.event.refresh_from_db()
        self.assertEqual(self.event.attempts, 1)

        self.rotate(
            cutoff=now + datetime.timedelta(hours=24),
            new_secret="the new secret of this endpoint!",
        )
        self.status = 200
        delivery.deliver_pending(now=now + datetime.timedelta(minutes=2))

        self.assertEqual(len(self.received), 2)
        retry = self.received[1]
        self.assertTrue(self.is_signed_with("the secret of this endpoint", retry))
        self.assertTrue(self.is_signed_with("the new secret of this endpoint!", retry))
        self.assertFalse(WebhookEvent.objects.filter(pk=self.event.pk).exists())

    def test_a_rotation_survives_a_worker_restart(self):
        # Everything about a rotation is a database row, so a fresh invocation
        # of the command behaves exactly like the process that started it.
        cutoff = timezone.now() + datetime.timedelta(hours=1)
        self.rotate(cutoff=cutoff, new_secret="the new secret of this endpoint!")
        out = StringIO()
        call_command("deliver_webhooks", stdout=out, timeout=5)
        self.assertEqual(len(self.header_signatures(self.received[0])), 2)

        # A later run -- standing in for the restarted worker reaching the
        # cutoff -- signs with the new key only and drops the old row.
        WebhookEvent.objects.create(
            endpoint=self.endpoint, type="feeding.updated", object_id="43"
        )
        delivery.deliver_pending(now=cutoff)
        self.assertEqual(len(self.header_signatures(self.received[1])), 1)
        self.assertFalse(WebhookSecret.objects.filter(version=1).exists())

    def test_expired_keys_are_purged_once_per_run_for_every_endpoint(self):
        other = WebhookEndpoint.objects.create(
            name="Other",
            url=self.endpoint.url,
            secret="the secret of the other end",
        )
        WebhookEvent.objects.create(endpoint=other, type="sleep.created", object_id="7")
        now = timezone.now()
        WebhookSecret.objects.create(
            endpoint=self.endpoint,
            version=50,
            secret="expired-key-of-the-first-end-01",
            expires_at=now - datetime.timedelta(hours=1),
        )
        WebhookSecret.objects.create(
            endpoint=other,
            version=50,
            secret="expired-key-of-the-second-end-1",
            expires_at=now - datetime.timedelta(hours=1),
        )

        with patch.object(
            WebhookSecret, "purge_expired", wraps=WebhookSecret.purge_expired
        ) as purged:
            delivery.deliver_pending(now=now)

        self.assertEqual(purged.call_count, 1)
        self.assertFalse(
            WebhookSecret.objects.filter(
                expires_at__lt=now, endpoint__in=[self.endpoint, other]
            ).exists()
        )
        self.assertEqual(len(self.received), 2)

    def test_one_unreachable_endpoint_holds_up_nobody_else(self):
        # This server keeps answering. The second endpoint points at a port
        # nothing is listening on, and must cost only its own events.
        other = WebhookEndpoint.objects.create(
            name="Elsewhere", url="http://127.0.0.1:1/hook", secret="secret"
        )
        WebhookEvent.objects.create(endpoint=other, type="sleep.created", object_id="7")
        now = timezone.now()
        delivered = delivery.deliver_pending(now=now, timeout=1)
        self.assertEqual(delivered, 1)

        self.assertFalse(WebhookEvent.objects.filter(pk=self.event.pk).exists())

        failed = WebhookEvent.objects.get(endpoint=other)
        self.assertEqual(failed.attempts, 1)
        self.assertTrue(failed.last_error)


class BodyTestCase(TestCase):
    def test_the_body_names_the_change_only(self):
        event = WebhookEvent(
            type="sleep.updated", object_id="abc", created=timezone.now()
        )
        body = json.loads(delivery.body_for(event))
        self.assertEqual(sorted(body), ["created", "id", "object_id", "type"])

    def test_an_event_id_keeps_repeats_apart(self):
        first = WebhookEvent(type="sleep.updated", object_id="abc")
        second = WebhookEvent(type="sleep.updated", object_id="abc")
        self.assertNotEqual(first.event_id, second.event_id)
