"""credit lifecycle, legal hold, purge fence and operational roles

Revision ID: 041
Revises: 040

neurostack-credit-system Group 5.  Additive only: no existing column,
constraint, trigger, type or row changes; ``media_assets`` and
``knowledge_documents`` keep their schema (a tombstone hides an object by a
row here, never by a new column there).

Lifecycle (exhaustion episodes and their transitions, thresholds):

* ``credit_lifecycle_episodes`` -- one exhaustion episode of a builder,
  opened only by an effect after ``credit_enforcement_at``.  At most ONE open
  episode per builder (partial unique index), so concurrent writers never
  open a second one.  The guard trigger keeps ``exhausted_at`` and
  ``deletion_due_at`` immutable, lets ``grace_ends_at`` and
  ``suspension_due_at`` only DECREASE, makes ``observed_inactive_at`` and
  ``next_binding_at`` first-write-wins, and freezes the row once
  ``reactivated_at`` is set; DELETE is refused.
* ``credit_lifecycle_transitions`` -- append-only, ``UNIQUE(builder_id,
  effect_id)``: the worker and the request path write the same
  deterministic effect id, so a transition is recorded exactly once.
* ``credit_threshold_alerts`` -- append-only, ``UNIQUE(builder_id, arm_key,
  threshold)``: one 50/90/100 alert per episode generation and window.

Legal hold and purge (append-only by ``reject_credit_history_mutation``):

* ``credit_hold_generations`` -- the ``(builder_id, scope)`` row every hold
  event locks; its guard lets ``generation`` only grow by exactly one;
* ``credit_hold_events`` -- the exact stream ``hold_requested |
  hold_activated | hold_released``; one event per intent and type, each
  consuming its own single-use authorization;
* ``credit_purge_fences`` -- ``UNIQUE(builder_id, scope, generation)``: the
  CAS of a purge, never a second fence for one generation;
* ``credit_retention_tombstones``, ``credit_purge_jobs``
  (``prepared|storage_applied|purge_fenced``), ``credit_purge_results``
  (``deleted|held|failed|blocked``) and ``credit_purge_receipts``.

Operational authority (credit-only roles, never the ASAAS ones):

* ``credit_operational_role_assignments`` -- append-only grants and
  revocations of ``production_data_lifecycle_owner``, ``legal_hold_officer``
  and ``legal_hold_release_officer``; a grant is never for its own actor;
* ``credit_operational_authorizations`` -- backend-signed (HMAC-SHA256 with
  ``credit_authorization_signing_key``) authorizations for one purpose,
  environment, builder, scope, resource and fence generation, issued to a
  user other than the issuer;
* ``credit_operational_authorization_receipts`` -- the single use
  (primary key = the authorization).

Downgrade: refused (SQLSTATE 55000, nothing changed) while any table of this
revision has a row or ``alembic_version`` is not ``041``.
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "041"
down_revision: Union[str, None] = "040"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"
SHA256 = "'^[0-9a-f]{64}$'"
SCOPE_CHECK = (
    "(scope = 'builder' OR scope ~ "
    "'^tenant:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')"
)
ROLES = (
    "'production_data_lifecycle_owner', 'legal_hold_officer', "
    "'legal_hold_release_officer'"
)

CREATED_TABLES = (
    "credit_lifecycle_episodes",
    "credit_lifecycle_transitions",
    "credit_threshold_alerts",
    "credit_operational_role_assignments",
    "credit_operational_authorizations",
    "credit_operational_authorization_receipts",
    "credit_hold_generations",
    "credit_hold_events",
    "credit_purge_fences",
    "credit_retention_tombstones",
    "credit_purge_jobs",
    "credit_purge_results",
    "credit_purge_receipts",
)
# History rows: every UPDATE or DELETE is refused.
APPEND_ONLY_TABLES = (
    "credit_lifecycle_transitions",
    "credit_threshold_alerts",
    "credit_operational_role_assignments",
    "credit_operational_authorizations",
    "credit_operational_authorization_receipts",
    "credit_hold_events",
    "credit_purge_fences",
    "credit_retention_tombstones",
    "credit_purge_jobs",
    "credit_purge_results",
    "credit_purge_receipts",
)
CREATED_FUNCTIONS = (
    "guard_credit_lifecycle_episode()",
    "guard_credit_hold_generation()",
)


def _execute(statements: tuple[str, ...]) -> None:
    for statement in statements:
        ddl = sa.text(statement)
        if ddl._bindparams:
            raise ValueError("raw migration DDL contains bind parameters")
        op.execute(ddl)


FUNCTIONS = (
    """
    CREATE FUNCTION guard_credit_lifecycle_episode()
    RETURNS trigger LANGUAGE plpgsql AS $function$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'credit_lifecycle_episodes is append-only'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF OLD.reactivated_at IS NOT NULL THEN
            RAISE EXCEPTION 'a closed credit lifecycle episode is frozen'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.builder_id IS DISTINCT FROM OLD.builder_id
           OR NEW.account_id IS DISTINCT FROM OLD.account_id
           OR NEW.opened_effect_id IS DISTINCT FROM OLD.opened_effect_id
           OR NEW.exhausted_at IS DISTINCT FROM OLD.exhausted_at
           OR NEW.cycle_end_at IS DISTINCT FROM OLD.cycle_end_at
           OR NEW.deletion_due_at IS DISTINCT FROM OLD.deletion_due_at
           OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'credit lifecycle episode identity is immutable'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF OLD.observed_inactive_at IS NOT NULL
           AND NEW.observed_inactive_at IS DISTINCT FROM OLD.observed_inactive_at THEN
            RAISE EXCEPTION 'observed_inactive_at is first-write-wins'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF OLD.next_binding_at IS NOT NULL
           AND NEW.next_binding_at IS DISTINCT FROM OLD.next_binding_at THEN
            RAISE EXCEPTION 'next_binding_at is first-write-wins'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF (OLD.grace_ends_at IS NULL AND NEW.grace_ends_at IS NOT NULL)
           OR (OLD.grace_ends_at IS NOT NULL AND (
                NEW.grace_ends_at IS NULL OR NEW.grace_ends_at > OLD.grace_ends_at)) THEN
            RAISE EXCEPTION 'the binding grace of an episode can only shorten'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.suspension_due_at > OLD.suspension_due_at THEN
            RAISE EXCEPTION 'the suspension deadline of an episode never extends'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $function$
    """,
    """
    CREATE FUNCTION guard_credit_hold_generation()
    RETURNS trigger LANGUAGE plpgsql AS $function$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'credit_hold_generations rows are never deleted'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.builder_id IS DISTINCT FROM OLD.builder_id
           OR NEW.scope IS DISTINCT FROM OLD.scope
           OR NEW.generation IS DISTINCT FROM OLD.generation + 1 THEN
            RAISE EXCEPTION 'a hold generation only grows by exactly one'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $function$
    """,
)

TABLES = (
    """
    CREATE TABLE credit_lifecycle_episodes (
        id UUID PRIMARY KEY,
        builder_id UUID NOT NULL,
        account_id UUID NOT NULL,
        opened_effect_id VARCHAR(512) NOT NULL,
        exhausted_at TIMESTAMPTZ NOT NULL,
        cycle_end_at TIMESTAMPTZ NULL,
        grace_ends_at TIMESTAMPTZ NULL,
        observed_inactive_at TIMESTAMPTZ NULL,
        next_binding_at TIMESTAMPTZ NULL,
        suspension_due_at TIMESTAMPTZ NOT NULL,
        deletion_due_at TIMESTAMPTZ NOT NULL,
        reactivated_at TIMESTAMPTZ NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT fk_credit_lifecycle_episodes_account
            FOREIGN KEY (builder_id, account_id)
            REFERENCES credit_accounts (builder_id, id) ON DELETE RESTRICT,
        CONSTRAINT ck_credit_lifecycle_episodes_effect CHECK (length(opened_effect_id) > 0),
        CONSTRAINT ck_credit_lifecycle_episodes_deadlines CHECK (
            suspension_due_at >= exhausted_at + interval '72 hours'
            AND deletion_due_at > exhausted_at),
        CONSTRAINT ck_credit_lifecycle_episodes_grace CHECK (
            grace_ends_at IS NULL OR cycle_end_at IS NOT NULL),
        CONSTRAINT ck_credit_lifecycle_episodes_reactivated CHECK (
            reactivated_at IS NULL OR reactivated_at >= exhausted_at)
    )
    """,
    "CREATE UNIQUE INDEX uq_credit_lifecycle_episodes_open "
    "ON credit_lifecycle_episodes (builder_id) WHERE reactivated_at IS NULL",
    "CREATE INDEX ix_credit_lifecycle_episodes_due "
    "ON credit_lifecycle_episodes (suspension_due_at) WHERE reactivated_at IS NULL",
    """
    CREATE TABLE credit_lifecycle_transitions (
        id UUID PRIMARY KEY,
        builder_id UUID NOT NULL,
        episode_id UUID NOT NULL
            REFERENCES credit_lifecycle_episodes (id) ON DELETE RESTRICT,
        transition VARCHAR(30) NOT NULL,
        effect_id VARCHAR(512) NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL,
        source VARCHAR(20) NOT NULL,
        detail JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_lifecycle_transitions_transition CHECK (transition IN (
            'opened', 'grace_shortened', 'suspended', 'deletion_due', 'reactivated')),
        CONSTRAINT ck_credit_lifecycle_transitions_source CHECK (
            source IN ('effect', 'worker')),
        CONSTRAINT ck_credit_lifecycle_transitions_effect CHECK (length(effect_id) > 0),
        CONSTRAINT uq_credit_lifecycle_transitions_effect UNIQUE (builder_id, effect_id)
    )
    """,
    "CREATE INDEX ix_credit_lifecycle_transitions_episode "
    "ON credit_lifecycle_transitions (episode_id, occurred_at)",
    """
    CREATE TABLE credit_threshold_alerts (
        id UUID PRIMARY KEY,
        builder_id UUID NOT NULL,
        arm_key VARCHAR(120) NOT NULL,
        threshold SMALLINT NOT NULL,
        capacity_mc BIGINT NOT NULL,
        available_mc BIGINT NOT NULL,
        effect_id VARCHAR(512) NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_threshold_alerts_threshold CHECK (threshold IN (50, 90, 100)),
        CONSTRAINT ck_credit_threshold_alerts_amounts CHECK (
            capacity_mc >= 0 AND available_mc >= 0),
        CONSTRAINT uq_credit_threshold_alerts_arm UNIQUE (builder_id, arm_key, threshold)
    )
    """,
    f"""
    CREATE TABLE credit_operational_role_assignments (
        id UUID PRIMARY KEY,
        role VARCHAR(40) NOT NULL,
        user_id UUID NOT NULL,
        environment VARCHAR(20) NOT NULL,
        action VARCHAR(10) NOT NULL,
        revokes_assignment_id UUID NULL
            REFERENCES credit_operational_role_assignments (id) ON DELETE RESTRICT,
        actor_user_id UUID NOT NULL,
        reason VARCHAR(500) NOT NULL,
        expires_at TIMESTAMPTZ NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_operational_role_assignments_role CHECK (role IN ({ROLES})),
        CONSTRAINT ck_credit_operational_role_assignments_environment CHECK (
            environment IN ('development', 'staging', 'production', 'test')),
        CONSTRAINT ck_credit_operational_role_assignments_action CHECK (
            (action = 'granted' AND revokes_assignment_id IS NULL
             AND actor_user_id <> user_id)
            OR (action = 'revoked' AND revokes_assignment_id IS NOT NULL
                AND expires_at IS NULL)),
        CONSTRAINT ck_credit_operational_role_assignments_reason CHECK (length(reason) > 0),
        CONSTRAINT uq_credit_operational_role_assignments_revocation
            UNIQUE (revokes_assignment_id)
    )
    """,
    "CREATE INDEX ix_credit_operational_role_assignments_user "
    "ON credit_operational_role_assignments (user_id, role, environment)",
    f"""
    CREATE TABLE credit_operational_authorizations (
        id UUID PRIMARY KEY,
        purpose VARCHAR(20) NOT NULL,
        environment VARCHAR(20) NOT NULL,
        builder_id UUID NOT NULL,
        scope VARCHAR(200) NOT NULL,
        resource VARCHAR(512) NULL,
        fence_generation BIGINT NULL,
        subject_user_id UUID NOT NULL,
        issued_by UUID NOT NULL,
        issued_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        key_fingerprint VARCHAR(64) NOT NULL,
        signature VARCHAR(64) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_operational_authorizations_purpose CHECK (purpose IN (
            'hold_request', 'hold_activate', 'hold_release', 'purge')),
        CONSTRAINT ck_credit_operational_authorizations_environment CHECK (
            environment IN ('development', 'staging', 'production', 'test')),
        CONSTRAINT ck_credit_operational_authorizations_scope CHECK {SCOPE_CHECK},
        CONSTRAINT ck_credit_operational_authorizations_fence CHECK (
            (purpose = 'purge' AND fence_generation IS NOT NULL AND fence_generation >= 0)
            OR (purpose <> 'purge' AND fence_generation IS NULL)),
        CONSTRAINT ck_credit_operational_authorizations_window CHECK (expires_at > issued_at),
        CONSTRAINT ck_credit_operational_authorizations_issuer CHECK (
            issued_by <> subject_user_id),
        CONSTRAINT ck_credit_operational_authorizations_digests CHECK (
            signature ~ {SHA256} AND key_fingerprint ~ {SHA256})
    )
    """,
    """
    CREATE TABLE credit_operational_authorization_receipts (
        authorization_id UUID PRIMARY KEY
            REFERENCES credit_operational_authorizations (id) ON DELETE RESTRICT,
        consumed_by UUID NOT NULL,
        consumed_for VARCHAR(40) NOT NULL,
        result_ref VARCHAR(512) NOT NULL,
        consumed_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_operational_authorization_receipts_ref CHECK (
            length(consumed_for) > 0 AND length(result_ref) > 0)
    )
    """,
    f"""
    CREATE TABLE credit_hold_generations (
        builder_id UUID NOT NULL,
        scope VARCHAR(200) NOT NULL,
        generation BIGINT NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (builder_id, scope),
        CONSTRAINT ck_credit_hold_generations_scope CHECK {SCOPE_CHECK},
        CONSTRAINT ck_credit_hold_generations_generation CHECK (generation >= 0)
    )
    """,
    f"""
    CREATE TABLE credit_hold_events (
        id UUID PRIMARY KEY,
        intent_id UUID NOT NULL,
        builder_id UUID NOT NULL,
        scope VARCHAR(200) NOT NULL,
        event_type VARCHAR(20) NOT NULL,
        reason_code VARCHAR(80) NOT NULL,
        actor_user_id UUID NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL,
        previous_event_id UUID NULL REFERENCES credit_hold_events (id) ON DELETE RESTRICT,
        generation BIGINT NOT NULL,
        authorization_id UUID NOT NULL
            REFERENCES credit_operational_authorizations (id) ON DELETE RESTRICT,
        requested_by UUID NULL,
        activated_by UUID NULL,
        justification VARCHAR(2000) NULL,
        digest VARCHAR(64) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_hold_events_type CHECK (event_type IN (
            'hold_requested', 'hold_activated', 'hold_released')),
        CONSTRAINT ck_credit_hold_events_scope CHECK {SCOPE_CHECK},
        CONSTRAINT ck_credit_hold_events_reason CHECK (length(reason_code) > 0),
        CONSTRAINT ck_credit_hold_events_generation CHECK (generation > 0),
        CONSTRAINT ck_credit_hold_events_digest CHECK (digest ~ {SHA256}),
        CONSTRAINT ck_credit_hold_events_chain CHECK (
            (event_type = 'hold_requested' AND previous_event_id IS NULL)
            OR (event_type <> 'hold_requested' AND previous_event_id IS NOT NULL)),
        CONSTRAINT ck_credit_hold_events_release CHECK (
            event_type <> 'hold_released'
            OR (requested_by IS NOT NULL AND activated_by IS NOT NULL
                AND justification IS NOT NULL AND length(justification) > 0
                AND actor_user_id <> requested_by AND actor_user_id <> activated_by)),
        CONSTRAINT uq_credit_hold_events_intent_type UNIQUE (intent_id, event_type),
        CONSTRAINT uq_credit_hold_events_authorization UNIQUE (authorization_id),
        CONSTRAINT uq_credit_hold_events_generation UNIQUE (builder_id, scope, generation)
    )
    """,
    "CREATE INDEX ix_credit_hold_events_scope "
    "ON credit_hold_events (builder_id, scope, generation)",
    f"""
    CREATE TABLE credit_purge_fences (
        id UUID PRIMARY KEY,
        builder_id UUID NOT NULL,
        scope VARCHAR(200) NOT NULL,
        generation BIGINT NOT NULL,
        operation_id UUID NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_purge_fences_scope CHECK {SCOPE_CHECK},
        CONSTRAINT ck_credit_purge_fences_generation CHECK (generation >= 0),
        CONSTRAINT uq_credit_purge_fences_generation UNIQUE (builder_id, scope, generation),
        CONSTRAINT uq_credit_purge_fences_operation UNIQUE (operation_id)
    )
    """,
    f"""
    CREATE TABLE credit_retention_tombstones (
        id UUID PRIMARY KEY,
        token UUID NOT NULL,
        operation_id UUID NOT NULL,
        builder_id UUID NOT NULL,
        tenant_id UUID NOT NULL,
        scope VARCHAR(200) NOT NULL,
        source_kind VARCHAR(16) NOT NULL,
        source_id UUID NOT NULL,
        content_sha256 VARCHAR(64) NOT NULL,
        size_bytes BIGINT NOT NULL,
        s3_object_key VARCHAR(1024) NULL,
        s3_version_id VARCHAR(512) NULL,
        s3_etag VARCHAR(512) NULL,
        qdrant_point_ids JSONB NOT NULL,
        observed_generation BIGINT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_retention_tombstones_scope CHECK {SCOPE_CHECK},
        CONSTRAINT ck_credit_retention_tombstones_source CHECK (
            source_kind IN ('media', 'knowledge')),
        CONSTRAINT ck_credit_retention_tombstones_sha CHECK (content_sha256 ~ {SHA256}),
        CONSTRAINT ck_credit_retention_tombstones_size CHECK (size_bytes >= 0),
        CONSTRAINT ck_credit_retention_tombstones_copy CHECK (
            (s3_object_key IS NULL) = (s3_version_id IS NULL)),
        CONSTRAINT ck_credit_retention_tombstones_generation CHECK (observed_generation >= 0),
        CONSTRAINT uq_credit_retention_tombstones_token UNIQUE (token),
        CONSTRAINT uq_credit_retention_tombstones_item UNIQUE (operation_id, source_kind, source_id)
    )
    """,
    "CREATE INDEX ix_credit_retention_tombstones_source "
    "ON credit_retention_tombstones (source_kind, source_id)",
    """
    CREATE TABLE credit_purge_jobs (
        id UUID PRIMARY KEY,
        token UUID NOT NULL
            REFERENCES credit_retention_tombstones (token) ON DELETE RESTRICT,
        operation_id UUID NOT NULL,
        state VARCHAR(20) NOT NULL,
        fence_id UUID NULL REFERENCES credit_purge_fences (id) ON DELETE RESTRICT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_purge_jobs_state CHECK (state IN (
            'prepared', 'storage_applied', 'purge_fenced')),
        CONSTRAINT ck_credit_purge_jobs_fence CHECK (
            (state = 'purge_fenced') = (fence_id IS NOT NULL)),
        CONSTRAINT uq_credit_purge_jobs_state UNIQUE (token, state)
    )
    """,
    "CREATE INDEX ix_credit_purge_jobs_claim ON credit_purge_jobs (state, created_at)",
    """
    CREATE TABLE credit_purge_results (
        id UUID PRIMARY KEY,
        token UUID NOT NULL
            REFERENCES credit_retention_tombstones (token) ON DELETE RESTRICT,
        operation_id UUID NOT NULL,
        attempt INTEGER NOT NULL,
        outcome VARCHAR(10) NOT NULL,
        detail VARCHAR(500) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_purge_results_outcome CHECK (outcome IN (
            'deleted', 'held', 'failed', 'blocked')),
        CONSTRAINT ck_credit_purge_results_attempt CHECK (attempt > 0),
        CONSTRAINT uq_credit_purge_results_attempt UNIQUE (token, attempt)
    )
    """,
    "CREATE UNIQUE INDEX uq_credit_purge_results_final "
    "ON credit_purge_results (token) WHERE outcome IN ('deleted', 'held', 'blocked')",
    f"""
    CREATE TABLE credit_purge_receipts (
        operation_id UUID PRIMARY KEY,
        builder_id UUID NOT NULL,
        scope VARCHAR(200) NOT NULL,
        status VARCHAR(10) NOT NULL,
        deleted_count INTEGER NOT NULL,
        held_count INTEGER NOT NULL,
        failed_count INTEGER NOT NULL,
        blocked_count INTEGER NOT NULL,
        digest VARCHAR(64) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_purge_receipts_scope CHECK {SCOPE_CHECK},
        CONSTRAINT ck_credit_purge_receipts_status CHECK (
            status IN ('completed', 'partial', 'blocked')),
        CONSTRAINT ck_credit_purge_receipts_counts CHECK (
            deleted_count >= 0 AND held_count >= 0
            AND failed_count >= 0 AND blocked_count >= 0),
        CONSTRAINT ck_credit_purge_receipts_digest CHECK (digest ~ {SHA256})
    )
    """,
)

TRIGGERS = (
    """
    CREATE TRIGGER trg_credit_lifecycle_episodes_guard
    BEFORE UPDATE OR DELETE ON credit_lifecycle_episodes
    FOR EACH ROW EXECUTE FUNCTION guard_credit_lifecycle_episode()
    """,
    """
    CREATE TRIGGER trg_credit_hold_generations_guard
    BEFORE UPDATE OR DELETE ON credit_hold_generations
    FOR EACH ROW EXECUTE FUNCTION guard_credit_hold_generation()
    """,
    *(f"""
        CREATE TRIGGER trg_{table}_immutable
        BEFORE UPDATE OR DELETE ON {table}
        FOR EACH ROW EXECUTE FUNCTION reject_credit_history_mutation()
        """ for table in APPEND_ONLY_TABLES),
    """
    CREATE TRIGGER trg_credit_retention_tombstones_tenant_builder
    BEFORE INSERT ON credit_retention_tombstones
    FOR EACH ROW EXECUTE FUNCTION check_credit_tenant_builder()
    """,
)


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def downgrade_preflight() -> str:
    """The downgrade's refusal block (SQLSTATE 55000, nothing changed)."""

    occupied = ",\n            ".join(
        f"({position}, {_literal(table)}, EXISTS (SELECT 1 FROM {table}))"
        for position, table in enumerate(CREATED_TABLES, start=1)
    )
    return f"""
DO $preflight$
DECLARE
    problems TEXT[] := ARRAY[]::TEXT[];
    listed TEXT;
BEGIN
    LOCK TABLE {", ".join(CREATED_TABLES)} IN ACCESS EXCLUSIVE MODE;
    SELECT string_agg(version_num, ', ' ORDER BY version_num) INTO listed
    FROM alembic_version;
    IF listed IS DISTINCT FROM '041' THEN
        problems := problems || format(
            'alembic_version is %s, not 041', coalesce(listed, 'empty'));
    END IF;
    SELECT string_agg(name, ', ' ORDER BY position) INTO listed
    FROM (VALUES
            {occupied}
    ) AS guarded(position, name, has_rows)
    WHERE has_rows;
    IF listed IS NOT NULL THEN
        problems := problems || format('tables that have rows: %s', listed);
    END IF;
    IF cardinality(problems) > 0 THEN
        RAISE EXCEPTION 'downgrade of migration 041 refused (nothing was changed): %',
            array_to_string(problems, '; ')
            USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
END
$preflight$
"""


# No CASCADE: an object the preflight did not foresee makes the statement
# fail (nothing changed) instead of vanishing.
DOWNGRADE_DROPS = (
    f"DROP TABLE {', '.join(reversed(CREATED_TABLES))}",
    f"DROP FUNCTION {', '.join(reversed(CREATED_FUNCTIONS))}",
)


def upgrade() -> None:
    _execute((LOCK_TIMEOUT, *FUNCTIONS, *TABLES, *TRIGGERS))


def downgrade() -> None:
    _execute((LOCK_TIMEOUT, downgrade_preflight(), *DOWNGRADE_DROPS))
