# ASAAS producer image rollout — Group 5 operator runbook

This runbook describes the protected workflow at `.github/workflows/asaas-producer-image-deploy.yml`. It covers code and operator evidence only; it does not authorize or perform AWS/GitHub provisioning, image publication, deployment, database changes, or backend registration by itself.

The ECS service `neurostack-staging-app` is production serving. Keep ASAAS/credit egress and commercial top-up disabled throughout. The bootstrap slot is consumed when the protected `deploy` job starts, even if a later check fails. A failed bootstrap must roll back to the task-definition ARN recorded in its private predeployment readback; it cannot be retried under this WISH.

## Readiness contract

The live target group `neurostack-staging-app` currently checks `/health`. That endpoint is liveness and does not prove database, Redis, or Qdrant readiness. The workflow records `DescribeTargetGroups` and `DescribeTargetHealth` as ALB health evidence, retaining the real health-check path, target-group ARN, observation time, target counts, and health-state counts. It never labels `/health` as readiness.

Before approval and after deployment, an administrator also probes **every exact running task private IPv4 address** with `GET http://<task-ip>:8000/ready` from a VPC-connected host whose network policy already allows access. Record only the private target IP, HTTP status code, and `ready` boolean; discard response bodies. Every task must return HTTP 200 with `ready=true`. The evidence is a fresh, human-authenticated operator attestation bound to the Actions run; the runner does not independently make the HTTP or ELB readback. If no already-authorized VPC host can reach every task IP, stop before approval or acceptance. Do not use `/health`, an ALB request that may hit only one target, ECS Exec, or a security-group change as a substitute.

`GET /ready` checks the production DB session, Redis, Qdrant, AbacatePay webhook configuration, and billing-observability compatibility. The checked-in SQL query separately proves the database registration schema and the active task-to-runtime-role mapping.

## Required prerequisites

1. Group 2 trust and protection resources have been provisioned and read back under their separate approval. Group 4 has produced the exact successful signed build artifact and immutable ECR digest. No build JWT or user JWT is passed to this workflow.
2. The GitHub repository is public. Keep task-definition JSON, environment values, private hostnames, secret ARNs, task IPs, and raw AWS readbacks out of repository files, workflow inputs, issue text, Actions logs, and public artifacts.
3. The protected environment `asaas-production-deployment` has the Group 2 reviewed rules/reviewer protections and variables. **Do not create the task-template secrets until the Group 2 KMS pins are provisioned and reviewed.**
4. The database is RDS PostgreSQL 16.13 with IAM database authentication disabled. Use an already authenticated PostgreSQL console/session; do not attempt IAM DB authentication or retrieve `DATABASE_URL`. If the task connections cannot identify one runtime role through `pg_stat_activity`, or catalog access is denied, stop before environment approval. Schema/privilege remediation requires a separate approval.
5. A VPC-connected operator host is already permitted to reach each current task IP on container port 8000. If this is not true, readiness evidence cannot be collected under this workflow; stop before approval.

## Prepare the protected task-definition template

Create a private local JSON file with mode `0600`, based on the authenticated operator task-definition readback. Its exact outer shape is:

```json
{
  "schema_version": "asaas_producer_task_definition_template.v1",
  "source_task_definition_arn": "arn:aws:ecs:us-east-1:388460142431:task-definition/neurostack-staging-app:8",
  "task_definition": {
    "family": "neurostack-staging-app",
    "taskRoleArn": "arn:aws:iam::388460142431:role/neurostack-staging-ecs-task",
    "executionRoleArn": "arn:aws:iam::388460142431:role/neurostack-staging-ecs-execution",
    "containerDefinitions": [
      {
        "name": "app",
        "image": "<current image reference>",
        "environment": [
          {"name": "ENVIRONMENT", "value": "<observed value>"},
          {"name": "HOST", "value": "<observed value>"},
          {"name": "LLM_MODEL", "value": "<observed value>"},
          {"name": "LOG_FORMAT", "value": "<observed value>"},
          {"name": "PORT", "value": "<observed value>"},
          {"name": "QDRANT_HOST", "value": "<observed value>"}
        ],
        "secrets": [
          {"name": "DATABASE_URL", "valueFrom": "<observed plain secret ARN>"},
          {"name": "REDIS_URL", "valueFrom": "<observed secret ARN>"},
          {"name": "SECRET_KEY", "valueFrom": "<observed secret ARN>"},
          {"name": "LLM_API_KEY", "valueFrom": "<observed secret ARN>"},
          {"name": "BILLING_OPERATION_RECEIPT_SIGNING_KEY", "valueFrom": "<observed secret ARN>"}
        ],
        "portMappings": ["<copy every observed mapping>"],
        "essential": true
      },
      {
        "name": "otel",
        "image": "<observed collector image>",
        "essential": false,
        "environment": [],
        "secrets": []
      }
    ],
    "networkMode": "awsvpc",
    "requiresCompatibilities": ["FARGATE"],
    "cpu": "1024",
    "memory": "2048"
  }
}
```

