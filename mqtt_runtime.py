"""Spawn-isolated Paho MQTT adapters with explicit delivery outcomes.

The parent process never owns a Paho network loop. One spawned child owns one
Paho client and loop for each connection generation. This gives shutdown an
OS-enforced boundary: after a cooperative deadline the child is terminated,
killed if necessary, and joined before ``close`` can report success.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
import hashlib
import logging
import multiprocessing
from multiprocessing.connection import Connection
import secrets
import ssl
import threading
import time
from typing import Callable, Mapping, Protocol
import uuid

import paho.mqtt.client as mqtt

from runtime_config import MQTTConfig


LOGGER = logging.getLogger(__name__)
_PROTOCOL_VERSION = 1


class ClientState(str, Enum):
    CREATED = "CREATED"
    CONNECTING = "CONNECTING"
    ACTIVE = "ACTIVE"
    RECONNECTING = "RECONNECTING"
    STOPPING = "STOPPING"
    CLOSED = "CLOSED"


class PublishOutcome(str, Enum):
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    NOT_CONNECTED = "NOT_CONNECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class PublishResult:
    operation_id: str
    outcome: PublishOutcome
    mid: int | None
    qos: int


@dataclass(frozen=True, slots=True)
class ReceivedMessage:
    topic: str
    payload: str
    qos: int
    retain: bool
    duplicate: bool
    generation: int


class MessageHandler(Protocol):
    def __call__(self, message: ReceivedMessage) -> None: ...


class ShutdownError(RuntimeError):
    """The owned child could not be proven absent inside the finite budget."""


@dataclass(frozen=True, slots=True)
class _ShutdownSchedule:
    """Immutable phase deadlines that preserve time for post-kill reaping."""

    started_at: float
    cooperative_deadline: float
    terminate_deadline: float
    kill_not_later_than: float
    overall_deadline: float
    kill_reap_reserve: float

    @classmethod
    def create(
        cls,
        *,
        started_at: float,
        shutdown_timeout: float,
        forced_cleanup_budget: float,
        cooperative: bool,
    ) -> _ShutdownSchedule:
        terminate_grace = min(0.05, forced_cleanup_budget / 8.0)
        kill_reap_reserve = forced_cleanup_budget - terminate_grace
        overall_deadline = started_at + shutdown_timeout + forced_cleanup_budget
        kill_not_later_than = overall_deadline - kill_reap_reserve
        cooperative_deadline = min(
            started_at + (shutdown_timeout if cooperative else 0.0),
            kill_not_later_than - terminate_grace,
        )
        terminate_deadline = min(cooperative_deadline + terminate_grace, kill_not_later_than)
        return cls(
            started_at=started_at,
            cooperative_deadline=cooperative_deadline,
            terminate_deadline=terminate_deadline,
            kill_not_later_than=kill_not_later_than,
            overall_deadline=overall_deadline,
            kill_reap_reserve=kill_reap_reserve,
        )


class _SubscriptionDecision(str, Enum):
    ACCEPTED = "ACCEPTED"
    DENIED = "DENIED"
    IGNORED = "IGNORED"


def _reason_failed(reason: object) -> bool:
    if bool(getattr(reason, "is_failure", False)):
        return True
    value = getattr(reason, "value", reason)
    try:
        return int(value) >= 128
    except (TypeError, ValueError):
        return value not in {0, "0", None}


def _subscription_granted(reason_codes: object) -> bool:
    if not isinstance(reason_codes, (list, tuple)) or not reason_codes:
        return False
    return all(not _reason_failed(reason) for reason in reason_codes)


@dataclass(slots=True)
class _SubscriptionTracker:
    pending_generation: int | None = None
    pending_mid: int | None = None
    accepted_generations: set[int] | None = None

    def __post_init__(self) -> None:
        if self.accepted_generations is None:
            self.accepted_generations = set()

    def begin(self, generation: int, mid: int) -> None:
        self.pending_generation = generation
        self.pending_mid = mid

    def invalidate(self, generation: int) -> None:
        if self.pending_generation == generation:
            self.pending_generation = None
            self.pending_mid = None

    def acknowledge(self, generation: int, mid: int, reason_codes: object) -> _SubscriptionDecision:
        if generation in self.accepted_generations:
            return _SubscriptionDecision.IGNORED
        if generation != self.pending_generation or mid != self.pending_mid:
            return _SubscriptionDecision.IGNORED
        self.pending_generation = None
        self.pending_mid = None
        if not _subscription_granted(reason_codes):
            return _SubscriptionDecision.DENIED
        self.accepted_generations.add(generation)
        return _SubscriptionDecision.ACCEPTED


def _event(session: str, generation: int, kind: str, **fields: object) -> dict[str, object]:
    return {
        "protocol": _PROTOCOL_VERSION,
        "session": session,
        "generation": generation,
        "kind": kind,
        **fields,
    }


class _PahoChildEngine:
    """Runs only in the spawned child process."""

    def __init__(
        self,
        connection: Connection,
        config: MQTTConfig,
        role: str,
        session: str,
        shared_stop: object,
    ) -> None:
        self.connection = connection
        self.config = config
        self.role = role
        self.session = session
        self.generation = 0
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._shared_stop = shared_stop
        self._disconnected = threading.Event()
        self._fatal = threading.Event()
        self._tracker = _SubscriptionTracker()
        self._client: mqtt.Client | None = None

    def _stop_requested(self) -> bool:
        return self._stop.is_set() or bool(getattr(self._shared_stop, "value", 0))

    def send(self, kind: str, **fields: object) -> None:
        message = _event(self.session, self.generation, kind, **fields)
        try:
            with self._send_lock:
                self.connection.send(message)
        except (BrokenPipeError, EOFError, OSError):
            self._stop.set()

    def _new_client(self, generation: int) -> mqtt.Client:
        client_id = f"{self.config.client_id_prefix}-{secrets.token_hex(4)}"
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311,
            reconnect_on_failure=False,
        )
        client.username_pw_set(self.config.username, self.config.password)
        if self.config.ca_file is not None:
            client.tls_set(
                ca_certs=str(self.config.ca_file),
                cert_reqs=ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )
            client.tls_insecure_set(False)
        client.on_connect = lambda c, u, f, r, p: self._on_connect(generation, c, r)
        client.on_disconnect = lambda c, u, f, r, p: self._on_disconnect(generation)
        client.on_message = lambda c, u, m: self._on_message(generation, m)
        client.on_subscribe = lambda c, u, mid, reasons, p: self._on_subscribe(generation, mid, reasons)
        return client

    def _on_connect(self, generation: int, client: mqtt.Client, reason_code: object) -> None:
        if generation != self.generation or self._stop_requested():
            return
        if _reason_failed(reason_code):
            self.send("START_FAILED", failure_class="ConnectionRejected")
            self._fatal.set()
            return
        self.send("CONNACK")
        if self.role == "publisher":
            self.send("READY")
            return
        result, mid = client.subscribe(self.config.topic, qos=self.config.qos)
        if result != mqtt.MQTT_ERR_SUCCESS:
            self.send("START_FAILED", failure_class="SubscribeRejectedLocally")
            self._fatal.set()
            return
        self._tracker.begin(generation, int(mid))
        self.send("SUBSCRIBE_SENT", mid=int(mid))

    def _on_subscribe(self, generation: int, mid: int, reason_codes: object) -> None:
        decision = self._tracker.acknowledge(generation, int(mid), reason_codes)
        if decision is _SubscriptionDecision.ACCEPTED:
            self.send("READY", mid=int(mid))
        elif decision is _SubscriptionDecision.DENIED:
            self.send("START_FAILED", failure_class="SubscriptionDenied")
            self._fatal.set()

    def _on_disconnect(self, generation: int) -> None:
        self._tracker.invalidate(generation)
        if generation == self.generation and not self._stop_requested():
            self.send("DISCONNECTED")
            self._disconnected.set()

    def _on_message(self, generation: int, message: object) -> None:
        if generation != self.generation or self._stop_requested():
            return
        self.send(
            "MESSAGE",
            topic=str(getattr(message, "topic", "")),
            payload=bytes(getattr(message, "payload", b"")),
            qos=int(getattr(message, "qos", 0)),
            retain=bool(getattr(message, "retain", False)),
            duplicate=bool(getattr(message, "dup", False)),
        )

    def _valid_command(self, command: object) -> bool:
        return (
            isinstance(command, dict)
            and command.get("protocol") == _PROTOCOL_VERSION
            and command.get("session") == self.session
        )

    def _publish(self, command: Mapping[str, object]) -> None:
        operation_id = str(command["operation_id"])
        qos = int(command["qos"])
        accepted = False
        mid: int | None = None
        try:
            client = self._client
            if client is None:
                self.send("PUBLISH_RESULT", operation_id=operation_id, outcome=PublishOutcome.NOT_CONNECTED.value, mid=None, qos=qos)
                return
            info = client.publish(
                str(command["topic"]),
                bytes(command["payload"]),
                qos=qos,
                retain=bool(command["retain"]),
            )
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                self.send("PUBLISH_RESULT", operation_id=operation_id, outcome=PublishOutcome.REJECTED.value, mid=None, qos=qos)
                return
            accepted = True
            mid = int(info.mid)
            self.send("PUBLISH_ACCEPTED", operation_id=operation_id, mid=mid, qos=qos)
            info.wait_for_publish(timeout=self.config.publish_timeout)
            outcome = PublishOutcome.CONFIRMED if info.is_published() else PublishOutcome.UNKNOWN
        except Exception:
            outcome = PublishOutcome.UNKNOWN if accepted else PublishOutcome.REJECTED
        self.send("PUBLISH_RESULT", operation_id=operation_id, outcome=outcome.value, mid=mid, qos=qos)

    def _handle_command(self, command: object) -> None:
        if not self._valid_command(command):
            return
        kind = command.get("kind")
        if kind == "STOP":
            self._shared_stop.value = 1
            self._stop.set()
        elif kind == "PUBLISH" and self.role == "publisher":
            self._publish(command)
        elif kind == "FORCE_TRANSPORT_LOSS" and self._client is not None:
            self._client._sock_close()
            # This command is a deterministic local test hook.  Closing the
            # socket bypasses Paho's normal error callback on some platforms,
            # so drive the same generation-bound transition explicitly.
            self._on_disconnect(self.generation)

    def _poll_command(self, timeout: float) -> None:
        try:
            if self.connection.poll(timeout):
                self._handle_command(self.connection.recv())
        except (BrokenPipeError, EOFError, OSError):
            self._stop.set()

    def _sleep_with_commands(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop_requested() and time.monotonic() < deadline:
            self._poll_command(min(0.05, max(0.0, deadline - time.monotonic())))

    def _run_generation(self) -> None:
        self.generation += 1
        generation = self.generation
        self._disconnected.clear()
        self._fatal.clear()
        client = self._new_client(generation)
        self._client = client
        loop_started = False
        try:
            result = client.connect(self.config.host, self.config.port, self.config.keepalive)
            if result != mqtt.MQTT_ERR_SUCCESS:
                self.send("START_FAILED", failure_class="ConnectRejectedLocally")
                self._fatal.set()
                return
            client.loop_start()
            loop_started = True
            while not (self._stop_requested() or self._disconnected.is_set() or self._fatal.is_set()):
                self._poll_command(0.05)
        finally:
            try:
                client.disconnect()
            finally:
                if loop_started:
                    client.loop_stop()
                self._client = None
                self._tracker.invalidate(generation)

    def run(self) -> None:
        delay = float(self.config.reconnect_min_delay)
        while not (self._stop_requested() or self._fatal.is_set()):
            self._run_generation()
            if self._stop_requested() or self._fatal.is_set():
                break
            self._sleep_with_commands(delay)
            delay = min(delay * 2, float(self.config.reconnect_max_delay))
        self.send("TERMINAL", failure_class="None" if self._stop_requested() else "FatalProtocolState")


def _mqtt_child_main(
    connection: Connection,
    config: MQTTConfig,
    role: str,
    session: str,
    shared_stop: object,
    _options: Mapping[str, object] | None = None,
) -> None:
    try:
        _PahoChildEngine(connection, config, role, session, shared_stop).run()
    except BaseException as exc:
        try:
            connection.send(_event(session, 0, "TERMINAL", failure_class=type(exc).__name__))
        except BaseException:
            pass
    finally:
        connection.close()


EngineTarget = Callable[[Connection, MQTTConfig, str, str, object, Mapping[str, object] | None], None]


class _LifecycleClient:
    def __init__(
        self,
        config: MQTTConfig,
        *,
        role: str,
        engine_target: EngineTarget = _mqtt_child_main,
        engine_options: Mapping[str, object] | None = None,
    ) -> None:
        self.config = config
        self._role = role
        self._engine_target = engine_target
        self._engine_options = dict(engine_options or {})
        self._context = multiprocessing.get_context("spawn")
        self._stop_signal = self._context.RawValue("b", 0)
        self._session = secrets.token_hex(16)
        self._state = ClientState.CREATED
        self._generation = 0
        self._subscribed_generations: set[int] = set()
        self._state_lock = threading.RLock()
        self._receive_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._operation_lock = threading.Lock()
        self._closed = threading.Event()
        self._shutdown_error: BaseException | None = None
        self._shutdown_trace: tuple[tuple[str, float], ...] = ()
        self._shutdown_exitcode: int | None = None
        self._process: multiprocessing.Process | None = None
        self._connection: Connection | None = None
        self._pending_events: deque[dict[str, object]] = deque()
        self._messages: deque[ReceivedMessage] = deque()
        self._publish_results: dict[str, PublishResult] = {}
        self._publish_mids: dict[str, int] = {}
        self._last_operation_id: str | None = None
        self._start_failure: str | None = None

    @property
    def state(self) -> ClientState:
        self._drain_available()
        with self._state_lock:
            return self._state

    @property
    def generation(self) -> int:
        self._drain_available()
        with self._state_lock:
            return self._generation

    @property
    def subscribed_generations(self) -> tuple[int, ...]:
        self._drain_available()
        with self._state_lock:
            return tuple(sorted(self._subscribed_generations))

    @property
    def loop_started(self) -> bool:
        self._drain_available()
        with self._state_lock:
            process = self._process
            return self._state is ClientState.ACTIVE and process is not None and process.is_alive()

    @property
    def owned_process_alive(self) -> bool:
        with self._state_lock:
            return self._process is not None and self._process.is_alive()

    @property
    def last_operation_id(self) -> str | None:
        with self._state_lock:
            return self._last_operation_id

    def operation_result(self, operation_id: str) -> PublishResult | None:
        with self._state_lock:
            return self._publish_results.get(operation_id)

    def _spawn(self) -> None:
        parent, child = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=self._engine_target,
            args=(child, self.config, self._role, self._session, self._stop_signal, self._engine_options),
            name=f"mqtt-{self._role}-engine",
            daemon=True,
        )
        try:
            process.start()
        except BaseException:
            parent.close()
            child.close()
            raise
        child.close()
        with self._state_lock:
            self._connection = parent
            self._process = process

    def _send(self, kind: str, **fields: object) -> None:
        with self._state_lock:
            connection = self._connection
        if connection is None:
            raise BrokenPipeError("MQTT child IPC is absent")
        message = {"protocol": _PROTOCOL_VERSION, "session": self._session, "kind": kind, **fields}
        with self._send_lock:
            connection.send(message)

    def _recv_event(self, timeout: float) -> dict[str, object] | None:
        with self._receive_lock:
            with self._state_lock:
                connection = self._connection
            if connection is None:
                return None
            try:
                if not connection.poll(max(0.0, timeout)):
                    return None
                event = connection.recv()
            except (BrokenPipeError, EOFError, OSError):
                return None
            return event if isinstance(event, dict) else None

    def _valid_event(self, event: Mapping[str, object]) -> bool:
        return event.get("protocol") == _PROTOCOL_VERSION and event.get("session") == self._session

    def _process_event(self, event: Mapping[str, object]) -> None:
        if not self._valid_event(event):
            return
        kind = event.get("kind")
        generation = int(event.get("generation", 0))
        with self._state_lock:
            if kind == "CONNACK":
                if generation >= self._generation and self._state not in {ClientState.STOPPING, ClientState.CLOSED}:
                    self._generation = generation
                    self._state = ClientState.CONNECTING if generation == 1 else ClientState.RECONNECTING
            elif kind == "READY":
                if generation == self._generation and self._state not in {ClientState.STOPPING, ClientState.CLOSED}:
                    self._state = ClientState.ACTIVE
                    if self._role == "subscriber":
                        self._subscribed_generations.add(generation)
            elif kind == "DISCONNECTED":
                if generation == self._generation and self._state not in {ClientState.STOPPING, ClientState.CLOSED}:
                    self._state = ClientState.RECONNECTING
            elif kind == "START_FAILED":
                if generation >= self._generation:
                    self._start_failure = str(event.get("failure_class", "ProtocolFailure"))
            elif kind == "PUBLISH_ACCEPTED":
                self._publish_mids[str(event["operation_id"])] = int(event["mid"])
            elif kind == "PUBLISH_RESULT":
                operation_id = str(event["operation_id"])
                self._publish_results[operation_id] = PublishResult(
                    operation_id,
                    PublishOutcome(str(event["outcome"])),
                    int(event["mid"]) if event.get("mid") is not None else self._publish_mids.get(operation_id),
                    int(event["qos"]),
                )
            elif kind == "MESSAGE":
                payload = bytes(event.get("payload", b""))
                try:
                    decoded = payload.decode("utf-8", "strict")
                except UnicodeDecodeError:
                    LOGGER.warning(
                        "rejected malformed MQTT payload length=%d sha256=%s",
                        len(payload),
                        hashlib.sha256(payload).hexdigest(),
                    )
                else:
                    self._messages.append(
                        ReceivedMessage(
                            topic=str(event.get("topic", "")), payload=decoded,
                            qos=int(event.get("qos", 0)), retain=bool(event.get("retain", False)),
                            duplicate=bool(event.get("duplicate", False)), generation=generation,
                        )
                    )

    def _drain_available(self) -> None:
        if not self._receive_lock.acquire(blocking=False):
            return
        try:
            while True:
                with self._state_lock:
                    connection = self._connection
                if connection is None:
                    return
                try:
                    if not connection.poll(0):
                        return
                    event = connection.recv()
                except (BrokenPipeError, EOFError, OSError):
                    return
                if isinstance(event, dict):
                    self._process_event(event)
        finally:
            self._receive_lock.release()

    def _wait_until(self, predicate: Callable[[], bool], timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not predicate():
            with self._state_lock:
                process = self._process
            if process is None or not process.is_alive():
                self._drain_available()
                return predicate()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            event = self._recv_event(min(0.05, remaining))
            if event is not None:
                self._process_event(event)
        return True

    def start(self) -> None:
        with self._operation_lock:
            with self._state_lock:
                if self._state is ClientState.ACTIVE:
                    return
                if self._state is not ClientState.CREATED:
                    raise RuntimeError(f"client cannot start from {self._state.value}")
                self._state = ClientState.CONNECTING
            try:
                self._spawn()
                ready = self._wait_until(
                    lambda: self._state is ClientState.ACTIVE or self._start_failure is not None,
                    self.config.connect_timeout,
                )
                if not ready or self._state is not ClientState.ACTIVE:
                    raise TimeoutError("MQTT broker acknowledgement timed out or was rejected")
            except BaseException:
                self.close()
                raise

    def _forced_cleanup_budget(self) -> float:
        """Reserve a bounded interval for terminate/kill and final reaping."""
        return min(2.0, max(0.7, self.config.shutdown_timeout / 2.0))

    def _total_shutdown_budget(self) -> float:
        return self.config.shutdown_timeout + self._forced_cleanup_budget()

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(0.0, deadline - time.monotonic())

    def _perform_shutdown(self, pre_close_state: ClientState) -> None:
        started_at = time.monotonic()
        publish_in_flight = self._role == "publisher" and self._operation_lock.locked()
        cooperative = pre_close_state is ClientState.ACTIVE and not publish_in_flight
        schedule = _ShutdownSchedule.create(
            started_at=started_at,
            shutdown_timeout=self.config.shutdown_timeout,
            forced_cleanup_budget=self._forced_cleanup_budget(),
            cooperative=cooperative,
        )
        trace: list[tuple[str, float]] = []

        def record(phase: str) -> None:
            trace.append((phase, max(0.0, time.monotonic() - started_at)))

        def save_trace(exitcode: int | None) -> None:
            with self._state_lock:
                self._shutdown_trace = tuple(trace)
                self._shutdown_exitcode = exitcode

        with self._state_lock:
            process = self._process
            connection = self._connection
        self._stop_signal.value = 1
        record("stop-signaled")
        if publish_in_flight:
            record("inflight-publish-force")
        if process is None:
            if connection is not None:
                connection.close()
            save_trace(None)
            return
        if process.is_alive() and cooperative:
            record("cooperative-join")
            process.join(self._remaining(schedule.cooperative_deadline))
        if process.is_alive() and time.monotonic() < schedule.kill_not_later_than:
            record("terminate")
            process.terminate()
            process.join(self._remaining(schedule.terminate_deadline))
        if process.is_alive():
            record("kill")
            process.kill()
            process.join(self._remaining(schedule.overall_deadline))
        exitcode = process.exitcode
        record("reap-observed" if not process.is_alive() and exitcode is not None else "reap-unproven")
        save_trace(exitcode)
        if process.is_alive() or exitcode is None:
            raise ShutdownError("MQTT child remained alive at the absolute shutdown deadline")
        if connection is not None:
            connection.close()
        process.close()
        with self._state_lock:
            self._connection = None
            self._process = None

    def close(self) -> None:
        with self._state_lock:
            if self._state is ClientState.CLOSED:
                return
            if self._state is ClientState.STOPPING:
                owner = False
                pre_close_state = ClientState.STOPPING
            else:
                owner = True
                pre_close_state = self._state
                self._state = ClientState.STOPPING
        if not owner:
            total = self._total_shutdown_budget()
            if not self._closed.wait(total):
                raise TimeoutError("MQTT client shutdown owner exceeded its finite budget")
            if self._shutdown_error is not None:
                raise ShutdownError("MQTT client shutdown did not complete cleanly") from self._shutdown_error
            return
        try:
            self._perform_shutdown(pre_close_state)
        except BaseException as exc:
            with self._state_lock:
                self._shutdown_error = exc
            raise
        else:
            with self._state_lock:
                self._state = ClientState.CLOSED
        finally:
            self._closed.set()

    def _force_transport_loss_for_test(self) -> None:
        self._send("FORCE_TRANSPORT_LOSS")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


class Publisher(_LifecycleClient):
    """Publish once per call and expose ambiguous completion without replay."""

    def __init__(
        self, config: MQTTConfig, *, engine_target: EngineTarget = _mqtt_child_main,
        engine_options: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(config, role="publisher", engine_target=engine_target, engine_options=engine_options)

    def _publish_payload(
        self, payload: bytes, *, topic: str | None, qos: int | None, retain: bool | None,
    ) -> PublishResult:
        operation_id = uuid.uuid4().hex
        selected_qos = self.config.qos if qos is None else qos
        if selected_qos not in {0, 1, 2}:
            raise ValueError("qos must be 0, 1 or 2")
        selected_topic = self.config.topic if topic is None else topic
        if not selected_topic or "+" in selected_topic or "#" in selected_topic or "\x00" in selected_topic:
            raise ValueError("topic must be concrete")
        self._drain_available()
        if self.state is not ClientState.ACTIVE:
            return PublishResult(operation_id, PublishOutcome.NOT_CONNECTED, None, selected_qos)
        unknown = PublishResult(operation_id, PublishOutcome.UNKNOWN, None, selected_qos)
        with self._state_lock:
            self._last_operation_id = operation_id
            self._publish_results[operation_id] = unknown
        try:
            self._send(
                "PUBLISH", operation_id=operation_id, payload=payload, topic=selected_topic,
                qos=selected_qos, retain=self.config.retain if retain is None else retain,
            )
            self._wait_until(lambda: self._publish_results.get(operation_id) is not unknown, self.config.publish_timeout)
        except (KeyboardInterrupt, SystemExit):
            with self._state_lock:
                mid = self._publish_mids.get(operation_id)
                self._publish_results[operation_id] = PublishResult(operation_id, PublishOutcome.UNKNOWN, mid, selected_qos)
            raise
        except Exception:
            pass
        with self._state_lock:
            result = self._publish_results[operation_id]
            if result is unknown and operation_id in self._publish_mids:
                result = PublishResult(operation_id, PublishOutcome.UNKNOWN, self._publish_mids[operation_id], selected_qos)
                self._publish_results[operation_id] = result
            return result

    def publish(
        self, payload: str, *, topic: str | None = None, qos: int | None = None,
        retain: bool | None = None,
    ) -> PublishResult:
        with self._operation_lock:
            return self._publish_payload(payload.encode("utf-8", "strict"), topic=topic, qos=qos, retain=retain)

    def _publish_raw_for_test(self, payload: bytes) -> PublishResult:
        with self._operation_lock:
            return self._publish_payload(payload, topic=None, qos=None, retain=None)


class Subscriber(_LifecycleClient):
    """Pull messages on the caller thread; no parent callback thread exists."""

    def __init__(
        self, config: MQTTConfig, *, engine_target: EngineTarget = _mqtt_child_main,
        engine_options: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(config, role="subscriber", engine_target=engine_target, engine_options=engine_options)

    def receive(self, timeout: float | None = None) -> ReceivedMessage | None:
        selected_timeout = self.config.connect_timeout if timeout is None else timeout
        if selected_timeout < 0:
            raise ValueError("timeout must be non-negative")
        with self._operation_lock:
            deadline = time.monotonic() + selected_timeout
            while True:
                self._drain_available()
                with self._state_lock:
                    if self._messages:
                        return self._messages.popleft()
                    process = self._process
                    state = self._state
                if state in {ClientState.STOPPING, ClientState.CLOSED}:
                    return None
                if process is None or not process.is_alive():
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                event = self._recv_event(min(0.05, remaining))
                if event is not None:
                    self._process_event(event)

    def run(
        self, handler: MessageHandler, *, stop: threading.Event | None = None,
        poll_interval: float = 0.2,
    ) -> None:
        signal = stop or threading.Event()
        while not signal.is_set():
            message = self.receive(timeout=poll_interval)
            if message is None:
                continue
            try:
                handler(message)
            except Exception as exc:
                LOGGER.error("MQTT handler failed: %s", type(exc).__name__)
