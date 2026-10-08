from __future__ import annotations

import ast
import csv
import hashlib
import importlib.metadata
import io
import json
from pathlib import Path
import sys
import tomllib
import zipfile

import pytest

import bootstrap_env


ROOT = Path(__file__).parents[1]


def test_installed_graph_is_exact_and_pip_free():
    actual = {bootstrap_env.canonicalize(item.metadata["Name"]): item.version for item in importlib.metadata.distributions()}
    expected = dict(bootstrap_env.EXPECTED)
    if sys.platform == "linux":
        expected.pop("colorama")
    assert actual == expected
    assert not set(actual).intersection(bootstrap_env.FORBIDDEN_INSTALLED)


@pytest.mark.parametrize(
    ("name", "count", "environment"),
    [
        ("pylock.windows.toml", 31, "sys_platform == 'win32' and platform_machine == 'AMD64'"),
        ("pylock.linux.toml", 30, "sys_platform == 'linux' and platform_machine == 'x86_64'"),
    ],
)
def test_locks_are_exact_wheel_only_pep751(name, count, environment):
    lock = tomllib.loads((ROOT / name).read_text(encoding="utf-8"))
    assert lock["lock-version"] == "1.0"
    assert lock["requires-python"] == "==3.13.16"
    assert lock["environments"] == [environment]
    assert len(lock["packages"]) == count
    names = set()
    for package in lock["packages"]:
        canonical = bootstrap_env.canonicalize(package["name"])
        assert canonical not in names
        names.add(canonical)
        assert package["version"] == bootstrap_env.EXPECTED[canonical]
        assert set(package).issubset({"name", "version", "marker", "wheels"})
        assert len(package["wheels"]) == 1
        wheel = package["wheels"][0]
        assert set(wheel["hashes"]) == {"sha256"}
        assert len(wheel["hashes"]["sha256"]) == 64
        assert wheel["url"].startswith("https://files.pythonhosted.org/")
        assert wheel["url"].endswith(".whl")
        assert wheel["size"] > 0


def test_bootstrap_rejects_outer_hash_before_archive_use(tmp_path):
    descriptor = {
        "url": "https://files.pythonhosted.org/packages/example.whl",
        "size": 3,
        "hashes": {"sha256": hashlib.sha256(b"expected").hexdigest()},
    }
    (tmp_path / "example.whl").write_bytes(b"bad")
    with pytest.raises(ValueError, match="size/hash mismatch"):
        bootstrap_env.validate_outer(tmp_path, descriptor)


def test_bootstrap_rejects_unsigned_record_member(tmp_path):
    wheel = tmp_path / "example-1.0-py3-none-any.whl"
    record_name = "example-1.0.dist-info/RECORD"
    rows = [
        ["example.py", "", ""],
        [record_name, "", ""],
    ]
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("example.py", b"value=1\n")
        archive.writestr(record_name, buffer.getvalue())
    with pytest.raises(ValueError, match="unsigned wheel payload"):
        bootstrap_env.validate_record(wheel)


def test_project_metadata_and_source_scope_are_exact():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == "0.1.1"
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == "0.1.1"
    handover = (ROOT / "docs" / "HANDOVER_0.1.1.md").read_text(encoding="utf-8")
    assert "external task evidence" in handover
    assert "does not authorize commit" in handover
    assert project["project"]["requires-python"] == "==3.13.16"
    assert project["project"]["dependencies"] == [
        "amqtt==0.12.1",
        "paho-mqtt==2.1.0",
        "paramiko==5.0.0",
    ]
    assert project["project"]["optional-dependencies"]["test"] == ["pytest==9.1.1"]
    assert "hbmqtt" not in "\n".join(
        path.read_text(encoding="utf-8", errors="strict")
        for path in ROOT.glob("*.py")
    ).lower()


def test_application_has_no_import_time_prompt_or_literal_auth_call():
    for name in ("brokerServer.py", "receiveMessage.py", "sendMessage.py", "SFTP.py", "runtime_config.py", "mqtt_runtime.py"):
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"), filename=name)
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                    assert value.func.id not in {"input", "open"}, name
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                function = node.value.func
                assert not (isinstance(function, ast.Name) and function.id in {"input", "open"}), name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg in {"password", "username", "hostname", "host"}:
                    assert not isinstance(keyword.value, ast.Constant) or not isinstance(keyword.value.value, str), name
