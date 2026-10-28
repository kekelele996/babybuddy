# Webhooks

_Requires the `deliver_webhooks` command to be run on a schedule._

Webhooks tell another application when something changes in Baby Buddy. Each
endpoint is a URL that receives one request per change, and the receiving
application decides what to do with it.

## What an endpoint receives

The body names the change and carries nothing about its contents:

```json
{
  "created": "2026-09-23T18:04:11.512341+00:00",
  "id": "6f0d1f1e-2d67-4a1e-9a1e-3f9d0c0f0a11",
  "object_id": "42",
  "type": "feeding.created"
}
```

`type` is the record kind and what happened to it, so `feeding.created`,
`feeding.updated` and `feeding.deleted` are all distinct events. `object_id`
identifies the record. Anything that wants the details reads them back through
the [API](../api.md) with its own credentials.

That is all it is told. An endpoint is a URL that somebody configured once and
then left alone, and it is not given the name of a child, how much was fed or
where anyone was. It does see which record kinds changed and when they changed,
which is enough to tell that a household is awake -- so an endpoint is still a
place that knows something about a family, and it is worth treating as one.

## Checking that a request came from Baby Buddy

Every request carries four headers:

| Header                   | Contents                                                                                  |
| ------------------------ | ----------------------------------------------------------------------------------------- |
| `X-BabyBuddy-Event-Id`   | The same value as `id` in the body, for the sake of a receiver that has not parsed it yet |
| `X-BabyBuddy-Event-Type` | The same value as `type` in the body                                                      |
| `X-BabyBuddy-Timestamp`  | The request time, in whole seconds since the epoch                                        |
| `X-BabyBuddy-Signature`  | One or more `v1=` HMAC-SHA256 hex digests, comma-separated while a key is being rotated  |

The signature covers the timestamp and the body, joined with a period. It does
not cover the headers, so read `id` and `type` out of the body -- that copy is
the one the signature vouches for.

Normally the header holds one digest. While a signing key is being rotated it
holds one digest per key that is still valid -- the current key's first, then
the old key's -- so a request can be checked with either key. Split on the
comma and accept a request if **any** digest matches:

```python
import hashlib
import hmac

timestamp = request.headers["X-BabyBuddy-Timestamp"]
body = request.body  # exactly as it arrived; re-encoding parsed JSON
                     # changes the bytes, and the digest with them

expected = hmac.new(
    secret.encode(),
    timestamp.encode() + b"." + body,
    hashlib.sha256,
).hexdigest()

received = request.headers["X-BabyBuddy-Signature"].split(",")
if not any(
    hmac.compare_digest(expected, part.removeprefix("v1="))  # not ==, so
    for part in received                                      # guessing does
):                                                            # not get easier
    raise PermissionError("signature mismatch")               # as it closes in
```

Three things make that check worth something, and two of them are on this side
of the connection:

1. **Check the timestamp** against the current time and refuse anything too far
   off -- a few minutes is usual. Signing the timestamp stops someone _moving_
   it to dress an old body up as a fresh one. It does not stop them sending a
   captured request again exactly as it stands: the digest is the same and it
   verifies forever.
2. **Remember the event ids** you have already acted on, and ignore a repeat.
   Two attempts carry the same `id`, so this also covers a retry.
3. **Compare with `compare_digest`** as above.

Without the first two, a captured request is one that can be used again.

Redirects are not followed. A signature would otherwise end up at whatever host
the endpoint points at next.

## Sending changes

Events are queued as records change and are sent by a command:

```bash
python manage.py deliver_webhooks
```

Run it from cron or a systemd timer, and each run sends what is due and exits.

For anything that should hear about a change within seconds, and for a Docker
install, which has no cron to add to, the command can keep running instead:

```bash
python manage.py deliver_webhooks --every 1
```

That checks the queue once a second from one process. An empty second is only
a couple of small queries — the events that are due and the signing keys whose
cutoff has just passed — so it costs next to nothing; starting the command
anew from a loop every second would start Django every second. In Docker it
runs from a second container that shares the configuration and database with
the app, with this as its command. A run that fails — the database briefly
unreachable, or not migrated yet while the app starts — is reported and the
next one goes ahead, and only a run that sent something is written to the log.

Use `https` for anything on another
machine: over plain `http` the secret and the body travel in the clear, and the
signature then proves nothing about where they have been. Anything that is not delivered is tried
again on the next run, waiting twice as long as the previous attempt: one, two,
four and eight minutes. After five attempts the event is left alone and stays
in the database as a record of what happened. An event that is delivered is
removed, so what remains is only what is still waiting or has been given up on.

