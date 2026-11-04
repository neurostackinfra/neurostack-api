# Staging image build (`.github/workflows/deploy.yml`)

How a new application image is produced for the staging service
`neurostack-staging-app`, and how it reaches the service. The workflow only
**builds, smokes and publishes** the image. Deploying it stays a manual,
reviewed operator step through the production wiring planner, exactly as for
live step L2b of asaas-rollout-opening ("The image bump", in
[`asaas-deployment.md`](./asaas-deployment.md)).

## Why it exists

Images had been built on an operator's machine and pushed by hand (the L2b
image `asaas-l1-20261102-248593b6`, for example). That depends on one person,
on a host with Docker and on a Linux checkout (a `/mnt/c` checkout in WSL
ships world-writable files). The protected
`asaas-build-attestation.yml` builds the signed producer image for the
rollout ceremony and is not meant for routine staging builds. This workflow
gives a reproducible build of any commit of `main`, smoked, with the digest
the planner needs.

## What it does

`Staging image build`, manual trigger (`workflow_dispatch`) from `main` only:

1. **Build and smoke** with `scripts/production_image_smoke.py`: the same
   `docker buildx build --platform linux/amd64 --load --target production`
   with `VCS_REF` set to the commit, then the image checks run offline
   (`--network none`): imports, the locked site-packages, no build
   toolchain, native libraries, no world-writable files, `src` imports, KMS
   signer factories, the billing fact bundle digest, the single Alembic head
   (the `expected_head` input), the SPA index, the offline migration SQL,
   and serving `/health`, `/` and `/v1/sessions` as the image's own user. A
   failed smoke pushes nothing.
2. **Push the smoked image by its image ID** (never rebuilt) to the ECR
   repository `neurostack-staging` with the tag
   `build-<YYYYMMDD>-<commit8>-<run id>` (tags are immutable there), then
   read the digest back from ECR.
3. **Summary** with the commit, tag, digest and the exact
   `app_image_override` line for the planner.

It never registers a task definition, updates the ECS service or runs a
migration.

## Access

The job assumes `arn:aws:iam::388460142431:role/neurostack-gha-app-deploy` by
OIDC. That role lives in the infrastructure repository
(`neurostack-infra-terraform`, `bootstrap/oidc.tf`); its trust accepts only
this repository, this file on `main`, with the repository's custom subject
template (`repo`, `context`, `ref`, `workflow_ref`):

```
repo:neurostackinfra@340698182/neurostack-api@1152335815:ref:refs/heads/main:ref:refs/heads/main:workflow_ref:neurostackinfra/neurostack-api/.github/workflows/deploy.yml@refs/heads/main
```

So:

- the file name `deploy.yml` is part of the trust: renaming the file breaks
  the build until the trust is updated (infrastructure repository variable
  `app_deploy_workflow`);
- the job declares no `environment:`: an environment changes the token's
  context and the role refuses it;
- a run from another branch is skipped (`if: github.ref == 'refs/heads/main'`)
  and could not assume the role anyway.

The role's policy (`deploy-neurostack-staging`) allows ECR push to
`neurostack-staging` and, for a future full pipeline, ECS register/update and
the migration task; this workflow uses only the ECR part.

## How to run it

1. GitHub → Actions → **Staging image build** → *Run workflow* on `main`.
2. `expected_head`: the Alembic head of the commit (`ls alembic/versions`,
   the highest revision; `042` on 2026-11-03).
3. Wait for the run; read the summary: tag, digest and the
   `app_image_override` line.

## From the image to the service (operator, each step with the owner's go)

This is the L2b procedure of `asaas-deployment.md`, with the new digest:

1. Copy `infra/production/asaas-production-wiring.params.yaml` to a private
   directory and set only `app_image_override` to the summary's line. Never
   commit it.
2. `python scripts/asaas_production_wiring.py plan --params <copy>`: expect
   `ready` with a single change, `app_image`, replacing the revision that
   serves. Any other change (flags, AppConfig group) means the copy or the
   repository file differs from what serves: stop.
3. `render-taskdef` into the private directory, compare it field by field
   with the serving revision (only the `app` image differs), register it,
   and check `plan` reports `registered_not_deployed:<ARN>`.
4. `aws ecs update-service ... --task-definition <ARN>`, wait for
   `COMPLETED`, check `/health`, the SPA and the app logs.
5. Run the post-deploy webhook hook (`scripts/asaas_deploy_hook.py`) and
   verify `registered`.

The way back is the previous revision (`update-service` to it).

## Staging on the ASAAS sandbox

Since the ASAAS environment is chosen by the API key's namespace
(`$aact_hmlg_` sandbox, `$aact_prod_` production; a sandbox key is refused
under `ENVIRONMENT=production`), moving staging to the sandbox is:

1. deploy an image that contains that change (this workflow, then the steps
   above);
2. put the sandbox key into the secret `neurostack/staging/asaas-api-key` and
   a new webhook token into `neurostack/staging/asaas-webhook-token` (the
   sandbox is another ASAAS account), without the values passing through any
   file, log or chat;
3. force a new deployment so the tasks read the new secrets, then run the
   post-deploy hook: `reconcile` registers the webhook on the sandbox
   account;
4. open financial egress in AppConfig and run the end-to-end test with the
   ASAAS sandbox test card.

## Production

The same workflow pattern serves production once `envs/prod` exists: a
second ECR repository and a role whose trust names its own workflow file (or
the same file with a target input and a separate role), with the production
key (`$aact_prod_`) in production's own secrets.
