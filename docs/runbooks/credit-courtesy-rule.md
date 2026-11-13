# Credit courtesy rule runbook

> **Nothing in this runbook authorizes a deployment, a one-off task or an
> infra PR.** Each live step (L2a, L1, L2b, L3, a rollback) needs the product
> owner's own "vai" at the moment of that step.

State on 2026-11-13 (wish `debito-cortesia-automatico`): production serves
`prod-fe800aec` with the database at `047`; the courtesy rule is neither
merged nor deployed, and no command of this runbook has run in production.
This release has **no migration** (the database stays at `047`).

The courtesy rule charges, for real and with no operator action, every
builder that ever received a courtesy lot, from its courtesy only, while the
global L10 enforcement (`credit_enforcement_at`) stays unset. The contract
is in [`credits.md`](../billing/credits.md), section "Courtesy rule"; the
operator mark is in [`credit-charge-mark.md`](./credit-charge-mark.md).

## The rule

- **Who is under it:** a builder with no global enforcement, no operator
  mark, that ever received a `courtesy` lot. Precedence is global > mark >
  courtesy (`CreditRepository.effective_enforcement`,
  `src/repositories/credit.py`; test
  `test_global_then_mark_then_courtesy` in
  `tests/integration/test_credit_courtesy_rule_postgres.py`). The reader is
  gated on revision `046`: below it the rule reads nothing
  (`test_below_046_the_rule_reads_nothing`).
- **The instant** is the first courtesy grant's `occurred_at`, stable across
  later grants (`CreditRepository.courtesy_rule`;
  `test_the_rule_charges_blocks_and_keeps_the_first_grant_instant`).
- **Paid plan in force** (`CreditRepository.paid_plan_in_force`): a
  `monthly` or `topup` lot whose `expires_at` is in the future (any balance,
  any quarantine; `test_a_topup_with_nothing_left_protects_until_it_expires`);
  or an ACTIVE ASAAS subscription inside the grace
  (`credit_binding_grace_days`, default 5, `src/config/settings.py`) of the
  `cycle_end` of its last active binding
  (`test_an_active_subscription_in_its_grace_protects_under_quarantine`); or
  an ACTIVE subscription whose current São Paulo cycle is paid
  (settled/restored, not reversed) with no binding yet
  (`test_a_paid_current_cycle_without_binding_protects`). An inactive or
  cancelled subscription gets no grace
  (`test_an_inactive_or_cancelled_subscription_has_no_grace`).

## The states

| State | When | Effect |
|---|---|---|
| `charged` | spendable courtesy > 0 | enforced path; debits courtesy only |
| `blocked` | 0 spendable courtesy, no paid plan in force | everything below under "Block" |
| `protected` | 0 spendable courtesy, a paid plan in force | `origin` `none`: observational, as today |

(`CreditRepository.effective_enforcement`; `builder_state` in
`src/ops/credit_courtesy_rule.py`; test
`test_the_rule_charges_blocks_and_keeps_the_first_grant_instant`.)

### What is debited

- Only `courtesy` lots, earliest expiry first; what courtesy cannot pay is
  forgiven: never debt, never settlement of old debt; `monthly` and `topup`
  lots are never touched
  (`test_the_captured_rule_debits_only_courtesy_fefo_under_quarantine`,
  `test_the_rule_never_settles_old_debt_with_a_started_renewal`,
  `test_forgiven_rule_traces_are_never_partial_debits`).
- Usage: a deliverable decision (a reply or a handoff, 1 credit each); a turn
  with no reply records `trace_no_response`, which costs zero (module
  docstring of `src/services/credit_metering.py`). Storage (RAG and media,
  per window): the window holding the first grant opens free, later windows
  charge courtesy (`test_the_first_grant_window_is_free_and_later_ones_charge_courtesy_only`).
  Egress above the included amount
  (`test_egress_above_the_included_amount_is_paid_by_courtesy_only`).
- A new grant first pays old open debt, so a builder with old debt can stay
  blocked after a grant
  (`test_a_grant_or_a_paid_plan_unblocks_and_old_debt_is_settled_first`).
  A removal is never usage (`test_a_removal_is_never_usage_for_the_courtesy_thresholds`).

### Block

Immediate: no 72 h grace, no lifecycle episode
(`courtesy_block` in `src/services/credit_lifecycle.py`;
`test_no_episode_and_thresholds_on_the_courtesy_basis`).

- `POST /v1/events` answers 402 `credit_suspended` (`src/api/routes/events.py`,
  the `credit_suspended` precheck branch);
