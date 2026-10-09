# MQTT Python Script 0.1.3 handover

This bounded repair closes the Linux scheduler-sensitive shutdown blocker from
the committed 0.1.2 candidate. Dependency locks, public configuration, SFTP
behavior, and the four broker, publisher, subscriber, and SFTP command-line use
cases remain unchanged.

## State-aware shutdown and reaping

- Each spawned MQTT child receives a lock-free shared stop byte. `close()` sets
  it without waiting for the command-pipe send lock or writing to a potentially
  back-pressured pipe.
- `close()` snapshots the pre-close lifecycle state. `CONNECTING` and
  `RECONNECTING` skip an unusable cooperative wait. An ACTIVE publisher with a
  publish already in progress also skips that wait because the child may be
  blocked inside publish completion and unable to poll the stop token.
- Other ACTIVE clients retain graceful cooperative shutdown. The real Paho
  child still performs `disconnect()` and `loop_stop()` before exit when it can
  observe the stop token.
- Phase deadlines are partitioned before shutdown work begins. With the fault
  configuration `shutdown_timeout=0.15`, terminate probing uses at most 0.05
  seconds, at least 0.65 seconds remains reserved for kill/reap, the complete
  owner budget stays 0.85 seconds, and the existing `<1.0s` acceptance
  threshold is unchanged.
- A successful close requires both `is_alive() == false` and an observed exit
  code before process and IPC handles are released. Missing that proof leaves
  the handle available for diagnosis and raises `ShutdownError`; it is never
  reported as `CLOSED`.
- An admitted publish interrupted by shutdown keeps one stable operation ID and
  returns `UNKNOWN`. No automatic replay or fabricated application idempotency
  was added.

## Validation contract

The external task evidence retains the original R3 Linux full-suite failure,
the R4 test-fixture interface failure, and the R4 Linux accepted-publish reaping
failure. The final functional source snapshot passed:

- Windows AMD64 CPython 3.13.16: 110/110 tests, zero required skips.
- Network-disabled Linux x86-64 CPython 3.13.16: 110/110 tests, zero required
  skips.
- The six Linux CPU-pressure and fault selectors for blocked connect,
  disconnect, `loop_stop`, accepted incomplete publish, IPC loss, and concurrent
  close: 6/6 with no surviving owned execution.
- Deterministic fake-clock cases prove phase partitioning, positive reap time,
  and fail-closed behavior when the parent misses the absolute deadline.

This version/documentation closeout expires that functional snapshot. Root must
create a normal local commit, then a fresh raw committed-blob Windows/Linux
validation and a different-agent exact Sol Max review must pass before any
remote release.

## Residual risks

- Real MQTT/SFTP providers, credentials, certificates, host keys, WAN timing,
  cross-session ordering, and business acceptance remain untested.
- Raw MQTT has no durable application identity. QoS 1 duplicates and
  reconciliation of `UNKNOWN` outcomes remain caller responsibilities.
- Native extension members remain inventoried, but embedded and linked native
  advisory coverage is not proven zero.
- aMQTT 0.12.1 retains its unsuppressed upstream subscription-ACL deprecation
  warning while local ACL positive and negative behavior remains covered.

This handover, together with its external task evidence, does not authorize commit,
push, PR, merge, deployment, provider access, credential rotation, cleanup,
publication, or production acceptance.
