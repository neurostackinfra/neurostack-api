"""credit ledger, fact inbox, readiness and ingress schema

Revision ID: 038
Revises: 037
Create Date: 2026-10-26 00:00:01

neurostack-credit-system Group 1a: the additive schema the credit consumer
(Group 2), the metering (Group 3) and the enforcement gate (Group 7) need from
the start.  Nothing that existed at 037 is altered: no column, constraint,
index, trigger, type, function or row of an existing table changes.  The only
side effect on an existing object is the pair of internal RI triggers
PostgreSQL itself adds on ``tenants`` for each of the two foreign keys below
(``fk_credit_ingress_claims_tenant`` and ``fk_credit_response_outbox_tenant``).

Tables (``CREATED_TABLES``), in creation order:

* history, append-only by trigger (``APPEND_ONLY_TABLES``,
  ``reject_credit_history_mutation()``): ``credit_accounts`` (one per
  builder), ``credit_effects`` (``UNIQUE(builder_id, effect_id)``, the
  idempotency boundary of every financial effect), ``credit_lots`` (one
  ``monthly`` lot per ``(subscription_id, cycle_start, cycle_end)``, one
  ``topup`` lot per ``order_id``), ``credit_ledger_entries``,
  ``credit_debts``, ``credit_debt_origins``, ``credit_debt_settlements``,
  ``credit_fact_inbox`` (``inbox_seq`` identity; unique per
  ``(producer, event_id, schema_version)`` and per
  ``(producer, aggregate_kind, aggregate_id, financial_revision)``),
  ``credit_capability_readiness`` and ``credit_observational_effects``;
* ``credit_revision_gaps``: rows are never deleted and only ``closed_at``
  changes, once, from NULL (``guard_credit_revision_gap()``);
* ``credit_enforcement_binding``: the one history table that receives
  UPDATE -- its cycle identity and grant effect are fixed, its
  ``financial_revision`` never regresses and it is never deleted
  (``guard_credit_enforcement_binding()``);
* queue, claim and state rows: ``credit_ingress_claims`` (deduplicated per
  tenant: ``UNIQUE(tenant_id, principal_id, idempotency_key)`` and
  ``UNIQUE(tenant_id, source, provider_message_id)``),
  ``credit_response_outbox`` and ``credit_enforcement_state`` (at most one
  row, ``id = 1``; no row is seeded).

Foreign-key policy.  History keeps ``builder_id``/``tenant_id`` as plain UUIDs
with no foreign key to ``builders`` or ``tenants``, so deleting a clinic or a
builder and reparenting a tenant keep working with credit rows present.  The
two per-tenant mutable tables (claims, response outbox) reference
``tenants(id) ON DELETE CASCADE ON UPDATE NO ACTION``.  Tenant/builder
coherence is checked on write by ``check_credit_tenant_builder()``
(``TENANT_BUILDER_CHECKED_TABLES``): the row's ``builder_id`` must be the
tenant's current ``builder_id`` (NULL for a standalone tenant), read under
``FOR SHARE`` so a concurrent reparent or delete is waited for.  Among credit
tables, composite ``(builder_id, ...)`` foreign keys keep every reference
inside one builder.  ``credit_enforcement_binding`` has no foreign key to the
ASAAS tables (that would add RI triggers to them); it references its grant
effect by ``(builder_id, grant_effect_id)``.

Downgrade (``downgrade_preflight()``): ``LOCK_TIMEOUT``, then one ``DO``
block that locks ``tenants`` and every 038 table in ``ACCESS EXCLUSIVE``
mode (dropping the two foreign keys removes their triggers on ``tenants``)
and ``billing_fact_acks`` in ``SHARE`` mode (no ack may appear while the
check stands), and refuses with SQLSTATE 55000, changing nothing, when the
revision is not exactly 038 or when ANY 038 table or ``billing_fact_acks``
has a row: credit history is never discarded by a downgrade, and an ack the
consumer wrote names an inbox row that would vanish.  Only with all of them
empty does it drop exactly the 038 tables and functions (no ``CASCADE``: an
object the preflight did not foresee makes the statement fail instead of
vanishing).

Lock wait (both directions): the first statement is ``LOCK_TIMEOUT`` (``SET
LOCAL lock_timeout = '5s'``, as 024, 025, 036 and 037).  A timeout raises
SQLSTATE 55P03 inside the migration transaction, so nothing changed.
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "038"
down_revision: Union[str, None] = "037"
branch_labels = None
depends_on = None

# First statement of both directions; see "Lock wait" in the module docstring.
LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"

CREATED_TABLES = (
    "credit_accounts",
    "credit_effects",
    "credit_lots",
    "credit_ledger_entries",
    "credit_debts",
    "credit_debt_origins",
    "credit_debt_settlements",
    "credit_fact_inbox",
    "credit_revision_gaps",
    "credit_capability_readiness",
    "credit_ingress_claims",
    "credit_response_outbox",
    "credit_enforcement_state",
    "credit_enforcement_binding",
    "credit_observational_effects",
)
# Exact signatures, in creation order.
CREATED_FUNCTIONS = (
    "reject_credit_history_mutation()",
    "check_credit_tenant_builder()",
    "guard_credit_revision_gap()",
    "guard_credit_enforcement_binding()",
)
APPEND_ONLY_TABLES = (
    "credit_accounts",
    "credit_effects",
    "credit_lots",
    "credit_ledger_entries",
    "credit_debts",
    "credit_debt_origins",
    "credit_debt_settlements",
    "credit_fact_inbox",
    "credit_capability_readiness",
    "credit_observational_effects",
)
TENANT_BUILDER_CHECKED_TABLES = (
    "credit_effects",
    "credit_ingress_claims",
    "credit_observational_effects",
)
# The only foreign keys to an existing table; each adds two internal RI
# triggers on ``tenants`` (ON DELETE CASCADE, ON UPDATE NO ACTION).
TENANT_FOREIGN_KEYS = (
    ("credit_ingress_claims", "fk_credit_ingress_claims_tenant"),
    ("credit_response_outbox", "fk_credit_response_outbox_tenant"),
)
# The consumer-owned ack table (migration 027): the downgrade refuses while
# it has a row, because each ack names a credit inbox row.
CONSUMER_ACK_TABLE = "billing_fact_acks"
DOWNGRADE_LOCKED = ("tenants", *CREATED_TABLES)

_SHA256 = "'^[0-9a-f]{64}$'"


def _execute(statements: tuple[str, ...]) -> None:
    for statement in statements:
        ddl = sa.text(statement)
        if ddl._bindparams:
            bind_names = ", ".join(sorted(ddl._bindparams))
            raise ValueError(
                f"raw migration DDL contains bind parameters: {bind_names}"
            )
        op.execute(ddl)


FUNCTIONS = (
    """
    CREATE FUNCTION reject_credit_history_mutation()
    RETURNS trigger LANGUAGE plpgsql AS $function$
    BEGIN
        RAISE EXCEPTION '% is append-only: correct it with a new row', TG_TABLE_NAME
            USING ERRCODE = 'integrity_constraint_violation';
    END;
    $function$
    """,
    """
    CREATE FUNCTION check_credit_tenant_builder()
    RETURNS trigger LANGUAGE plpgsql AS $function$
    DECLARE
        tenant_builder UUID;
    BEGIN
        IF NEW.tenant_id IS NULL THEN
            RETURN NEW;
        END IF;
        -- FOR SHARE waits for a concurrent reparent or delete of the tenant
        -- and then reads the committed builder (or no row).
        SELECT builder_id INTO tenant_builder
        FROM tenants WHERE id = NEW.tenant_id FOR SHARE;
        IF NOT FOUND THEN
            RAISE EXCEPTION '% row names a tenant that does not exist', TG_TABLE_NAME
                USING ERRCODE = 'foreign_key_violation';
        END IF;
        IF NEW.builder_id IS DISTINCT FROM tenant_builder THEN
            RAISE EXCEPTION '% row builder does not match its tenant''s builder',
                TG_TABLE_NAME
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $function$
    """,
    """
    CREATE FUNCTION guard_credit_revision_gap()
    RETURNS trigger LANGUAGE plpgsql AS $function$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'credit_revision_gaps rows are never deleted'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF OLD.closed_at IS NOT NULL
           OR NEW.closed_at IS NULL
           OR NEW.aggregate_kind IS DISTINCT FROM OLD.aggregate_kind
           OR NEW.aggregate_id IS DISTINCT FROM OLD.aggregate_id
           OR NEW.first_missing_revision IS DISTINCT FROM OLD.first_missing_revision
           OR NEW.opened_at IS DISTINCT FROM OLD.opened_at THEN
            RAISE EXCEPTION 'a credit revision gap only closes, once'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $function$
    """,
    """
    CREATE FUNCTION guard_credit_enforcement_binding()
    RETURNS trigger LANGUAGE plpgsql AS $function$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'credit_enforcement_binding rows are never deleted'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.builder_id IS DISTINCT FROM OLD.builder_id
           OR NEW.subscription_id IS DISTINCT FROM OLD.subscription_id
           OR NEW.subscription_cycle_id IS DISTINCT FROM OLD.subscription_cycle_id
           OR NEW.cycle_start IS DISTINCT FROM OLD.cycle_start
           OR NEW.cycle_end IS DISTINCT FROM OLD.cycle_end
           OR NEW.grant_effect_id IS DISTINCT FROM OLD.grant_effect_id
           OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'a credit enforcement binding keeps its cycle identity'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.financial_revision < OLD.financial_revision THEN
            RAISE EXCEPTION 'a credit enforcement binding never regresses its revision'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $function$
    """,
)

TABLES = (
    """
    CREATE TABLE credit_accounts (
        id UUID PRIMARY KEY,
        builder_id UUID NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_credit_accounts_builder UNIQUE (builder_id),
        CONSTRAINT uq_credit_accounts_builder_id_id UNIQUE (builder_id, id)
    )
    """,
    f"""
    CREATE TABLE credit_effects (
        id UUID PRIMARY KEY,
        builder_id UUID NOT NULL,
        tenant_id UUID NULL,
        effect_id VARCHAR(512) NOT NULL,
        effect_kind VARCHAR(40) NOT NULL,
        decision_digest VARCHAR(64) NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_effects_effect_id CHECK (length(effect_id) > 0),
        CONSTRAINT ck_credit_effects_effect_kind CHECK (length(effect_kind) > 0),
        CONSTRAINT ck_credit_effects_decision_digest CHECK (
            decision_digest IS NULL OR decision_digest ~ {_SHA256}
        ),
        CONSTRAINT fk_credit_effects_account
            FOREIGN KEY (builder_id) REFERENCES credit_accounts(builder_id)
            ON DELETE RESTRICT,
        CONSTRAINT uq_credit_effects_builder_effect UNIQUE (builder_id, effect_id)
    )
    """,
    """
    CREATE TABLE credit_lots (
        id UUID PRIMARY KEY,
        builder_id UUID NOT NULL,
        account_id UUID NOT NULL,
        lot_kind VARCHAR(20) NOT NULL,
        subscription_id UUID NULL,
        cycle_start DATE NULL,
        cycle_end DATE NULL,
        order_id UUID NULL,
        payment_id VARCHAR(160) NOT NULL,
        paid_at DATE NOT NULL,
        granted_mc BIGINT NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        grant_effect_id VARCHAR(512) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_lots_kind CHECK (lot_kind IN ('monthly', 'topup')),
        CONSTRAINT ck_credit_lots_source CHECK (
            (lot_kind = 'monthly' AND subscription_id IS NOT NULL
                AND cycle_start IS NOT NULL AND cycle_end IS NOT NULL
                AND cycle_start < cycle_end AND order_id IS NULL)
            OR (lot_kind = 'topup' AND order_id IS NOT NULL
                AND subscription_id IS NULL AND cycle_start IS NULL
                AND cycle_end IS NULL)
        ),
        CONSTRAINT ck_credit_lots_granted_mc CHECK (granted_mc > 0),
        CONSTRAINT fk_credit_lots_account
            FOREIGN KEY (builder_id, account_id)
            REFERENCES credit_accounts(builder_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_credit_lots_grant_effect
            FOREIGN KEY (builder_id, grant_effect_id)
            REFERENCES credit_effects(builder_id, effect_id) ON DELETE RESTRICT,
        CONSTRAINT uq_credit_lots_monthly_cycle
            UNIQUE (subscription_id, cycle_start, cycle_end),
        CONSTRAINT uq_credit_lots_topup_order UNIQUE (order_id),
        CONSTRAINT uq_credit_lots_builder_id_id UNIQUE (builder_id, id)
    )
    """,
    "CREATE INDEX ix_credit_lots_builder_expires ON credit_lots (builder_id, expires_at)",
    """
    CREATE TABLE credit_ledger_entries (
        id UUID PRIMARY KEY,
        builder_id UUID NOT NULL,
        account_id UUID NOT NULL,
        effect_id VARCHAR(512) NOT NULL,
        lot_id UUID NULL,
        entry_type VARCHAR(30) NOT NULL,
        amount_mc BIGINT NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_ledger_entries_type CHECK (
            entry_type IN ('grant', 'debit', 'expiry', 'reversal', 'debt',
                           'debt_settlement', 'debt_release',
                           'settlement_release', 'lot_regrant')
        ),
        CONSTRAINT ck_credit_ledger_entries_amount CHECK (amount_mc > 0),
        CONSTRAINT ck_credit_ledger_entries_lot CHECK (
            lot_id IS NOT NULL OR entry_type IN ('debt', 'debt_release')
        ),
        CONSTRAINT fk_credit_ledger_entries_account
            FOREIGN KEY (builder_id, account_id)
            REFERENCES credit_accounts(builder_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_credit_ledger_entries_effect
            FOREIGN KEY (builder_id, effect_id)
            REFERENCES credit_effects(builder_id, effect_id) ON DELETE RESTRICT,
        CONSTRAINT fk_credit_ledger_entries_lot
            FOREIGN KEY (builder_id, lot_id)
            REFERENCES credit_lots(builder_id, id) ON DELETE RESTRICT,
        CONSTRAINT uq_credit_ledger_entries_builder_id_id UNIQUE (builder_id, id)
    )
    """,
    "CREATE INDEX ix_credit_ledger_entries_builder_lot "
    "ON credit_ledger_entries (builder_id, lot_id)",
    "CREATE INDEX ix_credit_ledger_entries_builder_effect "
    "ON credit_ledger_entries (builder_id, effect_id)",
    """
    CREATE TABLE credit_debts (
        id UUID PRIMARY KEY,
        builder_id UUID NOT NULL,
        account_id UUID NOT NULL,
        debt_source_kind VARCHAR(40) NOT NULL,
        debt_source_effect_id VARCHAR(512) NOT NULL,
        original_grant_lot_id UUID NULL,
        amount_mc BIGINT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_debts_source_kind CHECK (length(debt_source_kind) > 0),
        CONSTRAINT ck_credit_debts_amount CHECK (amount_mc > 0),
        CONSTRAINT fk_credit_debts_account
            FOREIGN KEY (builder_id, account_id)
            REFERENCES credit_accounts(builder_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_credit_debts_source_effect
            FOREIGN KEY (builder_id, debt_source_effect_id)
            REFERENCES credit_effects(builder_id, effect_id) ON DELETE RESTRICT,
        CONSTRAINT fk_credit_debts_original_lot
            FOREIGN KEY (builder_id, original_grant_lot_id)
            REFERENCES credit_lots(builder_id, id) ON DELETE RESTRICT,
        CONSTRAINT uq_credit_debts_builder_id_id UNIQUE (builder_id, id)
    )
    """,
    "CREATE INDEX ix_credit_debts_builder_order "
    "ON credit_debts (builder_id, created_at, id)",
    """
    CREATE TABLE credit_debt_origins (
        builder_id UUID NOT NULL,
        debt_id UUID NOT NULL,
        consumption_entry_id UUID NOT NULL,
        PRIMARY KEY (debt_id, consumption_entry_id),
        CONSTRAINT fk_credit_debt_origins_debt
            FOREIGN KEY (builder_id, debt_id)
            REFERENCES credit_debts(builder_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_credit_debt_origins_consumption
            FOREIGN KEY (builder_id, consumption_entry_id)
            REFERENCES credit_ledger_entries(builder_id, id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE credit_debt_settlements (
        id UUID PRIMARY KEY,
        builder_id UUID NOT NULL,
        debt_id UUID NOT NULL,
        settling_grant_lot_id UUID NOT NULL,
        amount_mc BIGINT NOT NULL,
        effect_id VARCHAR(512) NOT NULL,
        CONSTRAINT ck_credit_debt_settlements_amount CHECK (amount_mc > 0),
        CONSTRAINT fk_credit_debt_settlements_debt
            FOREIGN KEY (builder_id, debt_id)
            REFERENCES credit_debts(builder_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_credit_debt_settlements_lot
            FOREIGN KEY (builder_id, settling_grant_lot_id)
            REFERENCES credit_lots(builder_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_credit_debt_settlements_effect
            FOREIGN KEY (builder_id, effect_id)
            REFERENCES credit_effects(builder_id, effect_id) ON DELETE RESTRICT
    )
    """,
    "CREATE INDEX ix_credit_debt_settlements_builder_debt "
    "ON credit_debt_settlements (builder_id, debt_id)",
    f"""
    CREATE TABLE credit_fact_inbox (
        id UUID PRIMARY KEY,
        inbox_seq BIGINT GENERATED ALWAYS AS IDENTITY,
        producer VARCHAR(80) NOT NULL,
        event_id UUID NOT NULL,
        schema_version VARCHAR(40) NOT NULL,
        aggregate_kind VARCHAR(30) NOT NULL,
        aggregate_id UUID NOT NULL,
        financial_revision BIGINT NOT NULL,
        fact_sha256 VARCHAR(64) NOT NULL,
        bundle_digest VARCHAR(64) NOT NULL,
        schema_digest VARCHAR(64) NOT NULL,
        outcome VARCHAR(20) NOT NULL,
        received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_fact_inbox_aggregate_kind CHECK (
            aggregate_kind IN ('subscription_cycle', 'topup_order')
        ),
        CONSTRAINT ck_credit_fact_inbox_financial_revision CHECK (
            financial_revision > 0
        ),
        CONSTRAINT ck_credit_fact_inbox_outcome CHECK (
            outcome IN ('accepted', 'rejected')
        ),
        CONSTRAINT ck_credit_fact_inbox_digests CHECK (
            fact_sha256 ~ {_SHA256}
            AND bundle_digest ~ {_SHA256}
            AND schema_digest ~ {_SHA256}
        ),
        CONSTRAINT uq_credit_fact_inbox_seq UNIQUE (inbox_seq),
        CONSTRAINT uq_credit_fact_inbox_event
            UNIQUE (producer, event_id, schema_version),
        CONSTRAINT uq_credit_fact_inbox_revision
            UNIQUE (producer, aggregate_kind, aggregate_id, financial_revision)
    )
    """,
    """
    CREATE TABLE credit_revision_gaps (
        aggregate_kind VARCHAR(30) NOT NULL,
        aggregate_id UUID NOT NULL,
        first_missing_revision BIGINT NOT NULL,
        opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        closed_at TIMESTAMPTZ NULL,
        PRIMARY KEY (aggregate_kind, aggregate_id, first_missing_revision),
        CONSTRAINT ck_credit_revision_gaps_aggregate_kind CHECK (
            aggregate_kind IN ('subscription_cycle', 'topup_order')
        ),
        CONSTRAINT ck_credit_revision_gaps_revision CHECK (
            first_missing_revision > 0
        ),
        CONSTRAINT ck_credit_revision_gaps_closed CHECK (
            closed_at IS NULL OR closed_at >= opened_at
        )
    )
    """,
    f"""
    CREATE TABLE credit_capability_readiness (
        id UUID PRIMARY KEY,
        schema_version VARCHAR(40) NOT NULL,
        environment VARCHAR(20) NOT NULL,
        billing_fact_schema_digest VARCHAR(64) NOT NULL,
        inbox_high_watermark BIGINT NOT NULL,
        reconciliation_complete BOOLEAN NOT NULL,
        readback_complete BOOLEAN NOT NULL,
        has_open_gap BOOLEAN NOT NULL,
        has_backlog BOOLEAN NOT NULL,
        has_alert BOOLEAN NOT NULL,
        operation_id UUID NULL,
        deployment_digest VARCHAR(64) NULL,
        credit_deployment_digest VARCHAR(64) NULL,
        producer_compatibility_digest VARCHAR(64) NULL,
        consumer_compatibility_digest VARCHAR(64) NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_capability_readiness_schema_version CHECK (
            schema_version = 'credit_capability_ready.v1'
        ),
        CONSTRAINT ck_credit_capability_readiness_environment CHECK (
            environment IN ('development', 'staging', 'production', 'test')
        ),
        CONSTRAINT ck_credit_capability_readiness_watermark CHECK (
            inbox_high_watermark >= 0
        ),
        CONSTRAINT ck_credit_capability_readiness_digests CHECK (
            billing_fact_schema_digest ~ {_SHA256}
            AND (deployment_digest IS NULL OR deployment_digest ~ {_SHA256})
            AND (credit_deployment_digest IS NULL
                 OR credit_deployment_digest ~ {_SHA256})
            AND (producer_compatibility_digest IS NULL
                 OR producer_compatibility_digest ~ {_SHA256})
            AND (consumer_compatibility_digest IS NULL
                 OR consumer_compatibility_digest ~ {_SHA256})
        )
    )
    """,
    "CREATE INDEX ix_credit_capability_readiness_environment_created "
    "ON credit_capability_readiness (environment, created_at)",
    f"""
    CREATE TABLE credit_ingress_claims (
        id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        builder_id UUID NULL,
        source VARCHAR(20) NOT NULL,
        principal_id VARCHAR(255) NOT NULL,
        idempotency_key VARCHAR(255) NULL,
        provider_message_id VARCHAR(255) NULL,
        interaction_id UUID NOT NULL,
        payload_sha256 VARCHAR(64) NOT NULL,
        decision_version INTEGER NOT NULL,
        session_version BIGINT NULL,
        mode VARCHAR(20) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_ingress_claims_source CHECK (
            source IN ('events', 'zapi', 'meta', 'uazapi', 'evolution')
        ),
        CONSTRAINT ck_credit_ingress_claims_key CHECK (
            (source = 'events' AND idempotency_key IS NOT NULL
                AND provider_message_id IS NULL)
            OR (source <> 'events' AND provider_message_id IS NOT NULL
                AND idempotency_key IS NULL)
        ),
        CONSTRAINT ck_credit_ingress_claims_identity CHECK (
            length(principal_id) > 0
            AND (idempotency_key IS NULL OR length(idempotency_key) > 0)
            AND (provider_message_id IS NULL OR length(provider_message_id) > 0)
        ),
        CONSTRAINT ck_credit_ingress_claims_versions CHECK (
            decision_version > 0
            AND (session_version IS NULL OR session_version >= 0)
        ),
        CONSTRAINT ck_credit_ingress_claims_mode CHECK (
            mode IN ('observational', 'enforced')
        ),
        CONSTRAINT ck_credit_ingress_claims_payload_sha256 CHECK (
            payload_sha256 ~ {_SHA256}
        ),
        CONSTRAINT fk_credit_ingress_claims_tenant
            FOREIGN KEY (tenant_id) REFERENCES tenants(id)
            ON DELETE CASCADE ON UPDATE NO ACTION,
        CONSTRAINT uq_credit_ingress_claims_idempotency
            UNIQUE (tenant_id, principal_id, idempotency_key),
        CONSTRAINT uq_credit_ingress_claims_provider_message
            UNIQUE (tenant_id, source, provider_message_id)
    )
    """,
    """
    CREATE TABLE credit_response_outbox (
        id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        effect_id VARCHAR(512) NOT NULL,
        payload JSONB NOT NULL,
        state VARCHAR(20) NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_response_outbox_effect_id CHECK (length(effect_id) > 0),
        CONSTRAINT ck_credit_response_outbox_state CHECK (length(state) > 0),
        CONSTRAINT ck_credit_response_outbox_attempts CHECK (attempts >= 0),
        CONSTRAINT fk_credit_response_outbox_tenant
            FOREIGN KEY (tenant_id) REFERENCES tenants(id)
            ON DELETE CASCADE ON UPDATE NO ACTION,
        CONSTRAINT uq_credit_response_outbox_effect UNIQUE (tenant_id, effect_id)
    )
    """,
    "CREATE INDEX ix_credit_response_outbox_state_created "
    "ON credit_response_outbox (state, created_at)",
    """
    CREATE TABLE credit_enforcement_state (
        id SMALLINT PRIMARY KEY,
        credit_enforcement_at TIMESTAMPTZ NULL,
        set_by VARCHAR(255) NULL,
        set_at TIMESTAMPTZ NULL,
        CONSTRAINT ck_credit_enforcement_state_singleton CHECK (id = 1),
        CONSTRAINT ck_credit_enforcement_state_set CHECK (
            credit_enforcement_at IS NULL
            OR (set_by IS NOT NULL AND set_at IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE credit_enforcement_binding (
        id UUID PRIMARY KEY,
        builder_id UUID NOT NULL,
        subscription_id UUID NOT NULL,
        subscription_cycle_id UUID NOT NULL,
        cycle_start DATE NOT NULL,
        cycle_end DATE NOT NULL,
        financial_revision BIGINT NOT NULL,
        grant_effect_id VARCHAR(512) NOT NULL,
        status VARCHAR(20) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_enforcement_binding_status CHECK (
            status IN ('active', 'reversed')
        ),
        CONSTRAINT ck_credit_enforcement_binding_cycle CHECK (cycle_start < cycle_end),
        CONSTRAINT ck_credit_enforcement_binding_revision CHECK (
            financial_revision > 0
        ),
        CONSTRAINT fk_credit_enforcement_binding_grant_effect
            FOREIGN KEY (builder_id, grant_effect_id)
            REFERENCES credit_effects(builder_id, effect_id) ON DELETE RESTRICT,
        CONSTRAINT uq_credit_enforcement_binding_cycle UNIQUE (subscription_cycle_id),
        CONSTRAINT uq_credit_enforcement_binding_subscription_cycle
            UNIQUE (subscription_id, cycle_start, cycle_end)
    )
    """,
    "CREATE INDEX ix_credit_enforcement_binding_builder "
    "ON credit_enforcement_binding (builder_id)",
    f"""
    CREATE TABLE credit_observational_effects (
        id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        builder_id UUID NULL,
        effect_id VARCHAR(512) NOT NULL,
        effect_kind VARCHAR(20) NOT NULL,
        trace_id UUID NULL,
        origin VARCHAR(20) NULL,
        authenticated BOOLEAN NULL,
        would_debit_mc BIGINT NOT NULL,
        decision_digest VARCHAR(64) NULL,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credit_observational_effects_kind CHECK (
            effect_kind IN ('trace', 'rag', 'media', 'egress', 'maintenance')
        ),
        CONSTRAINT ck_credit_observational_effects_origin CHECK (
            origin IS NULL OR origin IN ('production', 'simulator', 'unknown')
        ),
        CONSTRAINT ck_credit_observational_effects_effect_id CHECK (
            length(effect_id) > 0
        ),
        CONSTRAINT ck_credit_observational_effects_would_debit CHECK (
            would_debit_mc >= 0
        ),
        CONSTRAINT ck_credit_observational_effects_decision_digest CHECK (
            decision_digest IS NULL OR decision_digest ~ {_SHA256}
        ),
        CONSTRAINT uq_credit_observational_effects_effect UNIQUE (tenant_id, effect_id)
    )
    """,
    "CREATE INDEX ix_credit_observational_effects_recorded "
    "ON credit_observational_effects (recorded_at)",
)

TRIGGERS = (
    *(f"""
        CREATE TRIGGER trg_{table}_immutable
        BEFORE UPDATE OR DELETE ON {table}
        FOR EACH ROW EXECUTE FUNCTION reject_credit_history_mutation()
        """ for table in APPEND_ONLY_TABLES),
    """
    CREATE TRIGGER trg_credit_effects_tenant_builder
    BEFORE INSERT ON credit_effects
    FOR EACH ROW EXECUTE FUNCTION check_credit_tenant_builder()
    """,
    """
    CREATE TRIGGER trg_credit_ingress_claims_tenant_builder
    BEFORE INSERT OR UPDATE OF tenant_id, builder_id ON credit_ingress_claims
    FOR EACH ROW EXECUTE FUNCTION check_credit_tenant_builder()
    """,
    """
    CREATE TRIGGER trg_credit_observational_effects_tenant_builder
    BEFORE INSERT ON credit_observational_effects
    FOR EACH ROW EXECUTE FUNCTION check_credit_tenant_builder()
    """,
    """
    CREATE TRIGGER trg_credit_revision_gaps_guard
    BEFORE UPDATE OR DELETE ON credit_revision_gaps
    FOR EACH ROW EXECUTE FUNCTION guard_credit_revision_gap()
    """,
    """
    CREATE TRIGGER trg_credit_enforcement_binding_guard
    BEFORE UPDATE OR DELETE ON credit_enforcement_binding
    FOR EACH ROW EXECUTE FUNCTION guard_credit_enforcement_binding()
    """,
)


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def downgrade_preflight() -> str:
    """The downgrade's refusal block (see "Downgrade" in the docstring)."""

    guarded = (*CREATED_TABLES, CONSUMER_ACK_TABLE)
    occupied = ",\n            ".join(
        f"({position}, {_literal(table)}, EXISTS (SELECT 1 FROM {table}))"
        for position, table in enumerate(guarded, start=1)
    )
    return f"""
DO $preflight$
DECLARE
    problems TEXT[] := ARRAY[]::TEXT[];
    listed TEXT;
BEGIN
    LOCK TABLE {", ".join(DOWNGRADE_LOCKED)}
        IN ACCESS EXCLUSIVE MODE;
    LOCK TABLE {CONSUMER_ACK_TABLE} IN SHARE MODE;
    SELECT string_agg(version_num, ', ' ORDER BY version_num) INTO listed
    FROM alembic_version;
    IF listed IS DISTINCT FROM '038' THEN
        problems := problems || format(
            'alembic_version is %s, not 038', coalesce(listed, 'empty'));
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
        RAISE EXCEPTION 'downgrade of migration 038 refused (nothing was changed): %',
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
