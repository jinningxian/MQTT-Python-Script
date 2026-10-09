"""Install the exact platform lock into an empty pip-free virtualenv.

All artifacts must already be in ``--wheelhouse``. The bootstrap validates
outer hashes, safe archive paths, every RECORD row, tags, metadata identity,
active dependency closure, and the final installed graph. It never downloads.
"""

from __future__ import annotations

import argparse
import base64
import csv
import email.parser
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import runpy
import socket
import stat
import sys
import tomllib
from urllib.parse import urlsplit
import zipfile


EXPECTED_PYTHON = (3, 13, 16)
EXPECTED = {
    "amqtt": "0.12.1",
    "annotated-doc": "0.0.5",
    "argon2-cffi": "25.1.0",
    "argon2-cffi-bindings": "26.1.0",
    "bcrypt": "5.0.0",
    "cffi": "2.1.1",
    "colorama": "0.4.6",
    "cryptography": "50.0.2",
    "dacite": "1.9.2",
    "iniconfig": "2.3.1",
    "invoke": "3.0.3",
    "markdown-it-py": "4.2.0",
    "mdurl": "0.1.2",
    "packaging": "26.3",
    "paho-mqtt": "2.1.0",
    "paramiko": "5.0.0",
    "pluggy": "1.6.0",
    "psutil": "7.2.2",
    "pwdlib": "0.3.1",
    "pycparser": "3.0",
    "pygments": "2.21.0",
    "pynacl": "1.6.2",
    "pytest": "9.1.1",
    "pyyaml": "6.0.3",
    "rich": "15.0.0",
    "shellingham": "1.5.4",
    "six": "1.17.0",
    "transitions": "0.9.3",
    "typer": "0.27.3",
    "typing-extensions": "4.16.0",
    "websockets": "15.0.1",
}
DIRECT_ROOTS = {
    "amqtt": "0.12.1",
    "paho-mqtt": "2.1.0",
    "paramiko": "5.0.0",
    "pytest": "9.1.1",
}
INSTALLER = {
    "name": "installer",
    "version": "1.0.1",
    "filename": "installer-1.0.1-py3-none-any.whl",
    "url": "https://files.pythonhosted.org/packages/26/48/bedf3ba4163e7db392c9d4cbdfc8217423196c8fccbe5a404a0f094fde63/installer-1.0.1-py3-none-any.whl",
    "size": 464455,
    "hashes": {"sha256": "011d045df8b954ced7dde3a7e42ae4418da40ecda7990f2d11d5ed7c146fd98b"},
}
FORBIDDEN_INSTALLED = {"installer", "pip", "setuptools", "uv", "wheel"}


