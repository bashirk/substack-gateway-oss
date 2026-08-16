from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path
from types import ModuleType

import pytest


def _load_script() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "import-x-cookies.py"
    spec = importlib.util.spec_from_file_location("import_x_cookies", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record(domain: str, name: str, value: str, cookie_path: str = "/") -> str:
    return "\t".join([domain, "TRUE", cookie_path, "TRUE", "0", name, value])


def test_parse_netscape_cookies_accepts_x_and_twitter_domains(tmp_path: Path) -> None:
    script = _load_script()
    source = tmp_path / "cookies.txt"
    source.write_text(
        "# Netscape HTTP Cookie File\n"
        + "#HttpOnly_"
        + _record(".x.com", "auth_token", "auth-secret")
        + "\n"
        + _record("twitter.com", "ct0", "csrf-secret")
        + "\n"
        + _record("example.com", "auth_token", "wrong-domain")
        + "\n",
        encoding="utf-8",
    )

    assert script.parse_netscape_cookies(source) == {
        "auth_token": "auth-secret",
        "ct0": "csrf-secret",
    }


def test_parse_rejects_missing_and_ellipsized_without_exposing_values(
    tmp_path: Path,
) -> None:
    script = _load_script()
    source = tmp_path / "cookies.txt"
    source.write_text(
        _record("x.com", "auth_token", "private...value") + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ellipsized") as error:
        script.parse_netscape_cookies(source)

    assert "private...value" not in str(error.value)


def test_write_cookie_json_atomically_replaces_with_mode_0600(tmp_path: Path) -> None:
    script = _load_script()
    destination = tmp_path / "private" / "cookies.json"
    destination.parent.mkdir()
    destination.write_text("old", encoding="utf-8")
    destination.chmod(0o644)

    script.write_cookie_json(
        destination, {"auth_token": "auth-secret", "ct0": "csrf-secret"}
    )

    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "auth_token": "auth-secret",
        "ct0": "csrf-secret",
    }
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert list(destination.parent.glob(f".{destination.name}.*.tmp")) == []


def test_cli_never_logs_cookie_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script()
    source = tmp_path / "cookies.txt"
    destination = tmp_path / "cookies.json"
    source.write_text(
        _record(".twitter.com", "auth_token", "auth-secret")
        + "\n"
        + _record(".x.com", "ct0", "csrf-secret")
        + "\n",
        encoding="utf-8",
    )

    assert script.main([str(source), str(destination)]) == 0

    output = capsys.readouterr()
    assert "auth-secret" not in output.out + output.err
    assert "csrf-secret" not in output.out + output.err
    assert destination.exists()
