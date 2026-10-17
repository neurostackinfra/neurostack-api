/*
Read-only predeployment check for POST /v1/admin/billing/producer-build-attestations.

Replace the reserved 192.0.2.10 address below with every current task ENI IPv4
address from the operator-authenticated ECS task readback. Run in the already-
authenticated PostgreSQL console connected to the API database. Do not add
application selects or query text fields. A query error or any false result
blocks the rollout.
*/
WITH supplied_task_ips AS (
    SELECT ARRAY[INET '192.0.2.10'] AS task_ips
),
matched_sessions AS (
    SELECT DISTINCT activity.client_addr, activity.usename
    FROM supplied_task_ips AS supplied
    CROSS JOIN LATERAL UNNEST(supplied.task_ips) AS task_ip(address)
    JOIN pg_stat_activity AS activity
      ON activity.client_addr = task_ip.address
     AND activity.datname = current_database()
     AND activity.backend_type = 'client backend'
),
task_binding AS (
    SELECT
        ARRAY(
            SELECT host(task_ip.address)
            FROM UNNEST(supplied.task_ips) AS task_ip(address)
            ORDER BY host(task_ip.address)
        ) AS task_ips,
        CARDINALITY(supplied.task_ips) AS task_ip_count,
        COUNT(DISTINCT matched.client_addr) AS matched_task_ip_count,
        COUNT(DISTINCT matched.usename) AS runtime_role_count,
        CASE
            WHEN CARDINALITY(supplied.task_ips) > 0
             AND COUNT(DISTINCT matched.client_addr) = CARDINALITY(supplied.task_ips)
             AND COUNT(DISTINCT matched.usename) = 1
            THEN MIN(matched.usename::text)
            ELSE NULL
        END AS api_runtime_role
    FROM supplied_task_ips AS supplied
    LEFT JOIN matched_sessions AS matched ON TRUE
    GROUP BY supplied.task_ips
),
required_columns AS (
    SELECT *
    FROM (VALUES
        ('public', 'billing_producer_v1_build_attestations', 'id', 'uuid'),
        ('public', 'billing_producer_v1_build_attestations', 'operation_id', 'uuid'),
        ('public', 'billing_producer_v1_build_attestations', 'envelope', 'jsonb'),
        ('public', 'billing_producer_v1_build_attestations', 'envelope_digest', 'character varying(64)'),
        ('public', 'billing_producer_v1_build_attestations', 'payload_digest', 'character varying(64)'),
        ('public', 'billing_producer_v1_build_attestations', 'registered_by_user_id', 'uuid'),
        ('public', 'billing_producer_v1_build_attestations', 'registered_at', 'timestamp with time zone'),
        ('public', 'users', 'id', 'uuid'),
        ('public', 'users', 'is_active', 'boolean'),
        ('public', 'users', 'role', NULL)
    ) AS expected(schema_name, table_name, column_name, expected_type)
),
column_status AS (
    SELECT BOOL_AND(
        EXISTS (
            SELECT 1
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            JOIN pg_catalog.pg_attribute AS attribute
              ON attribute.attrelid = relation.oid
            WHERE namespace.nspname = expected.schema_name
              AND relation.relname = expected.table_name
              AND relation.relkind IN ('r', 'p')
              AND attribute.attname = expected.column_name
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
              AND (
                    expected.expected_type IS NULL
                    OR pg_catalog.format_type(attribute.atttypid, attribute.atttypmod)
                       = expected.expected_type
              )
        )
    ) AS required_columns_present
    FROM required_columns AS expected
),
required_constraints AS (
    SELECT *
    FROM (VALUES
        ('billing_producer_v1_build_attestations', 'billing_producer_v1_build_attestations_pkey', 'p', ARRAY['id']::text[], NULL, NULL, NULL, NULL, NULL),
        ('billing_producer_v1_build_attestations', 'ck_billing_producer_v1_attestation_envelope_digest', 'c', ARRAY['envelope_digest']::text[], 'lengthenvelope_digest=64andenvelope_digest~''^[0-9a-f]{64}$''', NULL, NULL, NULL, NULL),
        ('billing_producer_v1_build_attestations', 'ck_billing_producer_v1_attestation_payload_digest', 'c', ARRAY['payload_digest']::text[], 'lengthpayload_digest=64andpayload_digest~''^[0-9a-f]{64}$''', NULL, NULL, NULL, NULL),
        ('billing_producer_v1_build_attestations', 'uq_billing_producer_v1_attestation_digest', 'u', ARRAY['id', 'envelope_digest']::text[], NULL, NULL, NULL, NULL, NULL),
        ('billing_producer_v1_build_attestations', 'billing_producer_v1_build_attestations_registered_by_user_id_fkey', 'f', ARRAY['registered_by_user_id']::text[], NULL, 'public', 'users', ARRAY['id']::text[], 'r')
    ) AS expected(table_name, constraint_name, constraint_type, key_columns, check_expression, referenced_schema, referenced_table, referenced_columns, delete_action)
),
constraint_status AS (
    SELECT BOOL_AND(
        EXISTS (
            SELECT 1
            FROM pg_catalog.pg_constraint AS constraint_row
            JOIN pg_catalog.pg_class AS relation
              ON relation.oid = constraint_row.conrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relname = expected.table_name
              AND constraint_row.conname = expected.constraint_name
              AND constraint_row.contype = expected.constraint_type
              AND constraint_row.convalidated
              AND CASE
                    WHEN expected.constraint_type = 'c' THEN
                        lower(regexp_replace(
                            regexp_replace(
                                pg_catalog.pg_get_expr(
                                    constraint_row.conbin,
                                    constraint_row.conrelid,
                                    true
                                ),
                                '::text',
                                '',
                                'g'
                            ),
                            '[[:space:]()]',
                            '',
                            'g'
                        )) = expected.check_expression
                    ELSE ARRAY(
                        SELECT attribute.attname::text
                        FROM unnest(constraint_row.conkey)
                             WITH ORDINALITY AS key_column(attnum, ordinal)
                        JOIN pg_catalog.pg_attribute AS attribute
                          ON attribute.attrelid = constraint_row.conrelid
                         AND attribute.attnum = key_column.attnum
                        ORDER BY key_column.ordinal
                    )::text[] = expected.key_columns
                  END
              AND (
                    expected.constraint_type <> 'f'
                    OR (
                        constraint_row.confrelid = pg_catalog.to_regclass(
                            pg_catalog.format(
                                '%I.%I',
                                expected.referenced_schema,
                                expected.referenced_table
                            )
                        )
                        AND ARRAY(
                            SELECT referenced_attribute.attname::text
                            FROM unnest(constraint_row.confkey)
                                 WITH ORDINALITY AS referenced_key(attnum, ordinal)
                            JOIN pg_catalog.pg_attribute AS referenced_attribute
                              ON referenced_attribute.attrelid = constraint_row.confrelid
                             AND referenced_attribute.attnum = referenced_key.attnum
                            ORDER BY referenced_key.ordinal
                        )::text[] = expected.referenced_columns
                        AND constraint_row.confdeltype = expected.delete_action
                    )
              )
        )
    ) AS required_constraints_present
    FROM required_constraints AS expected
),
privilege_status AS (
    SELECT
        binding.api_runtime_role IS NOT NULL
        AND pg_catalog.has_schema_privilege(binding.api_runtime_role, 'public', 'USAGE')
        AND pg_catalog.has_table_privilege(
            binding.api_runtime_role,
            'public.billing_producer_v1_build_attestations',
            'SELECT'
        )
        AND pg_catalog.has_table_privilege(
            binding.api_runtime_role,
            'public.billing_producer_v1_build_attestations',
            'INSERT'
        )
        AND pg_catalog.has_table_privilege(binding.api_runtime_role, 'public.users', 'SELECT')
        AND pg_catalog.has_table_privilege(binding.api_runtime_role, 'public.users', 'UPDATE')
        AS required_privileges_present,
        binding.api_runtime_role IS NOT NULL
        AND pg_catalog.has_table_privilege(
            binding.api_runtime_role,
            'public.users',
            'SELECT'
        )
        AND pg_catalog.has_table_privilege(
            binding.api_runtime_role,
            'public.users',
            'UPDATE'
        ) AS row_lock_privilege_present
    FROM task_binding AS binding
)
SELECT
    to_json(binding.task_ips) AS task_ips,
    binding.task_ip_count,
    binding.matched_task_ip_count,
    binding.runtime_role_count,
    binding.api_runtime_role,
    columns.required_columns_present,
    constraints.required_constraints_present,
    privileges.required_privileges_present,
    privileges.row_lock_privilege_present,
    (
        binding.api_runtime_role IS NOT NULL
        AND columns.required_columns_present
        AND constraints.required_constraints_present
        AND privileges.required_privileges_present
        AND privileges.row_lock_privilege_present
    ) AS readiness_ok
FROM task_binding AS binding
CROSS JOIN column_status AS columns
CROSS JOIN constraint_status AS constraints
CROSS JOIN privilege_status AS privileges;
