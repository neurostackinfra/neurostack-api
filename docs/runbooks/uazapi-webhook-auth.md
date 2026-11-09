# Uazapi webhook authentication runbook

> **Nothing in this runbook authorizes enforcing, rotating a token, a
> deployment or any other live action.** Each step below needs the product
> owner's explicit order at the moment of that step. An authorization given
> earlier does not cover a later step. An agent never acts as the operator
> and never holds AWS, uazapi or user tokens.

This runbook covers the envelope-token check of the uazapi webhook: how to
read what the shadow mode measured, how to enforce it, how to watch it once
enforced, how to rotate the instance token, and how to roll back. It is
self-contained: every rule, threshold and query needed to follow it is
written here. The decisions behind it are recorded in the wish
`uazapi-webhook-auth` (`.genie/wishes/uazapi-webhook-auth/WISH.md`); the
decision numbers below refer to it, but nothing here needs the wish to be
followed. The code is the source of truth: `uazapi_webhook` in
`src/api/routes/webhook.py`, the flag questions in
`src/services/egress_policy.py`, and the flag reader in
`src/infrastructure/appconfig.py`.

## Target environment

**Before every step, the owner confirms in writing which environment
currently serves `POST /v1/webhook/uazapi` for the clinic**, and every
query, SQL statement and AppConfig write of that step uses those values:

| What | Placeholder | Known today (staging), per the existing runbooks |
|---|---|---|
| ECS service that receives the uazapi callbacks | `ECS_SERVICE` | `neurostack-staging-app` ([`asaas-deployment.md`](./asaas-deployment.md), "Standing guidance") |
| Its app log group | `LOG_GROUP` | `/ecs/neurostack-staging/app` (the group [`asaas-deployment.md`](./asaas-deployment.md) and [`../agent-media-delivery.md`](../agent-media-delivery.md) query) |
| Its ALB target group | `TARGET_GROUP` | `neurostack-staging-app` ([`asaas-producer-image-rollout-group5.md`](./asaas-producer-image-rollout-group5.md)) |
| Region | `REGION` | `us-east-1` |
| AppConfig application, environment and egress-flags profile | `APP_ID`, `ENV_ID`, `PROFILE_ID` | not recorded in this repository: read `APPCONFIG_APPLICATION`, `APPCONFIG_ENVIRONMENT` and `APPCONFIG_PROFILE` from the serving task definition |
| Database | | the `DATABASE_URL` secret of the serving task definition |
| The webhook URL registered on each uazapi instance | | not recorded in this repository: **verify** it on the uazapi side for each instance |

**Nothing here may be assumed to stay true.** `production` is a deployable
environment (`DEPLOYABLE_ENVIRONMENTS` in `src/ops/asaas_webhook.py`), and
#208 added a per-environment billing webhook URL
(`WEBHOOK_URL_BY_ENVIRONMENT`) whose production entry names a
Terraform-managed service `neurostack-prod-app` with `ENVIRONMENT=production`. This repository does not record that
service's log group, AppConfig identifiers, target group or database. If
the uazapi callbacks are, or start being, served by another service, the
evidence, the watch and the flag deployments all move to it; evidence read
on one service never counts for another, and a flag deployed to a profile
the serving tasks do not read enforces nothing.

The queries below are written for `LOG_GROUP`, and the CLI examples use
`"$REGION"` and `"$LOG_GROUP"`.

## What the check does

