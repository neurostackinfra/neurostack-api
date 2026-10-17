# ASAAS producer build trust bootstrap

This runbook documents the one-time build-trust bootstrap for Group 3/4. The
`asaas-build-attestation.yml` workflow may validate trust readiness, build and
publish one immutable image digest, sign its canonical
`billing_build_attestation.v1` envelope, and upload that artifact. It never
deploys or registers the attestation. Group 5 uses a separate protected deploy
workflow and role.

This sequence follows the SHIP-approved plan review recorded in WISH SHA
`eb217bef741981537e6b6277242e0fb7fccd05b3e8b5e216768d7c7926d8ccb8`.

The build bootstrap is not the deployment bootstrap. Group 5 permits exactly one
protected deployment of the exact Group 4 signed digest before backend
registration, followed immediately by human registration of that identical
envelope. Every later deployment requires canonical registration before its
protected deployment approval.

## Committed build-trust pins

Bootstrap pins the build identity to the reviewed workflow commit and to the
read-only KMS inventory recorded on 2026-10-15. The public-key fingerprint is
SHA-256 over the KMS SubjectPublicKeyInfo DER:

| Pin | Value |
| --- | --- |
| Backend origin | `https://staging.api.neurostack.global` |
| BuildRole | `arn:aws:iam::388460142431:role/neurostack-asaas-producer-release-BuildRole-ptpxs3j9NIZM` |
| KMS signing key | `arn:aws:kms:us-east-1:388460142431:key/6a74b35d-b97f-4a0a-9269-240a762d633b` |
| Attestation issuer | `github-actions:neurostack-build` |
| Public-key fingerprint | `86d440d5c7f2248499bd6fefc37064ec4b5eae2fae0b3aebb30c43819bbd384b` |
| ECR repository | `388460142431.dkr.ecr.us-east-1.amazonaws.com/neurostack-asaas-producer-release-producerimagerepository-jqtdjqst9o1j` |
| Application audience | `billing-api:producer-build-attestation` |

The repository variable `BILLING_PRODUCER_BUILD_PUBLIC_KEY_PEM` must contain the
public key returned by the administrative KMS `GetPublicKey` readback. The
workflow checks its fingerprint against the committed value before any OIDC
request. Bootstrap also pins the issuer to `github-actions:neurostack-build`.
The build role remains restricted to ECR publication/readback and `kms:Sign`;
the build workflow does not call `GetPublicKey`, ECS, Secrets Manager, or
payment services.

Normal mode reads the active public trust tuple from
`GET /v1/billing/producer-build-trust`. The exact GET paths
`/v1/billing/producer-build-trust` and
`/v1/admin/billing/producer-build-trust` are both public middleware exceptions
and require no JWT. Each returns only non-secret verification pins and uses
`no-store` cache headers; the public path returns the verifier PEM and fresh
read timestamp, while the legacy administrative path preserves its
fingerprint-only schema and staging-only response. Do not infer authentication
from the `/admin` prefix. Adjacent paths, methods, or suffixes remain protected
by authentication middleware.

Bootstrap mode probes both exact public readback paths without a JWT and
accepts only when both return the known legacy `401` body with no redirect. This
is named `readback_unavailable`: it shows only that neither path exposes the
readback on this request. It does not establish whether trust exists elsewhere
in the backend or infer the route code from the image digest. This response is
only the one-time bootstrap exception, and the probe must not be relaxed. A
`200` from either path, differing responses, `404`, `503`, redirects, malformed
or ambiguous responses, and every other status fail closed.

## Group 3/4 build bootstrap

1. Merge the reviewed commit through the protected `main` ruleset. Before any
   OIDC-enabled job runs, the no-OIDC preflight re-reads the active source-ref
   ruleset, build and deployment environment settings, allowed ref, and exact
   Actions run identity. The ruleset must require a PR review and named checks,
   block force-push/deletion, and have no bypass actors. The environment
   approval must be bound to this run and ref; self-review and administrator
   bypass remain disabled. An administrator separately verifies the
   hidden-bypass controls in the GitHub UI and retains sanitized evidence.
   Felps (`felps-json`) remains the sole independent PR reviewer and the
   current protected-environment reviewer required by live settings. Lorenzo
   performs final PR approval and merge. Add no other reviewers or approvers,
   and do not change settings to bypass last-push review.
2. Use `trust_mode=bootstrap` only for Group 3/4 trust/build readiness, with a
   newly allocated operation UUID and the explicit operational authorization
   at `asaas-producer-image-rollout/WISH.md#2026-10-15-separate-build-and-rollout-plan-approved`.
   The run title binds the UUID; concurrency and a complete, paginated
   workflow-run history check reject reuse. A failed or retried build attempt
   consumes that operation UUID. The protected `asaas-build` environment
   still requires approval for the exact run.
3. Before OIDC, bootstrap preflight probes only the committed HTTPS backend
   origin and both exact public readback paths. The probe does not infer
   authentication from the `/admin` prefix: both exact paths are public. The
   one-time bootstrap exception requires the known legacy `401` body from both
   paths, without redirects or divergent responses. This reports only
   `readback_unavailable`; it does not prove that trust is absent or infer
   route code from the serving image digest. A `200` from either path, `404`,
   `503`, redirect, malformed or ambiguous response, timeout, or any other
   status blocks bootstrap.
   The canonical probe digest is included in evidence. After environment
   approval and before `configure-aws-credentials`, the verifier repeats the
   probe and requires the same digest.
