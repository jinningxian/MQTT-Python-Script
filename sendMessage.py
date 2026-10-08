"""Interactive MQTT publisher. Importing this module has no side effects."""

from __future__ import annotations

from mqtt_runtime import PublishOutcome, Publisher
from runtime_config import MQTTConfig


def send_message(message: str, config: MQTTConfig | None = None):
    selected = config or MQTTConfig.from_env()
    with Publisher(selected) as publisher:
        return publisher.publish(message)


def main() -> int:
    config = MQTTConfig.from_env()
    with Publisher(config) as publisher:
        while True:
            try:
                message = input("Enter your message (Ctrl+C to stop) >>> ")
            except (EOFError, KeyboardInterrupt):
                return 0
            result = publisher.publish(message)
            print(result.outcome.value)
            if result.outcome is PublishOutcome.UNKNOWN:
                print("Delivery is uncertain; reconcile before sending a new operation.")


if __name__ == "__main__":
    raise SystemExit(main())

