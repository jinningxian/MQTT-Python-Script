from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from brokerServer import build_broker_settings
from runtime_config import BrokerConfig, ConfigurationError, MQTTConfig, SFTPConfig


def mqtt_env(tmp_path: Path) -> dict[str, str]:
    return {
        "MQTT_HOST": "127.0.0.1",
        "MQTT_PORT": "1883",
        "MQTT_TOPIC": "fixture/security",
        "MQTT_USERNAME": "fixture-user",
        "MQTT_PASSWORD": "fixture-password-not-production",
        "MQTT_QOS": "1",
        "MQTT_RETAIN": "false",
        "MQTT_KEEPALIVE": "30",
        "MQTT_CONNECT_TIMEOUT": "2",
        "MQTT_PUBLISH_TIMEOUT": "2",
        "MQTT_SHUTDOWN_TIMEOUT": "2",
        "MQTT_CLIENT_ID_PREFIX": "fixture",
        "MQTT_RECONNECT_MIN_DELAY": "1",
        "MQTT_RECONNECT_MAX_DELAY": "4",
        "MQTT_CA_FILE": "",
    }


def sftp_env(tmp_path: Path) -> dict[str, str]:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("synthetic-host-key-line\n", encoding="utf-8")
    local = tmp_path / "local"
    local.mkdir()
    return {
        "SFTP_HOST": "127.0.0.1",
        "SFTP_PORT": "22",
        "SFTP_USERNAME": "fixture-user",
        "SFTP_PASSWORD": "fixture-password-not-production",
        "SFTP_KEY_FILE": "",
        "SFTP_KNOWN_HOSTS": str(known_hosts),
        "SFTP_LOCAL_ROOT": str(local),
        "SFTP_REMOTE_ROOT": "/data",
        "SFTP_CONNECT_TIMEOUT": "2",
        "SFTP_AUTH_TIMEOUT": "2",
        "SFTP_BANNER_TIMEOUT": "2",
        "SFTP_OPERATION_TIMEOUT": "2",
    }


def test_mqtt_config_accepts_loopback_and_exact_types(tmp_path):
    config = MQTTConfig.from_env(mqtt_env(tmp_path))
    assert (config.host, config.port, config.qos, config.retain) == ("127.0.0.1", 1883, 1, False)
    assert config.ca_file is None


def test_passwords_preserve_leading_and_trailing_spaces_exactly(tmp_path):
    mqtt_values = mqtt_env(tmp_path)
    mqtt_values["MQTT_PASSWORD"] = "  exact mqtt secret  "
    assert MQTTConfig.from_env(mqtt_values).password == "  exact mqtt secret  "

    sftp_values = sftp_env(tmp_path)
    sftp_values["SFTP_PASSWORD"] = "  exact sftp secret  "
    assert SFTPConfig.from_env(sftp_values).password == "  exact sftp secret  "


@pytest.mark.parametrize("field", ["MQTT_PASSWORD", "SFTP_PASSWORD"])
@pytest.mark.parametrize("value", ["   ", "secret\n", "secret\x7f"])
def test_secret_fields_reject_whitespace_only_and_controls(tmp_path, field, value):
    values = mqtt_env(tmp_path) if field.startswith("MQTT") else sftp_env(tmp_path)
    values[field] = value
    factory = MQTTConfig.from_env if field.startswith("MQTT") else SFTPConfig.from_env
    with pytest.raises(ConfigurationError, match=field):
        factory(values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("MQTT_USERNAME", ""),
        ("MQTT_PASSWORD", ""),
        ("MQTT_PORT", "0"),
        ("MQTT_QOS", "3"),
        ("MQTT_RETAIN", "yes"),
        ("MQTT_TOPIC", "fixture/#"),
        ("MQTT_CONNECT_TIMEOUT", "0"),
        ("MQTT_RECONNECT_MAX_DELAY", "0"),
    ],
)
def test_mqtt_config_rejects_missing_or_invalid_fields(tmp_path, field, value):
    env = mqtt_env(tmp_path)
    env[field] = value
    with pytest.raises(ConfigurationError, match=field):
        MQTTConfig.from_env(env)


