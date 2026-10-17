# ASAAS bootstrap slot ledger

Implementation status: the claim described here is the target design and is not yet invoked by any workflow (task t_mvcn8tm75222ec86).

The S3 ledger is the durable authority for whether the one-time build and deployment bootstrap slots have been consumed. GitHub Actions run history is an additional identity and audit check; it is not the consumption record and cannot clear an S3 claim. A completed or failed attempt that successfully writes its claim consumes that slot permanently.

The release stack exposes the retained bucket name as `BootstrapSlotLedgerBucketName`. The bucket has versioning enabled, default Object Lock retention of 100 years in COMPLIANCE mode, SSE-S3 encryption, all four S3 Block Public Access controls enabled, and no lifecycle rules. The bucket and its bucket policy use `DeletionPolicy: Retain` and `UpdateReplacePolicy: Retain`.

## Fixed keys and role boundaries

| Release role | `s3:ListBucketVersions` prefix | `s3:PutObject` object ARN |
| --- | --- | --- |
| `BuildRole` | `slots/build-bootstrap.json` | `arn:aws:s3:::<ledger-bucket>/slots/build-bootstrap.json` |
| `DeployRole` | `slots/deployment-bootstrap.json` | `arn:aws:s3:::<ledger-bucket>/slots/deployment-bootstrap.json` |

Each role also has read-only access to the ledger bucket's versioning, Object Lock, encryption, public-access-block, bucket-policy, and lifecycle configuration. The bucket-level `ListBucketVersions` permission requires `StringEquals` on the role's single exact `s3:prefix`; each `PutObject` names only that role's exact object ARN. Neither release role can delete objects or versions, list the other slot, or change bucket settings or policy. Neither role receives `s3:PutBucket*`, `s3:DeleteBucket*`, `s3:DeleteObject`, or `s3:DeleteObjectVersion`.

The bucket policy requires `If-None-Match: *` for writes to either fixed object, denies deletion of both objects and their versions, and denies non-TLS requests. A conditional write alone is insufficient in a versioned bucket: S3 evaluates `If-None-Match` against the current version, so a current delete marker can allow a new write. The pre-claim check therefore reads every version and delete marker, and the bucket policy prevents release roles from creating delete markers or deleting versions.

## Readback before a claim

Before the first release-side AWS write, verify the stack output and read the live bucket controls. The bootstrap readback must capture a fresh timestamp and the complete results for:

1. `GetBucketVersioning`: status `Enabled`.
2. `GetObjectLockConfiguration`: Object Lock enabled; default retention `COMPLIANCE`, `Years: 100`.
3. `GetBucketEncryption`: default encryption algorithm `AES256`.
4. `GetPublicAccessBlock`: `BlockPublicAcls`, `BlockPublicPolicy`, `IgnorePublicAcls`, and `RestrictPublicBuckets` all `true`.
5. `GetBucketPolicy`: exactly the release template's TLS deny, per-key `If-None-Match: *` denies, and per-key object/version deletion denies.
6. `GetBucketLifecycleConfiguration`: no lifecycle rules. `NoSuchLifecycleConfiguration` is normalized as an empty rule list; any rule or read error fails closed.
7. `ListObjectVersions` for each fixed key, using the exact key as `Prefix`.

For a complete aggregate inspection, let the AWS CLI paginator follow every page:

```bash
aws s3api list-object-versions --bucket "$BUCKET" --prefix slots/build-bootstrap.json --output json
aws s3api list-object-versions --bucket "$BUCKET" --prefix slots/deployment-bootstrap.json --output json
```

For an auditable readback, retain every API page, including `IsTruncated`, the request and next `KeyMarker`/`VersionIdMarker` cursor pair, the returned `Versions`, and `DeleteMarkers`. Continue with the returned next cursor until a terminal page reports `IsTruncated: false`; hash cursor values in the normalized evidence rather than exposing any unrelated data. Do not filter the response before checking it. Every returned key must equal the requested fixed key, and the combined version and delete-marker item count must be zero. An unexpected key sharing the queried prefix also blocks the claim. A failed request, missing page, repeated cursor, nonterminal last page, or unverified bucket control blocks the claim.

To preserve individual pages instead of the aggregate result, request one page at a time with `--no-paginate`; when `IsTruncated` is `true`, repeat the request with the returned `NextKeyMarker` and `NextVersionIdMarker` values passed as `--key-marker` and `--version-id-marker`. Continue until the last response is terminal. Record both request-marker and next-marker digests for every page so the evidence proves the full cursor chain.

## Claim ordering and retries

GitHub Actions history must also be reviewed for the exact repository, workflow, ref, run, and attempt. It provides identity and audit context in addition to the ledger. Expired or deleted Actions history does not make a prior S3 claim available again.

After all read-only readiness, provenance, signature, configured-pin, bucket-control, history, and complete version-list checks pass, the conditional S3 claim is the first release-side AWS write. The caller creates the canonical claim document with `PutObject` and `If-None-Match: *`. The build flow claims before ECR publication or KMS signing; the deployment flow claims before ECS task-definition registration or service update. A claim write failure, an ambiguous conditional-write response, or any later failure consumes the slot if S3 committed the claim. Do not retry a claim using the same key after any version or delete marker exists.

A fresh UUID pre-claim retry is permitted only when all of the following are proven again:

- the exact slot key has no versions and no delete markers across every page;
- versioning, COMPLIANCE retention, encryption, public access blocking, bucket policy, no-lifecycle state, and the expected bucket identity all pass fresh readback;
- retained workflow history has no conflicting or ambiguous run/attempt;
- if an older pre-claim history entry has expired or was deleted, available independent records establish that it did not reach a claim attempt; otherwise the slot is blocked as ambiguous.

Any prior object version or delete marker, even one without readable content, means the slot is consumed and blocks reuse. Release-role controls cannot reset or remove a claim. Only privileged account administrators remain a trusted break-glass boundary; such access is outside the release-role contract and does not authorize reuse in the normal workflow.
