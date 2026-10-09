"""Authenticated loopback-only aMQTT broker entry point."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from amqtt.broker import Broker

from runtime_config import BrokerConfig, ConfigurationError


LOGGER = logging.getLogger(__name__)


def _acl(path: Path) -> dict[str, dict[str, list[str]]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("MQTT_BROKER_ACL_FILE is invalid") from exc
    if not isinstance(raw, dict) or set(raw) != {"publish", "subscribe"}:
        raise ConfigurationError("MQTT_BROKER_ACL_FILE must contain publish and subscribe maps")
    result: dict[str, dict[str, list[str]]] = {}
    for action in ("publish", "subscribe"):
        mapping = raw[action]
        if not isinstance(mapping, dict) or not mapping:
            raise ConfigurationError("MQTT_BROKER_ACL_FILE contains an invalid ACL map")
        checked: dict[str, list[str]] = {}
        for username, topics in mapping.items():
            if not isinstance(username, str) or not username or not isinstance(topics, list) or not topics:
                raise ConfigurationError("MQTT_BROKER_ACL_FILE contains an invalid ACL entry")
            if any(not isinstance(topic, str) or not topic or "\x00" in topic for topic in topics):
                raise ConfigurationError("MQTT_BROKER_ACL_FILE contains an invalid topic")
            checked[username] = topics
        result[action] = checked
    return result


def build_broker_settings(config: BrokerConfig) -> dict[str, Any]:
    acl = _acl(config.acl_file)
    return {
        "listeners": {
            "default": {
                "type": "tcp",
                "bind": f"{config.host}:{config.port}",
                "max_connections": 32,
            }
        },
        "timeout_disconnect_delay": 0,
        "plugins": {
            "amqtt.plugins.authentication.FileAuthPlugin": {
                "password_file": str(config.password_file),
            },
            "amqtt.plugins.topic_checking.TopicAccessControlListPlugin": {
                "publish_acl": acl["publish"],
                "acl": acl["subscribe"],
            },
        },
    }


async def run_broker(config: BrokerConfig, stop: asyncio.Event | None = None) -> None:
    broker = Broker(build_broker_settings(config))
    await broker.start()
    shutdown_signal = stop or asyncio.Event()
    try:
        await shutdown_signal.wait()
    finally:
        await asyncio.wait_for(broker.shutdown(), timeout=config.shutdown_timeout)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(run_broker(BrokerConfig.from_env()))
    except KeyboardInterrupt:
        LOGGER.info("broker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

