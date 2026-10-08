# MQTT Python Script

This repository contains import-safe command-line adapters for an authenticated
MQTT broker, publisher, subscriber, and atomic SFTP transfers.

Configuration is read from process environment variables only when a command
or factory is invoked. Copy variable names from `.env.example`; keep values in
the caller's secret store. The application does not load `.env` files.

## Security and delivery contract

- The embedded aMQTT broker binds only to loopback and requires an Argon2
  password file plus explicit publish/subscribe ACL JSON.
- MQTT clients use Paho Callback API VERSION2 in a spawned child process. One
  child owns one network loop for each connection generation; the parent uses
  caller-thread IPC and applies a finite terminate/kill/join shutdown bound.
  QoS 1 may duplicate and QoS 2 is exactly-once only at the MQTT protocol
  boundary. Raw payloads do not contain a durable application message ID.
- A subscriber becomes active only after a successful generation- and
  MID-bound SUBACK. Messages are delivered by `Subscriber.receive()` or
  caller-thread `Subscriber.run(handler)`; the 0.1.0 candidate-only callback
  constructor is no longer supported.
- A publish accepted locally but not observed complete returns `UNKNOWN` and is
  never retried automatically.
- Non-loopback MQTT requires a CA file and verified hostname TLS.
- SFTP requires a known-hosts match, disables agent/key discovery, contains all
  paths under configured roots, and exposes final uploads only through the
  OpenSSH `posix_rename` extension. A disconnect around rename returns
  `UNKNOWN`; reconcile before retrying.

## Commands

Create a Python 3.13.16 virtual environment with `--without-pip`, download the
exact wheels named by the platform lock to a wheelhouse, and run:

```text
python bootstrap_env.py --lock pylock.windows.toml --wheelhouse <wheelhouse> --report <report.json>
```

Use `pylock.linux.toml` on Linux. The bootstrap rejects network access, sdists,
wrong hashes/tags, incomplete RECORD files, dependency drift, and non-empty
target environments.

After exporting the required variables:

```text
python brokerServer.py
python receiveMessage.py
python sendMessage.py
```

The historical SFTP function names remain as secured wrappers:
`getConnect`, `uploadFile`, and `downloadFile`.

Release version: `0.1.2`. The current security handover is documented in
`docs/HANDOVER_0.1.2.md`; the 0.1.0 and 0.1.1 handovers are retained as
historical candidate evidence. Exact source and validation hashes live in the
external task evidence handoff generated after the source writer lease is
released.

## Residual boundaries

Source removal does not rotate or revoke historical credentials or clean Git
history. Real broker/SFTP interoperability, credential validity, certificates,
host-key provisioning, cross-session ordering, application idempotency, and
reconciliation of `UNKNOWN` outcomes require separate human-controlled
acceptance.
