from __future__ import annotations

import asyncio
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import socket
import threading
import time

import paramiko
import pytest
from pwdlib import PasswordHash

from brokerServer import run_broker
from runtime_config import BrokerConfig, MQTTConfig, SFTPConfig


SYNTHETIC_USER = "fixture-user"
SYNTHETIC_PASSWORD = "fixture-password-not-production"
SYNTHETIC_TOPIC = "fixture/security"


@pytest.fixture(scope="session", autouse=True)
def deny_non_loopback_network():
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def allowed(address) -> bool:
        if isinstance(address, tuple) and address:
            host = str(address[0]).rstrip(".").lower()
            if host == "localhost":
                return True
            try:
                return ipaddress.ip_address(host).is_loopback
            except ValueError:
                return False
        return False

    def guarded_connect(instance, address):
        if not allowed(address):
            raise RuntimeError("external network is forbidden in the test matrix")
        return original_connect(instance, address)

    def guarded_connect_ex(instance, address):
        if not allowed(address):
            raise RuntimeError("external network is forbidden in the test matrix")
        return original_connect_ex(instance, address)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    try:
        yield
    finally:
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex


def unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def mqtt_config(port: int, *, qos: int = 1, username: str = SYNTHETIC_USER, topic: str = SYNTHETIC_TOPIC) -> MQTTConfig:
    return MQTTConfig(
        host="127.0.0.1",
        port=port,
        topic=topic,
        username=username,
        password=SYNTHETIC_PASSWORD,
        qos=qos,
        retain=False,
        keepalive=10,
        connect_timeout=5.0,
        publish_timeout=5.0,
        shutdown_timeout=5.0,
        client_id_prefix="fixture",
        reconnect_min_delay=1,
        reconnect_max_delay=2,
        ca_file=None,
    )


class BrokerThread:
    def __init__(self, config: BrokerConfig) -> None:
        self.config = config
        self.ready = threading.Event()
        self.stop_requested = threading.Event()
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._target, name="fixture-amqtt", daemon=True)

    def _target(self) -> None:
        async def serve() -> None:
            stop = asyncio.Event()

            async def bridge() -> None:
                self.ready.set()
                while not self.stop_requested.is_set():
                    await asyncio.sleep(0.02)
                stop.set()

            bridge_task = asyncio.create_task(bridge())
            try:
                await run_broker(self.config, stop)
            finally:
                bridge_task.cancel()
                await asyncio.gather(bridge_task, return_exceptions=True)

        try:
            asyncio.run(serve())
        except BaseException as exc:
            self.error = exc
            self.ready.set()

    def start(self) -> None:
        self.thread.start()
        if not self.ready.wait(10):
            raise TimeoutError("broker fixture did not start")
        if self.error is not None:
            raise self.error
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((self.config.host, self.config.port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.02)
        raise TimeoutError("broker listener did not become ready")

    def stop(self) -> None:
        self.stop_requested.set()
        self.thread.join(10)
        if self.thread.is_alive():
            raise TimeoutError("broker fixture did not stop")
        if self.error is not None:
            raise self.error


@pytest.fixture
def mqtt_broker(tmp_path: Path):
    port = unused_port()
    password_file = tmp_path / "passwords.txt"
    password_file.write_text(
        f"{SYNTHETIC_USER}:{PasswordHash.recommended().hash(SYNTHETIC_PASSWORD)}\n"
        f"restricted:{PasswordHash.recommended().hash(SYNTHETIC_PASSWORD)}\n",
        encoding="utf-8",
    )
    acl_file = tmp_path / "acl.json"
    acl_file.write_text(
        json.dumps(
            {
                "publish": {SYNTHETIC_USER: ["fixture/#"], "restricted": ["restricted/#"]},
                "subscribe": {SYNTHETIC_USER: ["fixture/#"], "restricted": ["restricted/#"]},
            }
        ),
        encoding="utf-8",
    )
    config = BrokerConfig("127.0.0.1", port, password_file, acl_file, 5.0)
    server = BrokerThread(config)
    server.start()
    try:
        yield server, mqtt_config(port)
    finally:
        server.stop()


class LocalServer(paramiko.ServerInterface):
    def check_auth_password(self, username, password):
        return (
            paramiko.AUTH_SUCCESSFUL
            if username == SYNTHETIC_USER and password == SYNTHETIC_PASSWORD
            else paramiko.AUTH_FAILED
        )

    def get_allowed_auths(self, _username):
        return "password"

    def check_channel_request(self, kind, _channel_id):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class LocalSFTPInterface(paramiko.SFTPServerInterface):
    def __init__(self, server, *args, root: Path, **kwargs):
        super().__init__(server, *args, **kwargs)
        self.root = root.resolve()

    def _path(self, value: str) -> Path:
        path = PurePosixPath(value)
        if "\\" in value or any(part in {"", ".", ".."} for part in path.parts):
            raise PermissionError
        relative = PurePosixPath(*path.parts[1:]) if path.is_absolute() else path
        candidate = self.root.joinpath(*relative.parts).resolve()
        candidate.relative_to(self.root)
        return candidate

    def canonicalize(self, path):
        try:
            candidate = self._path(path)
        except (PermissionError, ValueError):
            return "/"
        relative = candidate.relative_to(self.root).as_posix()
        return "/" + relative if relative != "." else "/"

    def open(self, path, flags, attr):
        try:
            target = self._path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(target, flags, 0o600)
            if flags & os.O_RDWR:
                mode = "r+b"
            elif flags & os.O_WRONLY:
                mode = "wb"
            else:
                mode = "rb"
            stream = os.fdopen(descriptor, mode)
            handle = paramiko.SFTPHandle(flags)
            if flags & os.O_WRONLY or flags & os.O_RDWR:
                handle.writefile = stream
            if not flags & os.O_WRONLY or flags & os.O_RDWR:
                handle.readfile = stream
            return handle
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(self._path(path).stat())
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def lstat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(self._path(path).lstat())
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def remove(self, path):
        try:
            self._path(path).unlink()
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def rename(self, _oldpath, _newpath):
        return paramiko.SFTP_OP_UNSUPPORTED

    def posix_rename(self, oldpath, newpath):
        try:
            os.replace(self._path(oldpath), self._path(newpath))
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)


