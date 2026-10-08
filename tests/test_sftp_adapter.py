from __future__ import annotations

from dataclasses import replace
import errno
import os
from pathlib import Path, PurePosixPath
import subprocess
from types import SimpleNamespace

import paramiko
import pytest

from SFTP import AtomicSFTP, TransferOutcome, _local_path, _remote_path


_WINDOWS_RESERVED_COMPONENTS = (
    "COM¹",
    "LPT².txt",
    "CONIN$",
    "CONOUT$",
    "CLOCK$",
    "CLOCK$.txt",
    "a?b",
    "a*b",
    "a|b",
    "a<b",
    "a>b",
    'a"b',
)


def test_real_loopback_upload_and_download_with_unicode_and_spaces(sftp_server):
    _server, config, server_root, local_root = sftp_server
    source = local_root / "source ü space.txt"
    payload = "synthetic payload ü\n".encode()
    source.write_bytes(payload)
    final = server_root / "data" / "folder" / "remote ü space.txt"
    final.parent.mkdir()
    final.write_bytes(b"pre-existing")
    with AtomicSFTP(config) as adapter:
        uploaded = adapter.upload(source.name, "folder/remote ü space.txt")
        assert uploaded.outcome is TransferOutcome.CONFIRMED
        assert uploaded.byte_count == len(payload)
        assert final.read_bytes() == payload
        downloaded = adapter.download("folder/remote ü space.txt", "download ü space.txt")
        assert downloaded.outcome is TransferOutcome.CONFIRMED
    assert (local_root / "download ü space.txt").read_bytes() == payload
    assert not list(server_root.rglob("*.tmp"))
    assert not list(local_root.rglob("*.tmp"))


def test_unknown_host_key_and_bad_auth_are_rejected(sftp_server, tmp_path):
    _server, config, _server_root, _local_root = sftp_server
    unknown = tmp_path / "unknown_hosts"
    unknown.write_text("", encoding="utf-8")
    with pytest.raises((paramiko.SSHException, OSError)):
        with AtomicSFTP(replace(config, known_hosts=unknown)):
            pass
    mismatched = tmp_path / "mismatched_hosts"
    wrong_key = paramiko.RSAKey.generate(2048)
    mismatched.write_text(
        f"[127.0.0.1]:{config.port} {wrong_key.get_name()} {wrong_key.get_base64()}\n",
        encoding="utf-8",
    )
    with pytest.raises(paramiko.BadHostKeyException):
        with AtomicSFTP(replace(config, known_hosts=mismatched)):
            pass
    with pytest.raises(paramiko.AuthenticationException):
        with AtomicSFTP(replace(config, password="incorrect-synthetic-password")):
            pass


@pytest.mark.parametrize(
    "value",
    [
        "../escape", "/absolute", "folder/../../escape", "folder\\escape",
        "folder//file", "bad\x00name", "C:drive-relative", "C:/drive",
        "//server/share", "\\\\server\\share", "\\\\?\\C:\\device",
        "folder/name:stream", "CON", "con.txt", "NUL.data", "COM1.log",
        "LPT9", "folder/trailing.", "folder/trailing ", "folder/",
    ],
)
def test_remote_paths_fail_before_transfer(value):
    with pytest.raises(ValueError):
        _remote_path(PurePosixPath("/data"), value)


@pytest.mark.parametrize(
    "value",
    [
        "C:drive-relative", "C:/drive", "//server/share", "\\\\server\\share",
        "\\\\.\\NUL", "name:stream", "AUX.txt", "COM9.any", "LPT1",
        "folder/repeated//name", "folder/trailing.", "folder/trailing ",
    ],
)
def test_local_windows_ambiguous_paths_fail_before_filesystem_effect(tmp_path, value):
    root = tmp_path / "root"
    root.mkdir()
    before = tuple(root.iterdir())
    with pytest.raises(ValueError):
        _local_path(root.resolve(), value, must_exist=False)
    assert tuple(root.iterdir()) == before