This schema illustrates the required identity and name sets. Copy all actual non-secret task-definition settings from the private source readback; do not replace unknown settings with the illustrative values above. Include only `RegisterTaskDefinition` request fields (no response-only ARN/revision/status fields). Preserve all exact existing app and sidecar settings. The app has exactly six base environment names and five secret reference names. The `DATABASE_URL` must be the plain secret ARN observed in the active task definition; the rollout resolves its active selector by metadata and pins the replacement to the same VersionId. No secret value belongs in this template.

After Group 2 pins are installed, validate the private template on a trusted local Python 3.12 environment with the Group 2 variables present:

```bash
umask 077
export ASAAS_PRODUCER_TASK_DEFINITION_TEMPLATE_JSON="$(< /private/path/task-definition-template.json)"
export ASAAS_PRODUCER_TASK_DEFINITION_TEMPLATE_SHA256="<canonical SHA-256 computed below>"
python3 scripts/asaas_producer_rollout_evidence.py validate-template
unset ASAAS_PRODUCER_TASK_DEFINITION_TEMPLATE_JSON
```

Compute the hash without printing the JSON. This is SHA-256 over compact canonical JSON (`sort_keys=True`, ASCII escaping, no NaN):

```bash
python3 - /private/path/task-definition-template.json <<'PY'
import hashlib, json, sys
from pathlib import Path
value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
canonical = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
print(hashlib.sha256(canonical).hexdigest())
PY
```

Store these only as environment secrets on `asaas-production-deployment`:

- `ASAAS_PRODUCER_TASK_DEFINITION_TEMPLATE_JSON`: the complete private template JSON.
- `ASAAS_PRODUCER_TASK_DEFINITION_TEMPLATE_SHA256`: the lowercase 64-hex canonical hash.
- `ASAAS_PRODUCER_PREDEPLOY_READBACK_JSON`: replaced for each workflow run with that run's fresh, sanitized operator evidence described below.

Use the GitHub environment secret UI or `gh secret set --env asaas-production-deployment NAME < private-file`; do not put JSON or values in shell arguments, workflow inputs, repository variables, commits, or logs. Record the hash (not the JSON) in the independent admin approval evidence. Recompute it inside the protected workflow before its first AWS OIDC step. This template is operator-provided and secret-scoped; the runner does not call `ecs:DescribeTaskDefinition`.

## Predeployment evidence and approval