- the Z-API, Meta, Uazapi and Evolution webhooks answer 200
  `{"ok": true, "skipped": "credit_suspended"}` with no session, no reply,
  no LGPD notice and no LLM call. A patient audio is never downloaded or
  transcribed (no STT fallback), and an image is never described
  (`rule_blocked` in `src/api/routes/webhook.py`; module docstring of
  `src/services/credit_metering.py`;
  `test_a_blocked_rule_builder_zapi_audio_sends_nothing_and_runs_no_stt`
  and `test_a_blocked_rule_builder_uazapi_turn_sends_nothing` in
  `tests/api/test_credit_suspension_webhooks.py`;
  `test_a_blocked_courtesy_builder_is_suspended_with_nothing_written` in
  `tests/integration/test_credit_effective_enforcement_postgres.py`);
- on Z-API the message is not marked read (owner decision 16): it stays
  unread on the clinic's phone, and the patient does not see it read (the
  comment above "mark message as read" in `src/api/routes/webhook.py`);
- a paused session, or a tenant with the AI off, answers 202
  `session_paused`/`bot_paused` as before. A paused session keeps its
  text-only capture for the clinic team, never an audio transcription
  (`test_a_blocked_rule_builder_with_a_paused_session_still_captures_the_text`,
  `test_a_blocked_rule_builder_with_a_paused_uazapi_session_keeps_the_text`,
  `test_a_blocked_rule_builder_with_a_paused_evolution_session_keeps_the_text`
  in `tests/api/test_credit_suspension_webhooks.py`;
  `test_a_blocked_uazapi_builder_writes_nothing_but_the_paused_capture`,
  `test_a_blocked_evolution_builder_writes_nothing_but_the_paused_capture`);
- agent outbound media does not go out. When the reply that spent the last
  courtesy carried a file, the reply is delivered and the file is refused
  (`credit_refused`). The fixed notice "Não consegui enviar o arquivo
  agora. A equipe da clínica pode te enviar por aqui." then goes to the
  patient at no cost (owner decision 18; `REPLY_MEDIA_NOTICES` in
  `src/services/reply_media.py`;
  `test_a_rule_blocked_builder_s_file_never_goes_out_on_postgres` in
  `tests/integration/test_reply_media_postgres.py` asserts
  `ReplyMediaResult("credit_refused", True)`, the second field being
  `notice_sent`);
- RAG and media uploads answer 402 `credit_suspended` before the body is
  read or anything is written, by API key and by the panel. RAG search and
  media download by API key answer 402 `credit_suspended`. The panel's reads
  (search, lists, download) and `/v1/credits/export` stay open
  (`_refuse_blocked` in `src/api/routes/knowledge.py` and
  `src/api/routes/media_assets.py`;
  `test_charged_then_blocked_then_unblocked_over_http`;
  `test_a_rule_blocked_builder_is_402_for_api_keys_and_open_for_the_panel`
  in `tests/api/test_credit_suspension_rag_media.py`);
- `/v1/credits/state` answers `state: "suspended"` with
  `suspension_due_at`, `deletion_due_at` and `export_deadline_at` null
  (`DEADLINES` in `test_charged_then_blocked_then_unblocked_over_http`);
- from the next message on, the patient receives nothing. Unblock is
  automatic: a paid plan in force or a new grant with balance
  (`test_charged_then_blocked_then_unblocked_over_http` in
  `tests/integration/test_credit_courtesy_surfaces_postgres.py`).
- the panel simulator is not blocked: a simulator turn (`POST /v1/events`
  with a panel JWT, origin `simulator`, no `Idempotency-Key` needed) is
  answered 200 on the free path: no claim, no credit effect, no ledger row,
  no suspension, and one `credit_observational_effects` row with origin
  `simulator` and `would_debit_mc` 0 (under the global the observational
  writer drops that row). Only origin `production` is enforced on
  `/v1/events`: `capture_events_enforcement`
  (`src/services/credit_metering.py`) returns before it reads the
  builder's mode for any other origin, and `_claim_key` claims only for
  `production`.
  `test_the_panel_simulator_is_served_free_for_a_rule_blocked_builder`
  (`tests/api/test_credit_suspension_events.py`) pins it for a rule-blocked
  builder, and `test_the_panel_jwt_is_served_without_a_key_and_never_charged`
  (`tests/api/test_events_credit_enforced.py`) under the global. The
  simulator was left out of the rule on purpose (design, Scope OUT).

