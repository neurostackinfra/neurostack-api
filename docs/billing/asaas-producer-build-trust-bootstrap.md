# ASAAS producer build trust bootstrap

This runbook covers the one-time first build while staging does not yet serve
`GET /v1/admin/billing/producer-build-trust`. The default workflow mode is
`normal`; it always requires a live canonical backend trust readback before the
build job can request AWS credentials. Bootstrap is an explicit, single
operation exception for build-trust initialization. This workflow never deploys.

## Committed pins

Bootstrap pins the build identity to the reviewed workflow commit and to the
read-only KMS inventory recorded on 2026-10-15. The public key fingerprint is
SHA-256 over the KMS SubjectPublicKeyInfo DER:

| Pin | Value |
| --- | --- |
| Backend origin | `https://staging.api.neurostack.global` |
| BuildRole | `arn:aws:iam::388460142431:role/neurostack-asaas-producer-release-BuildRole-ptpxs3j9NIZM` |
| KMS signing key | `arn:aws:kms:us-east-1:388460142431:key/6a74b35d-b97f-4a0a-9269-240a762d633b` |
| Attestation issuer | `github-actions:neurostack-build` |
| Public key fingerprint | `86d440d5c7f2248499bd6fefc37064ec4b5eae2fae0b3aebb30c43819bbd384b` |
| ECR repository | `388460142431.dkr.ecr.us-east-1.amazonaws.com/neurostack-asaas-producer-release-producerimagerepository-jqtdjqst9o1j` |
| Application audience | `billing-api:producer-build-attestation` |

The repository variable `BILLING_PRODUCER_BUILD_PUBLIC_KEY_PEM` must contain the
public key returned by the administrative KMS `GetPublicKey` readback. The
workflow checks its fingerprint against the committed value before any OIDC
request. The build role remains restricted to ECR publication/readback and
`kms:Sign`; the workflow does not call `GetPublicKey`, ECS, Secrets Manager, or
payment services.

## Bootstrap sequence

1. Merge the reviewed commit through the protected `main` ruleset. The
   preflight reads the ruleset and both protected environments, rejects
   bypasses, and requires the exact `main` branch policy. Bootstrap is allowed
   only for rollout group `4`, run attempt `1`, the committed repository,
workflow, role, KMS key, issuer, audience, ECR repository, and public-key
fingerprint. The issuer is fixed to `github-actions:neurostack-build`, the
canonical value used by the backend trust contract; a different GitHub
variable is rejected. This is the committed trust choice, not a claim that
the active rev8 runtime was independently read to confirm its setting.
2. Dispatch `asaas-build-attestation.yml` with `trust_mode=bootstrap` and a
   newly allocated operation UUID. The run name binds the UUID; a serialized
   concurrency group and complete workflow-run history check reject a prior
   run for that operation. Any failed or retried bootstrap attempt consumes
   that operation UUID. Use only the explicit operational WISH authorization
   reference
   `asaas-producer-image-rollout/WISH.md#2026-10-15-separate-build-and-rollout-plan-approved`;
   every run still requires approval in the protected `asaas-build`
   environment. The WISH reference is the recorded operational authorization;
   the environment approval must bind to the exact run. Do not carry an
   environment approval from a failed attempt to a new operation.
3. Before OIDC, the preflight probes only the committed HTTPS backend origin
   and exact trust path. It accepts only the staging app's known unauthenticated
   `401` response shape as evidence that the public route is not deployed yet.
   A `200`, `404`, redirect, malformed body, timeout, or any other response
   blocks bootstrap. The probe's canonical SHA-256 is included in the review
   evidence. After the `asaas-build` required reviewer approves the run, the
   approval verifier repeats the probe and requires the same digest before
   `configure-aws-credentials`.
4. Only the approved `build-attestation` job receives `id-token: write`. It
   assumes the committed BuildRole, builds and pushes a run-specific image tag,
   reads back and validates the immutable ECR digest, then signs and locally
   verifies a `billing_build_attestation.v1` envelope. The signed artifact is
   uploaded as the Group 4 artifact. This workflow has no deployment step and
   no ECS permission; do not use the BuildRole to deploy.
5. The rollout WISH requires Group 4 to register the canonical attestation
   before Group 5 deploys that registered digest. The current sequence is a
   **hard stop** after artifact creation: the active `neurostack-staging`
   service's rev8 task definition has no `BILLING_PRODUCER_BUILD_*` settings,
   and the live trust route is not available. The backend's
   `_build_attestation_entry()` therefore cannot read a configured trust tuple
   there, and the human `register-build-attestation` command cannot establish
   registration/readback before Group 5. Do not start Group 5, deploy an
   unregistered digest, or defer registration until after deployment.
6. Resuming requires a separately reviewed and explicitly authorized plan
   that makes the exact build-trust tuple and both the readback and registration
   routes available on a backend before Group 5. The required tuple includes
   the committed issuer, KMS key ID, audience, public key, repository ID/name,
   workflow path/ref, and OIDC issuer/subject. That plan must define how this
   pre-deploy backend capability is provided without using the unregistered
   application digest as its own prerequisite. No such path is implemented or
   authorized by this bootstrap. Keep payment egress and commercial top-ups
   disabled while blocked.
7. Only after Group 4 registration and trust readback are independently
   verified may a separately approved Group 5 use the registered immutable
   digest with its distinct deploy identity. Receipts and grants remain blocked
   until their own WISH criteria pass. Normal build mode continues to require
   an active backend trust readback; bootstrap does not provide a post-deploy
   registration exception.

Bootstrap does not create or register backend trust, and it does not enable
payments. If any pin, approval, probe, signature, digest readback, or backend
trust field differs, stop. Resolve the hard-stop dependency through an amended,
explicitly authorized plan; do not weaken the Group 4 registration gate.
