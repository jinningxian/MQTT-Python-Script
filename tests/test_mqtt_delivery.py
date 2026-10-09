from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import logging
import multiprocessing
import os
import threading
import time

import pytest

import mqtt_runtime as mqtt_module
from mqtt_runtime import (
    ClientState,
    PublishOutcome,
    Publisher,
    Subscriber,
    _ShutdownSchedule,
    _SubscriptionDecision,
    _SubscriptionTracker,
)
from runtime_config import MQTTConfig


def wait_until(predicate, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true")


def _send_fault_event(connection, session, generation, kind, **fields):
    connection.send({"protocol": 1, "session": session, "generation": generation, "kind": kind, **fields})


def _cpu_pressure_worker(stop, ready):
    ready.set()
    value = 1
    while not stop.is_set():
        for _ in range(100_000):
            value = ((value * 1_103_515_245) + 12_345) & 0x7FFFFFFF
        time.sleep(0.001)


@contextmanager
def cpu_pressure():
    """Run one bounded CPU worker and prove it is absent on exit."""
    context = multiprocessing.get_context("spawn")
    stop = context.Event()
    ready = context.Event()
    worker = context.Process(target=_cpu_pressure_worker, args=(stop, ready), daemon=True)
    worker.start()
    assert ready.wait(3)
    try:
        yield
    finally:
        stop.set()
        worker.join(1)
        if worker.is_alive():
            worker.terminate()
            worker.join(1)
        if worker.is_alive():
            worker.kill()
            worker.join(1)
        assert not worker.is_alive()
        worker.close()


def fault_engine(connection, _config, role, session, shared_stop, options):
    """Picklable spawned fault engine; it never opens a socket."""
    options = dict(options or {})
    mode = options.get("mode", "normal")
    entered = options.get("entered")
    send_count = options.get("send_count")

    def mark_entered():
        if entered is not None:
            entered.set()

    if mode == "block_connect":
        mark_entered()
        while True:
            time.sleep(1)

    generation = 2 if mode == "stale_suback_then_valid" else 1
    _send_fault_event(connection, session, generation, "CONNACK")
    if role == "subscriber":
        if mode == "suback_denied":
            _send_fault_event(connection, session, generation, "START_FAILED", failure_class="SubscriptionDenied")
        elif mode == "suback_missing":
            pass
        elif mode == "stale_suback_then_valid":
            _send_fault_event(connection, session, 1, "READY", mid=11)
            time.sleep(0.05)
            _send_fault_event(connection, session, 2, "READY", mid=12)
        else:
            _send_fault_event(connection, session, generation, "READY", mid=11)
            if mode == "duplicate_suback":
                _send_fault_event(connection, session, generation, "READY", mid=11)
    else:
        _send_fault_event(connection, session, generation, "READY")

    if mode == "ipc_loss":
        connection.close()
        mark_entered()
        while True:
            time.sleep(1)

    while True:
        if bool(shared_stop.value):
            if mode in {"block_disconnect", "block_loop_stop"}:
                mark_entered()
                while True:
                    time.sleep(1)
            _send_fault_event(connection, session, generation, "TERMINAL", failure_class="None")
            return
        if not connection.poll(0.05):
            continue
        command = connection.recv()
        if not isinstance(command, dict) or command.get("session") != session:
            continue
        if command.get("kind") == "STOP":
            shared_stop.value = 1
            if mode in {"block_disconnect", "block_loop_stop"}:
                mark_entered()
                while True:
                    time.sleep(1)
            _send_fault_event(connection, session, generation, "TERMINAL", failure_class="None")
            return
        if command.get("kind") == "PUBLISH":
            if send_count is not None:
                with send_count.get_lock():
                    send_count.value += 1
            operation_id = command["operation_id"]
            _send_fault_event(
                connection,
                session,
                generation,
                "PUBLISH_ACCEPTED",
                operation_id=operation_id,
                mid=17,
                qos=command["qos"],
            )
            mark_entered()
            if mode == "crash_after_admission":
                os._exit(23)
            if mode in {"block_completion", "interrupt_wait"}:
                while True:
                    time.sleep(1)
            _send_fault_event(
                connection,
                session,
                generation,
                "PUBLISH_RESULT",
                operation_id=operation_id,
                outcome=PublishOutcome.CONFIRMED.value,
                mid=17,
                qos=command["qos"],
            )


def _bounded_test_config() -> MQTTConfig:
    return MQTTConfig(
        host="127.0.0.1",
        port=1883,
        topic="fixture/shutdown",
        username="fixture-user",
        password="fixture-password-not-production",
        qos=1,
        retain=False,
        keepalive=10,
        connect_timeout=5.0,
        publish_timeout=5.0,
        shutdown_timeout=0.15,
        client_id_prefix="fixture",
        reconnect_min_delay=1,
        reconnect_max_delay=2,
        ca_file=None,
    )


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


class _FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def poll(self, timeout: float = 0.0) -> bool:
        return False

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, clock: _FakeClock, *, reap_after_kill: bool = True, late_terminate: float = 0.0) -> None:
        self.clock = clock
        self.reap_after_kill = reap_after_kill
        self.late_terminate = late_terminate
        self.alive = True
        self.exitcode = None
        self.killed = False
        self.closed = False
        self.actions: list[tuple[str, float | None]] = []

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float | None = None) -> None:
        self.actions.append(("join", timeout))
        self.clock.advance(0.0 if timeout is None else timeout)
        if self.killed and self.reap_after_kill:
            self.alive = False
            self.exitcode = -9

    def terminate(self) -> None:
        self.actions.append(("terminate", None))
        self.clock.advance(self.late_terminate)

    def kill(self) -> None:
        self.actions.append(("kill", None))
        self.killed = True

    def close(self) -> None:
        self.actions.append(("close", None))
        self.closed = True


