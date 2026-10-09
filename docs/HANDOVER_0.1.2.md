# MQTT Python Script 0.1.2 handover

This bounded repair strengthens the spawned MQTT engine shutdown contract
after the committed 0.1.1 candidate exposed a Linux scheduler-sensitive child
reaping failure. Dependency locks, configuration, SFTP behavior, and the four
broker, publisher, subscriber, and SFTP command-line use cases are unchanged.

## Absolute shutdown deadline

- `close()` grants the child the configured cooperative `shutdown_timeout`,
  then applies terminate and kill within one monotonic absolute cleanup
  deadline. Each join receives only the remaining time, so phases cannot
  silently extend the lifecycle bound.
- The forced cleanup reserve is bounded from 0.7 to 2.0 seconds. Terminate
  probing consumes at most 0.1 seconds, leaving the balance for final kill and
  process reaping. With the fault-test `shutdown_timeout=0.15`, the complete
  owner and concurrent-waiter budget remains 0.85 seconds and the existing
  `<1.0s` acceptance threshold is unchanged.
- A client reaches `CLOSED` and releases process/IPC handles only after the
  child is proven absent. Failure at the deadline raises `ShutdownError`; no
  helper thread is left running and no live child is reported as closed.
- Accepted operations interrupted by close, IPC loss, or child failure retain
  their stable operation ID and `UNKNOWN` result. The parent never replays an
  uncertain publish.

## Validation contract

The external task evidence preserves the original Linux full-suite failure,
the first R3 pressure-test failures, and the changed-condition correction. The
required final matrix is:

- Windows AMD64 and network-disabled Linux x86-64 on CPython 3.13.16, each
  using the unchanged exact no-pip lock and complete test suite with zero
  required skips.
- CPU-pressure fault cases for blocked connect, disconnect, `loop_stop`, IPC
  loss, accepted-but-incomplete publish, and concurrent close callers. Every
  successful close must finish below the existing threshold and leave no
  owned execution.
- Existing QoS 0/1/2, SUBACK, reconnect, cancellation, stable operation-ID,
  SFTP containment/atomicity, secret-preservation, lock/RECORD, source-scope,
  and fresh OSV gates remain required.

Functional validation precedes this closeout and is therefore expired by the
version/documentation change. Root must create a normal local commit, then a
fresh raw committed-blob Windows/Linux validation and a different-agent exact
Sol Max review must pass before any remote release.

## Residual risks

- Real MQTT/SFTP providers, credentials, certificates, host keys, WAN timing,
  cross-session ordering, and business acceptance remain untested.
- Raw MQTT has no durable application identity. QoS 1 duplicates and
  reconciliation of `UNKNOWN` outcomes remain caller responsibilities.
- Native extension members remain inventoried, but embedded and linked native
  advisory coverage is not proven zero.
- aMQTT 0.12.1 retains its unsuppressed upstream subscription-ACL deprecation
  warning while local ACL positive and negative behavior remains covered.

This handover, together with its external task evidence, does not authorize commit, push,
PR, merge, deployment, provider access, credential rotation, cleanup,
publication, or production acceptance.