1. Generate a fresh deployment `operation_id` UUID. It is deployment metadata, distinct from the signed envelope's operation ID. Supply the exact Group 4 build run, attestation ID, immutable digest, signed-envelope SHA-256, and evidence issue number to `workflow_dispatch`. Do not include a task ARN, secret ARN, task IP, SQL result, or raw snapshot in inputs.
2. The no-OIDC `preflight` verifies repository/ruleset/environment state, exact run/workflow/ref/SHA, build artifact identity and deployment history. It does not have environment secrets, AWS credentials, or `id-token:write`.
3. Before the first protected environment approval, an independent repository admin checks the live ruleset and environment UI, captures the ruleset and `asaas-production-deployment` screenshots, and posts one sanitized UI-attestation JSON comment to the designated issue. It must bind the exact run ID/attempt, SHA, ref, workflow path, environment, reviewer, template hash and `checked_at_utc`. The screenshot attachment URLs and SHA-256s are fetched and checked by the verifier. It records `ruleset_bypass_empty=true`, `environment_admin_bypass_disabled=true`, and `sole_branch_policy="main"`. The UI timestamp must be after preflight and no more than five minutes before the protected job starts. GitHub's approvals API does not expose the approval click time; the workflow records only its later `approval_observed_at_utc`.
4. Obtain the exact currently running task ARNs, ENI IPv4s, task-definition ARN, created/started timestamps, image digests, service desired/running counts, load-balancer mapping, ECR digest, and Secrets Manager metadata through authenticated read-only admin calls. Keep raw responses in a mode-0600 local file. The operator snapshot is not a cryptographic AWS attestation; it is a human-authenticated readback bound to this Actions run and checked by the protected workflow.
5. For each task IP, issue a direct readiness request from the already-authorized VPC host. Save only the status code; discard the response body:

   ```bash
   curl --silent --show-error --output /dev/null --write-out '%{http_code}\n' \
     --max-time 5 "http://<task-private-ip>:8000/ready"
   ```

   Every exact task IP must return `200`; record `ready=true` only on that result. Capture the `DescribeTargetGroups` `HealthCheckPath` exactly (`/health` is liveness) and the `DescribeTargetHealth` target IDs, ports and states. Require the same target-group ARN as the ECS service, exact healthy target IP set equal to the running task IP set, target port equal to the service app port, and no unhealthy target. The TG response alone is never readiness evidence.
6. Run `scripts/asaas_registration_database_readiness.sql` in the existing authenticated PostgreSQL console. Replace the reserved `192.0.2.10` with every current task ENI IP. The SQL reads `pg_stat_activity` and PostgreSQL catalogs/privilege functions only; it does no DML, reads no application rows or query text, and never retrieves a secret. It must map all task IPs to one active runtime role and report all schema, constraints, table privileges, and `row_lock_privilege_present` true. That explicit row-lock check means the runtime role has both `SELECT` and `UPDATE` on `public.users`, the permissions needed by the registration flow's `SELECT ... FOR UPDATE`. Run `scripts/validate_asaas_registration_database_readiness.py --input <private-row.json> --output <private-validated.json>` against the single query result. Do not attempt IAM database authentication; it is disabled for this RDS instance.
7. Store a single `asaas_producer_run_bound_readback.v1` JSON in `ASAAS_PRODUCER_PREDEPLOY_READBACK_JSON` with exact run tuple, operation ID, mode, operator login, checked time, template/build-trust hashes, artifact tuple, readiness SQL result, target-group readback, per-task readiness probe, and the private ECS/ECR/secret-metadata snapshot. The validator rejects mismatched task IPs, timestamps, desired counts, secret refs/version, extra environment names, health state, path, run, or hash. Do not post this JSON publicly.
8. After the first approval and predeploy validation finish, the same independent admin repeats the live UI check and posts a second fresh sanitized UI-attestation comment before the second protected approval. The no-OIDC deploy gate requires the two distinct comments to bracket the predeploy and deploy jobs.

The readiness probe evidence has this shape; replace every placeholder with the private run's observed data, and do not include a body, URL, or response text:

```json
{
  "source": "operator_attested_vpc_http_probe",
  "method": "GET",
  "path": "/ready",
  "checked_at_utc": "<UTC timestamp>",
  "targets": [
    {"id": "<task private IPv4>", "status_code": 200, "ready": true}
  ]
}
```

The target-group portion separately records `target_group_arn`, `source_apis=["DescribeTargetGroups","DescribeTargetHealth"]`, `checked_at_utc`, `health_check_enabled`, actual `health_check_path`, `healthy_count`, `unhealthy_count`, and each target's private `id`, `port`, and `health_state`. The validated result retains the resource ARN, source, timestamp, counts and aggregate states while omitting task IPs from public summaries.

## Deployment, human registration, and rollback