### Channels

- `/v1/events` by API key (origin `production`): under the rule a call with
  no `Idempotency-Key` answers 400 `idempotency_key_required`
  (`IDEMPOTENCY_KEY_REQUIRED`, `src/services/credit_metering.py`).
- Z-API and Meta; Uazapi and Evolution **only under the rule** (under a mark
  or the global they stay observational:
  `test_under_a_mark_the_uazapi_turn_stays_observational_on_postgres`,
  `test_under_a_mark_the_evolution_turn_stays_observational_on_postgres`).
- A turn whose principal is not authenticated (origin `unknown`: an
  unverified Uazapi callback, every Evolution callback) is charged only
  under the rule; under the global or a mark it keeps the free path. Every
  charged decision's `decision_digest` carries the turn's real origin
  (module docstring of `src/services/credit_metering.py`, "origin
  `unknown`"; `precheck_webhook`).
- A redelivery of the same provider message id neither debits nor replies
  twice (`test_a_redelivery_across_a_mode_change_neither_debits_nor_replies_twice`,
  both Uazapi and Evolution suites).
- With `uazapi_egress_enabled` closed a Uazapi turn is not charged
  (`test_with_the_egress_closed_a_charged_uazapi_turn_is_not_charged`).

### What the builder sees

- `/billing`: charged shows no "Modo observacional" notice; blocked shows
  "Serviço suspenso por falta de créditos."; charged in quarantine with the
  top-up hidden shows "Um pagamento está em verificação; o consumo da
  cortesia não é afetado."
  (`frontend/artifacts/neurostack/src/components/billing/credits-and-topups.tsx`).
  The top-up stays blocked in quarantine
  (`frontend/artifacts/neurostack/src/pages/billing-view.test.ts`, "is
  blocked for a builder charged under the rule in quarantine (active +
  quarantine)"); `/v1/credits/state` keeps `quarantine`, never
  `reconciliation`
  (`test_a_charged_builder_in_quarantine_is_served_from_courtesy_only`,
  `test_an_unusable_coverage_is_never_reconciliation_under_the_rule`).
- Thresholds 50/90/100 % on the courtesy basis. They go to the record, the
  metric `neurostack_credit_balance_thresholds_total`
  (`CREDIT_BALANCE_THRESHOLDS_TOTAL`, `src/services/credit_lifecycle.py`),
  `/billing` and `/v1/credits/thresholds`
  (`test_the_thresholds_follow_real_consumption_on_the_courtesy_basis`).
- Master-admin: "Cortesia: cobrando" / "Cortesia: bloqueado" from
  `courtesy_rule` (`frontend/artifacts/neurostack/src/pages/master-admin.tsx`).

## The pre-deploy list (L1)

```
python -m src.ops.credit_courtesy_rule status
```

One subcommand only. It reads in one `REPEATABLE READ READ ONLY`
transaction, always rolled back, and prints one JSON document on stdout and
the same plus `event: credit_courtesy_rule_ops` on stderr (module docstring
of `src/ops/credit_courtesy_rule.py`; `tests/ops/test_credit_courtesy_rule.py`).

| Verdict | Exit | Reasons |
|---|---|---|
| `ok` | 0 | |
| `refused` | 20 | `courtesy_rule_gate_closed` (database below `046`) |
| `switch_off` | 21 | `credit_switch_off` |
| `unconfigured` | 30 | `usage`, `settings_invalid` |
| `error` | 50 | `import_failed`, `unexpected` |

(`EXIT_CODES`; `test_the_exit_codes_are_the_published_literals`.)

Per builder that ever received courtesy (`_builder_entry`):

- `state`: `charged`, `blocked`, `protected`, `global` or `mark`; also
  `not_under_rule` (a courtesy lot without a grant entry, no
  `first_grant_at`) and `rule_unread` (the repository read nothing; cannot
  happen once the `046` gate is open, printed so a surprise is not hidden)
  (`STATES` and `builder_state` in `src/ops/credit_courtesy_rule.py`;
  `test_the_state_is_the_request_paths_verdict`);
- `courtesy`: `spendable_mc`, `first_grant_at`, `nearest_expiry_at`,
  `last_expiry_at` (the last: when a charged builder is blocked by expiry if
  nothing changes; `test_the_expiries_are_printed_as_read`);
- `paid_plan`: `in_force`, `paid_lot_expires_at`, `in_grace`,
  `unbound_paid_cycle` (`test_the_paid_plan_is_printed_as_read`);
- `open_debt.open_debt_mc`;
- `reservation`: the estimated storage charge of the first window after the
  deploy, an estimate that says so in `approximation`
  (`test_the_first_window_after_the_deploy`,
  `test_the_reservation_uses_the_inventory_rules`). It is `null` when the
  builder has no enforcement instant at all, and
  `{"applies": ..., "error": "inventory_unreadable"}` when the inventory
  cannot be read (`_reservation` in `src/ops/credit_courtesy_rule.py`);
- `clinics`: tenants per channel (`test_the_clinics_are_counted_per_channel`);
- `events_without_idempotency_key`: `/v1/events` traffic of the last 7 days
  with no key, a **floor** (errored requests and dropped observational
  writes are not counted; one known excess) explained in
  `events_window.approximation`
  (`test_the_count_says_it_is_a_floor_with_one_known_excess`);
- `subscription`, `binding_lag`, `coverage` (`usable`,
  `reconciliation_failure`);
- incomplete send credentials (owner decision 20), in three places:
  - per tenant, `clinics.tenants[].credentials_incomplete`: the list of the
    channels the tenant is on whose SEND credentials are incomplete;
  - per builder, `clinics.credentials_incomplete`:
    `{count, tenant_ids, by_channel}` (`by_channel` gives `count` and
    `tenant_ids` per channel);
  - in the totals, `summary.under_rule.clinics_with_incomplete_credentials`
    and `summary.under_rule.incomplete_credentials_per_channel`, and the
    same two keys under `summary.all_states`

  (`clinics_document` and `_exposure` in `src/ops/credit_courtesy_rule.py`;
  `test_incomplete_credentials_are_listed_only_on_the_tenants_channels`,
  `test_the_incomplete_credentials_are_counted_per_channel`). "Complete" is
  what `CreditReplySender.prepare` (`src/api/routes/webhook.py`) requires
  before it sends (`SEND_CREDENTIALS`, module docstring): `zapi` =
  `zapi_token` + `zapi_instance_id`; `uazapi` = `uazapi_api_url` +
  `uazapi_token`; `evolution` = `evolution_api_url` +
  `evolution_instance_name` + `evolution_api_key`; `meta` =
  `meta_phone_number_id` + `meta_permanent_token`. Only whether each key
  is non-empty is judged (after trimming whitespace, except Evolution,
  whose values count as stored), inside the database; no value is printed
  (`_send_ready_sql`). Under the rule such a clinic spends courtesy on
  every turn while nothing is delivered (the send fails `no_credentials`
  and the decision stays charged) until the builder is blocked. Before the
  go/no-go, report every non-empty list to the owner; a clinic's
  credentials are changed only by the clinic or on the owner's own
  go-ahead.

  Complete means only that the charged sender will ATTEMPT the send
  (`CreditReplySender.prepare` finds every key it requires); it does not
  prove the reply is delivered. Not judged: a wrong or expired token; a
  Z-API account with the security token turned on and no
  `zapi_client_token` stored (that key is optional for the sender, which
  sends the `Client-Token` header only when it is set:
  `src/services/zapi_service.py`); the process-level
  `uazapi_egress_enabled` flag. A clinic listed only because its
  `whatsapp_provider` names a channel, with no inbound route for it
  (`inbound_routes`, printed next to it in `clinics.tenants[]`), receives
  no turn on that channel and is never charged there, so its entry
  over-reports (module docstring and `TenantChannels.channels` in
  `src/ops/credit_courtesy_rule.py`).

`summary` (`summary_document`): `states`, `open_debt_mc`, `under_rule`
(charged + blocked) and `all_states`
(`test_the_summary_totals_the_rule_apart_from_every_state`).

It prints no credential, URL, phone, e-mail or tenant name: tenants by id,
builders by id and display name, and only a known provider name
(`test_only_a_known_provider_name_is_printed`).

**Size.** Measured on 2026-11-13 with the module's own formatters
(`_builder_entry`'s documents, `summary_document`, `_dumps`) on synthetic
values: one builder entry with one clinic is about 2.3 KB, each extra
clinic adds about 0.3 KB, and the whole document is about 3.7 KB for one
builder and about 12.9 KB for five; the stderr line is the same plus the
log fields. The CloudWatch Logs event limit is not the bound (check the
current PutLogEvents quota; it is not recorded in this repository): the
Fargate log shim cuts a line longer than 16 KiB and `awslogs` sends each
piece as its own event (as
[`uazapi-webhook-auth.md`](./uazapi-webhook-auth.md) notes; not measured
here). Above about six builders the line is split: rejoin the pieces in
order before parsing.

### The binding lag (`binding_lag`)

`binding_lag.lagging: true` means an active ASAAS subscription whose current
cycle is paid has no binding yet: the billing fact consumer is behind
(`_binding_lag`, `src/ops/credit_courtesy_rule.py`). While it lasts that
cycle is a paid plan in force
(`test_a_paid_current_cycle_without_binding_protects`). `seconds` says how
long; `since_column` names the timestamp used.

What to do: the lag decides a state only for a builder whose courtesy is
spent. That builder is `protected` (observational) instead of `blocked`
while the lag lasts; a builder with courtesy left is `charged` whatever the
lag (the rule reads the paid plan only once the courtesy is spent: module
docstring of `src/ops/credit_courtesy_rule.py`, `paid_plan`). A long lag
means the credit fact consumer is not applying billing facts: follow
[`asaas-billing-reconciliation.md`](./asaas-billing-reconciliation.md),
"When the consumer is not ready". Report every `lagging: true` to the owner
before the go/no-go; do not deploy on a list the owner has not read.

## Deploy (L2a, L1, L2b, L3)

**L2a, image and task definition.** Workflow "Staging image build" on
`main` with `expected_head=047` (the push/smoke step is flaky: retry once);
copy the image by digest to ECR `neurostack-prod`; a PR in
`neurostackinfra/neurostack-infra-terraform` changing `image_tag` in
`envs/prod/app/main.tf`; human merge; `terraform-apply-prod.yml` (layer
`app`). The new task definition exists; the service still runs the old one.

**L1, the list.** A one-off ECS task of the **new** task definition, between
the apply and the `update-service`: cluster `neurostack-prod`, network
configuration copied from the service `neurostack-prod-app`, a `startedBy`
label (for example `courtesy-rule-status`), the command override above,
`DEBUG=false`, built as
[`credit-charge-mark.md`](./credit-charge-mark.md) "Running a step" builds
its tasks, with three differences: `TASK_DEFINITION` is the revision the
apply just registered, NOT the service's current one (that one still runs
`prod-fe800aec`); the module in the `run_step` guard is
`src.ops.credit_courtesy_rule` with step `status`; and `--started-by` is
the label above. Logs in `/ecs/neurostack-prod/app`. The owner reads who
becomes charged, blocked and protected, and gives go or no-go. **No-go** = no
`update-service`, and an infra PR putting `image_tag` back to
`prod-fe800aec`.

**L2b, serve.** `update-service`; wait for the deployment to be `COMPLETED`;
healthy targets, `/health`, the credit routes (`/v1/credits/state`,
`/v1/credits/summary`, `/v1/admin/builders/subscriptions`) answer 401
without a token, the credit switch effective, no new error in the log.
Then L3, live QA.

**L3, live QA.** Its preconditions and steps are the wish's QA L3
(`.genie/wishes/debito-cortesia-automatico/WISH.md`, "QA Criteria"):
record before it the `uazapi_egress_enabled` state (with its AppConfig
version), the webhook authentication state, and that the test clinic's LLM
credentials and `agent_presentation` are filled. Each step needs the
owner's own "vai".

## Rollback

Forward fix: an infra PR putting `image_tag` back to `prod-fe800aec`, human
merge, `terraform-apply-prod.yml`, `update-service`. Expected latency: the
PR review and apply, then one service rollout; the rule stays on until every
task runs the old image. **Never an Alembic downgrade** (there is no
migration to undo). Verify on the Terraform plan whether the apply replaces
the old task definitions (they would then become INACTIVE); if so, the
infra PR is the only way back.

- The rule turns off: the old image has no courtesy origin
  (`EnforcementOrigin = Literal["global", "mark"]` in
  `src/repositories/credit.py` at `fe800aec`).
- Debits already written stay: the ledger is append-only (trigger
  `reject_credit_history_mutation()` on `credit_ledger_entries`, migration
  `038`).
- Charged replies still `pending` in `credit_response_outbox` (any channel;
  in practice crash-window rows) are never sent by the old image. Its
  publisher runs only with the global set or a mark event recorded
  (`CreditResponsePublisher._tick`, `src/services/credit_metering.py` at
  `fe800aec`), so it is idle while production has no mark event recorded
  (DESIGN Decision 20: production has no marks; with one recorded it would
  run and could send pending Z-API/Meta rows). Its sender refuses every
  channel but `zapi`/`meta` as `payload_unusable`
  (`CreditReplySender.prepare`, `src/api/routes/webhook.py` at
  `fe800aec`). The rows are not deleted: the
  retention sweep (`sweep_expired_replies` →
  `CreditRepository.sweep_outbox_payloads` at `fe800aec`) empties their
  payload (`'{}'`) once they are older than `credit_replay_retention_hours`
  (default 24), and they stay `pending`. A row a stopping task left
  `sending` keeps its payload: the sweep skips `sending` rows, and only the
  publisher, idle here, would reclaim it. The patients of those turns get
  no reply.
- The Redis claim keeps deduplicating redeliveries.

## Risks the owner accepted

- **Forged Uazapi messages.** The prod webhook auth is in shadow (flag
  `uazapi_webhook_auth_enforced` absent, as read 2026-11-12; record its
  state again before L3): a forged message spends courtesy and, by the
  immediate block, can mute the agent.
  Mitigation, ready and separate: `uazapi_webhook_auth_enforced`
  ([`uazapi-webhook-auth.md`](./uazapi-webhook-auth.md)).
- **Forged Evolution messages**, with no mitigation: Evolution has no origin
  verification (every Evolution turn is origin `unknown`).
- **Large storage**: a large window or upload can consume the courtesy and
  block at once.
- **All at once**: the deploy turns the rule on for every courtesy builder
  (hence L1).
- `/v1/events` without `Idempotency-Key` starts answering 400.

## Accepted residuals

- (a) A blocked builder's session whose pause just expired stays paused
  until the builder is unblocked: the lazy unpause is rolled back with the
  blocked turn, so the row keeps `is_paused = true` with a past
  `paused_until` until the first turn after the unblock (`_check_pause_state`
  and the comment above `capture_uazapi` in `src/api/routes/webhook.py`;
  `test_a_blocked_uazapi_builder_writes_nothing_but_the_paused_capture`).
- (b) For a tenant already seen under the rule whose mode read then fails,
  the webhook answers 503 `credit_enforcement_unavailable` at once, for that
  tenant only (the per-tenant D1,
  `enforcement_rule_unreadable_after_courtesy`). This happens before a
  paused session's text capture. The message was already claimed in
  Redis, so the provider's retry answers `duplicate_message` and the
  message is lost, for the clinic team too. This holds on Z-API and
  Evolution for every message, and on Uazapi for a text. Only on Uazapi
  are a patient audio and an image claimed later, so they are retried.
  (The comments at the credit capture of each webhook in
  `src/api/routes/webhook.py`, and `_claim_deferred_message` for Uazapi;
  `test_a_tenant_seen_under_the_rule_whose_read_fails_is_503_at_once_on_zapi`,
  `test_a_tenant_seen_under_the_rule_whose_rule_fails_is_503_on_uazapi`,
  `test_a_tenant_seen_under_the_rule_whose_rule_fails_is_503_on_evolution`
  in `tests/api/test_credit_suspension_webhooks.py`.)
- (c) If `uazapi_egress_enabled` closes during the LLM call, the reply is
  charged and not delivered (the Z-API/Meta lost-reply rule):
  `CreditReplySender.send` raises
  `CreditReplyUndeliverable("uazapi_egress_disabled")` and
  `_deliver_claimed` marks the row `failed`
  (`test_a_charged_uazapi_reply_refused_by_a_closing_egress_is_main_s_answer`).
- (d) Across a mode change from charged to free, dedup of a redelivery
  depends on the Redis claim only
  (`test_charged_then_free_with_the_replay_store_down_is_the_accepted_residual`).
- (e) The media delivery does its own mode read, separate from the turn's
  (`_courtesy_mode`, `src/services/reply_media.py`).
- (f) Outside the rule, Z-API, Uazapi and Evolution turns do one extra mode
  read (`test_zapi_reads_the_mode_once_and_only_where_it_did` in
  `tests/api/test_credit_suspension_webhooks.py`,
  `test_a_uazapi_turn_outside_the_rule_is_main_s_plus_one_mode_read`,
  `test_an_evolution_turn_outside_the_rule_is_main_s_plus_one_mode_read` in
  `tests/integration/test_credit_schema_dominance_postgres.py`), and
  `/v1/credits/balance` and `/thresholds` do the precedence read for
  builders holding courtesy lots.
- (g) The L10 cutoff report shows rule-charged traffic as
  `message_without_observation`
  ([`credit-enforcement-arming.md`](./credit-enforcement-arming.md)).