The uazapi 2.6.0 documentation describes the instance `token` in the
callback envelope, next to `owner`, `BaseUrl` and `instanceName`. That a
real uazapi server sends it on every callback type is the hypothesis shadow
mode tests ([below](#what-shadow-measures-and-why-it-is-a-hypothesis)).
`uazapi_webhook` compares the envelope token with the `uazapi_token` stored
in `clinic_configs.api_credentials` for **every** row whose
`uazapi_instance_name` equals the envelope's `instanceName`. The comparison
is `hmac.compare_digest` over the stripped UTF-8 bytes of both tokens. A
token that is not a string, or is empty after stripping, is no token.

The check runs after the early returns that have no effect (`invalid_data`,
`missing_fields`, `group_chat`) and before the Redis replay claim. So every
callback that reached it is "authenticable", and the early returns are
excluded by construction. A provider retry of the same message reaches the
check again: **each delivery logs one line.**

### The flag: `uazapi_webhook_auth_enforced`

An AppConfig flag of the egress-flags profile, read **once** per callback,
before the instance query. **The flag is global**: it switches every uazapi
tenant at once, not one clinic. Every tenant with a uazapi instance name is
enforced by the same deployment (see the [inventory](#inventory-of-uazapi-tenants-read-only)).

- **Absent or `false`: shadow.** The check only logs and counts. Routing goes
  through the resolver as before (by instance name), the turn's origin stays
  `UNKNOWN`, and serving is unchanged.
- **`true`: enforce.** Any result other than `match` answers
  `401 {"error":"invalid_webhook_auth"}` before the claim, the session or any
  event is created. A `match` routes to the one matching row, the turn is
  `InteractionOrigin.PRODUCTION`, and the credit identity is built with
  `inbound_verified=True`, so the turn is metered as authenticated. Nothing
  is charged for it (see [The L10 guard](#the-l10-guard-and-the-follow-up)).

"Fail-closed" here means **no authentication**. `false` for any doubt is
shadow: AppConfig disabled or unconfigured, the Agent unreachable, a failed
or stale SDK poll, or a flag that is absent or not a boolean all read
shadow. This is the opposite of the egress flags, where `false` stops an
effect.

### Results

| `result` | When |
|---|---|
| `match` | exactly one row of the instance has a non-empty stored token equal to the envelope token |
| `mismatch` | a token was presented and a row has a stored token, but none is equal |
| `token_missing` | no usable envelope token (absent, not a string, or empty after strip) |
| `token_not_configured` | no row of the instance has a stored token |
| `unknown_instance` | no row has this instance name |
| `ambiguous_instance` | two or more rows of the instance match the token |
| `check_error` | shadow only: the instance query failed. The session was rolled back and the callback was routed as before. In enforce mode the same failure is a 500, before the claim, and writes **no** `uazapi_webhook_auth` line and no count (see [the 5xx watch](#w3-5xx-on-the-uazapi-webhook)) |

### The log line `uazapi_webhook_auth`

One line per check, plain text (also with `LOG_FORMAT=json`: it is written
by a stdlib logger whose format is `%(message)s`), rendered from named
fields in this order:

```text
uazapi_webhook_auth mode=shadow result=match instance=my-instance token_present=True routed_matches=True provider_matches=True owner_present=True from_me=False base_url_matches=True
```

- `mode`: `shadow` or `enforce`, as that request read the flag.
- `instance`: the envelope `instanceName`, **percent-encoded** (only the
  unreserved characters `A-Z a-z 0-9 - . _ ~` are kept) and cut at 128
  characters, with `...[truncated]` marking the cut. An ordinary name such as
  `my-instance_1` renders unchanged. **Queries must use the encoded form.**
- `token_present`: the envelope carried a usable token.
- `routed_matches`: the matching row is the row the request was routed to.
  It is `False` by construction on every result other than `match`.
- `provider_matches`, `base_url_matches`: for the matching row (else the
  routed row, else the instance's only row): its `whatsapp_provider` is
  `uazapi`, and the envelope `BaseUrl` names its stored `uazapi_api_url`
  (scheme, host, port and path, case, default port and trailing slash
  normalised).
- `owner_present`: the envelope carried a non-empty `owner`.
- `from_me`: the message was sent from the clinic's own number.

Booleans render as `True` and `False`. **The token never appears, not even
as a hash.** The auth line carries no message id. Two other lines carry the
same prefix: `uazapi_webhook_auth check failed in shadow: <exception type>`
(next to a `check_error` line) and `uazapi_webhook_auth dropped: unreadable
outcome` (the line could not be built; under enforce the callback is still
refused unless it matched, so a `dropped` line can hide a refused message).

Every callback with a dictionary `message` also logs
`Uazapi webhook: instance=<encoded> from=<sender> type=<type> mediaType=<mediaType> body_len=<n> fromMe=<True|False>`
in the same log stream, **before** the early returns and the check. It is
how a given auth line is tied to a message type and sender (see
[Known messages](#known-messages-of-each-type)). Because it comes before the
early returns, a `Uazapi webhook:` line with no auth line after it is an
early return (`missing_fields`, `group_chat`), not a dropped check: never
count `Uazapi webhook:` lines as authenticable events. Its `from`, `type`
and `mediaType` are raw body values (only `instance` is encoded).

To encode a clinic's instance name for a query:

```bash
python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1][:128], safe="") + ("...[truncated]" if len(sys.argv[1]) > 128 else ""))' 'the instance name'
```

### Log integrity

A log line is evidence only if a request cannot write it. Three things make
that hold, and all are needed:

- **Every stdlib record is one physical line.** The escaping formatters
  (`SingleLineFormatter` on the root handler and
  `SingleLineDelegatingFormatter` on uvicorn's own handlers, both in
  `src/config/logging.py`) escape the final rendered line. The log then holds one backslash per escape: CR, LF and
  TAB become `\r`, `\n` and `\t`; every other character below 0x20, and
  0x7f, becomes `\xNN` (for example `\x0b`); U+0085, U+2028, U+2029 and any
  lone surrogate (U+D800 to U+DFFF) become `\uNNNN` (for example `\u2028`),
  in the JSON and the console tables alike.
  Backslashes already in the text are left as they are. With
  `LOG_FORMAT=console` (development) ESC is kept, so the console colours
  work; ESC cannot break a line, and production runs `json` (the default;
  verify `LOG_FORMAT`, as W3 says). The `awslogs`
  driver starts a new CloudWatch event at every newline, so before this
  formatter a newline in a body value (for example in the `type` of the
  `Uazapi webhook:` line) could start a forged `uazapi_webhook_auth …` line.
  A traceback also becomes one line, with `\n` escapes. The structlog JSON
  lines (`http_request`, `Unhandled exception`,
  `appconfig_egress_flags_changed`) are written through the same handler and
  are unchanged below the cap.
- **Every line is capped at 15000 bytes.** The Fargate log shim cuts a line
  longer than 16 KiB into pieces, and `awslogs` sends each piece as its own
  CloudWatch event (found by the independent review of this change; not
  measured in this repository). Without a cap, a request of about 33 KB
  could aim that cut so that a piece started with a forged
  `uazapi_webhook_auth … result=match`, escaping or not. So every rendered
  line is capped at `MAX_LINE_BYTES` = 15000 bytes of UTF-8, newline
  excluded (`src/config/logging.py`), below the shim's 16384-byte cut. The
  cap applies wherever the escaping applies: the root `SingleLineFormatter`
  (stdlib lines and the structlog JSON lines, which go through the same
  handler), the `SingleLineDelegatingFormatter` that wraps uvicorn's own
  handlers, and the `unformattable log record:` fallback line. Above the
  cap the middle is removed:
  - the cut is made on raw characters **before** escaping, so it never
    splits a multi-byte character or an escape such as `\n`;
  - at most the first `HEAD_BYTES` = 8000 and the last `TAIL_BYTES` = 6000
    bytes, escaped, are kept, so the line holds at most 14000 bytes plus the
    marker; the tail keeps a traceback's final exception line;
  - the two parts are joined by the elision marker `...[N chars elided]...`
    (`_ELISION_MARKER`), where `N` is the number of **characters** of the
    formatted record (message plus any traceback or stack text) dropped
    before escaping. It is not a byte count.

  Lines under the cap are unchanged (a structlog JSON line stays
  byte-identical).
- **Every query is anchored at the start of `@message`** (`^`), on the
  exact leading form the code writes: a fragment embedded inside another
  line never matches. Do not write unanchored variants of these queries.

So a request can still put any text **inside** its own line, but it can
never **start** a line or a CloudWatch event.

**What the cap does to the queries.** A `uazapi_webhook_auth` line is a few
hundred bytes and is never cut. A `Uazapi webhook:` line with a huge `type`
or `from` keeps its head, where `instance=` is, so Q3 still finds it. A JSON
line over the cap (an `Unhandled exception` record with a very long
traceback is the likely one) **can no longer be read as JSON**: depending on
where the cut falls it does not parse at all, or it parses with the middle
fields gone or merged into one string value. Anything that reads JSON
fields, such as Logs Insights' discovered fields or a metric filter on
`$.event`, skips it or reads it wrong. The queries of this runbook read the
text of `@message`, anchored on the head, so they still match it; the
elision marker is in the middle.

**This protection holds only for lines written by an image that carries
the escaping formatter with the cap.** Record the time the first such image
served on `ECS_SERVICE` (`ESCAPING_IMAGE_AT`: the deployment of an image
whose source commit contains `SingleLineFormatter` in `src/config/logging.py`
with the 15000-byte cap; the build tag names the commit). **Lines older than
`ESCAPING_IMAGE_AT` never count toward any evidence of this runbook**,
including lines of an image that escaped but did not cap: start every period
of the criterion and of the watch at or after it.

Even so, each trigger of the [watch](#the-post-enforce-watch) is
corroborated against a source no request can write
([Corroboration](#corroboration-before-and-after-acting)).

### The counter

`neurostack_uazapi_webhook_auth_total{mode,result}`, every child pre-created
at zero. It has no instance label, and it is per process: `/metrics` reaches
one process per scrape. It cannot replace the log: the enforce criterion and
the post-enforce watch are both judged from the log.

## Reading the shadow results

### What shadow measures, and why it is a hypothesis

Shadow measures one thing: **whether the token uazapi puts in the envelope
equals the token stored for the instance.** The provider documentation
directs receivers to validate `token` and bind it to the instance, but the
evidence for uazapi is still `claim: unproven`: the repository's extracts of
the 2.6.0 documentation are paraphrases, no sandbox capture exists, and no
rotation was observed. Enforcing before shadow confirms the equality on real
traffic would refuse the clinic's own messages.

### The queries

All of these run in CloudWatch Logs Insights over `LOG_GROUP` in `REGION`
([Target environment](#target-environment)), over a period that starts at or
after `ESCAPING_IMAGE_AT` ([Log integrity](#log-integrity)). Replace
`ENCODED_INSTANCE` with the encoded instance name. Every filter and parse is
anchored at the start of the line.

**Q1, summary of every instance** (run this first: it shows which instances
have traffic):

```text
fields @timestamp, @message
| filter @message like /^uazapi_webhook_auth mode=shadow result=/
| parse @message /^uazapi_webhook_auth mode=(?<mode>\S+) result=(?<result>\S+) instance=(?<instance>\S+) token_present=(?<token_present>\S+) routed_matches=(?<routed_matches>\S+) provider_matches=(?<provider_matches>\S+) owner_present=(?<owner_present>\S+) from_me=(?<from_me>\S+) base_url_matches=(?<base_url_matches>\S+)$/
| stats count(*) as events, min(@timestamp) as first_seen, max(@timestamp) as last_seen by instance, result, token_present, owner_present, base_url_matches, provider_matches, routed_matches
| sort instance asc
```

**Q2, summary of one instance:** Q1 with this line added after the `parse`
line:

```text
| filter instance = "ENCODED_INSTANCE"
```

**Q3, timeline of one instance** (the rotation, the known messages, and any
line that needs a look). It lists the auth lines of both modes together with
the `Uazapi webhook:` line of each callback; `instance` is parsed from the
start of each line, never matched anywhere in it. Narrow the period to the window
being examined (for the rotation: from a few minutes before the recorded
rotation start to a few minutes after the recorded end); the result is
capped at 10,000 rows, and a longer period can cut the sequence off:

```text
fields @timestamp, @logStream, @message
| filter @message like /^(uazapi_webhook_auth mode=\S+ result=\S+|Uazapi webhook:) instance=/
| parse @message /^(?<kind>uazapi_webhook_auth mode=\S+ result=\S+|Uazapi webhook:) instance=(?<instance>\S+) /
| filter instance = "ENCODED_INSTANCE"
| sort @timestamp asc
| limit 10000
```

**Q4, any `check_error` or unreadable line in the period** (the
`check_error` line carries the instance; the companion `check failed in
shadow` line does not):

```text
fields @timestamp, @logStream, @message
| filter @message like /^uazapi_webhook_auth (mode=\S+ result=check_error instance=|check failed in shadow: |dropped: unreadable outcome$)/
| sort @timestamp asc
| limit 10000
```

From the CLI, save the query text to a file and run it over the period
(times in epoch seconds):

```bash
QUERY_ID=$(aws logs start-query --region "$REGION" \
  --log-group-name "$LOG_GROUP" \
  --start-time "$START_S" --end-time "$END_S" \
  --query-string "$(cat shadow.insights)" \
  --output text --query queryId)
aws logs get-query-results --region "$REGION" --query-id "$QUERY_ID"
```

`get-query-results` answers `"status": "Running"` until the query finishes;
run it again until it reads `Complete`.

### Classifying non-`match` lines

Anyone can POST a clinic's `instanceName` to the webhook without the token.
That traffic is not the clinic's, and enforce will refuse it with 401, which
is the point. So each non-`match` line is described by how it looks:

- **Legitimate-looking:** `owner_present=True` **and** `base_url_matches=True`.
  The uazapi documentation describes `owner` and `BaseUrl` in the envelope,
  and only a caller that knows the clinic's uazapi server URL matches the
  stored `uazapi_api_url`.
- **Foreign-looking:** `owner_present=False` or `base_url_matches=False`.

**The split only explains lines. It never removes a line from the
decision.** A real uazapi callback type that lacks the envelope would lack
all three fields at once, look foreign, and be refused under enforce. And
in shadow a foreign post is routed and processed exactly like a real one
(session, events, reply), so the database cannot tell them apart. The rules:

- **Every non-`match` line is counted and listed**, legitimate-looking and
  foreign-looking alike, per instance (Q1, then Q3 for the lines).
- **A legitimate-looking non-`match` line blocks** (the only exception is the
  bounded rotation window of the [criterion](#the-enforce-criterion-decision-9)).
  A legitimate-looking `token_missing` line always blocks, with no
  exception: uazapi sent a callback without the token, and enforce would
  refuse it.
- **A foreign-looking line blocks until the owner has reviewed it.** If any
  foreign-looking line exists for an instance, the owner reviews each one
  before enforcing and records, per line (time, instance, result), why it is
  not a real uazapi delivery. The evidence must come from outside the
  database: the `Uazapi webhook:` line next to it in Q3 (sender, `type`,
  `mediaType`, `fromMe`), and whether the clinic's own WhatsApp shows that
  message from that number at that time. A foreign-looking line that matches
  a real message of the clinic blocks.
- **Any `check_error` or `dropped: unreadable outcome` line blocks** until it
  is investigated and explained (Q4; the `check failed in shadow` line gives
  the exception type): under enforce the same failure is a 500 and the
  message is lost.

Also take to the owner before enforcing: a legitimate-looking line with
`provider_matches=False` (the row has the instance name but its
`whatsapp_provider` is not `uazapi`), and any `ambiguous_instance` or
`unknown_instance` line for a configured instance (see the
[duplicate pre-check](#duplicate-pre-check-read-only)).

### Known messages of each type

Before relying on the split, check it on known messages, for **each**
message type the clinic actually receives. The handler processes, after the
check:

- a patient text;
- a patient voice note (`type` or `mediaType` `audio`/`ptt`, transcribed);
- a patient image, document or video (the file path);
- a message the clinic sends from its own phone (`fromMe=True`, text or
  voice note; it pauses the session);
- the echo of each reply the agent sends (`fromMe=True`, `wasSentByApi`),
  which arrives after every reply.

Stickers and other media are checked too and then skipped as `non-text`.

Ask the clinic which of these it receives. For each one, send or have sent
one known message, find its `Uazapi webhook:` line in Q3 (its `type`,
`mediaType` and `fromMe` name the type) and the `uazapi_webhook_auth` line
right after it in the same `@logStream` (under concurrent traffic, confirm
by timestamp). In shadow, each must read `result=match` with
`token_present=True`, `owner_present=True` and `base_url_matches=True`.

- If a known message reads `base_url_matches=False`, the stored
  `uazapi_api_url` does not normalise to the envelope `BaseUrl`, and the
  split cannot be used for that instance: stop and take it to the owner.
- If a known message reads `token_present=False` (`token_missing`), that
  type is delivered without the token and would be refused: it blocks.
- A type the clinic receives but that was not checked goes in the owner's
  record as unchecked; its deliveries may be refused under enforce.

### The enforce criterion (Decision 9)

Judged by the owner from the `uazapi_webhook_auth` log of `LOG_GROUP`,
filtered by `mode=shadow`, **separately for each instance that has
traffic** (each instance Q1 lists, run through Q2). Every clause must hold
for every such instance:

- [ ] **The period starts at or after `ESCAPING_IMAGE_AT`**, on the service
      that serves the clinic ([Log integrity](#log-integrity),
      [Target environment](#target-environment)). Older lines do not count.
- [ ] **At least 50 authenticable events over at least 48 h**
      (`last_seen - first_seen` of Q2), **corroborated against the
      database** ([below](#corroborating-the-counts-read-only)).
- [ ] **100% `match`** among the legitimate-looking lines: zero `mismatch`,
      `token_missing` or `token_not_configured`. The only exception is the
      `mismatch` lines of the deliberate rotation, between the **recorded
      rotation start and the recorded panel update** (both times are in the
      owner's record, [Deliberate rotation](#deliberate-rotation) steps 1 and 3).
      A `mismatch` outside that recorded window, or any `token_missing` at
      any time, does not fall under the exception.
- [ ] **Every foreign-looking line reviewed** by the owner and recorded as not
      a real uazapi delivery ([Classifying](#classifying-non-match-lines)).
- [ ] **Every type the clinic receives checked** with a known message
      ([Known messages](#known-messages-of-each-type)), or recorded as
      unchecked.
- [ ] **`routed_matches` always true** on the `match` lines. A `match` line
      with `routed_matches=False` means the shadow resolver served the
      message to another row than the one holding the token, and enforce
      would change where those messages go.
- [ ] **At least one deliberate rotation observed** while auth is in shadow,
      as `match` → `mismatch` → `match` after the panel update
      ([Deliberate rotation](#deliberate-rotation)).
- [ ] **No `check_error`** or `dropped: unreadable outcome` in the period, or
      each one investigated and explained.

#### Corroborating the counts (read-only)

The patient `message.in` events written for the tenant over the same
period, per hour:

```sql
BEGIN READ ONLY;
SELECT date_trunc('hour', created_at) AS hour, interaction_origin,
       count(*) AS messages_in
FROM events
WHERE tenant_id = 'TENANT_ID' AND event_type = 'message.in'
  AND event_data->>'channel' = 'whatsapp'
  AND created_at >= 'PERIOD_START' AND created_at < 'PERIOD_END'
GROUP BY 1, 2
ORDER BY 1, 2;
ROLLBACK;
```

and the patient `match` lines of the instance per hour (Q2 with its `stats`
line replaced by):

```text
| filter result = "match" and from_me = "False"
| stats count(*) as matches by bin(1h)
```

Hour by hour the two should move together. They are not equal: a `match`
line of a sticker, of an unsupported media type or of a redelivery writes no
event, and a message processed in shadow writes an `unknown` event (a
`simulator` event comes from the panel). Hours with `match` lines and no
event at all, or the reverse, are explained before the counts are accepted.
**The limit:** in shadow a foreign post is routed and processed exactly like
a real one, so it writes the same event; the database confirms that turns
happened, not who sent them. Who sent a turn comes only from the clinic's
own WhatsApp ([Classifying](#classifying-non-match-lines)).

**Or** the owner waives the criterion for that instance. A waiver is
recorded, with its date, the instance and what it waives, in the owner's
record of this step before enforcing.

**Configured tenants without evidence.** A tenant of the
[inventory](#inventory-of-uazapi-tenants-read-only) whose instance has no
lines in the period (no traffic) or does not meet the criterion will get 401
on its real messages from the moment the flag is on, because the flag is
global. Before step 4 of the enforce order, the owner accepts this
explicitly **per tenant**, and the acceptance is recorded (tenant, instance,
date, why). Without that record for every such tenant, do not enforce.

## Deliberate rotation

The rotation proves the check reads the live token: after uazapi rotates the
instance token, the envelope must carry the new one.

**Rotate only while auth is in shadow.** Under enforce every callback
between the rotation and the panel update is refused with 401. To rotate
after enforcing, roll back to shadow first ([Rollback](#rollback)), rotate,
and then enforce again through the whole [enforce order](#the-enforce-order-decision-10)
(the [re-enforce rule](#every-return-to-enforce-runs-the-whole-order)).

**Replies fail until the panel is updated with the new token, even in
shadow**: the stored `uazapi_token` is also the credential every reply is
sent with. Rotate in a low-traffic window and update the panel right away.

1. With the owner's order, in a low-traffic window, **record the rotation
   start time** (UTC) in the owner's record and call the uazapi operation
   `rotateInstanceToken` for the clinic's instance. **Verify the method, path
   and which credential it requires against the uazapi 2.6.0
   documentation** at <https://docs.uazapi.com/reference/rotateInstanceToken.md>
   before calling it: this runbook knows only that 2.6.0 exposes the
   operation, not its method or path. Keep the new token out of tickets,
   logs and chat.
2. Send a message to the clinic's number. Expect its line to read
   `mismatch` (new token in the envelope, old token stored). The patient is
   still served in shadow, but the reply fails.
3. Update the clinic's `uazapi_token` with the new token in the credentials
   panel (`PATCH /v1/config/api-credentials`), then **record the panel
   update time** (UTC) in the owner's record: it closes the rotation window.
   Change the token alone: with the same provider and URL no bundle is
   required (`provider_switch_bundle_required` applies to a provider or URL
   change, [`../configuration.md`](../configuration.md)).
4. Send another message. Expect `match`, and the reply arrives.
5. Read Q3 over the recorded window: the rotation is observed when the
   clinic's lines go `match` → `mismatch` → `match` around the update.

If step 2 still reads `match`, the envelope did not carry the new token:
the equality hypothesis does not hold as stated. Update the panel (step 3)
so replies work again, do not enforce, and take the result to the owner.

## Pre-checks before enforcing

Run each read-only against production (`BEGIN READ ONLY; … ROLLBACK;`).
These SQL statements use the **raw** instance name, not the encoded form of
the log.

### Inventory of uazapi tenants (read-only)

The flag enforces every tenant below at once. List them all:

```sql
BEGIN READ ONLY;
SELECT cc.tenant_id,
       t.name                                                         AS tenant_name,
       cc.api_credentials->>'uazapi_instance_name'                    AS uazapi_instance_name,
       cc.api_credentials->>'whatsapp_provider'                       AS whatsapp_provider,
       coalesce(cc.api_credentials->>'uazapi_token', '') !~ '^\s*$'   AS token_configured
FROM clinic_configs cc
LEFT JOIN tenants t ON t.id = cc.tenant_id
WHERE coalesce(cc.api_credentials->>'uazapi_instance_name', '') <> ''
ORDER BY 3, 1;
ROLLBACK;
```

Encode each `uazapi_instance_name` and find it in Q1. Each row is one of:
an instance with traffic that meets the [criterion](#the-enforce-criterion-decision-9)
(or is waived), or a tenant that needs the owner's recorded per-tenant
acceptance (no traffic, or the criterion not met). Test or demo tenants are
included: they are enforced too.

### Duplicate pre-check (read-only)

Nothing makes `uazapi_instance_name` unique, and any tenant can store a
name. Enforce accepts only when exactly one row's token matches, so a
squatter row without the token cannot block the clinic, but two rows with
the same name and the same token answer `ambiguous_instance` (401).

```sql
BEGIN READ ONLY;
SELECT api_credentials->>'uazapi_instance_name' AS uazapi_instance_name,
       count(*)                                  AS rows
FROM clinic_configs
WHERE coalesce(api_credentials->>'uazapi_instance_name', '') <> ''
GROUP BY 1
HAVING count(*) > 1;
ROLLBACK;
```

For one instance, list each row without reading the token. The token is
reported only as configured or not; a token of whitespace only counts as not
configured, as in the code (which strips it):

```sql
BEGIN READ ONLY;
SELECT tenant_id,
       api_credentials->>'whatsapp_provider'                          AS whatsapp_provider,
       coalesce(api_credentials->>'uazapi_token', '') !~ '^\s*$'      AS token_configured,
       api_credentials->>'uazapi_api_url'                             AS uazapi_api_url
FROM clinic_configs
WHERE api_credentials->>'uazapi_instance_name' = 'the instance name'
ORDER BY tenant_id;
ROLLBACK;
```

Exactly one row per instance is the expected answer. Any other row with an
instance's name goes to the owner before enforcing.

### Credit pre-check (read-only)

The owner must know each enforced clinic's credit mode and balance before
enforcing, because from then on its verified turns are metered as
authenticated production turns. For each tenant of the inventory:

```sql
BEGIN READ ONLY;
SELECT id, builder_id FROM tenants WHERE id = 'TENANT_ID';
SELECT id, builder_id, created_at FROM credit_accounts
WHERE builder_id = (SELECT builder_id FROM tenants WHERE id = 'TENANT_ID');
SELECT id, credit_enforcement_at, set_by, set_at FROM credit_enforcement_state;
SELECT effect_kind, origin, authenticated, count(*) AS effects,
       sum(would_debit_mc) AS would_debit_mc, max(recorded_at) AS last_recorded
FROM credit_observational_effects
WHERE tenant_id = 'TENANT_ID' AND recorded_at > now() - interval '7 days'
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;
ROLLBACK;
```

- `tenants.builder_id` `NULL` is a standalone tenant: it has no credit
  account.
- `credit_accounts` has no mode or balance column: balances are derived from
  the ledger. Read the mode and the balance from the read-only routes, as a
  `super_admin`, with `?tenant_id=TENANT_ID`: `GET /v1/credits/state`
  (fields `mode`, `state`, `enforcement_at`) and `GET /v1/credits/balance`
  ([`../billing/credits.md`](../billing/credits.md), "Routes"). They answer
  `503 credit_unavailable` while the credit switch is not effective.
- The database column `credit_enforcement_state.credit_enforcement_at`
  `NULL` means not armed: the mode is `observational` and nothing is
  charged.
- The recent observational effects show today's uazapi turns as
  `origin = 'unknown'`, `authenticated = false`, `would_debit_mc = 0`.

## The enforce order (Decision 10)

**No code prevents enforcing while agent media is open. This order is the
only control.** Once a uazapi clinic's turns are `PRODUCTION`, the media
selection offers them files whenever `agent_media_send_enabled` is open
(see [`../agent-media-delivery.md`](../agent-media-delivery.md#decision-12-before-uazapi-or-evolution-become-production)).
Each step needs the owner's order, and each step's precondition is the
previous step done and read back. **The order is the same for the first
enforce and for every later return to enforce**
([re-enforce rule](#every-return-to-enforce-runs-the-whole-order)).

**Hard preconditions** for step 1: the [inventory](#inventory-of-uazapi-tenants-read-only)
run; for every inventoried tenant, the [enforce criterion](#the-enforce-criterion-decision-9)
met or waived, or the owner's per-tenant acceptance of the 401s, all in the
owner's record; the running image logs `uazapi_webhook_auth` lines; and the
owner's order to start.

1. **Duplicate pre-check.** Run the [duplicate pre-check](#duplicate-pre-check-read-only).
   Go on only when each inventoried instance has exactly one row.
2. **Credit pre-check.** Run the [credit pre-check](#credit-pre-check-read-only)
   and tell the owner each enforced clinic's mode and balance.
3. **Close `agent_media_send_enabled`, then read it back per worker.** The
   flag is global: closing it **stops agent media for every clinic**, Meta
   and Z-API included, until step 8 reopens it. The owner's order for this
   step says so.
   Follow the kill switch of
   [`../agent-media-delivery.md`](../agent-media-delivery.md#the-flag-and-the-kill-switch):
   start from the deployed hosted version, change only that flag, deploy
   with the no-bake strategy, keep the previous version number. Read back
   with [R1](#r1-the-flag-readback-anchored) (the anchored form of its step 4
   query, `appconfig_egress_flags_changed` from the hosted deployment's start): one line per worker (4 per task, 8 with 2 tasks),
   each `poll_ok=true`, `agent_media_send_enabled=false` and the new
   `version_label`. A worker without a line has not seen the change. If the
   deployed version already has the flag false or absent, record that and
   skip the deployment; the last line of each worker since its start shows
   it.
4. **Open `uazapi_webhook_auth_enforced`, then read it back per worker.**
   Same procedure: from the deployed version (now the one of step 3), set
   only `values.uazapi_webhook_auth_enforced.enabled` to `true`. If the
   version predates the flag, add it under both `flags`
   (`"uazapi_webhook_auth_enforced": {"name": "uazapi_webhook_auth_enforced"}`)
   and `values`. Keep every other flag exactly as it is, in particular
   `agent_media_send_enabled` false and `uazapi_egress_enabled` at its
   current value (closing it would stop every uazapi reply). Read back with
   R1: one
   `appconfig_egress_flags_changed` line per worker with
   `uazapi_webhook_auth_enforced=true`, `agent_media_send_enabled=false`,
   the other flags unchanged and the new `version_label`. Each worker
   switches at its next successful poll (the poll interval is at most
   60 s). A worker whose poll fails or whose refresher stalls never reads
   `true`: it stays in shadow, and its missing readback line shows it.
   **Record the time of the last worker's readback line** (`ENFORCED_AT`):
   from then on every worker is enforcing, until the next worker start (a
   new worker serves in shadow until its first successful poll; see
   [Corroboration](#corroboration-before-and-after-acting), W4).
5. **Read `match` and `PRODUCTION` on real traffic.** Run Q1 with
   `mode=enforce` in place of `mode=shadow`, from `ENFORCED_AT`: each
   inventoried instance with traffic reads `result=match`. A
   legitimate-looking line with any other result is a real message refused
   with 401: roll back at once ([Rollback](#rollback)). Then confirm the
   origin in the database (read-only), per enforced tenant:

   ```sql
   BEGIN READ ONLY;
   SELECT interaction_origin, count(*) FROM events
   WHERE tenant_id = 'TENANT_ID' AND event_type = 'message.in'
     AND created_at > 'ENFORCED_AT'
   GROUP BY 1;
   SELECT origin, authenticated, count(*), sum(would_debit_mc)
   FROM credit_observational_effects
   WHERE tenant_id = 'TENANT_ID' AND recorded_at > 'ENFORCED_AT'
   GROUP BY 1, 2;
   ROLLBACK;
   ```

   Expect `production` events and `production` / `true` effects. Starting
   from `ENFORCED_AT`, not from the deployment start, leaves out the turns of
   workers that had not switched yet. `simulator` rows are expected too (the
   panel simulator). An `unknown` `message.in` row after `ENFORCED_AT` needs
   an explanation before going on.
6. **Start the [post-enforce watch](#the-post-enforce-watch).** Its first run
   is this step; the cadence runs on from here, through the media steps
   below. Go on only when this run is clean.
7. **Media pre-check SQL.** Run the
   [pre-check before any open](../agent-media-delivery.md#pre-check-before-any-open)
   and give the owner the counts, the `uazapi_route` rows included: from
   step 4 those rows are `PRODUCTION` routes.
8. **The owner's explicit go to reopen media**, given for that scope: every
   clinic whose turns are `PRODUCTION` origin, the uazapi clinics with
   bound automatic files among them, with the counts of step 7. Then open
   `agent_media_send_enabled` with the same procedure and read it back per
   worker. uazapi media also needs `uazapi_egress_enabled` open.
9. **Live QA on test instances**, per the
   [live QA](../agent-media-delivery.md#live-qa-owner-step) of the media
   page (its uazapi items). **Never open the production flag "just for
   QA"**: the open of step 8 is the only open. If an item fails, close
   `agent_media_send_enabled` again.

## The post-enforce watch

Nothing in the code refuses loudly: a refused callback is one log line and a
401 to uazapi. **How uazapi reacts to a 401 is unknown**: whether it
retries, how often, and whether it disables the webhook after repeated
failures. So **treat every refused real message as lost.** The watch is the
only way to see it.

### Cadence

Every run is recorded in the owner's record (time, period covered, counts of
W1 to W4).

- right after step 5 of the enforce order (step 6), over the period from
  `ENFORCED_AT`;
- then **hourly for the first 24 h** after `ENFORCED_AT`, each run over the
  last hour (or since the previous run);
- then **daily for the next 7 days**, each run over the last day;
- and, at any time, after any AppConfig deployment of the egress-flags
  profile and after any change of a uazapi tenant's credentials.

### W1: refused lines

Every `mode=enforce` line with a result other than `match`, per instance:

```text
fields @timestamp, @message
| filter @message like /^uazapi_webhook_auth mode=enforce result=/
| parse @message /^uazapi_webhook_auth mode=(?<mode>\S+) result=(?<result>\S+) instance=(?<instance>\S+) token_present=(?<token_present>\S+) routed_matches=(?<routed_matches>\S+) provider_matches=(?<provider_matches>\S+) owner_present=(?<owner_present>\S+) from_me=(?<from_me>\S+) base_url_matches=(?<base_url_matches>\S+)$/
| filter result != "match"
| stats count(*) as refused, min(@timestamp) as first_seen, max(@timestamp) as last_seen by instance, result, token_present, owner_present, base_url_matches, provider_matches
| sort instance asc
```

Rows with `owner_present=True` and `base_url_matches=True` are the
legitimate-looking refusals; the others are foreign-looking. To list the
lines of one instance, run Q3 over the period (it lists both modes). As in
shadow, the split only explains: **every** refused line is counted and
listed in the record.

### W2: unreadable lines

Under enforce a callback whose line could not be built is still refused
unless it matched, with no `result`:

```text
fields @timestamp, @logStream, @message
| filter @message like /^uazapi_webhook_auth dropped: unreadable outcome$/
| sort @timestamp asc
| limit 10000
```

### W3: 5xx on the uazapi webhook

Under enforce a failed instance query (a database or candidate-query
failure) is a 500 on `POST /v1/webhook/uazapi`, before the claim, and writes
**no** `uazapi_webhook_auth` line, so W1 cannot show it. The app's global
exception handler logs it as an `Unhandled exception` record whose
`endpoint` is the route template. **The route renders as
`"endpoint": "/uazapi"`** (measured with the locked FastAPI by driving the
real route). With `LOG_FORMAT=json` (the default of `src/config/settings.py`)
the record is a JSON line that starts, in this exact key order:
`{"endpoint": "/uazapi", "method": "POST", "error_type": "<class>", "event": "Unhandled exception", …`.
The query is anchored on that start; it also accepts `/v1/webhook/uazapi`,
in case a FastAPI upgrade renders the full path:

```text
fields @timestamp, @logStream, @message
| filter @message like /^\{"endpoint": "(\/v1\/webhook)?\/uazapi", "method": "POST", "error_type": "[A-Za-z0-9_.]+", "event": "Unhandled exception", /
| sort @timestamp asc
| limit 10000
```

**Verify `LOG_FORMAT` of the running task definition before relying on
W3**: with `LOG_FORMAT=console` the record is not JSON and W3 finds nothing.
The request-id middleware's `http_request` access record is **not** written
for an unhandled 500 (the exception passes through it), so it cannot replace
W3. It is written for every returned response, starting
`{"method": "POST", "endpoint": "/uazapi", "status": <n>, "event": "http_request", …`,
and gives a cross-check of the 401 count and of any other 5xx the webhook
returns:

```text
fields @timestamp, @message
| filter @message like /^\{"method": "POST", "endpoint": "(\/v1\/webhook)?\/uazapi", "status": \d+, "event": "http_request"/
| parse @message /^\{"method": "POST", "endpoint": "(?<endpoint>[^"]+)", "status": (?<status>\d+), /
| stats count(*) as requests by status
```

The `401` count is **at most** W1's total plus W2's: a W2 line can also be
a callback that matched and was served. A W1 total above the `401` count
means lines that no refusal wrote: treat it as a fault of the evidence and
take it to the owner. Any `5xx` row counts as a W3 hit.

### W4: workers back in shadow

Q1 as it is (`mode=shadow`), from `ENFORCED_AT`: any line means a worker is
back in shadow ([Decision 11](#every-hosted-version-from-now-on-decision-11)).

### R1: the flag readback, anchored

Every readback of this runbook (steps 3, 4 and 8 of the order, the
rollback, and W4's corroboration) reads `appconfig_egress_flags_changed`
with this query, anchored on the JSON line the reader writes
(`{"poll_ok": …, "financial_egress_enabled": …, "uazapi_egress_enabled": …, "agent_media_send_enabled": …, "uazapi_webhook_auth_enforced": …, "version_label": …, "error_type": …, "error_code": …, "event": "appconfig_egress_flags_changed", …`),
from the hosted deployment's start:

```text
fields @timestamp, @logStream, @message
| filter @message like /^\{"poll_ok": (true|false), "financial_egress_enabled": (true|false), /
| parse @message /^\{"poll_ok": (?<poll_ok>true|false), "financial_egress_enabled": (?<financial>true|false), "uazapi_egress_enabled": (?<uazapi_egress>true|false), "agent_media_send_enabled": (?<agent_media>true|false), "uazapi_webhook_auth_enforced": (?<auth_enforced>true|false), "version_label": (?<version_label>null|"[^"]*"), "error_type": (?<error_type>null|"[^"]*"), "error_code": (?<error_code>null|"[^"]*"), "event": "appconfig_egress_flags_changed"/
| sort @logStream asc, @timestamp asc
| limit 10000
```

The last line of each `@logStream` per worker is that worker's state. It
answers the same as the unanchored `filter-log-events` of
[`../agent-media-delivery.md`](../agent-media-delivery.md#the-flag-and-the-kill-switch)
step 4, without counting text embedded in another line.

### Corroboration before and after acting

A query can be wrong, the period can be wrong, and lines from before
`ESCAPING_IMAGE_AT` can be forged. So each trigger is corroborated against a
source no request can write. **The bias stays toward rolling back**: closing
the auth flag is safe for messages (shadow serves as before), so when a
trigger and its corroboration disagree, or the corroboration cannot be
done, roll back. What is never done on an uncorroborated trigger is the
costly part: closing `agent_media_send_enabled` for every clinic to
re-enforce.

- **W3 (500s):** the ALB metric `HTTPCode_Target_5XX_Count` of
  `TARGET_GROUP` (`AWS/ApplicationELB`), read with the existing recipe of
  the revision 10 observation in [`asaas-deployment.md`](./asaas-deployment.md),
  "Ordered procedure" (`aws cloudwatch get-metric-statistics`, dimensions
  `TargetGroup` and `LoadBalancer` as `DescribeTargetGroups` gives them,
  `--period 300 --statistics Sum Maximum`). A W3 hit with a `Sum` of 0 in
  the same 5-minute period is not a real 500. The metric covers every
  route, so a non-zero `Sum` with no W3 hit means run W3 again for that
  period and look at the other routes.
- **W4 (back in shadow):** the AppConfig deployment history, read-only, as
  in [`../agent-media-delivery.md`](../agent-media-delivery.md#the-flag-and-the-kill-switch)
  step 1: `aws appconfig list-deployments --application-id "$APP_ID"
  --environment-id "$ENV_ID"` for the latest completed deployment of the
  profile, then `aws appconfig get-hosted-configuration-version` of that
  version for its content. If the deployed version does not carry
  `uazapi_webhook_auth_enforced` `true`, enforcement really is off
  ([Decision 11](#every-hosted-version-from-now-on-decision-11)). If it does,
  a real W4 line has one of three normal causes, each with its own R1
  evidence for that worker's `@logStream`:
  - **a worker that has just started** (an image deployment, a task
    restart, or uvicorn replacing a dead worker): it serves in shadow until
    its first successful poll (the refresher is started, not awaited,
    `src/api/main.py`), and R1 shows a **first** line for that new
    `@logStream` with `poll_ok` `true` and the flag `true`, at or just after
    the W4 lines;
  - **a failed poll**: R1 shows a `poll_ok` `false` line; the worker stays
    in shadow until a poll succeeds, and failed polls retry with a backoff
    that grows from 15 s up to 300 s;
  - **a stalled refresher**: its reading goes stale after three poll
    intervals and reads shadow, and it writes **no** R1 line at all; the
    worker's last R1 line still reads the flag `true`.

  So after a deployment, `mode=shadow` lines with no `poll_ok` `false` line
  are expected, not a fault of the evidence. W4 lines that none of these
  explains go to the owner.
- **W1 (a legitimate-looking refusal):** a real message that went
  unanswered: the clinic's own WhatsApp shows a message from that sender at
  that time with no reply. **Verify** whether the uazapi panel offers a view
  of webhook deliveries and their HTTP status for the instance; this
  runbook does not know. Roll back first, then corroborate: a refused real
  message is lost.
- **W2:** corroborate as W1, with the `Uazapi webhook:` line just before it
  in the same `@logStream` (Q3) for the sender.
- **Decision 9 counts:** against the database, as far as it can
  ([Corroborating the counts](#corroborating-the-counts-read-only)): in
  shadow a foreign post writes the same rows as a real one.

### Rollback trigger

Any of these means **close the auth flag at once** ([Rollback](#rollback))
and read it back per worker (R1), then corroborate the trigger
([Corroboration](#corroboration-before-and-after-acting)) and take both to
the owner:

- any legitimate-looking line in W1;
- any line in W2;
- any hit of W3 (or a `5xx` row of the `http_request` cross-check), or a
  non-zero ALB `HTTPCode_Target_5XX_Count` that W3 does not explain;
- an instance that had daily traffic in shadow and shows no line at all in
  Q1 with `mode=enforce` over a day: uazapi may have stopped delivering to
  the webhook after the 401s, and silence writes no line.

Foreign-looking W1 lines alone do not trigger a rollback, but they are
listed in the record, and a sudden rise of one instance's foreign-looking
lines is reviewed the same day as in shadow (the clinic's WhatsApp is the
evidence). A W4 line is not a rollback trigger: it means enforcement has
silently stopped. Corroborate it against the deployment history first, then
handle it as in [Decision 11](#every-hosted-version-from-now-on-decision-11).

### Token or instance changes while enforced

Under enforce, the envelope token must equal the stored `uazapi_token` at
every moment. Each of these makes **every** inbound callback of that
instance a 401 until they are equal again (a 401 storm, and every refused
message is lost):

- a clinic admin changing `uazapi_token` (or `uazapi_instance_name`) in the
  credentials panel (`PATCH /v1/config/api-credentials`);
- the uazapi instance being re-created on the uazapi side (it gets a new
  token, and possibly a new name);
- a token rotation done by anyone, on the uazapi side or in the panel.

**The rule: any token or instance change of an enforced tenant first goes
back to shadow** ([Rollback](#rollback)), **then the change is made, then the
whole [enforce order](#the-enforce-order-decision-10) runs again**
([re-enforce rule](#every-return-to-enforce-runs-the-whole-order)).

**Nothing in the panel stops this**: the credentials panel accepts a token
change while the flag is on (changing the panel is out of the scope of this
check). The owner's notice to each enforced clinic must say that the uazapi
token and instance must not be changed or re-created without first
coordinating with Neurostack, and why.

### Optional alarm (owner step, AWS write)

An alarm can shorten the time to notice. **Creating it is an AWS write and
needs the owner's explicit go**; the watch above stands without it. First
test each pattern (read-only) against real rendered lines:

```bash
aws logs test-metric-filter --region "$REGION" \
  --filter-pattern '"uazapi_webhook_auth mode=enforce" -"result=match"' \
  --log-event-messages \
    'uazapi_webhook_auth mode=enforce result=mismatch instance=my-instance token_present=True routed_matches=False provider_matches=True owner_present=True from_me=False base_url_matches=True' \
    'uazapi_webhook_auth mode=enforce result=match instance=my-instance token_present=True routed_matches=True provider_matches=True owner_present=True from_me=False base_url_matches=True'

aws logs test-metric-filter --region "$REGION" \
  --filter-pattern '{ $.event = "Unhandled exception" && ($.endpoint = "/uazapi" || $.endpoint = "/v1/webhook/uazapi") }' \
  --log-event-messages '{"endpoint": "/uazapi", "method": "POST", "error_type": "RuntimeError", "event": "Unhandled exception", "level": "error"}'
```

The first must match only the `mismatch` line, the second its one line.
Then, with the go, create a metric filter for each on `LOG_GROUP`
(`aws logs put-metric-filter`, metric value
`1`, default `0`, a metric name and namespace chosen by the owner) and an
alarm on each metric (`aws cloudwatch put-metric-alarm`, `Sum` > 0 over
5 minutes, notifying a topic the owner names; verify the topic exists
first). The first pattern is a term match, **not anchored**: it also counts
foreign-looking refusals and any line that merely contains that text. An
alarm on it is only a prompt to run the anchored W1, never a trigger by
itself. The second is a JSON-field pattern: a stdlib line is not JSON, so
text embedded in one cannot match it, but an `Unhandled exception` line over
the 15000-byte cap may not parse as JSON either, so the alarm can miss a 500
whose traceback is long. W3 (text, anchored on the head) still finds it.

## Every hosted version from now on (Decision 11)

**From the moment auth is enforced, every hosted AppConfig version of the
egress-flags profile must carry `uazapi_webhook_auth_enforced: true`.** Here
`false` means no authentication, so the usual mistakes switch enforcement off
silently:

- deploying an older version (a rollback of another flag to a version from
  before step 4) returns to shadow;
- deploying the "Hosted content (closed)" template of
  [`asaas-deployment.md`](./asaas-deployment.md#appconfig-and-opening-financial-egress-follow-up-f2),
  where every flag is false, returns to shadow and closes every egress;
- a version that leaves the flag out returns to shadow;
- a failed or stale poll also reads shadow, for as long as it lasts.

Nothing refuses or alarms on any of these. The way to notice is the `mode`
label: after enforcing, a `uazapi_webhook_auth mode=shadow` line (W4) or an
increase of a `mode="shadow"` child of
`neurostack_uazapi_webhook_auth_total` means a worker is back in shadow.
Check it after every AppConfig deployment.

### Every return to enforce runs the whole order

**Every transition from shadow to enforce is a new origin switch, and runs
the whole [enforce order](#the-enforce-order-decision-10) from step 1.** This
covers:

- fixing a silent drop to shadow by deploying a version (an older AppConfig
  version, a version without the flag, or the closed template was
  deployed);
- re-enforcing after a [rollback](#rollback);
- re-enforcing after a rotation or any other token or instance change.

Each of these closes `agent_media_send_enabled` first (step 3) and goes
through every step, the inventory, the pre-checks, the post-enforce watch and
the owner's go to reopen media included. **Redeploying the flag `true` while
`agent_media_send_enabled` is open is forbidden**: it would make turns
`PRODUCTION` with media open and no media pre-check, which Decision 12
forbids. While the drop lasts, the clinics are in shadow, served as before.

A worker that dropped to shadow on a failed or stale poll, while the
deployed version still carries the flag `true`
([Corroboration](#corroboration-before-and-after-acting), W4), returns to
enforce by itself at its next successful poll: nobody deploys anything, so
this rule does not apply. Record it, with the R1 lines that show it. If the
owner prefers the uazapi clinics not to switch back by themselves, the only
lever is a [rollback](#rollback), and the return then follows this rule.

## Rollback

Close the auth flag: create and deploy a hosted version with
`values.uazapi_webhook_auth_enforced.enabled` `false` (or without the
flag), keeping every other flag at its current value, with the no-bake
strategy. Read it back per worker with R1, as in step 4 of the enforce
order: one `appconfig_egress_flags_changed` line per worker with
`uazapi_webhook_auth_enforced=false`. Each worker returns to shadow at its
next successful poll (at most 60 s); a worker whose refresher stalls or
whose poll fails also reads shadow, once its last successful reading is
three poll intervals old (at most 180 s) or at once on a failed poll
(closing needs no successful poll). Its
lines read `mode=shadow` again.

In shadow the clinic's uazapi turns are `UNKNOWN` again, so the media
selection stops offering them files (`origin_not_production`) and the
Central de Mídias of a uazapi clinic answers `delivery_enabled = false`,
whatever `agent_media_send_enabled` says.

**Enforcing again after a rollback** follows the
[re-enforce rule](#every-return-to-enforce-runs-the-whole-order): the whole
enforce order from step 1, with `agent_media_send_enabled` closed first.
Never redeploy the flag `true` with media open.

What remains:

- events already written with `interaction_origin = 'production'` stay as
  they are (the column is immutable, `trg_events_interaction_origin_immutable`);
- authenticated observational rows (`credit_observational_effects` with
  `origin = 'production'`, `authenticated = true`) stay as they are: the
  table is append-only.

### Mode-switch edge

An event seen in shadow and redelivered after enforce keeps its first
observational row, the unauthenticated one written in shadow: observational
rows are append-only and never rewritten. The redelivery logs its own
`mode=enforce` auth line, which carries no message id; it is then skipped by
the Redis replay claim. For most messages the skip logs
`Uazapi webhook: duplicate msg <id> from <phone> skipped`; a patient voice
note or image is claimed later, after its download, and its skip logs no
line. The same holds in the other direction after a rollback.

## Side effects of the switch to enforce

- **Observed consumption rises.** Each verified turn with a deliverable
  response records `would_debit_mc` of one trace, 1,000 mC
  (`TRACE_COST_MC`), where it recorded 0. The `observed` totals of
  `GET /v1/credits/consumption` rise accordingly. Nothing is charged.
- **`status` of the credit enforcement task shows the guard reason, while
  the credit switch is effective.** This does not depend on the auth flag:
  from the deployment of the image that carries this check,
  `python -m src.ops.credit_enforcement status` reports the reason
  `uazapi_enforced_credit_adapter_missing` and
  `readiness.ready_after_arm_closures: false`. With the credit switch off,
  `status` answers `switch_off` (exit 21, reason `credit_switch_off`) before
  the guard is read, and the guard reason does not appear. With the credit
  configuration off it reads nothing; with it on, the switch first reads
  `alembic_version` (the `switch_off` row of
  [`credit-enforcement-arming.md`](./credit-enforcement-arming.md)).
- **Analytics see uazapi turns change** from `unknown` to `production`
  (`events.interaction_origin`) from the switch on.

## The L10 guard and the follow-up

A verified uazapi turn is metered as authenticated, but no enforced credit
adapter charges or blocks it: the uazapi webhook never calls the enforced
pre-check nor the enforced sender. Arming the credits now would serve that
traffic for free. So `arm_enforcement` **always** refuses, before any write,
through the predicate `uazapi_enforced_credit_adapter_available()` in
`src/services/credit_rollout.py`, which always answers `False`. While the
credit switch is effective:

- `python -m src.ops.credit_enforcement arm` answers `refused` (exit 20)
  with the reason `uazapi_enforced_credit_adapter_missing`;
- `status` carries the same reason and `ready_after_arm_closures: false`;
- the reason is not a cutoff reason code, so `accept` cannot clear it, and
  nothing an operator sets lifts it.

With the credit switch off, `arm` and `status` both answer `switch_off`
(exit 21) first, so the guard reason is not shown; nothing can be armed then
either.

Only the follow-up wish **`uazapi-credit-enforcement`** removes the guard,
with the enforced uazapi adapter. **It must land before L10**
([`credit-enforcement-arming.md`](./credit-enforcement-arming.md)).