def test_shutdown_schedule_reserves_post_kill_reaping_budget():
    active = _ShutdownSchedule.create(
        started_at=10.0,
        shutdown_timeout=0.15,
        forced_cleanup_budget=0.7,
        cooperative=True,
    )
    connecting = _ShutdownSchedule.create(
        started_at=10.0,
        shutdown_timeout=0.15,
        forced_cleanup_budget=0.7,
        cooperative=False,
    )
    assert active.overall_deadline == pytest.approx(10.85)
    assert active.cooperative_deadline == pytest.approx(10.15)
    assert active.terminate_deadline == pytest.approx(10.20)
    assert active.kill_not_later_than == pytest.approx(10.20)
    assert active.kill_reap_reserve == pytest.approx(0.65)
    assert connecting.cooperative_deadline == pytest.approx(10.0)
    assert connecting.terminate_deadline == pytest.approx(10.05)
    assert connecting.kill_not_later_than == pytest.approx(10.20)
    assert connecting.kill_reap_reserve == pytest.approx(0.65)


@pytest.mark.parametrize(
    ("state", "expects_cooperative", "expected_kill_join"),
    [
        (ClientState.CONNECTING, False, 0.80),
        (ClientState.RECONNECTING, False, 0.80),
        (ClientState.ACTIVE, True, 0.65),
    ],
)
def test_shutdown_ignored_terminate_preserves_kill_reap_budget(
    monkeypatch, state, expects_cooperative, expected_kill_join,
):
    clock = _FakeClock()
    process = _FakeProcess(clock)
    connection = _FakeConnection()
    client = Publisher(_bounded_test_config(), engine_target=fault_engine)
    client._state = state
    client._process = process
    client._connection = connection
    monkeypatch.setattr(mqtt_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(client, "_send", lambda *_args, **_kwargs: pytest.fail("shutdown used pipe STOP"))

    client.close()

    phases = tuple(name for name, _elapsed in client._shutdown_trace)
    joins = [timeout for action, timeout in process.actions if action == "join"]
    assert ("cooperative-join" in phases) is expects_cooperative
    assert phases[-2:] == ("kill", "reap-observed")
    assert joins[-1] == pytest.approx(expected_kill_join)
    assert clock.now - 100.0 == pytest.approx(0.85)
    assert client._shutdown_exitcode == -9
    assert client.state is ClientState.CLOSED and not client.owned_process_alive
    assert process.closed and connection.closed


def test_shutdown_late_parent_fails_closed_and_retains_process_handle(monkeypatch):
    clock = _FakeClock()
    process = _FakeProcess(clock, reap_after_kill=False, late_terminate=1.0)
    connection = _FakeConnection()
    client = Publisher(_bounded_test_config(), engine_target=fault_engine)
    client._state = ClientState.CONNECTING
    client._process = process
    client._connection = connection
    monkeypatch.setattr(mqtt_module.time, "monotonic", clock.monotonic)

    with pytest.raises(mqtt_module.ShutdownError, match="remained alive"):
        client.close()

    assert client.state is ClientState.STOPPING
    assert client._process is process and client.owned_process_alive
    assert not process.closed and not connection.closed
    assert client._shutdown_trace[-1][0] == "reap-unproven"
    assert client._shutdown_exitcode is None


@pytest.mark.parametrize("qos", [0, 1, 2])
def test_loopback_delivery_qos(mqtt_broker, qos):
    _server, base = mqtt_broker
    config = replace(base, qos=qos, topic=f"fixture/qos/{qos}")
    subscriber = Subscriber(config)
    publisher = Publisher(config)
    try:
        subscriber.start()
        publisher.start()
        result = publisher.publish(f"message-{qos}")
        assert result.outcome is PublishOutcome.CONFIRMED
        received = subscriber.receive(timeout=5)
        assert received is not None
        assert (received.payload, received.qos) == (f"message-{qos}", qos)
    finally:
        publisher.close()
        subscriber.close()
    assert not publisher.loop_started and not subscriber.loop_started
    assert not publisher.owned_process_alive and not subscriber.owned_process_alive
    assert publisher.state is ClientState.CLOSED and subscriber.state is ClientState.CLOSED
    assert "cooperative-join" in {phase for phase, _elapsed in publisher._shutdown_trace}
    assert "cooperative-join" in {phase for phase, _elapsed in subscriber._shutdown_trace}
    assert publisher._shutdown_exitcode == 0 and subscriber._shutdown_exitcode == 0


def test_retained_message_reaches_late_subscriber_and_is_cleared(mqtt_broker):
    _server, base = mqtt_broker
    config = replace(base, topic="fixture/retained", qos=1)
    publisher = Publisher(config)
    publisher.start()
    try:
        assert publisher.publish("retained-value", retain=True).outcome is PublishOutcome.CONFIRMED
        subscriber = Subscriber(config)
        try:
            subscriber.start()
            received = subscriber.receive(timeout=5)
            assert received is not None
            assert received.payload == "retained-value" and received.retain is True
            assert publisher.publish("", retain=True).outcome is PublishOutcome.CONFIRMED
        finally:
            subscriber.close()
    finally:
        publisher.close()


def test_wrong_authentication_fails_closed(mqtt_broker):
    _server, base = mqtt_broker
    client = Publisher(replace(base, password="wrong-synthetic-password", connect_timeout=0.6, shutdown_timeout=0.2))
    with pytest.raises(TimeoutError):
        client.start()
    assert client.state is ClientState.CLOSED
    assert not client.owned_process_alive


def test_acl_denied_subscription_never_becomes_active(mqtt_broker):
    _server, base = mqtt_broker
    subscriber = Subscriber(replace(base, username="restricted", connect_timeout=0.8, shutdown_timeout=0.2))
    with pytest.raises(TimeoutError):
        subscriber.start()
    assert subscriber.state is ClientState.CLOSED
    assert subscriber.subscribed_generations == ()


def test_acl_rejects_unauthorized_publish_without_delivery(mqtt_broker):
    _server, base = mqtt_broker
    subscriber = Subscriber(base)
    restricted = Publisher(replace(base, username="restricted"))
    try:
        subscriber.start()
        restricted.start()
        result = restricted.publish("must-not-arrive")
        assert result.outcome in {PublishOutcome.CONFIRMED, PublishOutcome.UNKNOWN}
        assert subscriber.receive(timeout=0.5) is None
    finally:
        restricted.close()
        subscriber.close()


def test_unexpected_disconnect_reconnects_once_per_generation(mqtt_broker):
    _server, base = mqtt_broker
    subscriber = Subscriber(base)
    publisher = Publisher(base)
    try:
        subscriber.start()
        publisher.start()
        assert subscriber.subscribed_generations == (1,)
        subscriber._force_transport_loss_for_test()
        wait_until(lambda: subscriber.generation >= 2 and subscriber.state is ClientState.ACTIVE)
        assert subscriber.subscribed_generations == (1, 2)
        assert publisher.publish("after-reconnect").outcome is PublishOutcome.CONFIRMED
        received = subscriber.receive(timeout=5)
        assert received is not None and received.payload == "after-reconnect"
    finally:
        publisher.close()
        subscriber.close()


def test_three_connection_generations_preserve_delivery_order(mqtt_broker):
    _server, base = mqtt_broker
    config = replace(base, topic="fixture/reconnect-stress", qos=1)
    subscriber = Subscriber(config)
    publisher = Publisher(config)
    received: list[str] = []
    try:
        subscriber.start()
        publisher.start()
        for generation in range(1, 4):
            value = f"generation-{generation}"
            assert publisher.publish(value).outcome is PublishOutcome.CONFIRMED
            message = subscriber.receive(timeout=5)
            assert message is not None
            received.append(message.payload)
            if generation < 3:
                subscriber._force_transport_loss_for_test()
                wait_until(lambda: subscriber.generation == generation + 1 and subscriber.state is ClientState.ACTIVE)
        assert received == ["generation-1", "generation-2", "generation-3"]
        assert subscriber.subscribed_generations == (1, 2, 3)
    finally:
        publisher.close()
        subscriber.close()


def test_malformed_payload_is_hash_only_and_next_valid_message_recovers(mqtt_broker, caplog):
    _server, base = mqtt_broker
    subscriber = Subscriber(base)
    publisher = Publisher(base)
    try:
        subscriber.start()
        publisher.start()
        caplog.set_level(logging.WARNING)
        assert publisher._publish_raw_for_test(b"\xff\xfe").outcome is PublishOutcome.CONFIRMED
        assert publisher.publish("valid-after-malformed").outcome is PublishOutcome.CONFIRMED
        received = subscriber.receive(timeout=5)
        assert received is not None and received.payload == "valid-after-malformed"
        messages = [record.getMessage() for record in caplog.records if "malformed MQTT payload" in record.getMessage()]
        assert len(messages) == 1 and "length=2" in messages[0] and "sha256=" in messages[0]
        assert "�" not in messages[0]
    finally:
        publisher.close()
        subscriber.close()


def test_bounded_stress_preserves_count_and_arrival_order(mqtt_broker):
    _server, base = mqtt_broker
    config = replace(base, topic="fixture/stress", qos=1)
    subscriber = Subscriber(config)
    publisher = Publisher(config)
    try:
        subscriber.start()
        publisher.start()
        expected = [f"fixed-seed-{index:02d}" for index in range(30)]
        for value in expected:
            assert publisher.publish(value).outcome is PublishOutcome.CONFIRMED
        received = []
        for _ in expected:
            message = subscriber.receive(timeout=5)
            assert message is not None
            received.append(message.payload)
        assert received == expected
    finally:
        publisher.close()
        subscriber.close()


def test_subscription_tracker_rejects_denied_stale_wrong_mid_and_duplicate():
    tracker = _SubscriptionTracker()
    tracker.begin(2, 41)
    assert tracker.acknowledge(1, 41, [0]) is _SubscriptionDecision.IGNORED
    assert tracker.acknowledge(2, 40, [0]) is _SubscriptionDecision.IGNORED
    assert tracker.acknowledge(2, 41, [128]) is _SubscriptionDecision.DENIED
    tracker.begin(3, 42)
    assert tracker.acknowledge(3, 42, [1]) is _SubscriptionDecision.ACCEPTED
    assert tracker.acknowledge(3, 42, [1]) is _SubscriptionDecision.IGNORED


@pytest.mark.parametrize("mode", ["suback_denied", "suback_missing"])
def test_denied_or_missing_suback_fails_closed(mqtt_broker, mode):
    _server, base = mqtt_broker
    subscriber = Subscriber(
        replace(base, connect_timeout=0.25, shutdown_timeout=0.15),
        engine_target=fault_engine,
        engine_options={"mode": mode},
    )
    with pytest.raises(TimeoutError):
        subscriber.start()
    assert subscriber.state is ClientState.CLOSED
    assert subscriber.subscribed_generations == ()
    assert not subscriber.owned_process_alive


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("stale_suback_then_valid", (2,)), ("duplicate_suback", (1,))],
)
def test_stale_and_duplicate_suback_do_not_advance_wrong_generation(mqtt_broker, mode, expected):
    _server, base = mqtt_broker
    subscriber = Subscriber(base, engine_target=fault_engine, engine_options={"mode": mode})
    try:
        subscriber.start()
        assert subscriber.state is ClientState.ACTIVE
        assert subscriber.subscribed_generations == expected
    finally:
        subscriber.close()