1. The `deploy` job rechecks the approval evidence and private template/readback before the only OIDC step. It assumes only the Group 2 deploy role, reads only fixed ECS service/task, ECR digest and secret version metadata, registers one task-definition revision with the exact digest/trust pins/disabled commercial gates/pinned DB secret version, and updates only the fixed service without changing desired count.
2. The workflow waits for service rollout, then waits for an encrypted, run-bound postdeployment operator snapshot. The generated one-run public key is shown in the run summary. On a private operator host, create the after snapshot using the same procedure as predeployment, repeat the target-group and every-task `/ready` checks, then encrypt it:

   ```bash
   umask 077
   python3 scripts/asaas_producer_rollout_evidence.py encrypt-post-readback \
     --public-key /private/path/run-public.pem \
     --snapshot /private/path/post-readback.json \
     --output /private/path/post-readback-encrypted.json \
     --run-id "$RUN_ID" --run-attempt 1 --operation-id "$OPERATION_ID" \
     --artifact-sha256 "$SIGNED_ENVELOPE_SHA256"
   ```

   Post only the ciphertext object under the exact marker `<!-- asaas-producer-post-readback.v1` and closing `-->` to the designated issue. The private key remains on the runner; the full snapshot is encrypted for that one run and never uploaded as an artifact.
3. In bootstrap mode, after the post-readback is accepted, a human staging `super_admin` submits the byte-identical Group 4 envelope to `POST /v1/admin/billing/producer-build-attestations` through the existing authenticated backend path. CI receives no JWT. Retain only the canonical `attestation_id`, `attestation_digest`, and `registered_at` response. Post one sanitized `asaas_producer_registration.v1` issue comment bound to the current run ID and operation ID, with `registrar_role="super_admin"`, `registrar_login` equal to the GitHub comment author, and the canonical response fields. The deploy job waits for this comment after the post-readback; timeout, mismatch, or failed registration causes rollback. Only resubmit the exact same envelope to recover an idempotent response.
4. In later `registered` mode, provide the exact canonical registration tuple in workflow inputs and retain its run-bound comment before approval. The no-OIDC verifier checks it against the exact signed envelope. No unregistered digest is accepted after the single bootstrap.
5. On any failed health/readiness/digest/trust/secret-version/registration check, the executor restores the pre-recorded task-definition ARN and waits for that service rollout. It never changes desired count or another service. A failed rollback returns a fixed failure marker; use the approved operator recovery procedure and do not attempt another bootstrap.

## IAM read boundaries

The supplemental metadata policy grants only these reads:

- `ecs:DescribeServices` on `arn:aws:ecs:us-east-1:388460142431:service/neurostack-staging/neurostack-staging-app`, conditioned on the exact `neurostack-staging` cluster ARN.
- `ecs:DescribeTasks` on `arn:aws:ecs:us-east-1:388460142431:task/neurostack-staging/*`, conditioned on that exact cluster ARN. The runner supplies task ARNs from the authenticated operator snapshot.
- `ecr:DescribeImages` on the exact producer ECR repository ARN.
- `secretsmanager:DescribeSecret` and `secretsmanager:ListSecretVersionIds` on the exact `DATABASE_URL` secret ARN. Neither API returns the value; `GetSecretValue` is absent.

The policy intentionally does not grant `ecs:ListTasks`, `ecs:DescribeTaskDefinition`, or ELB read actions. ECS service authorization metadata lists `container-instance` and `ecs:cluster` resource constraints for `ListTasks`, which does not fit this Fargate readback contract. The ECS authorization table gives `DescribeTaskDefinition` no resource type; a truthful IAM statement would therefore need `Resource: "*"`, not a task-definition ARN that appears scoped. The reviewed Group 2 wildcard allowlist does not permit that broad read, so an authenticated administrator supplies a run-bound, human-attested sanitized task-definition snapshot instead. ELBv2's `DescribeTargetHealth` has no supported resource type, so granting it to the workflow role would also require `Resource: "*"`; ALB readbacks remain operator-provided. See the AWS [ECS service authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazonelasticcontainerservice.html), [ECR service authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazonelasticcontainerregistry.html), [Secrets Manager service authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_awssecretsmanager.html), and [ELBv2 service authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_elbv2.html).

Human-provided snapshots are authenticated and checked against the exact run, source ref, operation, operator admin status, task/service identity, target-group ARN, image digest, task cohort, secret metadata, and timestamps. They are not cryptographically signed AWS responses and do not claim runner-independent AWS API verification.
