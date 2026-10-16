# ASAAS secret rotation proof

This repository does not rotate real ASAAS credentials. Rotation is a
production change-window action and must preserve fail-closed billing rollout.

Before promoting `next` to `current`, record a proof containing only SHA-256
fingerprints for current, next, deployed-current and the smoke artifact. The
proof must also include an authenticated candidate API-key validation receipt,
a webhook overlap start/end and readback bounded to 15 minutes, an explicit
next-to-current promotion receipt, revocation/readback evidence, and a
compromise-recovery receipt proving rollout was disabled and reconciliation
started. All five receipts belong to one rotation operation and must be
observed strictly in this order: candidate validation, webhook overlap,
promotion, revocation, compromise recovery. The overlap may start only after
the candidate key was proven; the revocation readback closes it, must fall
inside the signed overlap end, and the window measured from the receipts
(revocation minus overlap start) is at most 15 minutes. Reconciliation starts
from the last trusted instant, before the candidate validation. The deployed-current fingerprint must equal next, all fingerprints
must differ where appropriate, the smoke digest must validate, and no billing
incident may be open. Missing or stale evidence fails closed. Never write the
secret itself, a JWT, headers, or a request/response body to a receipt or
ticket.

If the proof fails or an incident begins, use the incident runbook to leave
rollout `disabled`. A new receipt bundle is required after every failed proof;
reuse of a nonce or assignment is rejected.