One endpoint that is down costs only its own events: they are the ones that
fail, and everything else still goes out in the same run. What a slow endpoint
costs is time, since the run works through the list in order and waits up to
ten seconds for each.

## Configuring an endpoint

Endpoints are added under the webhooks section of `/admin/`. Each has a name, a
URL and a signing key; the key is generated when the endpoint is created, and
it can be [rotated](#rotating-the-signing-key) when it has to be replaced.
Deactivating an endpoint stops it being told anything, including events that
were still queued when it went off; those wait until it is turned back on.

### From another application

An application can also set up its own endpoint through the [API](../api.md),
at `/api/webhook-endpoints/`, with the same permissions as the admin area: a
user who can add webhook endpoints there can add them here, and caregivers and
read-only users can do neither.

```bash
curl -X POST https://baby.example.com/api/webhook-endpoints/ \
  -H "Authorization: Token <api key>" \
  -H "Content-Type: application/json" \
  -d '{"name": "My phone", "url": "https://receiver.example.com/hook/123"}'
```

The secret is the one field that is never read back. Send `secret` along if the
receiving application issued one — it has to be at least 16 characters — or
leave it out and Baby Buddy generates it and returns it in the response to that
request, the only time it appears. Replacing it later is a `PATCH` with a new
`secret`; that replacement is immediate for the current key, with no new
overlap — the current signature stops being sent at once, so prefer
[rotating](#rotating-the-signing-key) when the receiving side needs time to
change over. Older keys already in an admin-arranged overlap keep signing
until their own cutoffs. Switching the endpoint off is a `PATCH` with
`"active": false`.

## Rotating the signing key

Keys are versioned: every endpoint always has one **current** key (the newest
version) and, for a while after a rotation, one or more **retired** keys that
each keep working until an agreed cutoff. Rotate a key when an endpoint changes
hands or a secret may have leaked.

In the webhooks section of `/admin/`, open the endpoint and use **Rotate
signing key**:

1. Pick the moment the **old key stops working at**. It is pre-filled a day out
   as a suggestion; set it to whatever was agreed with whoever runs the
   receiving application.
2. Leave **New secret** blank to have one generated, or type one (at least 16
   characters) if the receiving side issued it.
3. Submit. The new secret is shown **once** on the next page — copy it to the
   receiving application then. It is never shown again.

What happens, in order:

- The new key becomes current straight away and its version number is one
  higher. Versions and the cutoff for each retired key are shown on the
  endpoint's list row and edit page, so it is always visible which key is
  current and when an old one stops.
- Until the cutoff, **every request is signed with both keys**: the signature
  header carries the current key's digest first and the old key's after it.
  A receiver still holding the old key keeps verifying requests; one already
  given the new key verifies with that. [Check all of them](#checking-that-a-request-came-from-baby-buddy),
  not just the first digest.
- At the cutoff the old key stops signing and its row is removed on the next
  delivery run — within about a second with `--every 1`, otherwise at the next
  scheduled run. From then on only the new signature is sent, so make sure the
  receiving application has taken the new key _before_ the cutoff.
- Nothing about a rotation lives outside the database — no process has to stay
  running or be told. Restarting Baby Buddy or the delivery worker in the
  middle changes nothing, and an expired key is still dropped on schedule.
- Events already queued are not touched. A change waiting through its retries
  is sent with whichever keys are valid when the attempt goes out.

A rotation can itself be rotated: retiring a second key while the first is
still in its overlap period is allowed, and each old key then stops at its own
cutoff. Requests in the meantime carry a digest for every key still valid.

## What is announced

Changes to children and to the entries that can be recorded for them: BMI,
diaper changes, feedings, head circumference, height, medication, notes,
pumping, sleep, temperature, timers, tummy time and weight. Percentiles are
left out because they are computed rather than written, so announcing them
would send an event for every measurement that merely moved a curve.

There is one place where nothing is announced: `QuerySet.update()` writes rows
without calling `save()`, so no signal fires. It is used in migrations and tests
only. A bulk write elsewhere would go unannounced.

An endpoint receives every one of these. `type` is enough to keep what you
want and drop the rest at the receiving end, so there is no filter to configure
here and no way to end up with an endpoint that is quietly not being told about
something you expected it to hear.

## What this does not do

Webhooks are a notification, not a delivery guarantee. Two runs of the command
at the same time can send the same change twice, and the same change goes out
again on every attempt that comes back with a failure. `X-BabyBuddy-Event-Id`
is what tells a repeat from a second change, so keep the ones you have seen if
you cannot afford to act on one twice. Baby Buddy sends one
request per change and gives up after five attempts, and it does not keep a
history of what an endpoint was told. An endpoint that was switched off and on
again has missed whatever happened in between, and the API is what tells it
where things stand now.
