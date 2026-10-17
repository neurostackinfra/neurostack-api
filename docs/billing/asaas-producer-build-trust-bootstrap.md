# ASAAS producer build trust bootstrap

This runbook documents the one-time build-trust bootstrap for Group 4. The
`asaas-build-attestation.yml` workflow may validate trust readiness, build and
publish one immutable image digest, sign its canonical
`billing_build_attestation.v1` envelope, and upload that artifact. It never
deploys or registers the attestation. Group 5 uses a separate protected deploy
workflow and role.

This sequence follows the current WISH and DESIGN. Their review status changes
as amendments land; see `asaas-producer-image-rollout/WISH.md` for the current
review record.

The build bootstrap is not the deployment bootstrap. Group 2 owns the durable
S3 slot ledger design and its exact role scopes, claim ordering, and
failure/retry readbacks; see `docs/billing/asaas-bootstrap-slot-ledger.md`.
The release template defines the ledger bucket and
`scripts/asaas_bootstrap_slot.py` implements the claim, but no workflow invokes
it yet (see Implementation status below). Group 5 permits
exactly one protected deployment of the exact Group 4 signed digest before backend
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
`GET /v1/billing/producer-build-trust`. That exact GET is the unauthenticated
readback and returns non-secret verification pins, including the public-key
fingerprint and fresh read timestamp, with `no-store` headers; it does not
return the PEM. The separate exact `GET /v1/admin/billing/producer-build-trust`
also currently has a no-JWT readback exception and preserves its
fingerprint-only schema and staging-only response. Do not infer authentication
from the `/admin` prefix. Adjacent paths and suffixes remain protected. Other
methods on these readback paths are not public operations; global OPTIONS
preflight handling remains unchanged.

Bootstrap mode probes only the pinned public endpoint
`GET /v1/billing/producer-build-trust`, without a JWT. It accepts only HTTP
`503`, `Content-Type: application/json`, and the exact body
`{"detail":{"error":"producer_build_trust_unavailable"}}`, with no redirect.
This is named `readback_unavailable`: it shows only that the trust readback was
unavailable on this request. It does not establish whether backend trust exists
or is absent. A `200`, `401`, `404`, any other status, changed URL, timeout,
malformed or ambiguous body, and every redirect fail closed. This is only the
one-time bootstrap exception, and the probe must not be relaxed.

