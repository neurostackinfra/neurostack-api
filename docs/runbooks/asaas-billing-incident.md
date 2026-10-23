# ASAAS billing incident and rollout lock

An incident is a containment event. Do not retry checkout creation, infer a
provider outcome, change a provider binding, or enable top-up while an incident
is open.

1. Collect only opaque IDs, state, bounded metrics, and SHA-256 digests. Do
   not attach JWTs, webhook tokens, provider payloads, customer data or
   screenshots.
2. Create a three-person signed `incident` operation with `to_state=disabled`.
   Submit its deployer, approver and reviewer receipts.
3. Call `/v1/admin/billing/rollout/incident-disable`. It can only transition
   to `disabled`; the current rollout revision prevents a stale receipt from
   overwriting later containment.
4. Review `BillingReadbackBacklog` and `BillingOperationReceiptRejected`.
   Resolve every exact-ID readback before considering a new rollout
   operation.
5. For a top-up health or revocation event, call only
   `/v1/admin/billing/topup-capability/disable` with reason `health` or
   `revocation`. No other endpoint may turn top-up off.

Rollback remains disabled until a new, independent signed rollout transition
passes its receipts and revision checks. A production deployment or provider
operation remains outside this local procedure.
