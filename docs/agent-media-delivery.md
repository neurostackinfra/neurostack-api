# Agent media delivery (Central de Mídias over WhatsApp)

The agent can send one file from the clinic's Central de Mídias to the
patient over WhatsApp, after its text reply (in a background task, so it can
arrive late; see [Known limits](#known-limits-and-debts)). This page describes how
the agent picks the file, how it is delivered and charged, what every
outcome means, and how the feature is rolled out, closed and rolled back.

The plan behind it is the wish `envio-midia-agente`
(`.genie/wishes/envio-midia-agente/WISH.md`; its design is
`.genie/brainstorms/envio-midia-agente/DESIGN.md`). The code is the source of
truth, and the [source map](#source-map) at the end lists every constant this
page cites.

> **State on 2026-11-08.** The schema (migration `045`) is on `main` (#201)
> but has not run in production, which is still at `042` on revision 17. The
> behaviour release (PR-B) is not merged. `agent_media_send_enabled` is closed
> everywhere. Nothing on this page authorizes a deployment, a window or
> opening the flag: each one is a separate owner step (see
> [Rollout order](#rollout-order)).

## In one paragraph

A clinic binds a file to patient subjects ("Quando enviar", the column
`media_assets.trigger_intents`) and switches "Sugestão Automática por IA" on
(`auto_suggest`). On a production WhatsApp turn whose intent is one of those
subjects, or a greeting or FAQ turn (the implied "Outras", which every file
with the switch on carries), the server offers the LLM up to 5 numbered
files. The LLM may end
its reply with `[[MIDIA:n]]` to choose one. The server always strips the
marker. After the text is accepted by the provider, a background task sends
the file at most once per session and file in 24 h, charges the egress per
the credit mode, and, if the file cannot go, sends a fixed notice instead.
Everything sits behind the AppConfig flag `agent_media_send_enabled`, which
fails closed.

## Providers, readiness and formats

### Which turns can carry a file

A file is offered only on a turn whose origin is
`InteractionOrigin.PRODUCTION`. That origin comes from the webhook, never
from the payload:

| Provider | Webhook origin today | Why |
|---|---|---|
| Meta Cloud | `PRODUCTION` when the request carries a valid `X-Hub-Signature-256` HMAC over `meta_app_secret` | the signature proves the caller |
| Z-API | `PRODUCTION` | the capability URL token matches `zapi_inbound_token_digest` and the body's instance |
| uazapi | `PRODUCTION` only when `uazapi_webhook_auth_enforced` is on and the callback's envelope `token` equals the stored `uazapi_token` of exactly one row of the instance (`match`); otherwise `UNKNOWN` | in shadow (the flag off, absent or unreadable) the check only measures and the instance name stays a lookup key; enforced, the instance token proves the caller and anything but `match` is refused with 401 |
| Evolution | `UNKNOWN` | the instance name is a lookup key, not a proof |

So **Evolution receives no media until its webhook is authenticated**, and
**uazapi receives none while its webhook authentication is in shadow**. The
uazapi check (wish `uazapi-webhook-auth`) ships in shadow; enforcing it is an
owner step with its own order
([`runbooks/uazapi-webhook-auth.md`](runbooks/uazapi-webhook-auth.md)), and
from then on the clinic's verified turns are `PRODUCTION` (see
[Decision 12](#decision-12-before-uazapi-or-evolution-become-production)).
Both senders exist and are tested; they start working on their own once the
origin changes.

The four webhooks pass the delivery provider as a server parameter. Only
those four paths do: `POST /v1/events`, the simulator and webchat pass none,
so their `EventResponse.attachment` is always `null`, even with
`channel = "whatsapp"`.

### Readiness: what the Central de Mídias promises (Decision 8)

`GET /v1/media-assets/bindable-intents` answers `delivery_enabled`. It is
true only when **both** hold:

- the flag `agent_media_send_enabled` is effective; and
- `media_delivery_provider_ready(credentials)` is true for the clinic's
  `api_credentials`.

`media_delivery_provider_ready` is the single readiness point. For a
uazapi clinic, `bindable-intents` reads `uazapi_webhook_auth_enforced` and,
only when it is on, `uazapi_egress_enabled`, and passes both to it:

| `whatsapp_provider` | Ready when | Otherwise |
|---|---|---|
| `meta_cloud` | `meta_phone_number_id`, `meta_permanent_token` **and** `meta_app_secret` are set | without the secret the readiness is `signature_pending` and the webhook answers 401: not ready |
| `zapi` | `zapi_instance_id`, `zapi_token` **and** `zapi_inbound_token_digest` are set | not ready |
| `uazapi` | `uazapi_api_url`, `uazapi_instance_name` **and** `uazapi_token` are set, `uazapi_webhook_auth_enforced` is on (its verified turns are production) **and** `uazapi_egress_enabled` is open (the file can be sent) | not ready; in shadow it is never ready |
| `evolution` | never | not ready until its webhook is authenticated and a gate is plugged in here |
| `dialog360`, `twilio`, anything else | never | no media sender at all |

Readiness drives only the screen's promise. The selection does not call it.
The selection is cut by the turn's origin, and the delivery reads the flag
again. A future Evolution gate is wired into `media_delivery_provider_ready`
with a test, as the uazapi one was.

### Formats each provider sends

One table, `_SENDABLE_MIME_TYPES` in `src/services/media_send_body.py`,
feeds the senders, the selection SQL and the `sendable_mime_types` field of
`bindable-intents`:

| Format | Meta | Z-API | uazapi | Evolution |
|---|---|---|---|---|
| `image/jpeg`, `image/png` | yes | yes | yes | yes |
| `application/pdf` (sent as a document with its `original_filename`) | yes | yes | yes | yes |
| `audio/mpeg` (MP3) | yes | yes | yes | yes |
| `audio/ogg` (OGG-Opus, **mono only**) | yes | no | no | no |
| `audio/mp4` (M4A) | yes | no | no | no |
| `video/mp4` | yes | yes | yes | yes |

- **Z-API is MP3-only for audio** until a live test on a real instance. Its
  `send-audio` documentation shows only `audio/mpeg`. To unlock OGG or M4A
  after that test, add them to the `zapi` set; nothing else changes.
- **Stereo OGG is refused at upload.** `POST /v1/media-assets` answers
  `422 media_audio_not_mono` for an OGG-Opus whose OpusHead does not say one
  channel. Meta accepts mono Opus only, and no other provider sends OGG, so
  every stored OGG is one the agent can send. The delivery still checks mono
  again before the claim (see [Delivery](#delivery)): the bridge image
  (`045` without the behaviour release) accepts stereo OGG, and such a file
  must never be claimed or charged.
- `sendable_mime_types` in `bindable-intents` ignores readiness: it lists
  the formats of the clinic's provider (`[]` for a provider with no sender).
  The screen only uses it while `delivery_enabled` is true.

### What the Central de Mídias shows

From `bindable-intents` (`intents`, `topics`, `delivery_enabled`,
`max_bytes_by_category`, `sendable_mime_types`):

- "Quando enviar" shows one checkbox per entry of `topics`, in its order:
  "Pedido de Preço", "Solicitação de Agendamento", "Interesse Tratamento",
  "Dúvida Pós-Procedimento". Checking one binds all of its intents; a topic
  shows checked when any of its intents is bound, and saving stores the whole
  topic. "Outras" (`implicit_intents`: `saudacao`, `faq`) has no checkbox: it
  is always included, and the help line says so ("Saudação e perguntas
  frequentes já estão incluídas.").

- `delivery_enabled = false`: "Envio automático ainda não ativo para o canal
  da clínica." The card "Sugestão Automática por IA" itself is unchanged.
- The read failed: "Não foi possível verificar se o envio automático está
  ativo." There is no client-side size check, no alert is shown, and nothing
  is unbound.
- `delivery_enabled = true` and the switch on: the real rule ("A IA pode
  enviar este arquivo pelo WhatsApp quando a conversa combinar com "Quando
  enviar" … no máximo uma vez a cada 24 h por paciente.").
- Alerts (a file with no subject marked raises none: it still goes on
  greeting and FAQ turns):
  - file above its category ceiling: "Grande demais para envio pelo
    WhatsApp", with the limit read from `max_bytes_by_category`;
  - format outside `sendable_mime_types`, only while `delivery_enabled` is
    true: "Este formato não é enviado automaticamente pelo canal da
    clínica.";
  - a stereo OGG upload: the message for `media_audio_not_mono` ("Áudio OGG
    precisa ser mono (1 canal)…").

## Selection

`select_media_candidates` (`src/services/media_selection.py`) runs in phase
2 of a turn, after the SRM and before the LLM. It stops at the first "no",
in this order.

**1. Pure turn checks (no SQL, no flag read).** `media_turn_exclusion`
returns the first reason that applies. These reasons go to a debug log only
(`agent_media_selection_skipped`), never to a metric. The exception is
`no_delivery_provider`: `select_media_candidates` returns before it computes
any exclusion, so that case logs nothing at all, not even at debug level.

| Reason | When |
|---|---|
| `no_delivery_provider` | the caller gave no provider (`POST /v1/events`, simulator, webchat, or a process without an `agent_sends` pool). Silent: never logged |
| `unknown_delivery_provider` | the provider is not `meta`, `zapi`, `uazapi` or `evolution` |
| `provider_sends_no_media` | the provider's sendable set is empty |
| `origin_not_production` | `context.origin` is not `PRODUCTION` (today: every Evolution turn, and every uazapi turn unless its webhook authentication is enforced and the callback matched) |
| `identity_mismatch` | the pipeline result's tenant or session differs from the context's |
| `deterministic_response` | the pipeline already produced a literal reply (scheduling link and similar) |
| `handoff` | handoff required, handoff mode, a handoff reason or summary, or a turn that pauses the session |
| `handoff_confirmation` | the scheduling confirmation question ("Posso te encaminhar?"), which the prompt asks alone |
| `scheduling_link_verification` | the patient's answer after the scheduling link (`concluiu` / `nao_concluiu`), which feeds the pending handoff |
| `policy_blocked` | a policy block |
| `safety` | a safety concern or a safety category other than `none` |
| `intent_not_bindable` | the turn's intent is neither in the allowlist below nor implied (`saudacao`, `faq`) |
| `price_hidden` | a `pedido_preco` turn whose prompt may not show a price |

**2. The flag.** `agent_media_send_enabled` is read before any database
session is opened. Off, absent, invalid or unreadable means no candidates
(debug reason `flag_off`).

**3. The candidate SQL**, on a session of its own, PostgreSQL only. A row
is a candidate when **all** of the following hold:

- it belongs to the turn's tenant;
- `auto_suggest = true`;
- the turn's intent is in `trigger_intents`, or the turn is a `saudacao` or
  `faq` one (`MEDIA_IMPLICIT_INTENT_SEQUENCE`), which reads every automatic
  file whatever it is bound to;
- its `mime_type` is in the provider's sendable set;
- its `size_bytes` is within its category ceiling (below);
- it was **not** sent to this session in the last 24 h
  (`media_deliveries.last_sent_at > now() - 24 h`);
- if it carries `pedido_preco` in `trigger_intents`, the turn's price policy
  allows a price. This applies **whatever the turn's intent is**: a price
  file is still hidden on a `faq` turn when the price is.

Rows are ordered newest first (`updated_at DESC, id DESC`). At most 5
(`MEDIA_CANDIDATES_MAX`) reach the prompt; more than 5 also counts
`candidates_capped`. When there are candidates, the names of up to 5 files
this session received in the last 24 h (`MEDIA_RECENTLY_SENT_NAMES_MAX`) go
with them. The SQL never reads `payload`. Any error from step 2 on reads as
"no candidates": the selection never breaks a reply.

**The topics and the allowlist** (`MEDIA_TOPIC_INTENTS`,
`MEDIA_BINDABLE_INTENT_SEQUENCE`,
`packages/neurostack-core/neurostack_core/protocol.py`):

| Topic (screen) | Intents it binds |
|---|---|
| `pedido_preco` "Pedido de Preço" | `pedido_preco` |
| `agendar` "Solicitação de Agendamento" | `agendar`, `pre_atendimento` |
| `interesse_tratamento` "Interesse Tratamento" | `interesse_tratamento`, `comparacao_tratamentos`, `objecao_vou_pensar`, `detalhes_servicos`, `cultura_historia`, `qualificacao_publico`, `informacoes_profissionais` |
| `pos_atendimento_duvida` "Dúvida Pós-Procedimento" | `pos_atendimento_duvida` |
| "Outras", implied, never stored (`MEDIA_IMPLICIT_INTENT_SEQUENCE`) | `saudacao`, `faq` |

The allowlist is the union of the four topics (11 intents). The owner opened
`agendar` to media on 2026-11-08; the scheduling gate still runs on the
reply's text and drops the attachment when it replaces it. The other
scheduling intents (`reagendar`, `cancelar`, `confirmar_presenca`,
`disponibilidade_horarios`), safety, handoff (including `fora_do_horario`,
which enqueues a handoff, and `reclamacao`, which always hands off), billing
and patient-data intents are left out on purpose. The API refuses any other
value in `trigger_intents` (`POST` multipart repeated field, `PATCH` JSON
list), `saudacao` and `faq` included, with `422 media_trigger_intent_unknown`.
It drops duplicates and stores the list in allowlist order.

**Category ceilings** (`MEDIA_CATEGORY_MAX_BYTES`, decimal bytes, the same
values as the upload limits): image 5,000,000; PDF 20,000,000; audio
16,000,000; video 16,000,000. Older assets of up to 50 MB can still exist.
They are never candidates, and the delivery refuses them as `too_large`.

**The price rule.** `turn_can_show_price` joins the clinic config adapter
(`clinic_price_variables`) with the pure `can_show_price` that the normal
prompt renders its pricing block from, so the prompt and the media never
disagree. A price may be shown when it is not held (insurance active and
the modality not confirmed as `particular` holds it) and the mode allows it:
`pricing_mode` `range_allowed` or `exact_allowed`, or `provide_prices = yes`,
which counts as `exact_allowed` even with `consult_only`.
`tests/services/test_price_disclosure_policy.py` pins the parity.

**The prompt.** With candidates, two messages go right before the patient's
current message, in the pattern of `PRIOR_EPISODE_CONTEXT_POLICY_MESSAGE`:

1. a constant `system` message (PT or EN) with the marker rule. It treats
   the clinic's names and descriptions as data. It forbids saying a file is
   being sent without the marker, and it forbids offering again, or claiming
   the patient received, the files sent in the last 24 h;
2. a `user` message with the JSON data: the numbered files (index, name,
   `usage_trigger`) and the recent names.

Without candidates nothing is added, and the message list is byte for byte
the one without media.

## The marker contract

The LLM chooses at most one file by ending its reply with `[[MIDIA:n]]`,
where `n` is an index the server numbered. `parse_media_marker`
(`src/services/media_marker.py`) runs on the raw LLM text before anything
else reads it: the price gate, the scheduling gate, the TTS, the persisted
`suggested_response` and the training records. It runs on **every** reply,
including turns that offered nothing. No marker variant ever reaches the
patient.

- **Spelling.** `MIDIA`, `MÍDIA` or `MEDIA`, any case, matched on the NFC
  form. The word must not run into a letter, so `[[Mediação]]`, `[[Mídias]]`,
  `[[Mídia social]]` and `[[Media Kit]]` stay as ordinary text.
- **Closed variants are removed wherever they are.** `[[midia:1]]`,
  `[[ MIDIA : 9 ]]`, `[[MIDIA:x]]`, `[[MIDIA 1]]`, `[[MIDIA-1]]`, the
  markdown-escaped `\[\[MIDIA:1\]\]` and `[[[MIDIA:1]]]` (one stray bracket
  on each side) all go, with the blanks just before them.
- **Half-bracket markers are removed and never resolve.** `[MIDIA:1]`,
  `[[MIDIA:1]` and `[MIDIA:1]]` all count as a marker with no valid index.
- **A truncated tail is removed only at the very end of the text, and only
  if it is a prefix of a marker.** `[[`, `[[M`, `[[MIDIA:` and `[[MIDIA:1`
  (a reply cut by `max_tokens`) go. Any other `[[` stays, so "Use [[ para
  abrir…" comes back unchanged.
- **Resolution.** Only a closed `[[MIDIA:<1 to 6 digits>]]` within
  `1..N` (the offer's size) resolves. The first valid one wins. An
  out-of-range, non-numeric, truncated or half-bracket marker never does.
- **Empty after stripping.** If the text is only markers, it parses to no
  text. The turn takes the `no_llm_response` path, and the file is dropped
  even when the index was valid.
- Text without a marker comes back unchanged. Every pattern is linear in the
  text length, because the parse runs synchronously on every reply.

The file is also dropped (outcome `dropped`) when the scheduling gate
rewrites the text, and, defensively, on a handoff or deterministic turn. A
chosen file becomes `EventResponse.attachment`
(`{media_id, display_name, category, mime_type, size_bytes}`).

## The 24 h window

A file reaches a session at most once in 24 h
(`MEDIA_DELIVERY_REPEAT_WINDOW`, `src/repositories/media_deliveries.py`).
The table `media_deliveries` holds one row per pair, `UNIQUE (session_id,
media_id)`. Deleting the tenant, the session or the media asset cascades to
its rows.

**The claim** is one atomic statement, measured on the database clock
(`now()`, the claiming transaction's start), so every worker agrees on the
window:

```sql
INSERT INTO media_deliveries (id, tenant_id, session_id, media_id, last_sent_at)
VALUES (…, now())
ON CONFLICT (session_id, media_id) DO UPDATE SET last_sent_at = now()
WHERE media_deliveries.last_sent_at <= now() - interval '24 hours'
RETURNING id
```

- A row back means this call claimed: the first send, or a pair whose last
  send is at least 24 h old.
- No row back means the file went within the window: `deduplicated`, in
  silence. A file sent to the session in the last 24 h is not even a
  candidate (selection step 3), so a later turn simply offers it no more.
  The claim therefore says `deduplicated` only when two deliveries of the
  pair race (two workers or tasks), or when the credit outbox recovers a row
  whose file already went.
- Two concurrent claims of a pair: the second waits on the first one's row
  lock and then sees its fresh `last_sent_at`.
- The claim runs in its own transaction and **commits before any byte
  leaves**. A send whose claim did not commit never happens. If the claim
  errors, the provider is not called.
- **A claim is never released.** If the send then fails (`failed`), the
  credit is refused at the charge (`credit_pending`), the asset disappears
  before its bytes are read, the outer bound cuts the delivery, or the
  process shuts down mid-send, the window stays consumed for 24 h. This was
  accepted in the design: delivery is at most once, like the outbox
  publisher. After 24 h the patient can ask again, or the clinic sends the
  file by hand.
- Refusals **before** the claim (`flag_off`, `asset_gone` at the re-read,
  `too_large`, `busy`, `credit_refused` from the mode or the suspension)
  write nothing in `media_deliveries` and consume no window.

## Delivery

`deliver_reply_media` (`src/services/reply_media.py`) delivers the chosen
file. It is started by `start_reply_media`
(`src/services/reply_media_runner.py`) only after the provider accepted the
reply's text (its 2xx), on two kinds of path:

- the four inline webhook replies (Z-API, Evolution, uazapi, Meta). The
  request ends its transaction first and answers as before;
- the credit response outbox. There it is a step after `_finish_delivered`,
  outside the outbox's `REPLY_SEND_TIMEOUT_SECONDS` (240 s),
  `SENDING_LEASE_SECONDS` (330 s) and its serial tick. A media failure never
  changes the text's row. The outbox carries Z-API and Meta replies only.

The delivery runs as a task the process owns. It never uses the caller's
session, and every database step is its own short session.

### The order of checks

The checks run in this order, and an earlier one never depends on a later
one:

1. **Flag.** `agent_media_send_enabled`, read before any session. Off or
   unreadable: `flag_off`.
2. **Re-read** of the asset in the turn's tenant, with the first 301 bytes
   of its payload (`REREAD_PREFIX_BYTES` = 27 + 255 + 19, enough for the
   OpusHead locator) and never more. The result is `asset_gone` when the
   asset:
   - was deleted, or is not in the tenant;
   - is no longer automatic (`auto_suggest` off; a file bound to no subject
     still goes on the implied "Outras" turns);
   - has a MIME the channel's provider does not send (reason
     `mime_not_sendable`);
   - is refused by the sender's own pre-network check
     (`require_sendable_media`) on that prefix. The reason is
     `ogg_opus_not_mono` for a non-mono OGG on Meta, or
     `empty_media_payload` / `unsupported_media_type`.
3. **Ceiling.** `size_bytes` above `MEDIA_CATEGORY_MAX_BYTES` for its
   category: `too_large`.
4. **Permit.** First, a pair this process is already waiting for or
   sending is `deduplicated` (reason `in_flight`), in silence, before any
   wait and with no write. Otherwise the pair joins the in-flight set and
   waits for the process's one `agent_sends` permit. The wait is FIFO and
   lasts at most `AGENT_MEDIA_PERMIT_WAIT_SECONDS` (60 s). If no permit is
   handed over in time: `busy`. The pair leaves the in-flight set on every
   exit (sent, refused, failed, `busy`, error, cancellation).
5. **Credit mode**, read without a `Request`, plus, when enforced, the
   builder's suspension. If the mode is unavailable or unreadable, the
   suspension is unreadable, or the tenant is suspended: `credit_refused`.
6. **Claim** (above). Taken within the window: `deduplicated`, in silence.
   If the claim errors: `failed`.
7. **Egress**, per the credit mode ([Credit](#credit)). `credit_pending` or
   a charge error: `credit_refused`. The window is already consumed.
8. **Sender.** The payload is read now, under the permit, in its own
   session. The provider's media sender then runs under one deadline. Any
   failure: `failed`. If the asset is gone at this read: `asset_gone`
   (reason `asset_gone_before_send`). If the provider accepts: `sent`. The
   permit is released as soon as the send ends.
9. **Record.** `record_sent_id` stores the provider's message id for the
   `fromMe` echo, so the echo is recognised as the agent's and does not
   pause the AI (on Z-API an unrecognised audio echo would read as staff
   audio). It is shielded from cancellation: once the provider answered,
   the message is out.

### Deadlines and outer bounds

| Bound | Meta | Z-API, uazapi, Evolution | Source |
|---|---|---|---|
| httpx timeout per operation | 60 s (connect 5 s) | same | `MEDIA_SEND_TIMEOUT` |
| Provider call deadline (step 8) | 250 s (upload + send, 2 × 125 s) | 125 s (1 request: 5 + 60 + 60) | `REPLY_MEDIA_SEND_TIMEOUT_SECONDS` |
| Permit wait (step 4) | 60 s | 60 s | `AGENT_MEDIA_PERMIT_WAIT_SECONDS` |
| Notice send | 35 s | 35 s | `REPLY_MEDIA_NOTICE_TIMEOUT_SECONDS` |
| Each record | 30 s | 30 s | `REPLY_MEDIA_RECORD_TIMEOUT_SECONDS` |
| Delivery worst case, database excluded | 60 + 250 + 35 + 30 = **375 s** | 60 + 125 + 35 + 30 = **250 s** | `REPLY_MEDIA_MAX_SECONDS` |
| Database allowance (5 steps × the 30 s pool timeout) | 150 s | 150 s | `REPLY_MEDIA_DB_SECONDS` |
| **Outer bound per delivery** | **525 s** | **400 s** | `REPLY_MEDIA_OUTER_SECONDS` |

The outer bound is a backstop against a hung statement, not a latency
budget. A delivery it cuts ends **with no notice** (log alert
`agent_media_outer_bound`), and any connection it cut is invalidated, never
returned to the pool (`CutSafeSession`). On shutdown the lifespan cancels
the deliveries still running before the database pool closes (log reason
`shutdown`). One cut while waiting leaves with no write. One cut mid-send
has consumed its window with no notice. Either cut counts once as `failed`,
unless the delivery had already counted its outcome.

### One permit per worker

`AGENT_MEDIA_SENDS_PER_PROCESS = 1` (Decision 9). A 20 MB PDF send peaks
below 80 MB with the real senders. The production task has 2048 MB shared
with the `otel` sidecar, runs 4 uvicorn workers per container, and already
peaked near 69 %. One permit each tops out around 85 %; two would be about
101 %. It is a constant, not a setting, and it rises only after a new memory
measurement. The wait costs no memory, because a waiting delivery holds the
301-byte prefix at most.

The `agent_sends` pool is separate from the panel's `uploads` and
`downloads` pools, which never queue. An agent send never takes a download's
permit, and the reverse.

## Outcomes and metrics

Two counter families, registered in `METRIC_DIMENSION_CONTRACTS`
(`src/observability/metric_dimensions.py`) with closed label vocabularies
and no tenant, session, media or phone identity (those go to the logs). Every
closed child starts at zero.

### `neurostack_agent_media_selection_total{outcome}`

Counted once per generated turn that **offered** candidates. Turns excluded
before the offer count nothing.

| `outcome` | Meaning |
|---|---|
| `selected` | a valid `[[MIDIA:n]]`: the turn carries the file |
| `none` | candidates offered, the LLM chose none (also when the LLM call failed or returned nothing) |
| `invalid_marker` | a marker was found but named no index the server generated (out of range, not a number, half-bracket, truncated) |
| `dropped` | a valid choice dropped afterwards: empty text once the marker is gone, the scheduling gate replaced the text, or a handoff or deterministic reply |
| `candidates_capped` | more than 5 candidates matched. Counted **in addition** to the final outcome, so it is not part of the "turns with candidates" denominator |

### `neurostack_agent_media_delivery_total{provider,outcome}`

`provider` is `meta`, `zapi`, `uazapi` or `evolution`. There is exactly one
outcome per started delivery. The log's `reason` tells the cases inside one
outcome apart.

| `outcome` | Meaning | Notice | Window consumed |
|---|---|---|---|
| `sent` | the provider accepted the file | no | yes |
| `deduplicated` | the 24 h claim was taken (sent to this session within the window: a race between two deliveries of the pair, or an outbox recovery), or this process is already waiting for or sending the same pair (reason `in_flight`) | **no** | claim taken: already was. `in_flight`: not necessarily, because the other delivery may still be waiting for the permit and end `busy` or `flag_off` without ever claiming |
| `failed` | the provider call failed or hit its deadline, or a database step (re-read, claim, payload read) errored; also a delivery cut by the outer bound (reason `outer_bound`) or by shutdown (reason `shutdown`) before its outcome was counted, which sends no notice | yes, except after a cut | yes, unless the re-read or the claim errored (a cut: only if it came after the claim) |
| `asset_gone` | no longer deliverable on this channel: deleted or left the tenant, no longer automatic, `mime_not_sendable`, `ogg_opus_not_mono` (or `empty_media_payload` / `unsupported_media_type`) at the re-read; `asset_gone_before_send` after the claim; `tenant_gone` at the charge | yes | no at the re-read; yes after the claim |
| `too_large` | above its category ceiling | yes | no |
| `busy` | no `agent_sends` permit handed over within the 60 s FIFO wait | yes | no |
| `credit_refused` | credit enforcement unavailable, mode or suspension unreadable, `tenant_suspended` (all before the claim), or `credit_pending` / the charge erroring (after it) | yes | no before the claim; yes after it |
| `flag_off` | `agent_media_send_enabled` not effective at delivery | yes | no |

### Logs

- **Delivery.** Each delivery logs `agent media delivery` with `outcome`,
  `reason`, `provider`, `tenant_id`, `session_id`, `media_id` and, once
  step 4 waited, `permit_wait_seconds`. It never logs the phone. Alerts on
  the same event name: `agent_media_notice` (the notice failed),
  `agent_media_record` (an echo record failed), `agent_media_outer_bound`
  (reason `outer_bound`) and `agent_media_task` (reason `not_started`,
  `shutdown` or `task_failed`). The two cut alerts carry `counted`, which
  says whether that cut added the `failed` count.
- **Selection.** `agent_media_selection` (info, per counted outcome),
  `agent_media_selection_skipped` (debug, with the exclusion reason; never
  for `no_delivery_provider`, which is silent) and
  `agent_media_selection_failed` (warning, the SQL errored).
- **Credit outbox.** `credit_response_media` alerts when the outbox could
  not start a file (`media_id_unusable`, `no_agent_sends_pool`,
  `media_not_started`).

The design's reconsideration triggers read these counters: `invalid_marker`
above 5 % of the turns with candidates over 2 weeks (consider a separate
selection call), and `failed` above 2 % of deliveries (consider a retry).
Read the `failed` trigger by the log's `reason`, not by the counter alone:

- `failed` also counts the cuts. Every deploy or scale-in that cancels
  deliveries adds `failed` with reason `shutdown`, and a hung statement adds
  reason `outer_bound`. Neither says anything about a retry. Only the
  provider and database reasons (`send_timeout`, `send_failed:*`, a step
  error) feed the retry question.
- A `shutdown` increment happens while the process is exiting, so the last
  scrape may never see it. The `agent media delivery` error line
  (`alert="agent_media_task"`, `reason="shutdown"`) is the durable signal;
  the counter is best effort.

## The failure notice

When the chosen file does not go, the patient gets one fixed text through
the same provider, in the turn's `response_language` (any `en*` is English,
everything else Portuguese):

- PT: "Não consegui enviar o arquivo agora. A equipe da clínica pode te
  enviar por aqui."
- EN: "I couldn't send the file right now. The clinic team can send it to
  you here."

It is sent for `failed`, `asset_gone`, `too_large`, `busy`, `credit_refused`
and `flag_off` (`NOTICE_OUTCOMES`). It is **never** sent for `sent` or
`deduplicated`. Its id goes through `record_sent_id` too, so its echo does
not pause the AI. The reply's text is never resent. A delivery cut by the
outer bound or by shutdown sends no notice.

## Credit

The send is credit egress, mode by mode, like a panel download, using the
same functions and the same pricing ([`billing/credits.md`](billing/credits.md),
"Units"). It is **charged to the tenant** of the turn whatever the turn's
credit principal kind (Evolution turns, and uazapi turns in shadow, are
unauthenticated with no decision debit; a verified uazapi turn is metered as
authenticated, observationally only, since uazapi has no enforced credit
adapter until `uazapi-credit-enforcement`; the media does not change that).

| Mode | What happens |
|---|---|
| off | nothing is recorded |
| observational | after the claim, `record_observational_egress` records what the overage **would** debit, with `size_bytes`. It is best effort and never refuses: a recording failure does not change the delivery |
| enforced | the builder's suspension is checked before the claim (`tenant_suspended` → `credit_refused`). After the claim, `charge_egress` runs **before any byte leaves**, with a fresh server `delivery_id` (`uuid4()`) per send, never the claim row's id, which every send of the pair shares. `credit_pending` → `credit_refused`, nothing sent |

- Credit enforcement unavailable, or the mode unreadable: `credit_refused`,
  before the claim.
- A provider failure after the charge is not refunded, as with downloads.
- The inline webhook paths use the credit switch the request captured at
  entry. The outbox reads the process-wide verdict when the delivery runs.

## The flag and the kill switch

`agent_media_send_enabled` is an AppConfig egress flag, read by the same
reader as `financial_egress_enabled`, `uazapi_egress_enabled` and
`uazapi_webhook_auth_enforced` (`src/services/egress_policy.py`, `EgressFlag`
and `QuestionFlag`; the reader accepts exactly the `SUPPORTED_FLAGS` of
`src/infrastructure/appconfig.py`). It is global. The cut by provider comes from the turn's origin.

- **Fail-closed.** It is true only when the reader answers
  `{"enabled": true}`. Absent, false, not a boolean, AppConfig disabled or
  unconfigured, the Agent unreachable, a stale or failed SDK poll, or a
  reader that cannot be built all read false.
- **Read three times:** by `bindable-intents` (the screen's promise), by the
  selection before any SQL, and again at delivery step 1.
- **A flag left out of a new AppConfig version reads false.** Every hosted
  version of the egress-flags profile must carry all four flags
  (`agent_media_send_enabled`, `financial_egress_enabled`,
  `uazapi_egress_enabled`, `uazapi_webhook_auth_enforced`). Dropping
  `agent_media_send_enabled` while changing another flag silently stops
  agent media in every task, and dropping another flag while changing this
  one closes that other egress. For `uazapi_webhook_auth_enforced`, a
  dropped flag means **no authentication**: once it is enforced, every
  version must carry it `true`, or the uazapi webhook silently returns to
  shadow ([`runbooks/uazapi-webhook-auth.md`](runbooks/uazapi-webhook-auth.md),
  Decision 11).

**To close it (kill switch, no deploy).** The setup (application,
environment, profile, no-bake strategy, IAM) is in
[`runbooks/asaas-deployment.md`](runbooks/asaas-deployment.md), "AppConfig
and opening financial egress (follow-up F2)". **Do not deploy that
section's "Hosted content (closed)" JSON block as is.** It is the L7
template, with every flag false: deploying it would also close
`financial_egress_enabled` (checkout creation) and `uazapi_egress_enabled`
(every uazapi reply), either of which may be open in production, and would
turn `uazapi_webhook_auth_enforced` off, which returns the uazapi webhook to
shadow (no authentication). Instead:

1. **Start from what is deployed now.** Find the profile's current
   configuration version (the latest completed deployment of the
   environment) and download that hosted version's content:

   ```bash
   aws appconfig list-deployments --region us-east-1 \
     --application-id "$APP_ID" --environment-id "$ENV_ID"
   aws appconfig get-hosted-configuration-version --region us-east-1 \
     --application-id "$APP_ID" --configuration-profile-id "$PROFILE_ID" \
     --version-number "$CURRENT_VERSION" current.json
   ```

2. **Change only this flag.** In a copy of `current.json`, set
   `values.agent_media_send_enabled.enabled` to `false` and leave every
   other flag and value exactly as it is. The first time the flag is
   **opened**, the current version predates it: add it under both `flags`
   (`"agent_media_send_enabled": {"name": "agent_media_send_enabled"}`) and
   `values`. A flag absent from `values` reads false, so a version without
   the entry is already closed for media.
3. **Create and deploy** the new hosted version (content type
   `application/json`) with the profile's no-bake strategy. Keep the
   previous version number: redeploying it is the rollback of this change.
4. **Verify on every worker.** Each worker process runs its own refresher
   and logs `appconfig_egress_flags_changed` once when it sees the change,
   with `agent_media_send_enabled` and the new `version_label`. Query from
   the hosted deployment's start time, as in the runbook's L7 step 4:

   ```bash
   QUERY_ID=$(aws logs start-query --region us-east-1 \
     --log-group-name /ecs/neurostack-staging/app \
     --start-time "$(( HOSTED_DEPLOY_START_MS / 1000 ))" --end-time "$(date +%s)" \
     --query-string 'fields @timestamp, @logStream, @message
   | filter @message like /^\{"poll_ok": (true|false), "financial_egress_enabled": (true|false), "uazapi_egress_enabled": (true|false), "agent_media_send_enabled": (true|false), "uazapi_webhook_auth_enforced": (true|false), "version_label": (null|"[^"]*"), "error_type": (null|"[^"]*"), "error_code": (null|"[^"]*"), "event": "appconfig_egress_flags_changed"/
   | sort @logStream asc, @timestamp asc
   | limit 10000' \
     --output text --query queryId)
   aws logs get-query-results --region us-east-1 --query-id "$QUERY_ID"
   ```

   The query is anchored on the start of the JSON line the reader writes
   (`src/infrastructure/appconfig.py`), so the word
   `appconfig_egress_flags_changed` inside another line (a body value in a
   `Uazapi webhook:` line, for example) is never counted as a worker's
   readback; a term filter such as `--filter-pattern
   '"appconfig_egress_flags_changed"'` would count it. Run
   `get-query-results` again until it reads `Complete`; the last line of each
   `@logStream` is that worker's state. The pattern is the reader's line with
   all four flags; an image older than `uazapi_webhook_auth_enforced` writes
   a line without it, which this query does not match.

   Expect one line per worker (4 per task, so 8 with 2 tasks), each with
   `agent_media_send_enabled=false`, the new version label, and the other
   three flags unchanged. A worker with no line has not seen the change yet,
   or its refresher stalled. A line with `poll_ok=false` also reads closed.
   A worker started after the deployment writes its first line with
   `poll_ok=true` at its first successful poll, and reads every flag false
   until then.

**How long it takes.** With `APPCONFIG_SOURCE=sdk`, each worker closes at its
next poll: one clamped poll interval, 15 s by default and never more than
60 s. A worker whose refresher stalls keeps its last value until the value
goes stale, after three clamped intervals: **up to 180 s**. Then:

- new turns are offered no file, and `bindable-intents` answers
  `delivery_enabled = false`;
- a delivery not yet at step 1 ends `flag_off`, with the notice;
- **a delivery already past step 1 is not stopped.** It may still be waiting
  for the permit and then send. To be sure nothing is leaving, wait out the
  outer bound **from the moment the last worker closed**, not from the
  AppConfig deployment. The worst case is about 180 s + 525 s on Meta and
  180 s + 400 s on the others. Declare containment only once no new
  `agent media delivery` line with `outcome=sent` appears after that.

Opening it is the same procedure with `true`, and only on the owner's
"vai". **The flag is global**: see [Rollout order](#rollout-order), step 5,
and [Decision 12](#decision-12-before-uazapi-or-evolution-become-production)
for what an open reaches.

## Rollout order

Decision 13 of the wish, with the details in
[`runbooks/asaas-deployment.md`](runbooks/asaas-deployment.md), "Migration
045: media trigger intents and deliveries (the media bridge release)". The
schema ships first in a **release ponte** (bridge release, PR-A: migration
`045` with no ORM mapping). The behaviour release (PR-B, everything on this
page) comes only after the window. **Each step needs its own owner "vai",
except steps 1 and 2, which are approved together in one "vai"** and run
back to back.

0. **Re-pin the rollback target 16 → 17.** Done: #202, squash `df56d3f7`,
   2026-11-08.
1. **Deploy the bridge image** as revision 18 (only the app image moves from
   17). First, build it from `main` with `.github/workflows/deploy.yml`
   ([`runbooks/staging-image-build.md`](runbooks/staging-image-build.md))
   and the input **`expected_head` = `045`**, the head of that commit. The
   runbook's example still says `042`: a copied `042` fails the smoke and
   pushes nothing.
2. **Run window `045`** right after step 1. It applies `042 → 043 → 044 →
   045` in one transaction, with rollback target 17. Until it runs, any
   image of the current `main` answers OGG and M4A uploads with
   `503 media_storage_unavailable` (the runbook's "Known gap"). That is why
   steps 1 and 2 are approved together.
3. **Re-pin the rollback target 17 → 18** in its own manifest PR, once the
   window is verified. The planner blocks PR-B's image change while the
   target is still 17.
4. **Merge and deploy the behaviour release (PR-B).** Merge it into `main`
   only after the window's post-read shows production `alembic_version` =
   `045` (confirmed by the owner), rebased on the `main` that contains PR-A,
   with proof of ancestry (Decision 17). Merges into `main` are made by
   humans.
5. **Open the flag**, after a new memory reading confirms the headroom of
   Decision 9, and after the [pre-check](#pre-check-before-any-open) below.
   **The flag is global.** The send path checks only that the turn is
   `PRODUCTION` origin; readiness drives only the screen. Opening it, for
   the [live QA](#live-qa-owner-step) or for any other reason, opens media
   for **every clinic whose turns are `PRODUCTION` origin**: today Meta with
   `meta_app_secret` and Z-API with the inbound token; once
   `uazapi_webhook_auth_enforced` is on, the uazapi clinics whose callbacks
   match too (and Evolution once its webhook is authenticated), whatever
   `media_delivery_provider_ready` says. Every file with the switch on can
   go: on the turns of its subjects, and on every greeting and FAQ turn (the
   implied "Outras"), whatever it is bound to. The Central de Mídias lets a
   clinic bind subjects and turn the switch on even while it shows "não
   ativo", so the pre-check count is what an open reaches.

### Pre-check before any open

Before **every** open of `agent_media_send_enabled` (the first one, the
reopen of Decision 12, a reopen after a kill switch), count the automatic
files per provider (every one can go on a greeting or FAQ turn). Run it read-only
(`BEGIN READ ONLY; … ROLLBACK;`) against production:

```sql
SELECT
  cc.api_credentials->>'whatsapp_provider'                             AS whatsapp_provider,
  coalesce(cc.api_credentials->>'meta_phone_number_id', '')      <> '' AS meta_route,
  coalesce(cc.api_credentials->>'zapi_inbound_token_digest', '') <> '' AS zapi_route,
  coalesce(cc.api_credentials->>'uazapi_instance_name', '')      <> '' AS uazapi_route,
  coalesce(cc.api_credentials->>'evolution_instance_name', '')   <> '' AS evolution_route,
  count(DISTINCT ma.tenant_id)                                         AS tenants,
  count(*)                                                             AS auto_files
FROM media_assets ma
LEFT JOIN clinic_configs cc ON cc.tenant_id = ma.tenant_id
WHERE ma.auto_suggest
GROUP BY 1, 2, 3, 4, 5
ORDER BY 1, 2, 3, 4, 5;
```

How to read it:

- The webhooks find the tenant by a credential key alone, not by
  `whatsapp_provider`: Meta by `meta_phone_number_id`, Z-API by
  `zapi_inbound_token_digest`, uazapi by `uazapi_instance_name` and
  Evolution by `evolution_instance_name`. So the `*_route` columns say which
  webhooks can reach the tenant, and a leftover key from an earlier provider
  counts too. `whatsapp_provider` is what the Central de Mídias reads.
- A row reaches patients on an open only through a route whose turns are
  `PRODUCTION` origin (see [Which turns can carry a
  file](#which-turns-can-carry-a-file)).
- The count is an upper bound: the selection also drops files whose format
  the provider does not send or that exceed their ceiling.
- Tell the owner the counts for the routes that are `PRODUCTION` at the time
  of the open, when asking for the "vai". A non-zero uazapi row while
  `uazapi_webhook_auth_enforced` is on (or an Evolution row once Evolution
  is authenticated) means those clinics start receiving media and paying
  egress at that open.

## Rollback

1. **Close the flag first**, always. It is the fastest containment, and it
   needs no deploy ([The flag and the kill switch](#the-flag-and-the-kill-switch)).
2. **Image rollback**, per
   [`runbooks/asaas-rollback.md`](runbooks/asaas-rollback.md), "Migration
   045", and the deployment runbook's "Rollback" under "Migration 045":
   - **PR-B back to the bridge image (revision 18): safe.** The bridge
     contains `045`, so the credit switch stays on. It maps neither object,
     so the rows PR-B wrote stay, unread.
   - **Bridge back to revision 17 before the window: ordinary.** The schema
     did not change, and the switch stays on. This also closes the OGG/M4A
     gap.
   - **Bridge back to revision 17 after the window: costly, and only
     before step 3**, while the rollback target is still 17. 17's chain
     ends before `045`, so the credit switch reads
     `revision_outside_image_chain` and stays **off** until an image with
     `045` serves again. Prefer going forward.
3. **The migration goes back only by restore** of the window's snapshot
   (`pre-043-045-<date>`), an owner decision of last resort that loses every
   later write. Never the Alembic downgrade in production: `045`'s refuses
   while `media_deliveries` has a row or an asset has trigger intents.

## Decision 12: before uazapi or Evolution become production

When a uazapi or Evolution webhook origin becomes `PRODUCTION`, the flag
stops being a Meta and Z-API matter. The flag is global, and the send path
checks only that the turn is `PRODUCTION` origin:
`media_delivery_provider_ready` drives only the screen's promise. So once
those turns are `PRODUCTION`, **opening the flag for any reason, live QA on
a test instance included, is the reopen**: it opens media for every clinic
of that provider with automatic files, the production customer on
uazapi among them. It needs the owner's informed "vai" as such.

**uazapi.** Its origin becomes `PRODUCTION` when the AppConfig flag
`uazapi_webhook_auth_enforced` is opened, not when code deploys: the check
ships in shadow, where every uazapi turn stays `UNKNOWN`. Enforcing it
follows the numbered order of
[`runbooks/uazapi-webhook-auth.md`](runbooks/uazapi-webhook-auth.md), "The
enforce order (Decision 10)", which carries the steps below with their hard
preconditions and per-worker readbacks. **No code prevents enforcing while
media is open: that order is the only control.** The uazapi readiness gate
is already wired into `media_delivery_provider_ready` (ready only with auth
enforced and uazapi egress open), with tests.

**Evolution.** Its webhook is not authenticated yet; the same steps apply
when it is, with its provider gate wired into `media_delivery_provider_ready`
(with a test) so the screen's promise matches what the send path will do.

1. **Close `agent_media_send_enabled` before** the origin switch (for
   uazapi, before opening `uazapi_webhook_auth_enforced`;
   [kill switch](#the-flag-and-the-kill-switch), with its verify step).
   Otherwise those clinics would start receiving media and paying egress
   with no step of their own.
2. Switch the origin and verify it on real traffic (for uazapi: open
   `uazapi_webhook_auth_enforced`, read it back per worker, and read `match`
   and `PRODUCTION`, per the runbook).
3. Run the [pre-check](#pre-check-before-any-open) and give the owner its
   uazapi and Evolution counts.
4. **Reopen the flag only with the owner's explicit "vai"**, given for that
   scope: every clinic whose turns are `PRODUCTION` origin, with the counts
   from step 3. This is the only open; there is no separate "QA open".
5. Run the [live QA](#live-qa-owner-step) for that provider right after the
   reopen, on test instances. If an item fails, close the flag again.

To run the uazapi or Evolution QA **before** the reopen instead, run it
outside production, on an environment whose AppConfig flag is not the
production one. Never open the production flag "just for QA".

uazapi media also goes through `require_uazapi_egress`, so it needs
`uazapi_egress_enabled` open as well. Rolling the uazapi authentication back
to shadow (closing `uazapi_webhook_auth_enforced`) stops uazapi media at the
selection (`origin_not_production`) whatever `agent_media_send_enabled` says.

## Live QA (owner step)

Live QA is an owner step after the behaviour release is deployed, not an
acceptance criterion. It runs on test instances with the flag open.

> **The flag is global.** Opening it for QA opens media for every clinic
> whose turns are `PRODUCTION` origin, not only the test instance: it **is**
> the open of [Rollout order](#rollout-order) step 5 or the reopen of
> [Decision 12](#decision-12-before-uazapi-or-evolution-become-production).
> Run the [pre-check](#pre-check-before-any-open) first and ask the owner's
> "vai" for that scope.

For each provider, use a clinic whose provider is ready
(`delivery_enabled = true`) and a file bound to a subject with "Sugestão
Automática por IA" on.

**Every provider:**

- [ ] A question that matches the bound subject gets the text, then the
      file of the right type (PDF as a document with its file name, image,
      audio, video).
- [ ] The same question within 24 h does not resend the file, and no
      notice goes. Expect **no** `agent media delivery` line for that
      `media_id`: the file is no longer a candidate, so there is no offer,
      no marker and no delivery. If other files were offered on that turn,
      this file's name appears in the prompt's data message under
      `enviados_nas_ultimas_24h` (`sent_in_last_24h` in English), not as a
      candidate. A `deduplicated` line appears only on a
      race between two deliveries of the pair, on `in_flight`, or on an
      outbox recovery; this sequential repeat produces none.
- [ ] A conversation in a clinic with no automatic file behaves as before.
- [ ] A greeting or FAQ question can be offered an automatic file bound to
      no subject (the implied "Outras"); the LLM picks it only when its
      "Gatilho de uso" fits.
- [ ] Scheduling: a file bound to "Solicitação de Agendamento" can go on the
      scheduling request turn; the confirmation question ("Posso te
      encaminhar?") and the turn after the scheduling link go without a
      file; a reply the scheduling gate rewrites goes without its file
      (selection outcome `dropped`).
- [ ] The bot's own messages (text, file, notice) do not pause the AI.
- [ ] Close the flag: no turn sends media, and the Central de Mídias shows
      "Envio automático ainda não ativo para o canal da clínica.".
- [ ] `neurostack_agent_media_delivery_total` shows the outcomes, and the
      `agent media delivery` log lines carry no phone number (the senders'
      own logs still do, see [Known limits](#known-limits-and-debts)).
- [ ] Whenever a notice goes out (any outcome other than `sent` or
      `deduplicated` in the log), the patient gets it in the turn's language
      and its echo does not pause the AI. **There is no deterministic way to
      provoke a notice on demand.** The selection already drops every file
      that would be refused for its format, its ceiling or the 24 h window,
      and the upload limits equal the ceilings, so `too_large` needs an
      older asset that is never a candidate. The practical trigger is a
      race on a test clinic: send the bound question, then, before the
      reply arrives (the LLM call takes seconds), turn "Sugestão Automática
      por IA" off for that file or delete it. The delivery's re-read then
      ends `asset_gone`, with the notice. It is timing-dependent; repeat it
      if the file went. A credit refusal (`tenant_suspended` in enforced
      mode) is not a safe trigger: it needs a suspended tenant in enforced
      credit mode, which is not a test setup. Otherwise check the notice on
      the first one production produces.

**Meta** (instance with `meta_app_secret`):

- [ ] MP3, M4A and a mono OGG-Opus each arrive as audio.
- [ ] Without `meta_app_secret`, `delivery_enabled` is false.

**Z-API** (instance with the inbound token):

- [ ] An MP3 file arrives as audio, and **its `fromMe` echo does not pause
      the session** (an unrecognised audio echo would read as staff audio).
- [ ] The failure notice's echo does not pause the session either (check it
      on the first notice the instance produces).
- [ ] Only if OGG/M4A are to be unlocked: send them on this test instance
      **through the Z-API API directly** (its `send-audio` endpoint, with
      the instance's own token), not through the agent. The app refuses
      those formats on Z-API at the selection, the re-read and the sender,
      so no agent turn can test them. If they arrive as playable audio,
      change the `zapi` format set in code.

**uazapi** (only after Decision 12):

- [ ] The file arrives (the data-URI body format is confirmed here for the
      first time), with `uazapi_egress_enabled` open.

**Evolution** (only after Decision 12):

- [ ] The file arrives (audio through `sendWhatsAppAudio`).
- [ ] Evolution reports the API sends (text, file, notice) as
      `send.message`. Check that the webhook answers those events with
      `{"ok": true, "skipped": "non_message_event"}` (the app does not log
      that return, so read it from Evolution's webhook delivery log if it
      shows responses), and that no `fromMe` `messages.upsert` arrives for
      the file. A pause test proves nothing here: the event never reaches
      the takeover logic.

## Known limits and debts

- **Z-API audio is MP3-only** until a live test shows OGG and M4A work.
- **Evolution `send.message`.** Evolution reports every message sent
  through its API, the agent's file and notice included, as `send.message`.
  The webhook drops that event, so no echo of an API send reaches the
  takeover logic, and the echo record on Evolution is defensive. The same
  long-standing gap means staff replies sent through the Evolution API (for
  example by Chatwoot) never pause the AI.
- **One permit per worker.** With 4 workers per container, at most 4 media
  sends run per task at once. Further deliveries queue FIFO for up to 60 s
  and then end `busy`, with the notice. The queue is bounded in time, not in
  length.
- **The claim is not released on failure.** A failed send, a
  `credit_pending` charge, an outer-bound cut or a shutdown mid-send
  consumes the 24 h window, and the latter two send no notice.
- **The file can arrive after the next turn's text.** The delivery is a
  background task that can wait up to 60 s for the permit and then send for
  up to 125 s (250 s on Meta). The next turn's text goes inline, so a quick
  follow-up question can be answered before the file lands. Support should
  not treat that order as a bug.
- **A deploy loses the files in flight.** A deploy or scale-in cancels every
  running delivery (the lifespan's shutdown). The patient's text already
  promised the file ("segue o arquivo"), and neither the file nor the
  notice follows. A delivery still waiting for the permit leaves with no
  write and no window consumed; one cut mid-send has consumed its window.
  Either way it counts once as `failed` with reason `shutdown`, and that
  increment may never be scraped before the process exits (the log line is
  the durable signal). When file loss matters for a planned deploy, close
  the flag first and wait out the outer bound, as in the
  [kill switch](#the-flag-and-the-kill-switch).
- **An outbox recovery can send notice, then text, then file.** On the
  credit outbox (Z-API and Meta), the file starts after the text is
  delivered even when the `delivered` mark was lost. If that first delivery
  was refused before the claim (`busy`, `flag_off`, …), the patient got the
  notice and no window was consumed. The publisher later recovers the row,
  resends the text, and this time the file goes: the patient sees the
  notice, the text again, then the file.
- **No refund** of an enforced charge when the provider fails afterwards.
- **No "promise without attachment" gate.** The prompt forbids announcing a
  file without the marker, and the notice covers a failed send. A dedicated
  gate waits for the first real complaint.
- **Closing the flag does not stop a delivery already past step 1.**
- **Only the WhatsApp webhooks.** Webchat, the simulator and direct
  `POST /v1/events` clients get `attachment = null`. Simulated turns cannot
  test media.
- **The credit outbox carries Z-API and Meta only.** uazapi and Evolution
  replies in enforced mode do not go through it (scope of the credit work).
- Recorded sender debts: the uazapi data-URI format is unconfirmed until a
  live test, and the media senders log the full phone, as the existing text
  and audio senders do.

## Source map

| Constant or name | Value | Where |
|---|---|---|
| `MEDIA_TOPIC_INTENTS`, `MEDIA_TOPIC_SEQUENCE` | 4 topics | `packages/neurostack-core/neurostack_core/protocol.py` |
| `MEDIA_BINDABLE_INTENT_SEQUENCE` | 11 intents, the topics' union | `packages/neurostack-core/neurostack_core/protocol.py` |
| `MEDIA_IMPLICIT_INTENT_SEQUENCE` | `saudacao`, `faq` | `packages/neurostack-core/neurostack_core/protocol.py` |
| `MEDIA_CATEGORY_MAX_BYTES` | image 5,000,000; PDF 20,000,000; audio and video 16,000,000 | `src/services/media_assets.py` |
| `_SENDABLE_MIME_TYPES`, `sendable_mime_types`, `require_sendable_media` | per provider | `src/services/media_send_body.py` |
| `MEDIA_SEND_TIMEOUT` | 60 s, connect 5 s | `src/services/media_send_body.py` |
| `media_delivery_provider_ready`, `MEDIA_DELIVERY_PROVIDERS` | Meta, Z-API ready; uazapi only with `uazapi_webhook_auth_enforced` on and `uazapi_egress_enabled` open; Evolution never | `src/services/api_credentials.py` |
| `agent_media_send_enabled` | AppConfig, fail-closed | `src/services/egress_policy.py` |
| `uazapi_webhook_auth_enforced` | AppConfig; off, absent or unreadable is shadow (no authentication) | `src/services/egress_policy.py`, `src/api/routes/webhook.py` |
| `MEDIA_CANDIDATES_MAX` | 5 | `src/services/media_selection.py` |
| `MEDIA_RECENTLY_SENT_NAMES_MAX` | 5 | `src/services/media_selection.py` |
| `parse_media_marker` | `[[MIDIA:n]]`, index of at most 6 digits | `src/services/media_marker.py` |
| `MEDIA_DELIVERY_REPEAT_WINDOW` | 24 h | `src/repositories/media_deliveries.py` |
| `AGENT_MEDIA_SENDS_PER_PROCESS` | 1 | `src/api/middleware/media_limits.py` |
| `AGENT_MEDIA_PERMIT_WAIT_SECONDS` | 60 | `src/services/reply_media.py` |
| `REREAD_PREFIX_BYTES` | 301 | `src/services/reply_media.py` |
| `REPLY_MEDIA_SEND_TIMEOUT_SECONDS` | Meta 250, others 125 | `src/services/reply_media.py` |
| `REPLY_MEDIA_NOTICE_TIMEOUT_SECONDS` | 35 | `src/services/reply_media.py` |
| `REPLY_MEDIA_RECORD_TIMEOUT_SECONDS` | 30 (`REPLY_RECORD_TIMEOUT_SECONDS`) | `src/services/reply_media.py`, `src/services/credit_metering.py` |
| `REPLY_MEDIA_MAX_SECONDS` | Meta 375, others 250 | `src/services/reply_media.py` |
| `REPLY_MEDIA_NOTICES`, `NOTICE_OUTCOMES` | PT and EN notice | `src/services/reply_media.py` |
| `REPLY_MEDIA_DB_SECONDS` | 150 | `src/services/reply_media_runner.py` |
| `REPLY_MEDIA_OUTER_SECONDS` | Meta 525, others 400 | `src/services/reply_media_runner.py` |
| `neurostack_agent_media_selection`, `neurostack_agent_media_delivery` | closed vocabularies | `src/observability/metric_dimensions.py`, `src/observability/metrics.py` |
| `GET /v1/media-assets/bindable-intents` | `intents`, `topics`, `implicit_intents`, `delivery_enabled`, `max_bytes_by_category`, `sendable_mime_types` | `src/api/routes/media_assets.py` |
