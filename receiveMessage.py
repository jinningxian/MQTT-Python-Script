"""Interactive MQTT subscriber. Importing this module has no side effects."""

from __future__ import annotations

import threading

from mqtt_runtime import ReceivedMessage, Subscriber
from runtime_config import MQTTConfig


def print_message(message: ReceivedMessage) -> None:
    print(message.payload)


def run_receiver(
    config: MQTTConfig | None = None,
    *,
    stop: threading.Event | None = None,
) -> None:
    selected = config or MQTTConfig.from_env()
    signal = stop or threading.Event()
    with Subscriber(selected) as subscriber:
        subscriber.run(print_message, stop=signal)


def main() -> int:
    try:
        run_receiver()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
