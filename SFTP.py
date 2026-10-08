"""Host-key-verified SFTP with contained paths and atomic finalization."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import errno
import ntpath
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
from typing import Callable

import paramiko

from runtime_config import SFTPConfig


class TransferOutcome(str, Enum):
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class TransferResult:
    outcome: TransferOutcome
    byte_count: int | None
    relative_path: str


_WINDOWS_EXPLICIT_RESERVED_NAMES = {"CLOCK$"}


def _safe_portable_component(part: str) -> bool:
    if not part or part in {".", ".."} or part.endswith((".", " ")) or ":" in part:
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in part):
        return False
    stem = part.split(".", 1)[0].upper()
    return not ntpath.isreserved(part) and stem not in _WINDOWS_EXPLICIT_RESERVED_NAMES


def _relative(value: str, *, remote: bool) -> PurePosixPath:
    if not value or "\x00" in value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("path is empty or contains control characters")
    if "\\" in value or value.startswith(("/", "//")) or value.endswith("/") or "//" in value:
        raise ValueError("path must use canonical relative POSIX separators")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or not path.parts
        or any(not _safe_portable_component(part) for part in path.parts)
    ):
        raise ValueError("path must be a contained portable relative path")
    return path


def _is_reparse_or_link(path: Path) -> bool:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        return True
    try:
        attributes = os.lstat(path).st_file_attributes
    except (AttributeError, FileNotFoundError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _local_path(root: Path, value: str, *, must_exist: bool) -> Path:
    relative = _relative(value, remote=False)
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() and _is_reparse_or_link(current):
            raise ValueError("local path crosses a link or reparse point")
    resolved = current.resolve(strict=must_exist)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("local path escapes the configured root") from exc
    return resolved


def _remote_path(root: PurePosixPath, value: str) -> PurePosixPath:
    relative = _relative(value, remote=True)
    candidate = root.joinpath(relative)
    if candidate.parts[: len(root.parts)] != root.parts:
        raise ValueError("remote path escapes the configured root")
    return candidate


SSHFactory = Callable[[], paramiko.SSHClient]


class AtomicSFTP:
    def __init__(self, config: SFTPConfig, *, ssh_factory: SSHFactory = paramiko.SSHClient) -> None:
        self.config = config
        self._ssh_factory = ssh_factory
        self._ssh: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None

    def __enter__(self) -> "AtomicSFTP":
        ssh = self._ssh_factory()
        ssh.load_host_keys(str(self.config.known_hosts))
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        connect: dict[str, object] = {
            "hostname": self.config.host,
            "port": self.config.port,
            "username": self.config.username,
            "allow_agent": False,
            "look_for_keys": False,
            "timeout": self.config.connect_timeout,
            "auth_timeout": self.config.auth_timeout,
            "banner_timeout": self.config.banner_timeout,
        }
        if self.config.password is not None:
            connect["password"] = self.config.password
        else:
            connect["key_filename"] = str(self.config.key_file)
        try:
            ssh.connect(**connect)
            sftp = ssh.open_sftp()
            sftp.get_channel().settimeout(self.config.operation_timeout)
        except BaseException:
            ssh.close()
            raise
        self._ssh = ssh
        self._sftp = sftp
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        if self._sftp is not None:
            self._sftp.close()
        if self._ssh is not None:
            self._ssh.close()
        self._sftp = None
        self._ssh = None

    def _client(self) -> paramiko.SFTPClient:
        if self._sftp is None:
            raise RuntimeError("SFTP connection is not open")
        return self._sftp

    def upload(self, local_relative: str, remote_relative: str) -> TransferResult:
        local = _local_path(self.config.local_root, local_relative, must_exist=True)
        if not local.is_file() or _is_reparse_or_link(local):
            raise ValueError("upload source must be a regular contained file")
        remote = _remote_path(self.config.remote_root, remote_relative)
        temporary = remote.with_name(f".{remote.name}.mqtt-upload-{secrets.token_hex(12)}.tmp")
        sftp = self._client()
        transfer_attempted = False
        size = local.stat().st_size
        try:
            with local.open("rb") as source:
                before = os.fstat(source.fileno())
                transfer_attempted = True
                sftp.putfo(source, str(temporary), file_size=before.st_size, confirm=True)
                after = os.fstat(source.fileno())
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise OSError("local source changed during transfer")
            if sftp.stat(str(temporary)).st_size != size:
                raise OSError("remote temporary size mismatch")
            try:
                sftp.posix_rename(str(temporary), str(remote))
            except (EOFError, TimeoutError, paramiko.SSHException, OSError) as exc:
                if isinstance(exc, OSError) and getattr(exc, "errno", None) in {
                    errno.EOPNOTSUPP,
                    errno.ENOSYS,
                }:
                    self._remove_owned_temp(temporary)
                    return TransferResult(TransferOutcome.REJECTED, None, remote_relative)
                self._remove_owned_temp(temporary)
                return TransferResult(TransferOutcome.UNKNOWN, None, remote_relative)
        except (EOFError, TimeoutError, paramiko.SSHException, OSError):
            if transfer_attempted:
                self._remove_owned_temp(temporary)
            return TransferResult(TransferOutcome.REJECTED, None, remote_relative)
        return TransferResult(TransferOutcome.CONFIRMED, size, remote_relative)

    def _remove_owned_temp(self, path: PurePosixPath) -> None:
        try:
            self._client().remove(str(path))
        except (EOFError, TimeoutError, paramiko.SSHException, OSError):
            return

    def download(self, remote_relative: str, local_relative: str) -> TransferResult:
        remote = _remote_path(self.config.remote_root, remote_relative)
        final = _local_path(self.config.local_root, local_relative, must_exist=False)
        final.parent.mkdir(parents=True, exist_ok=True)
        if _is_reparse_or_link(final.parent):
            raise ValueError("download destination crosses a link or reparse point")
        temporary = final.with_name(f".{final.name}.mqtt-download-{secrets.token_hex(12)}.tmp")
        sftp = self._client()
        try:
            remote_size = sftp.stat(str(remote)).st_size
            with temporary.open("xb") as target:
                sftp.getfo(str(remote), target)
                target.flush()
                os.fsync(target.fileno())
            if temporary.stat().st_size != remote_size:
                raise OSError("download size mismatch")
            checked = _local_path(self.config.local_root, local_relative, must_exist=False)
            if checked != final:
                raise OSError("download destination changed")
            os.replace(temporary, final)
            directory_fd = None
            try:
                directory_fd = os.open(final.parent, os.O_RDONLY)
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                if directory_fd is not None:
                    os.close(directory_fd)
        except (EOFError, TimeoutError, paramiko.SSHException, OSError):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return TransferResult(TransferOutcome.REJECTED, None, local_relative)
        return TransferResult(TransferOutcome.CONFIRMED, remote_size, local_relative)


def getConnect(config: SFTPConfig | None = None) -> AtomicSFTP:
    """Compatibility factory returning the secured context manager."""
    return AtomicSFTP(config or SFTPConfig.from_env())


def uploadFile(local_path: str, remote_path: str, config: SFTPConfig | None = None) -> TransferResult:
    with getConnect(config) as adapter:
        return adapter.upload(local_path, remote_path)


def downloadFile(remote_path: str, local_path: str, config: SFTPConfig | None = None) -> TransferResult:
    with getConnect(config) as adapter:
        return adapter.download(remote_path, local_path)