def test_close_kills_child_blocked_in_connect(mqtt_broker):
    _server, base = mqtt_broker
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    client = Publisher(
        replace(base, connect_timeout=5.0, shutdown_timeout=0.15),
        engine_target=fault_engine,
        engine_options={"mode": "block_connect", "entered": entered},
    )
    failures: list[BaseException] = []
    worker = threading.Thread(target=lambda: _capture_failure(client.start, failures))
    worker.start()
    assert entered.wait(3)
    with cpu_pressure():
        began = time.monotonic()
        client.close()
        elapsed = time.monotonic() - began
    worker.join(2)
    assert elapsed < 1.0
    assert not worker.is_alive() and failures and isinstance(failures[0], TimeoutError)
    assert client.state is ClientState.CLOSED and not client.owned_process_alive
    assert "cooperative-join" not in {phase for phase, _elapsed in client._shutdown_trace}
    assert client._shutdown_trace[-1][0] == "reap-observed"
    assert client._shutdown_exitcode is not None


@pytest.mark.parametrize("mode", ["block_disconnect", "block_loop_stop"])
def test_close_kills_child_blocked_in_shutdown_stage(mqtt_broker, mode):
    _server, base = mqtt_broker
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    client = Publisher(
        replace(base, shutdown_timeout=0.15),
        engine_target=fault_engine,
        engine_options={"mode": mode, "entered": entered},
    )
    client.start()
    with cpu_pressure():
        began = time.monotonic()
        client.close()
        elapsed = time.monotonic() - began
    assert entered.is_set()
    assert elapsed < 1.0
    assert client.state is ClientState.CLOSED and not client.owned_process_alive
    assert "cooperative-join" in {phase for phase, _elapsed in client._shutdown_trace}
    assert client._shutdown_trace[-1][0] == "reap-observed"
    assert client._shutdown_exitcode is not None