def test_non_loopback_mqtt_requires_existing_ca(tmp_path):
    env = mqtt_env(tmp_path)
    env["MQTT_HOST"] = "broker.example.invalid"
    with pytest.raises(ConfigurationError, match="MQTT_CA_FILE"):
        MQTTConfig.from_env(env)
    ca = tmp_path / "ca.pem"
    ca.write_text("synthetic-ca", encoding="utf-8")
    env["MQTT_CA_FILE"] = str(ca)
    assert MQTTConfig.from_env(env).ca_file == ca.resolve()


def test_broker_requires_loopback_and_explicit_plugin_files(tmp_path):
    passwords = tmp_path / "passwords"
    passwords.write_text("synthetic", encoding="utf-8")
    acl = tmp_path / "acl.json"
    acl.write_text(json.dumps({"publish": {"u": ["a/#"]}, "subscribe": {"u": ["a/#"]}}), encoding="utf-8")
    config = BrokerConfig("127.0.0.1", 1883, passwords, acl, 2.0)
    settings = build_broker_settings(config)
    assert set(settings["plugins"]) == {
        "amqtt.plugins.authentication.FileAuthPlugin",
        "amqtt.plugins.topic_checking.TopicAccessControlListPlugin",
    }
    assert "AnonymousAuthPlugin" not in repr(settings)
    env = {
        "MQTT_BROKER_HOST": "example.invalid",
        "MQTT_BROKER_PORT": "1883",
        "MQTT_BROKER_PASSWORD_FILE": str(passwords),
        "MQTT_BROKER_ACL_FILE": str(acl),
        "MQTT_BROKER_SHUTDOWN_TIMEOUT": "2",
    }
    with pytest.raises(ConfigurationError, match="MQTT_BROKER_HOST"):
        BrokerConfig.from_env(env)


def test_sftp_config_requires_one_auth_mode_and_canonical_root(tmp_path):
    env = sftp_env(tmp_path)
    config = SFTPConfig.from_env(env)
    assert config.password is not None and config.key_file is None
    key = tmp_path / "id"
    key.write_text("synthetic-key-placeholder", encoding="utf-8")
    env["SFTP_KEY_FILE"] = str(key)
    with pytest.raises(ConfigurationError, match="exactly one"):
        SFTPConfig.from_env(env)
    env["SFTP_PASSWORD"] = ""
    env["SFTP_KEY_FILE"] = ""
    with pytest.raises(ConfigurationError, match="exactly one"):
        SFTPConfig.from_env(env)
    env["SFTP_KEY_FILE"] = str(key)
    assert SFTPConfig.from_env(env).key_file == key.resolve()
    for invalid in ("../escape", "/data//nested", "//data"):
        env["SFTP_REMOTE_ROOT"] = invalid
        with pytest.raises(ConfigurationError, match="SFTP_REMOTE_ROOT"):
            SFTPConfig.from_env(env)


def test_imports_are_prompt_file_socket_loop_and_thread_safe(tmp_path):
    script = r'''
import builtins, pathlib, socket, threading
baseline={thread.ident for thread in threading.enumerate()}
def denied(*args, **kwargs):
    raise AssertionError("side effect during import")
builtins.input=denied
socket.create_connection=denied
socket.socket.connect=denied
before=set(pathlib.Path.cwd().iterdir())
import runtime_config, mqtt_runtime, brokerServer, receiveMessage, sendMessage, SFTP
after=set(pathlib.Path.cwd().iterdir())
assert before == after
assert {thread.ident for thread in threading.enumerate()} == baseline
print("PASS")
'''
    environment = dict(__import__("os").environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=Path(__file__).parents[1],
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "PASS"
