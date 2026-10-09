# MQTT Python Script 0.1.0 handover

This increment replaces retired `hbmqtt`, removes import-time prompting and
source-bound credentials, adds exact offline platform locks, and exposes
explicit MQTT and SFTP uncertain outcomes.

## Configuration migration

Only variable names are documented here. Values belong in the caller's secret
store and are never committed.

| Historical concept | Environment fields |
| --- | --- |
| MQTT endpoint and login | `MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD` |
| MQTT message behavior | `MQTT_TOPIC`, `MQTT_QOS`, `MQTT_RETAIN`, `MQTT_KEEPALIVE` |
| MQTT lifecycle and TLS | `MQTT_CONNECT_TIMEOUT`, `MQTT_PUBLISH_TIMEOUT`, `MQTT_SHUTDOWN_TIMEOUT`, `MQTT_CLIENT_ID_PREFIX`, `MQTT_RECONNECT_MIN_DELAY`, `MQTT_RECONNECT_MAX_DELAY`, `MQTT_CA_FILE` |
| Embedded broker | `MQTT_BROKER_HOST`, `MQTT_BROKER_PORT`, `MQTT_BROKER_PASSWORD_FILE`, `MQTT_BROKER_ACL_FILE`, `MQTT_BROKER_SHUTDOWN_TIMEOUT` |
| SFTP endpoint and trust | `SFTP_HOST`, `SFTP_PORT`, `SFTP_USERNAME`, exactly one of `SFTP_PASSWORD` or `SFTP_KEY_FILE`, and `SFTP_KNOWN_HOSTS` |
| SFTP paths and bounds | `SFTP_LOCAL_ROOT`, `SFTP_REMOTE_ROOT`, `SFTP_CONNECT_TIMEOUT`, `SFTP_AUTH_TIMEOUT`, `SFTP_BANNER_TIMEOUT`, `SFTP_OPERATION_TIMEOUT` |

## Validation contract

The external task evidence for this release records the exact post-closeout
HEAD, staged/unstaged/untracked snapshot and normalized diff after the source
writer lease is released. It also records these required commands and their
raw outputs:

- Windows CPython 3.13.16 `venv --without-pip`, exact 31-coordinate lock
  bootstrap, and the complete pytest suite.
- Linux x86_64 CPython 3.13.16 with network disabled, exact 30-coordinate lock
  bootstrap, and the same complete pytest suite.
- Fresh OSV queries for all 31 unique PyPI coordinates, lock/RECORD/tag/marker
  checks, import-side-effect checks, QoS 0/1/2, retained delivery,
  multi-reconnect, cancellation, malformed payload, ACL/auth, bounded stress,
  SFTP host-key/path/atomicity, and uncertain-rename cases.

The preserved first failures are an extras-closure omission in the initial
bootstrap, a synthetic SFTP server that stopped after an expected host-key
rejection reset, and two pre-source lock-generator diagnostics. Each rerun used
changed code or changed fixture logic; none is reported as a flaky pass.

## Residual risks

- Source removal does not rotate or revoke historical credentials and does not
  clean Git history.
- Real MQTT/SFTP endpoints, host keys, certificates, credentials and provider
  interoperability have not been tested.
- MQTT raw payloads have no durable application identity. QoS 1 duplicates,
  cross-session ordering and reconciliation after `UNKNOWN` remain caller
  responsibilities.
- A disconnect around `posix_rename` is `UNKNOWN`; no automatic resend occurs.
- Native extension members are inventoried on both platforms, but their
  embedded and linked native-library advisory graph is not proven zero.
- aMQTT 0.12.1 emits its unsuppressed upstream subscription-ACL deprecation
  warning while its strict plugin config exposes the legacy field. Publish and
  subscribe ACL behavior is covered by the local positive and negative tests.

This closeout does not authorize commit, push, deployment, provider access,
credential rotation, cleanup or production acceptance.
