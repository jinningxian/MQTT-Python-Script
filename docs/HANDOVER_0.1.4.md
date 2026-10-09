# MQTT Python Script 0.1.4 handover

This bounded repair closes the required-gate startup instability found after
the committed 0.1.3 candidate. MQTT runtime behavior, delivery semantics,
dependency locks, public configuration, SFTP behavior, and the four command
line use cases remain unchanged.

## Deterministic pressure-worker lifecycle

- The unchanged CPU-pressure loop now lives in a lightweight root-importable
  module whose imports are limited to the Python standard library. Spawned
  children no longer import pytest, MQTT runtime modules, Paho, aMQTT, or
  cryptography before signalling readiness.
- The three-second readiness limit is unchanged. The parent polls one monotonic
  deadline and reports the child PID, alive state, and exit code when the child
  exits early or misses readiness.
- Process start, readiness, the test body, and every failure path share one
  cleanup boundary. A started child receives the stop event, then bounded
  join, terminate, kill, and reap operations as needed. Success requires both
  `is_alive() == false` and a non-null exit code before the process object is
  closed.
- A `Process.start()` exception remains the original exception and never runs
  operations that assume the child started. Deterministic negative tests cover
  start failure, a live child that never becomes ready, and exit code 23 before
  readiness; every case proves no owned child remains.
- The application shutdown contract is unchanged: blocked child readiness is
  established before pressure readiness, timing starts only after both are
  ready, the complete close budget remains 0.85 seconds, and the acceptance
  assertion remains less than one second.

## Functional validation before closeout

The external task evidence preserves the original committed Linux 109/110
failure and its later diagnostic-only 6/6 result as unstable history. Against
the repaired source before this version/documentation closeout:

- Windows AMD64 CPython 3.13.16 passed 114/114 tests with zero skips.
- Network-disabled Linux x86-64 CPython 3.13.16 passed 114/114 tests with zero
  skips.
- The six pressure and fault selectors passed 6/6 on each platform.
- Four focused lifecycle checks passed on each platform: stdlib-only spawn
  support plus deterministic start-failure, readiness-timeout, and early-exit
  cleanup.
- The 24 warnings per full suite and six warnings per pressure selection are
  the retained aMQTT subscription-ACL deprecation warning; they are not hidden
  or waived.

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
