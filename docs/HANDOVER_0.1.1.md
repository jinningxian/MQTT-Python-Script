# MQTT Python Script 0.1.1 handover

This bounded repair closes the five findings from the independent 0.1.0
candidate review. It preserves the dependency locks and the four command-line
broker, publisher, subscriber, and SFTP use cases.

## MQTT ownership and delivery

- The parent starts the MQTT engine with the CPython `spawn` process context on
  Windows and Linux. Credentials travel through local process bootstrap/IPC;
  they are not placed in command lines, files, or logs.
- One child owns one Paho client and loop for a connection generation. The
  parent reports a publisher active after CONNACK. It reports a subscriber
  active only after a successful SUBACK with the expected MID and generation.
- Subscriber delivery is caller-thread based through `receive(timeout)` or
  `run(handler, stop=...)`. Code written against the candidate-only
  `Subscriber(config, callback)` form must construct `Subscriber(config)` and
  call one of those methods.
- The parent assigns one stable operation ID before one IPC publish attempt.
  Accepted operations that lose completion, IPC, or their child are `UNKNOWN`;
  no operation is replayed automatically.
- `close()` first requests cooperative stop, then uses finite
  terminate/kill/join budgets. It cannot report `CLOSED` while it owns a live
  child or IPC endpoint. Process-level cancellation records the admitted
  operation as `UNKNOWN` and propagates `KeyboardInterrupt` or `SystemExit`.

## Configuration and SFTP repair

MQTT and SFTP passwords reject missing, whitespace-only, and control-character
values while preserving every valid leading and trailing character. Evidence
records only field names and boolean/state results, never secret values.

Local and remote SFTP relative paths use the same portable component policy.
Rooted, drive-relative, UNC/device, ADS/colon, reserved DOS aliases, repeated
separators, and trailing dot/space forms fail before a filesystem or transfer
operation. The policy uses Python 3.13 `ntpath.isreserved` for Windows
component classification, including superscript COM/LPT aliases, CONIN$/
CONOUT$, and forbidden metacharacters, while retaining the explicit historical
CLOCK$ guard. Host-key verification, reparse checks, owned temporary files,
`posix_rename`-only upload finalization, and `UNKNOWN`/no-retry behavior remain.

## Validation contract

The external task evidence records both functional and post-closeout source
snapshots, all raw commands and outputs, and these required gates:

- Windows CPython 3.13.16 and network-disabled Linux x86_64 CPython 3.13.16,
  each using the existing no-pip exact binary-wheel lock and full pytest suite
  with zero required skips.
- Local aMQTT QoS 0/1/2, retained delivery, reconnect generations,
  SUBACK denial/staleness/duplication, ACL/auth, malformed payload recovery,
  bounded stress, owner-block/child-crash/IPC-loss faults, and cancellation.
- Local Paramiko host-key, authentication, cross-platform path, atomic
  upload/download, partial failure, and uncertain-rename cases.
- Fresh complete Python coordinate/OSV coverage, unchanged lock and RECORD
  graphs, source-scope checks, and no real endpoint/provider request.

The first reconnect-hook failures and Linux command-construction failure are
preserved. Each successful rerun follows a recorded source or harness change;
none is classified as flaky.

## Residual risks

- Source removal does not rotate or revoke historical credentials and does not
  clean Git history.
- Real MQTT/SFTP endpoints, host keys, certificates, credentials, provider
  interoperability, and business acceptance remain untested.
- Raw MQTT has no durable application identity. QoS 1 duplicates,
  cross-session ordering, and reconciliation of `UNKNOWN` remain caller
  responsibilities.
- Force-terminating a blocked child deliberately preserves accepted unsettled
  operations as `UNKNOWN`; it cannot prove remote delivery or non-delivery.
- Native extension members remain inventoried, but embedded and linked native
  advisory coverage is not proven zero.
- aMQTT 0.12.1 still emits its unsuppressed upstream subscription-ACL
  deprecation warning while the local ACL positive/negative behavior remains
  covered.

This closeout does not authorize commit, push, PR, merge, deployment, provider
access, credential rotation, cleanup, publication, or production acceptance.