4. Only the approved build/sign job receives `id-token: write`. It assumes the
   committed BuildRole, builds and publishes a run-specific ECR tag, reads
   back the immutable image digest, and signs/verifies an envelope binding the
   digest to the clean reviewed commit, tree, workflow and run. Upload the
   canonical artifact for Group 5. Group 4 publishes and signs only: it does
   not register the attestation, deploy the image, or enable payment egress.
   CI never receives a user JWT.

## Group 5 one-time deployment bootstrap

The replacement image is production-impacting: the service named
`neurostack-staging` currently serves production traffic. Keep ASAAS/credit
egress and commercial top-up disabled before, during, and after the bootstrap.
Use the distinct deploy role; the build role cannot deploy, and the deploy role
cannot publish images or sign attestations.

1. Before deployment approval, the no-OIDC deploy preflight re-reads the live
   source-ref and `asaas-production-deployment` protections and exact run
   identity. The environment must allow only the protected release branch,
   require its designated human approval, prevent self-review, and disable
   administrator bypass. A live UI check confirms hidden bypass controls.
   Felps (`felps-json`) remains the sole independent PR reviewer and the
   current protected-environment reviewer required by live settings. Lorenzo
   performs final PR approval and merge. Add no other reviewers or approvers,
   and do not change settings to bypass last-push review. No AWS OIDC
   credentials are available to the readback job. This protected run must also
   reference the explicit operational authorization at
   `asaas-producer-image-rollout/WISH.md#2026-10-15-separate-build-and-rollout-plan-approved`.
2. Before protected deployment approval, require the checked-in Group 5
   registration-readiness SQL query and its static safety test. If either is
   absent, withhold approval; do not substitute ad hoc SQL. Obtain current
   running task ENI addresses through read-only ECS metadata and run the
   no-write query from the existing authenticated PostgreSQL console/session.
   It must map those task
   connections to the API runtime role using `pg_stat_activity` and verify
   the registration table, required columns/constraints, and the role's
   read/insert/row-lock privileges through catalogs. It must not use ECS Exec,
   retrieve or print `DATABASE_URL` or any secret, read application rows or
   query text, run migrations, or issue DML. If the runtime role or required
   schema/privileges cannot be confirmed without reading a secret, withhold
   deployment approval and leave the bootstrap slot unused. Schema remediation
   requires separate approval.
3. Prove the currently loaded `DATABASE_URL` version from metadata before
   deployment approval. Read every running task's task-definition ARN,
   `createdAt`, and `startedAt`; inspect the secret's ARN, JSON-key selector,
   `VersionIdsToStages`, and `LastChangedDate` with read-only metadata calls.
   Never call `GetSecretValue`. For a stage/default selector, resolve exactly
   one VersionId and prove `LastChangedDate` precedes the earliest active
   task's `createdAt` by at least 60 seconds, preserving source timestamp
   precision. `createdAt` is the conservative `PENDING`-entry bound before
   startup secret injection. A change after `createdAt` but before
   `startedAt`, equal/rounded/ambiguous timestamps, a margin under 60 seconds,
   mixed active versions, or an unresolved selector blocks deployment. For an
   explicit VersionId, prove that exact version exists. Pin the replacement
   task definition to the same secret ARN, JSON key, and proven immutable
   VersionId. Record no secret value or database URL.
4. Bootstrap mode is available for exactly one started protected deployment
   job. A distinct preallocated deployment operation UUID binds the run to the
   exact Group 4 artifact tuple: attestation ID, immutable image digest, build
   run ID, and signed-envelope SHA-256. This operation ID is deployment
   metadata; it is not added to the signed envelope. A serialized, paginated
   history check fails closed if any earlier bootstrap deployment job started
   or if identity/history is ambiguous. Pending or rejected-before-start
   environment records do not consume the slot; once the protected job starts,
   any failure or cancellation consumes it. No retry or second unregistered
   bootstrap is allowed.
5. After the deployment environment approval, independently verify the exact
   signed envelope, trust claims, build run/ref, ECR digest, and configured
   public-key pin before requesting deploy-role OIDC. Change only the pinned
   production-serving service and image digest; preserve desired count and
   unrelated resources. Pin the replacement task definition to the verified
   trust settings and the proven active secret version. Read back the entire
   running task cohort, readiness, immutable digest, task role, trust settings,
   target, desired count, and disabled financial/commercial gates.
6. Immediately after successful healthy readbacks, a human staging
   `super_admin` registers the exact Group 4 envelope through
   `POST /v1/admin/billing/producer-build-attestations`. Confirm that the
   backend returns the same canonical attestation ID, envelope digest, and
   registration time. CI receives no user JWT; the human registration uses
   the established out-of-band authentication flow. If the response is lost,
   only the byte-identical envelope may be resubmitted for its idempotent
   result. Do not use a different envelope, direct database writes, or delayed
   registration.
7. Any signature, trust, digest, health, cohort, secret-version, or registration
   mismatch stops acceptance and rolls back to the recorded prior task
   definition using the pre-approved rollback path. Keep payment and commercial
   gates disabled through rollback and recovery. Once this bootstrap has
   started, a failed run does not reopen the one-time slot; another unregistered
   deployment requires a separately revised and explicitly approved WISH.

## Later deployments

Every deployment after the one-time bootstrap uses registered mode. Before its
protected deployment approval, a human staging `super_admin` submits the exact
signed envelope to the authenticated idempotent registration endpoint and
retains the canonical attestation ID, digest, and timestamp. The deploy
preflight requires that registration to match the immutable digest/run tuple.
No later deployment accepts an unregistered digest, and CI never receives a
user JWT.
