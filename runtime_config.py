"""Validated, import-safe environment configuration.

Values are read only when a ``from_env`` factory is called. Error messages
identify a field but never echo its value.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path, PurePosixPath
from typing import Mapping


class ConfigurationError(ValueError):
    """A required setting is missing or unsafe."""


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value or not value.strip():
        raise ConfigurationError(f"{name} is required")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConfigurationError(f"{name} contains control characters")
    return value.strip()


def _optional(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "")
    if not value or not value.strip():
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConfigurationError(f"{name} contains control characters")
    return value.strip()


def _required_secret(env: Mapping[str, str], name: str) -> str:
    """Validate a secret without changing any caller-supplied bytes."""
    value = env.get(name, "")
    if not value or not value.strip():
        raise ConfigurationError(f"{name} is required")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConfigurationError(f"{name} contains control characters")
    return value


def _optional_secret(env: Mapping[str, str], name: str) -> str | None:
    """Return an absent optional secret as None and preserve a present one."""
    value = env.get(name, "")
    if value == "":
        return None
    if not value.strip():
        raise ConfigurationError(f"{name} must not be whitespace only")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConfigurationError(f"{name} contains control characters")
    return value


def _integer(env: Mapping[str, str], name: str, minimum: int, maximum: int) -> int:
    raw = _required(env, name)
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} is outside the allowed range")
    return value


def _seconds(env: Mapping[str, str], name: str) -> float:
    raw = _required(env, name)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not 0.05 <= value <= 3600:
        raise ConfigurationError(f"{name} is outside the allowed range")
    return value


def _boolean(env: Mapping[str, str], name: str) -> bool:
    raw = _required(env, name).lower()
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _existing_file(env: Mapping[str, str], name: str, *, optional: bool = False) -> Path | None:
    raw = _optional(env, name) if optional else _required(env, name)
    if raw is None:
        return None
    path = Path(raw).expanduser().resolve()
    if not path.is_file() or path.is_symlink():
        raise ConfigurationError(f"{name} must name an existing regular file")
    return path


def _existing_root(env: Mapping[str, str], name: str) -> Path:
    path = Path(_required(env, name)).expanduser().resolve()
    if not path.is_dir() or path.is_symlink():
        raise ConfigurationError(f"{name} must name an existing directory")
    return path


def _loopback(host: str) -> bool:
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _safe_topic(topic: str, name: str) -> str:
    if topic.startswith("$") or "+" in topic or "#" in topic or "\x00" in topic:
        raise ConfigurationError(f"{name} must be a concrete application topic")
    return topic


def _canonical_remote_root(value: str) -> PurePosixPath:
    if "\\" in value or "\x00" in value:
        raise ConfigurationError("SFTP_REMOTE_ROOT is not a canonical POSIX path")
    root = PurePosixPath(value)
    if (
        not root.is_absolute()
        or root.as_posix() != value
        or value.startswith("//")
        or any(part in {"", ".", ".."} for part in root.parts[1:])
    ):
        raise ConfigurationError("SFTP_REMOTE_ROOT must be an absolute canonical path")
    return root


@dataclass(frozen=True, slots=True)
class MQTTConfig:
    host: str
    port: int
    topic: str
    username: str
    password: str
    qos: int
    retain: bool
    keepalive: int
    connect_timeout: float
    publish_timeout: float
    shutdown_timeout: float
    client_id_prefix: str
    reconnect_min_delay: int
    reconnect_max_delay: int
    ca_file: Path | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "MQTTConfig":
        values = os.environ if env is None else env
        host = _required(values, "MQTT_HOST")
        ca_file = _existing_file(values, "MQTT_CA_FILE", optional=True)
        if not _loopback(host) and ca_file is None:
            raise ConfigurationError("MQTT_CA_FILE is required for a non-loopback host")
        minimum = _integer(values, "MQTT_RECONNECT_MIN_DELAY", 1, 3600)
        maximum = _integer(values, "MQTT_RECONNECT_MAX_DELAY", 1, 3600)
        if maximum < minimum:
            raise ConfigurationError("MQTT_RECONNECT_MAX_DELAY must not be below the minimum")
        prefix = _required(values, "MQTT_CLIENT_ID_PREFIX")
        if len(prefix.encode("utf-8")) > 32:
            raise ConfigurationError("MQTT_CLIENT_ID_PREFIX is too long")
        return cls(
            host=host,
            port=_integer(values, "MQTT_PORT", 1, 65535),
            topic=_safe_topic(_required(values, "MQTT_TOPIC"), "MQTT_TOPIC"),
            username=_required(values, "MQTT_USERNAME"),
            password=_required_secret(values, "MQTT_PASSWORD"),
            qos=_integer(values, "MQTT_QOS", 0, 2),
            retain=_boolean(values, "MQTT_RETAIN"),
            keepalive=_integer(values, "MQTT_KEEPALIVE", 1, 65535),
            connect_timeout=_seconds(values, "MQTT_CONNECT_TIMEOUT"),
            publish_timeout=_seconds(values, "MQTT_PUBLISH_TIMEOUT"),
            shutdown_timeout=_seconds(values, "MQTT_SHUTDOWN_TIMEOUT"),
            client_id_prefix=prefix,
            reconnect_min_delay=minimum,
            reconnect_max_delay=maximum,
            ca_file=ca_file,
        )


@dataclass(frozen=True, slots=True)
class BrokerConfig:
    host: str
    port: int
    password_file: Path
    acl_file: Path
    shutdown_timeout: float

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "BrokerConfig":
        values = os.environ if env is None else env
        host = _required(values, "MQTT_BROKER_HOST")
        if not _loopback(host):
            raise ConfigurationError("MQTT_BROKER_HOST must be loopback")
        return cls(
            host=host,
            port=_integer(values, "MQTT_BROKER_PORT", 1, 65535),
            password_file=_existing_file(values, "MQTT_BROKER_PASSWORD_FILE"),
            acl_file=_existing_file(values, "MQTT_BROKER_ACL_FILE"),
            shutdown_timeout=_seconds(values, "MQTT_BROKER_SHUTDOWN_TIMEOUT"),
        )


@dataclass(frozen=True, slots=True)
class SFTPConfig:
    host: str
    port: int
    username: str
    password: str | None
    key_file: Path | None
    known_hosts: Path
    local_root: Path
    remote_root: PurePosixPath
    connect_timeout: float
    auth_timeout: float
    banner_timeout: float
    operation_timeout: float

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SFTPConfig":
        values = os.environ if env is None else env
        password = _optional_secret(values, "SFTP_PASSWORD")
        key_file = _existing_file(values, "SFTP_KEY_FILE", optional=True)
        if (password is None) == (key_file is None):
            raise ConfigurationError("exactly one SFTP authentication mode is required")
        remote_root = _canonical_remote_root(_required(values, "SFTP_REMOTE_ROOT"))
        return cls(
            host=_required(values, "SFTP_HOST"),
            port=_integer(values, "SFTP_PORT", 1, 65535),
            username=_required(values, "SFTP_USERNAME"),
            password=password,
            key_file=key_file,
            known_hosts=_existing_file(values, "SFTP_KNOWN_HOSTS"),
            local_root=_existing_root(values, "SFTP_LOCAL_ROOT"),
            remote_root=remote_root,
            connect_timeout=_seconds(values, "SFTP_CONNECT_TIMEOUT"),
            auth_timeout=_seconds(values, "SFTP_AUTH_TIMEOUT"),
            banner_timeout=_seconds(values, "SFTP_BANNER_TIMEOUT"),
            operation_timeout=_seconds(values, "SFTP_OPERATION_TIMEOUT"),
        )