def test_close_during_accepted_publish_returns_unknown_once(mqtt_broker):
    _server, base = mqtt_broker
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    send_count = context.Value("i", 0)
    publisher = Publisher(
        replace(base, publish_timeout=5.0, shutdown_timeout=0.15),
        engine_target=fault_engine,
        engine_options={"mode": "block_completion", "entered": entered, "send_count": send_count},
    )
    publisher.start()
    results = []
    worker = threading.Thread(target=lambda: results.append(publisher.publish("cancel-after-accept")))
    worker.start()
    assert entered.wait(3)
    with cpu_pressure():
        publisher.close()
    worker.join(2)
    assert not worker.is_alive()
    assert len(results) == 1 and results[0].outcome is PublishOutcome.UNKNOWN
    assert results[0].operation_id == publisher.last_operation_id
    assert send_count.value == 1
    assert publisher.state is ClientState.CLOSED and not publisher.owned_process_alive
    phases = tuple(phase for phase, _elapsed in publisher._shutdown_trace)
    assert phases[:2] == ("stop-signaled", "inflight-publish-force")
    assert "cooperative-join" not in phases
    assert phases[-1] == "reap-observed"
    assert publisher._shutdown_exitcode is not None


def test_child_crash_after_admission_is_unknown_and_never_replayed(mqtt_broker):
    _server, base = mqtt_broker
    context = multiprocessing.get_context("spawn")
    send_count = context.Value("i", 0)
    publisher = Publisher(
        replace(base, publish_timeout=0.8, shutdown_timeout=0.15),
        engine_target=fault_engine,
        engine_options={"mode": "crash_after_admission", "send_count": send_count},
    )
    publisher.start()
    try:
        result = publisher.publish("one-attempt")
        assert result.outcome is PublishOutcome.UNKNOWN
        assert result.operation_id == publisher.last_operation_id
        assert send_count.value == 1
    finally:
        publisher.close()
    assert not publisher.owned_process_alive


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), SystemExit(23)])
def test_process_cancellation_propagates_with_same_unknown_operation(mqtt_broker, interrupt):
    _server, base = mqtt_broker
    publisher = Publisher(
        replace(base, publish_timeout=5.0, shutdown_timeout=0.15),
        engine_target=fault_engine,
        engine_options={"mode": "interrupt_wait"},
    )
    publisher.start()
    original_receive = publisher._recv_event
    raised = False

    def interrupt_after_accept(timeout):
        nonlocal raised
        event = original_receive(timeout)
        if event is not None and event.get("kind") == "PUBLISH_ACCEPTED" and not raised:
            publisher._process_event(event)
            raised = True
            raise interrupt
        return event

    publisher._recv_event = interrupt_after_accept
    try:
        with pytest.raises(type(interrupt)):
            publisher.publish("cancel-me")
        operation_id = publisher.last_operation_id
        assert operation_id is not None
        result = publisher.operation_result(operation_id)
        assert result is not None and result.outcome is PublishOutcome.UNKNOWN
        assert result.operation_id == operation_id and result.mid == 17
    finally:
        publisher.close()
    assert not publisher.owned_process_alive


