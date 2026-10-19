# ASAAS sandbox card capture

`scripts/asaas_sandbox_card_capture.py` records what the **ASAAS sandbox**
(`https://api-sandbox.asaas.com/v3`) returns for a `CREDIT_CARD` payment: which
date fields are set when the payment is `CONFIRMED`, whether it ever becomes
`RECEIVED`, what the first payment of a card subscription looks like, and what
a partial and a full refund change.

> **Scope.** The output is evidence for a design question only. It is not a
> production gate. The tool is separate from the production `AsaasClient`,
> imports nothing from `src/`, and registers no webhook or callback URL.

## Before a live run

A live run (`--run`) needs two things from the **owner**:

1. **The owner's sandbox key.** The owner puts it in an untracked `.env` file
   as `ASAAS_SANDBOX_API_KEY`. The tool never asks for a key any other way.
2. **The owner's go-ahead for that run.** A live run creates real objects in
   the owner's sandbox account, and some of them cannot be removed (see
   [Cleanup](#cleanup-and-what-stays-behind)). A dry run and `--discover-only`
   create nothing, but `--discover-only` still calls the API with the owner's
   key, so it needs the go-ahead too.

## Safety model

- The origin is fixed to `https://api-sandbox.asaas.com`. There is no flag to
  change it. A request hook refuses any request with another scheme, host,
  `Host` header, port, userinfo or path. While a run is active, a process
  audit hook also refuses:
  - any DNS lookup of another host;
  - any connect, `sendto` or `sendmsg` to an IP address that the sandbox host
    did not resolve to in this run.
- The key is read by parsing only the `ASAAS_SANDBOX_API_KEY` line of one
  `.env` file. The file is never sourced or exported. It is opened without
  following a final symlink, and non-blocking, so a FIFO cannot hang the tool.
  It must be a regular file owned by you. A UTF-8 BOM is accepted, and bytes
  that are not valid UTF-8 never crash the tool.
- The key must start with `$aact_hmlg_`, and anything containing `aact_prod`
  is refused. The output only ever shows `is_sandbox_key: true/false`.
- If group or others can read or write the `.env` file, the `guard` line shows
  `env_file_mode_warning: true`. This is a **warning, not a refusal**, because
  files on a WSL `/mnt/c` mount always report mode 0777.
- The only card used is ASAAS's documented public sandbox approval card,
  `4444 4444 4444 4444`, with any future expiry and CCV `123`
  (<https://docs.asaas.com/docs/faq-sandbox>). The customer is synthetic: an
  algorithmic CPF, an `asaas-capture+<random>@example.com` email and the
  sample address from the ASAAS API reference.
- Every object the tool creates carries `neurostack-capture-<random>` as its
  `externalReference` and in its description. Before any refund or delete, the
  object is read back. It must carry this run's tag, or be a payment of a
  subscription that carries it. Objects created by the owner in the panel are
  only ever read.
- Requests are spaced at least 1 s apart. The status poll lasts at most 420 s,
  including request latency. It stops early when every payment is `RECEIVED`
  or when nothing has changed for 300 s.

## What the output can contain

Output is JSON lines. The tool prints these values and nothing else:

- dates, and timestamps with an offset;
- truncated ids (`pay_...(len=N)`);
- booleans;
- amounts, but only in amount fields (`value`, `netValue`, `requested_value`,
  and so on);
- numbers, but only in count fields (`count`, `totalCount`, `http_status`,
  `elapsed_s`, the date relations, and so on);
- words, `snake_case` and `kebab-case` values only from fixed lists: the
  identifiers spelled out in the tool's own source, plus the ASAAS statuses
  and the ASAAS error codes it lists. An unknown status or error code prints
  as `<refused>`. Error codes come with a fixed `reason` such as
  `partial_refund_next_day_only`, `delete_only_pending_or_overdue` or
  `unmapped`;
- field names, but only the tool's own names or lowercase camelCase ASAAS
  names (so `Owner_Joao` prints as `<refused>`).

**Provider free text is never printed.** That covers error descriptions,
names, emails, URLs and card data. Every string goes through three steps, in
this order:

1. It is scrubbed of the key and of every generated customer or card value.
   Matching is longest first and case-insensitive.
2. It is dropped if it contains `$aact_` or any 12 consecutive characters of
   the key.
3. It is printed only if it fits the rules above. Anything else prints as
   `<refused>`. Numbers are scrubbed the same way.

A failing output stream, such as a closed terminal or a dead `| tee`, never
stops the run. The records are kept in memory, and the capture file's
`emit_errors` field reports how many lines could not be printed and why the
first one failed.

## Commands

From the repository root:

```bash
# Dry run (default): prints the plan, a key-shape boolean and the .env mode
# warning, no network call. Exits 2 if the key check fails.
poetry run python -m scripts.asaas_sandbox_card_capture --env-file /path/to/.env

# Read-only discovery of existing customers, subscriptions and card payments
poetry run python -m scripts.asaas_sandbox_card_capture --run --discover-only \
    --env-file /path/to/.env

# Full live capture (discovery, then create, poll, refund, clean up)
mkdir -m 700 -p ~/.asaas-capture
poetry run python -m scripts.asaas_sandbox_card_capture --run \
    --env-file /path/to/.env --capture-file ~/.asaas-capture/capture.json
```

`--partial-refund-prior` (with `--run`) also tries one partial refund on a
CONFIRMED one-off payment left by an earlier run. That payment must carry
exactly `neurostack-capture-<16 lowercase hex>`. Owner objects are still
refused.

Every create and refund step records its instant in UTC and in
America/Sao_Paulo (`create_instant`, `refund_instant`):

- `tz_compare` says whether each date field equals the São Paulo date or the
  UTC date of that instant.
- `refund_tz` does the same for each refund entry. It also says whether the
  entry's timestamp matches São Paulo or UTC wall-clock time.

`--capture-file` holds the same sanitized records as stdout, as
`{"schema", "records", "emit_errors"}`:

- Use a directory on a Linux filesystem, for example `~/.asaas-capture`.
  WSL's `/mnt/c` (DrvFs) may not support hard links. The tool then falls
  back to a rename that still never replaces an existing file, but ext4 is
  the tested path.
- Its directory is checked before the first request.
- If the file already exists, the run refuses to start and never touches it.
- It is written atomically. The text goes to a new temp file in the same
  directory, named `.<name>.<16 hex>.tmp`, created exclusively, without
  following symlinks and with mode 0600. It is fsynced and then hard-linked
  into place. A link never replaces an existing file.
- **The capture is never lost.** If the link, the fsync or the write fails
  for any other reason, the temp file is kept, not deleted. The run then
  prints a `capture-file` record with outcome `write_failed`, a `reason`
  (`target_exists`, `link_failed`, `rename_failed`, `temp_write_failed` or
  `temp_create_failed`), the kept `temp_file` name (in the capture file's
  directory) and the `errno`. Rename the kept temp file to use it.
- If another process creates the target during the run, the tool does not
  overwrite it. It reports `write_failed` with reason `target_exists` and
  keeps its own capture in the temp file.
- A `kill -9` while the file is being fsynced or linked can leave a complete
  0600 `.<name>.<16 hex>.tmp` next to the target. It is safe to keep;
  rename it to recover the capture.
- It is written first in `finally`, before the last lines are printed. It
  therefore also exists after Ctrl-C, SIGTERM, a closed terminal (SIGHUP),
  a dead output stream or an unexpected error.
- Signals are deferred until the file is written and the summary printed, so
  the exit code always matches the printed `summary`. That also holds when
  stdout is a pipe that closed early, such as `| head`: after the first
  failed write, stdout is pointed at `/dev/null`, so Python's own flush at
  exit cannot change the exit code.

## Cleanup and what stays behind

ASAAS only deletes payments that are `PENDING` or `OVERDUE`. **Every normal
live run therefore leaves its CONFIRMED and REFUNDED card payments in the
sandbox.** It ends with `cleanup` outcome `incomplete` and exit code 4. Each
remaining payment is listed with the reason `delete_only_pending_or_overdue`.
The subscription and the customer are deleted.

Cleanup also runs after `kill` (SIGTERM), a closed terminal (SIGHUP), Ctrl-C
or an unexpected error. The first SIGTERM or SIGHUP is turned into an
exception that stops the scenario steps. While cleanup runs, **further
SIGTERM, SIGHUP and Ctrl-C are deferred**: pressing Ctrl-C again or sending
another `kill` does not abort cleanup. When cleanup finishes, the run exits
with the first interruption's code (130 or 128 + signal). A signal that
arrives only during cleanup is acted on after cleanup.

Deferred signals print nothing, and cleanup has no overall deadline. Each
request is bounded (connect 5 s, write 10 s, read 20 s), but a slow sandbox
can keep cleanup going for a while, so a second Ctrl-C can look as if the
tool is not responding. Only `kill -9` or `Ctrl-\` (SIGQUIT) forces an
immediate exit, and that skips the rest of cleanup (see the hard-kill
recovery below).

The `cleanup` record always lists what is left. If cleanup itself is cut
short by an exception (not by a signal), it ends with outcome `interrupted`.
Every object it had not finished is then listed under `remaining` with the
reason `cleanup_interrupted`. Recover those objects by hand as described
below, starting with any `subscription` in the list.

**A hard kill skips cleanup.** That means SIGKILL, `kill -9`, an OOM kill or
power loss. It can leave an `ACTIVE` card subscription that would charge the
test card again next cycle. The same recovery applies to anything a `cleanup`
record lists as `cleanup_interrupted`. To recover:

1. In the sandbox panel, open *Assinaturas*. Find subscriptions whose
   description starts with `neurostack-capture-` and delete them.
2. Delete the test customer (`Asaas Capture Teste`) if it is still there.
3. Leave CONFIRMED or REFUNDED payments alone. The API cannot delete them, and
   they move no real money.

## Reading the output

| Step | Meaning |
| --- | --- |
| `discovery-*` | Existing objects, each with `ownership` set to `this-run`, `prior-capture-run` or `owner-created, panel flow`. Owner card payments get a detail readback, plus `/refunds` and `/status`. The payments list is unfiltered, so non-card owner payments show up with their `billingType` and `status`. |
| `customer-create`, `payment-create`, `payment-readback` | The customer and the one-off card payment as created and as read back, with `create_instant` and `tz_compare`. `snapshot.dates` shows each design-question date as a value, `null` or `"absent"`. `snapshot.relations` gives day offsets from `dateCreated`. |
| `subscription-*` | The card subscription, `GET /v3/payments?subscription=`, and the first payment it generated. That payment carries the subscription's `create_instant`. |
| `poll-*` | Emitted on every status change, with `elapsed_s`. `poll` records why polling stopped (`all_received`, `no_change` or `budget_exhausted`). |
| `refund-*` | The partial refund (`value`) on the one-off payment and the full refund on the subscription's first payment, each with `refund_instant` and `refund_tz`. Each refund is followed by three readbacks and `GET /v3/payments/{id}/refunds`. |
| `cleanup` | `deleted` and `remaining` objects, with a reason for each remaining one. |
| `summary` | Always the last line: the outcome, `exit_code` and, on failure, the `code`. |

Exit codes:

| Exit | Meaning |
| --- | --- |
| 0 | ok |
| 2 | A guard failed. The key, `.env` and capture-file guards run before the first request, so nothing was sent. A request-path or egress guard can also trip mid-run, after earlier requests. |
| 4 | Provider error or incomplete cleanup. This includes every normal live run (see above). |
| 130 | Ctrl-C. |
| 128 + signal | SIGTERM (143) or SIGHUP (129). |
| 1 | Unexpected exception, with a traceback. The capture file is still written. |