def test_local_portable_unicode_and_internal_spaces_are_accepted(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    result = _local_path(root.resolve(), "folder ü/internal name.txt", must_exist=False)
    assert result == root.resolve() / "folder ü" / "internal name.txt"


@pytest.mark.parametrize("component", _WINDOWS_RESERVED_COMPONENTS)
def test_windows_reserved_components_fail_before_sftp_io(tmp_path, monkeypatch, component):
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.txt"
    source.write_bytes(b"synthetic")
    config = SimpleNamespace(local_root=root.resolve(), remote_root=PurePosixPath("/data"))
    adapter = AtomicSFTP(config)

    def unexpected_io():
        pytest.fail("reserved component reached the SFTP client")

    monkeypatch.setattr(adapter, "_client", unexpected_io)
    before = tuple(root.iterdir())
    with pytest.raises(ValueError):
        adapter.upload("source.txt", component)
    with pytest.raises(ValueError):
        adapter.download(component, "download.txt")
    with pytest.raises(ValueError):
        _local_path(root.resolve(), component, must_exist=False)
    assert tuple(root.iterdir()) == before


def test_local_symlink_or_junction_escape_is_rejected(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    with pytest.raises(ValueError, match="link or reparse"):
        _local_path(root.resolve(), "link/value.txt", must_exist=False)


class FakeSFTP:
    def __init__(self, mode: str = "success") -> None:
        self.mode = mode
        self.files: dict[str, bytes] = {"/data/final.txt": b"pre-existing"}
        self.put_count = 0
        self.rename_count = 0
        self.remove_count = 0

    def putfo(self, source, remote, *, file_size, confirm):
        self.put_count += 1
        payload = source.read()
        if self.mode == "partial":
            self.files[remote] = payload[: max(1, len(payload) // 2)]
            raise OSError("synthetic partial transfer")
        self.files[remote] = payload
        return SimpleNamespace(st_size=len(payload))

    def stat(self, remote):
        if remote not in self.files:
            raise FileNotFoundError(remote)
        return SimpleNamespace(st_size=len(self.files[remote]))

    def posix_rename(self, old, new):
        self.rename_count += 1
        if self.mode == "unsupported":
            raise OSError(errno.EOPNOTSUPP, "synthetic unsupported")
        if self.mode == "disconnect-before":
            raise paramiko.SSHException("synthetic disconnect")
        self.files[new] = self.files.pop(old)
        if self.mode == "disconnect-after":
            raise EOFError("synthetic disconnect after rename")

    def remove(self, remote):
        self.remove_count += 1
        self.files.pop(remote, None)


def adapter_with_fake(config, fake):
    adapter = AtomicSFTP(config)
    adapter._sftp = fake
    return adapter


def test_partial_upload_cleans_only_owned_temp_and_preserves_final(sftp_server):
    _server, config, _server_root, local_root = sftp_server
    (local_root / "source.txt").write_bytes(b"new-payload")
    fake = FakeSFTP("partial")
    result = adapter_with_fake(config, fake).upload("source.txt", "final.txt")
    assert result.outcome is TransferOutcome.REJECTED
    assert fake.files["/data/final.txt"] == b"pre-existing"
    assert list(fake.files) == ["/data/final.txt"]
    assert fake.put_count == 1 and fake.rename_count == 0 and fake.remove_count == 1


def test_unsupported_posix_rename_never_falls_back(sftp_server):
    _server, config, _server_root, local_root = sftp_server
    (local_root / "source.txt").write_bytes(b"new-payload")
    fake = FakeSFTP("unsupported")
    result = adapter_with_fake(config, fake).upload("source.txt", "final.txt")
    assert result.outcome is TransferOutcome.REJECTED
    assert fake.files == {"/data/final.txt": b"pre-existing"}
    assert fake.put_count == 1 and fake.rename_count == 1 and fake.remove_count == 1


@pytest.mark.parametrize("mode", ["disconnect-before", "disconnect-after"])
def test_disconnect_around_rename_is_unknown_and_never_retried(sftp_server, mode):
    _server, config, _server_root, local_root = sftp_server
    (local_root / "source.txt").write_bytes(b"new-payload")
    fake = FakeSFTP(mode)
    result = adapter_with_fake(config, fake).upload("source.txt", "final.txt")
    assert result.outcome is TransferOutcome.UNKNOWN
    assert fake.put_count == 1 and fake.rename_count == 1
    assert all("mqtt-upload" not in path for path in fake.files)
    if mode == "disconnect-before":
        assert fake.files["/data/final.txt"] == b"pre-existing"
    else:
        assert fake.files["/data/final.txt"] == b"new-payload"


def test_download_failure_preserves_preexisting_final(sftp_server):
    _server, config, _server_root, local_root = sftp_server
    final = local_root / "final.txt"
    final.write_bytes(b"pre-existing")
    fake = FakeSFTP()
    adapter = adapter_with_fake(config, fake)
    result = adapter.download("missing.txt", "final.txt")
    assert result.outcome is TransferOutcome.REJECTED
    assert final.read_bytes() == b"pre-existing"