## Group 4 build bootstrap

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
2. Use `trust_mode=bootstrap` only for Group 4 trust/build readiness, with a
   newly allocated operation UUID and the explicit operational authorization
   at `asaas-producer-image-rollout/WISH.md#2026-10-15-separate-build-and-rollout-plan-approved`.
   The run title binds the UUID; a workflow-wide concurrency group serializes
   every build run even when operation UUIDs differ. A complete, paginated run
   and job-history check uses authenticated Actions reads and checks exact run
   identity. It rejects UUID reuse in both normal and bootstrap modes and fails
   closed on conflicting or ambiguous retained history, incomplete pagination,
   or unreadable history. For bootstrap, it reads the job list of every run
   attempt of each earlier bootstrap run. An earlier bootstrap run is
   non-conflicting only when the run is `completed` and, in every attempt, its
   single build/sign job record is `completed` with conclusion `cancelled` or
   `skipped` and an explicit `started_at: null`. Whether GitHub ever reports
   that explicit `started_at: null` shape (for example for a skipped
   build/sign job, or a run cancelled before that job started) is UNVERIFIED
   against live GitHub. Until it is verified, treat a bootstrap run that
   created any build/sign job as having consumed the slot. Any non-null
   `started_at` blocks regardless of conclusion. A non-terminal run or job, an
   omitted `started_at`, a missing or duplicate build/sign record, or any
   other conclusion is ambiguous and blocks.
   Implementation status: neither `asaas-build-attestation.yml` nor
   `scripts/asaas_producer_deploy_executor.py` performs the S3 claim yet (task
   t_mvcn8tm75222ec86). Until it does, do not dispatch `trust_mode=bootstrap`
   or deploy `bootstrap` mode; the only enforced guard is retained Actions
   history: the build preflight blocks when any attempt of an earlier
   bootstrap build run has a started build/sign job, and the deploy gate
   blocks when the latest attempt of an earlier bootstrap deployment run has a
   started deploy job (it does not yet read earlier attempts). Never re-run a
   bootstrap run. The rest of this paragraph is the target design, not current
   behavior. In the target design, S3 claim success, not job start, is the
   durable slot consumption event. A successful claim consumes the slot even
   if later build or signing steps fail or are cancelled. A fresh UUID retry
   may proceed after a pre-claim failure only when complete
   version/delete-marker pagination proves the fixed key is empty under
   verified bucket controls and claim is guaranteed to be the first
   release-side AWS write, even if an older pre-claim Actions record expired
   or was deleted. Any prior S3 claim blocks regardless of history. Retained
   authenticated Actions history is an additional audit/identity check and
   cannot reset the claim. The protected `asaas-build` environment still
   requires approval for the exact run.
   Before either approval, make a private ZIP containing `snapshot.json` at the
   archive root and every artifact referenced by it at the same relative paths.
   Do not include symlinks. Keep the bundle within the verifier's 64 MiB
   download and 128 MiB unpacked limits. Upload that ZIP as a GitHub user
   attachment and record its exact URL and SHA-256; the protected approval
   comment binds that digest to the reviewer, run, ref, SHA, workflow,
   environment, and preflight readback. The no-OIDC verifier downloads the
   attachment, checks its digest and ZIP paths, extracts into a private
   temporary directory, and runs the full Group 2 snapshot/artifact validator.
   The build job downloads and validates the same bundle again before requesting
   OIDC. A locally rehashed snapshot alone is not evidence of origin. The
   standalone evidence validator and the approval verifier pin the canonical
   separate read-only AWS MCP observation. Removing a region from the raw
   response and matching scans still fails even if all local hashes or both
   recomputed. If AWS changes the region catalog or opt-in statuses, the gate
   remains closed until a fresh independent response is collected and its
   digest is updated through reviewed code. The repository and its user
   attachments are public: sanitize the
   bundle and screenshots and exclude credentials, tokens, customer data, and
   other secrets before uploading.
   The `asaas-build` environment has two distinct approvals. For the first,
   inspect and capture the ruleset and both environment settings, then package
   the three screenshots with `--approval-gate evidence`, the run identity, and
   the preflight SHA, the Group 2 bundle URL, and the bundle SHA-256. Attach that
   manifest and all three PNGs, and put its
   compact JSON in the first environment approval comment. The no-OIDC verifier
   requires this package timestamp to fall between preflight and five minutes
   before the no-OIDC job starts.
   After that job completes successfully, repeat the UI inspection and capture
   all three screenshots; the verifier checks the second timestamp is at or
   after the no-OIDC job's recorded completion time. The second screenshots
   must have new bytes, even if a new timestamp or attachment URL is supplied.
   Run the packer again with `--approval-gate build`; this creates
   a separate private package and requires a distinct timestamp and attachment
   URLs. Reuse the same Group 2 URL and digest. Attach the new manifest and PNGs and use its compact JSON in the second
   environment approval comment. The build verifier requires exactly one
   `evidence` and one `build` package, verifies both complete chains, checks the
   first against the no-OIDC outputs, and requires the second timestamp to be no
   more than five minutes before the build job starts. Do not reuse the first
   comment, timestamp, manifest, or attachment URLs for the second approval.