class SFTPThread:
    def __init__(self, root: Path, host_key: paramiko.PKey, port: int) -> None:
        self.root = root
        self.host_key = host_key
        self.port = port
        self.stop_requested = threading.Event()
        self.ready = threading.Event()
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._serve, name="fixture-sftp", daemon=True)
        self.listener: socket.socket | None = None

    def _serve(self) -> None:
        transports: list[paramiko.Transport] = []
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                self.listener = listener
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", self.port))
                listener.listen(8)
                listener.settimeout(0.2)
                self.ready.set()
                while not self.stop_requested.is_set():
                    try:
                        connection, _address = listener.accept()
                    except TimeoutError:
                        continue
                    transport = paramiko.Transport(connection)
                    transports.append(transport)
                    transport.add_server_key(self.host_key)
                    transport.set_subsystem_handler(
                        "sftp",
                        paramiko.SFTPServer,
                        LocalSFTPInterface,
                        root=self.root,
                    )
                    try:
                        transport.start_server(server=LocalServer())
                    except (EOFError, OSError, paramiko.SSHException):
                        # A client rejecting the host key can close during the
                        # handshake.  Keep the fixture available so the next
                        # independent authentication-negative case exercises
                        # the server rather than a dead listener.
                        transport.close()
                        continue
                for transport in transports:
                    transport.close()
        except BaseException as exc:
            self.error = exc
            self.ready.set()

    def start(self) -> None:
        self.thread.start()
        if not self.ready.wait(10):
            raise TimeoutError("SFTP fixture did not start")
        if self.error is not None:
            raise self.error

    def stop(self) -> None:
        self.stop_requested.set()
        self.thread.join(10)
        if self.thread.is_alive():
            raise TimeoutError("SFTP fixture did not stop")
        if self.error is not None:
            raise self.error


@pytest.fixture
def sftp_server(tmp_path: Path):
    server_root = tmp_path / "server"
    local_root = tmp_path / "local"
    server_root.joinpath("data").mkdir(parents=True)
    local_root.mkdir()
    port = unused_port()
    host_key = paramiko.RSAKey.generate(2048)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(
        f"[127.0.0.1]:{port} {host_key.get_name()} {host_key.get_base64()}\n",
        encoding="utf-8",
    )
    config = SFTPConfig(
        host="127.0.0.1",
        port=port,
        username=SYNTHETIC_USER,
        password=SYNTHETIC_PASSWORD,
        key_file=None,
        known_hosts=known_hosts,
        local_root=local_root,
        remote_root=PurePosixPath("/data"),
        connect_timeout=5.0,
        auth_timeout=5.0,
        banner_timeout=5.0,
        operation_timeout=5.0,
    )
    server = SFTPThread(server_root, host_key, port)
    server.start()
    try:
        yield server, config, server_root, local_root
    finally:
        server.stop()