def test_close_is_idempotent_and_concurrent_waiter_is_bounded(mqtt_broker):
    _server, base = mqtt_broker
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    client = Publisher(
        replace(base, shutdown_timeout=0.15),
        engine_target=fault_engine,
        engine_options={"mode": "block_disconnect", "entered": entered},
    )
    client.start()
    failures: list[BaseException] = []
    first = threading.Thread(target=lambda: _capture_failure(client.close, failures))
    second = threading.Thread(target=lambda: _capture_failure(client.close, failures))
    with cpu_pressure():
        began = time.monotonic()
        first.start()
        assert entered.wait(3)
        second.start()
        first.join(2)
        second.join(2)
        elapsed = time.monotonic() - began
    client.close()
    assert not first.is_alive() and not second.is_alive() and failures == []
    assert elapsed < 1.0
    assert client.state is ClientState.CLOSED and not client.owned_process_alive
    assert client._shutdown_trace[-1][0] == "reap-observed"
    assert client._shutdown_exitcode is not None


def test_ipc_loss_reaps_live_child_under_cpu_pressure(mqtt_broker):
    _server, base = mqtt_broker
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    client = Publisher(
        replace(base, shutdown_timeout=0.15),
        engine_target=fault_engine,
        engine_options={"mode": "ipc_loss", "entered": entered},
    )
    client.start()
    assert entered.wait(3)
    with cpu_pressure():
        began = time.monotonic()
        client.close()
        elapsed = time.monotonic() - began
    assert elapsed < 1.0
    assert client.state is ClientState.CLOSED and not client.owned_process_alive


def _capture_failure(action, failures):
    try:
        action()
    except BaseException as exc:
        failures.append(exc)