3. Before OIDC, bootstrap preflight probes only
   `GET /v1/billing/producer-build-trust` at the committed HTTPS backend
   origin. The one-time bootstrap exception requires HTTP `503`, JSON content
   type, and the exact body
   `{"detail":{"error":"producer_build_trust_unavailable"}}`, without any
   redirect. This reports only that readback is unavailable on this request;
   it does not prove that backend trust is absent. A `200`, `401`, `404`, any
   other status, changed URL, malformed or ambiguous body, timeout, or any
   redirect blocks bootstrap.
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
4. Implementation status: neither `asaas-build-attestation.yml` nor
   `scripts/asaas_producer_deploy_executor.py` performs the S3 claim yet (task
   t_mvcn8tm75222ec86). Until it does, do not dispatch `trust_mode=bootstrap`
   or deploy `bootstrap` mode; the only enforced guard is retained Actions
   history: the build preflight blocks when any attempt of an earlier
   bootstrap build run has a started build/sign job, and the deploy gate
   blocks when the latest attempt of an earlier bootstrap deployment run has a
   started deploy job (it does not yet read earlier attempts). Never re-run a
   bootstrap run. The rest of this step is the target design, not current
   behavior. A distinct preallocated deployment operation UUID binds the run
   to the exact Group 4 artifact tuple: attestation ID, immutable image
   digest, build run ID, and signed-envelope SHA-256. This operation ID is
   deployment metadata; it is not added to the signed envelope. In the target
   design, bootstrap mode permits one successful durable S3 deployment claim;
   retained authenticated paginated history checks exact identity and blocks
   conflicting or ambiguous records, and once the claim is enforced it no
   longer serves as the consumption record or resets the slot. After
   read-only checks, the deploy role will fully read versions and delete
   markers and conditionally write its claim as the first release-side AWS
   write, before ECS mutation. A fresh UUID
   retry may proceed if complete version/delete-marker pagination proves the
   key empty under verified controls and claim is the first release-side AWS
   write, even if an older pre-claim Actions record expired or was deleted.
   Conflicting or ambiguous retained history, incomplete readback/pagination,
   or changed controls blocks; any prior claim blocks regardless of history.
   A failure after claim consumes the slot permanently.
5. After the deployment environment approval, independently verify the exact
   signed envelope, trust claims, build run/ref, ECR digest, and configured
   public-key pin before requesting deploy-role OIDC. Change only the pinned
   production-serving service and image digest; preserve desired count and
   unrelated resources. Pin the replacement task definition to the verified
   trust settings and the proven active secret version. Read back the entire
   running task cohort, readiness, immutable digest, task role, trust settings,
   target, desired count, and disabled financial/commercial gates.
6. Immediately after successful healthy readbacks and before accepting any
   canonical registration, the deploy job makes a bounded unauthenticated GET
   to the pinned
   `https://staging.api.neurostack.global/v1/billing/producer-build-trust`
   endpoint. It rejects redirects, non-200 responses, invalid content types,
   oversized, duplicate-key, malformed, stale, or incomplete JSON, and compares
   the complete trust tuple and public-key fingerprint with the configured
   pins. It then verifies the byte-identical signed envelope and exact image
   digest again using that runtime-returned trust. Only the sanitized trust
   tuple SHA-256 and read timestamp are retained. If the endpoint is unavailable,
   any pin differs, or signature/digest verification fails, the existing
   post-update exception path rolls back to the recorded prior task definition
   and exits non-zero with `rollout_failed_rolled_back:<original reason>`; it
   must not wait for or accept human registration.
7. Register only while the deploy job is still running; if it has failed with
   `rollout_failed_rolled_back:…`, do not register. A human staging
   `super_admin` registers the exact Group 4 envelope through
   `POST /v1/admin/billing/producer-build-attestations`. Confirm that the
   backend returns the same canonical attestation ID, envelope digest, and
   registration time. CI receives no user JWT; the human registration uses
   the established out-of-band authentication flow. If the response is lost,
   only the byte-identical envelope may be resubmitted for its idempotent
   result. Do not use a different envelope, direct database writes, or delayed
   registration.
8. Any runtime trust, signature, digest, health, cohort, secret-version, or
   registration mismatch stops acceptance and rolls back to the recorded prior
   task definition using the pre-approved rollback path. Keep payment and
   commercial gates disabled through rollback and recovery. Never re-run a
   bootstrap run. Today the deploy gate blocks a later bootstrap deployment
   only when the latest attempt of an earlier bootstrap deployment run has a
   started deploy job (it does not yet read earlier attempts; after the S3
   claim is implemented, a successful claim consumes the slot). Treat a failed
   bootstrap deployment as having used the one-time slot; another unregistered
   deployment requires a separately revised and explicitly approved WISH.

## Later deployments

Every deployment after the one-time bootstrap uses registered mode. Before its
protected deployment approval, a human staging `super_admin` submits the exact
signed envelope to the authenticated idempotent registration endpoint and
retains the canonical attestation ID, digest, and timestamp. The deploy
preflight requires that registration to match the immutable digest/run tuple.
No later deployment accepts an unregistered digest, and CI never receives a
user JWT.