def canonicalize(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unsafe_windows_part(part: str) -> bool:
    stem = part.split(".", 1)[0].upper()
    devices = {"CON", "PRN", "AUX", "NUL"}
    devices.update({f"COM{number}" for number in range(1, 10)})
    devices.update({f"LPT{number}" for number in range(1, 10)})
    return stem in devices or part.endswith((" ", "."))


def validate_record(path: Path) -> list[str]:
    """Reject unsafe members and payloads that RECORD does not bind."""
    with zipfile.ZipFile(path) as archive:
        infos = [item for item in archive.infolist() if not item.is_dir()]
        names: dict[str, str] = {}
        expanded = 0
        for item in infos:
            name = item.filename
            parts = name.split("/")
            normalized = name.casefold()
            mode = (item.external_attr >> 16) & 0xFFFF
            if (
                not name
                or item.orig_filename != name
                or name.startswith("/")
                or "\\" in name
                or ":" in name
                or any(ord(character) < 32 or ord(character) == 127 for character in name)
                or any(part in {"", ".", ".."} or _unsafe_windows_part(part) for part in parts)
                or normalized in names
                or stat.S_ISLNK(mode)
                or (stat.S_IFMT(mode) not in {0, stat.S_IFREG})
                or item.flag_bits & 1
                or name.casefold().endswith(".pth")
            ):
                raise ValueError(f"unsafe or duplicate wheel member: {path.name}")
            names[normalized] = name
            expanded += item.file_size
            if item.file_size > 128 * 1024 * 1024 or expanded > 512 * 1024 * 1024:
                raise ValueError(f"wheel expansion bound exceeded: {path.name}")
        records = [item.filename for item in infos if item.filename.endswith(".dist-info/RECORD")]
        if len(records) != 1:
            raise ValueError(f"expected exactly one RECORD: {path.name}")
        rows = list(csv.reader(io.StringIO(archive.read(records[0]).decode("utf-8"))))
        mapping: dict[str, tuple[str, str]] = {}
        for row in rows:
            if len(row) != 3 or row[0] in mapping:
                raise ValueError(f"invalid or duplicate RECORD row: {path.name}")
            mapping[row[0]] = (row[1], row[2])
        actual_names = {item.filename for item in infos}
        if set(mapping) != actual_names:
            raise ValueError(f"RECORD membership mismatch: {path.name}")
        for name in actual_names:
            hash_field, size_field = mapping[name]
            if name == records[0]:
                if hash_field or size_field:
                    raise ValueError(f"RECORD self-row must be unsigned: {path.name}")
                continue
            if not hash_field or not size_field:
                raise ValueError(f"unsigned wheel payload: {path.name}")
            algorithm, encoded = hash_field.split("=", 1)
            if algorithm not in {"sha256", "sha384", "sha512"}:
                raise ValueError(f"weak RECORD hash: {path.name}")
            payload = archive.read(name)
            actual = base64.urlsafe_b64encode(hashlib.new(algorithm, payload).digest()).rstrip(b"=").decode("ascii")
            if actual != encoded or len(payload) != int(size_field):
                raise ValueError(f"RECORD content mismatch: {path.name}")
        return sorted(
            name for name in actual_names
            if name.lower().endswith((".dll", ".dylib", ".pyd", ".so")) or ".so." in name.lower()
        )


def wheel_filename(descriptor: dict[str, object]) -> str:
    parsed = urlsplit(str(descriptor.get("url", "")))
    filename = PurePosixPath(parsed.path).name
    if (
        parsed.scheme != "https"
        or parsed.hostname != "files.pythonhosted.org"
        or parsed.username
        or parsed.password
        or not filename.endswith(".whl")
        or PurePosixPath(filename).name != filename
    ):
        raise ValueError("wheel URL is outside official PyPI files")
    return filename


def validate_outer(wheelhouse: Path, descriptor: dict[str, object]) -> tuple[Path, list[str]]:
    filename = wheel_filename(descriptor)
    path = (wheelhouse / filename).resolve()
    hashes = descriptor.get("hashes")
    if (
        path.parent != wheelhouse.resolve()
        or not path.is_file()
        or path.is_symlink()
        or not isinstance(hashes, dict)
        or set(hashes) != {"sha256"}
        or not isinstance(hashes["sha256"], str)
        or len(hashes["sha256"]) != 64
        or not isinstance(descriptor.get("size"), int)
        or descriptor["size"] <= 0
    ):
        raise ValueError(f"invalid or missing exact wheel: {filename}")
    if path.stat().st_size != descriptor["size"] or sha256_file(path) != hashes["sha256"]:
        raise ValueError(f"wheel size/hash mismatch: {filename}")
    return path, validate_record(path)


def wheel_metadata(path: Path) -> dict[str, object]:
    with zipfile.ZipFile(path) as archive:
        metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        wheel_names = [name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")]
        if len(metadata_names) != 1 or len(wheel_names) != 1:
            raise ValueError(f"wheel metadata cardinality changed: {path.name}")
        metadata = email.parser.BytesParser().parsebytes(archive.read(metadata_names[0]))
        wheel = email.parser.BytesParser().parsebytes(archive.read(wheel_names[0]))
    return {
        "name": canonicalize(metadata["Name"] or ""),
        "version": metadata["Version"] or "",
        "requires_python": metadata["Requires-Python"],
        "requires_dist": metadata.get_all("Requires-Dist", []),
        "provides_extra": metadata.get_all("Provides-Extra", []),
        "wheel_version": wheel["Wheel-Version"],
        "wheel_tags": wheel.get_all("Tag", []),
    }


def expected_lock_name() -> tuple[str, str, int, set[str]]:
    if sys.platform == "win32" and platform.machine() == "AMD64":
        return (
            "pylock.windows.toml",
            "sys_platform == 'win32' and platform_machine == 'AMD64'",
            31,
            set(EXPECTED),
        )
    if sys.platform == "linux" and platform.machine() == "x86_64":
        expected = set(EXPECTED) - {"colorama"}
        return (
            "pylock.linux.toml",
            "sys_platform == 'linux' and platform_machine == 'x86_64'",
            30,
            expected,
        )
    raise ValueError("unsupported platform or architecture")


def select_and_verify(lock_path: Path, wheelhouse: Path):
    if sys.version_info[:3] != EXPECTED_PYTHON:
        raise ValueError("exact Python 3.13.16 is required")
    lock_name, environment_marker, expected_count, expected_names = expected_lock_name()
    if lock_path.name != lock_name:
        raise ValueError(f"platform lock mismatch: expected {lock_name}")
    raw = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    if (
        raw.get("lock-version") != "1.0"
        or raw.get("requires-python") != "==3.13.16"
        or raw.get("environments") != [environment_marker]
    ):
        raise ValueError("lock header changed")
    packages = raw.get("packages")
    if not isinstance(packages, list) or len(packages) != expected_count:
        raise ValueError("lock package cardinality changed")
    names = {canonicalize(str(item.get("name", ""))) for item in packages if isinstance(item, dict)}
    if names != expected_names:
        raise ValueError("lock package set changed")
    for package in packages:
        name = canonicalize(str(package.get("name", "")))
        if str(package.get("version", "")) != EXPECTED[name]:
            raise ValueError(f"lock version changed: {name}")
        if any(key in package for key in ("sdist", "archive", "vcs", "directory")):
            raise ValueError("only exact wheels are permitted")
        wheels = package.get("wheels")
        if not isinstance(wheels, list) or len(wheels) != 1:
            raise ValueError("every package must bind exactly one wheel")

    packaging_item = next(item for item in packages if canonicalize(str(item["name"])) == "packaging")
    packaging_path, _native = validate_outer(wheelhouse, packaging_item["wheels"][0])
    installer_path, installer_native = validate_outer(wheelhouse, INSTALLER)
    if installer_native:
        raise ValueError("bootstrap installer unexpectedly contains native code")

    sys.path.insert(0, str(packaging_path))
    try:
        from packaging.markers import default_environment
        from packaging.pylock import Pylock
        from packaging.requirements import Requirement
        from packaging.tags import parse_tag, sys_tags
        from packaging.utils import parse_wheel_filename
        from packaging.version import Version

        lock = Pylock.from_dict(raw)
        lock.validate()
        supported = set(sys_tags())
        selected: list[Path] = []
        identities: list[dict[str, object]] = []
        metadata: dict[str, dict[str, object]] = {}
        native_payloads: list[dict[str, object]] = []
        for package, artifact in lock.select():
            descriptor = {"url": artifact.url, "size": artifact.size, "hashes": dict(artifact.hashes)}
            path, native = validate_outer(wheelhouse, descriptor)
            parsed_name, parsed_version, _build, tags = parse_wheel_filename(path.name)
            if canonicalize(str(parsed_name)) != str(package.name) or parsed_version != package.version or not tags.intersection(supported):
                raise ValueError(f"wheel filename identity/tag mismatch: {path.name}")
            item = wheel_metadata(path)
            if item["name"] != str(package.name) or item["version"] != str(package.version):
                raise ValueError(f"METADATA identity mismatch: {path.name}")
            if item["requires_python"] and not Requirement(f"placeholder{item['requires_python']}").specifier.contains(Version(platform.python_version())):
                raise ValueError(f"Requires-Python mismatch: {path.name}")
            recorded_tags = {tag for value in item["wheel_tags"] for tag in parse_tag(str(value))}
            if item["wheel_version"] != "1.0" or recorded_tags != set(tags):
                raise ValueError(f"WHEEL metadata mismatch: {path.name}")
            selected.append(path)
            identities.append({"name": str(package.name), "version": str(package.version), "filename": path.name, "sha256": sha256_file(path)})
            metadata[str(package.name)] = item
            if native:
                native_payloads.append({"package": str(package.name), "wheel": path.name, "members": native})
        if len(selected) != expected_count or set(metadata) != expected_names:
            raise ValueError("selected graph differs from platform lock")

        environment = default_environment()
        pending = [Requirement(f"{name}=={version}") for name, version in DIRECT_ROOTS.items()]
        reachable: set[str] = set()
        requested_extras: dict[str, set[str]] = {}
        edges: set[tuple[str, str, str]] = set()
        while pending:
            requirement = pending.pop()
            name = canonicalize(requirement.name)
            if name not in metadata or not requirement.specifier.contains(Version(str(metadata[name]["version"])), prereleases=False):
                raise ValueError(f"missing or incompatible dependency: {name}")
            extras = set(requirement.extras)
            known_extras = requested_extras.setdefault(name, set())
            if name in reachable and extras.issubset(known_extras):
                continue
            provided = set(metadata[name]["provides_extra"])
            if not extras.issubset(provided):
                raise ValueError(f"unknown requested dependency extra: {name}")
            known_extras.update(extras)
            reachable.add(name)
            for value in metadata[name]["requires_dist"]:
                child = Requirement(str(value))
                active = child.marker is None or any(
                    child.marker.evaluate(environment | {"extra": extra})
                    for extra in {"", *known_extras}
                )
                if active:
                    child_name = canonicalize(child.name)
                    edges.add((name, child_name, str(child)))
                    pending.append(child)
        if reachable != expected_names:
            raise ValueError("lock contains omitted, unreachable or unexpected dependency")
        return installer_path, selected, identities, {
            "roots": len(DIRECT_ROOTS),
            "packages": len(metadata),
            "active_edges": len(edges),
            "unresolved": 0,
            "native_payloads": native_payloads,
        }
    finally:
        sys.path.remove(str(packaging_path))


def install(lock_path: Path, wheelhouse: Path, report_path: Path) -> None:
    lock_path = lock_path.resolve()
    wheelhouse = wheelhouse.resolve()
    report_path = report_path.resolve()
    if report_path.exists():
        raise ValueError("refusing to overwrite an existing report")
    baseline = list(importlib.metadata.distributions())
    if baseline:
        raise ValueError("target environment must be empty")
    installer_path, wheels, identities, graph = select_and_verify(lock_path, wheelhouse)
    socket_events: list[str] = []

    def deny_network(event: str, _arguments: tuple[object, ...]) -> None:
        if event.startswith("socket."):
            socket_events.append(event)
            raise RuntimeError("network use is forbidden during locked installation")

    sys.addaudithook(deny_network)
    old_argv = sys.argv[:]
    sys.path.insert(0, str(installer_path))
    try:
        sys.argv = ["python -m installer", "--validate-record", "all", "--no-compile-bytecode", *map(str, wheels)]
        runpy.run_module("installer", run_name="__main__")
    finally:
        sys.argv = old_argv
        sys.path.remove(str(installer_path))
    actual = {canonicalize(item.metadata["Name"]): item.version for item in importlib.metadata.distributions()}
    expected = {str(item["name"]): str(item["version"]) for item in identities}
    if actual != expected or set(actual).intersection(FORBIDDEN_INSTALLED):
        raise ValueError("installed distribution graph differs from the lock")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(
            {
                "result": "PASS",
                "python": platform.python_version(),
                "platform": sys.platform,
                "machine": platform.machine(),
                "lock_sha256": sha256_file(lock_path),
                "bootstrap_installer": {"name": INSTALLER["name"], "version": INSTALLER["version"], "sha256": INSTALLER["hashes"]["sha256"]},
                "installed": identities,
                "graph": graph,
                "external_request_attempts": len(socket_events),
                "compile_bytecode": False,
                "pip_or_uv_installed": False,
            },
            output,
            indent=2,
            sort_keys=True,
        )
        output.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--wheelhouse", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    arguments = parser.parse_args()
    install(arguments.lock, arguments.wheelhouse, arguments.report)


if __name__ == "__main__":
    main()
