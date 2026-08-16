#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

_ALLOWED_DOMAINS = frozenset({"x.com", ".x.com", "twitter.com", ".twitter.com"})
_REQUIRED_COOKIES = ("auth_token", "ct0")


def parse_netscape_cookies(path: Path) -> dict[str, str]:
    """Extract required X cookies from a Netscape cookies.txt file."""
    cookies: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"Could not read Netscape cookie file: {path}") from exc

    for line_number, line in enumerate(lines, start=1):
        if line.startswith("#HttpOnly_"):
            line = line.removeprefix("#HttpOnly_")
        elif not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            raise ValueError(
                f"Invalid Netscape cookie record on line {line_number}: expected 7 fields"
            )
        domain, _include_subdomains, _path, _secure, _expires, name, value = fields
        if domain.lower() in _ALLOWED_DOMAINS and name in _REQUIRED_COOKIES:
            cookies[name] = value

    validate_cookies(cookies)
    return cookies


def validate_cookies(cookies: dict[str, str]) -> None:
    """Validate required cookie values without exposing them in errors."""
    for name in _REQUIRED_COOKIES:
        value = cookies.get(name)
        if not value or not value.strip():
            raise ValueError(f"Missing nonempty {name} cookie for X")
        if "..." in value or "…" in value:
            raise ValueError(f"X cookie {name} appears to be ellipsized")


def write_cookie_json(path: Path, cookies: dict[str, str]) -> None:
    """Atomically write a flat JSON cookie file with mode 0600."""
    validate_cookies(cookies)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as output:
            json.dump(cookies, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(file_descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import X authentication cookies from a Netscape cookies.txt file."
    )
    parser.add_argument("source", type=Path, help="Netscape cookies.txt input")
    parser.add_argument("destination", type=Path, help="Flat JSON cookie output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        cookies = parse_netscape_cookies(args.source)
        write_cookie_json(args.destination, cookies)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Error writing X cookie file: {exc}", file=sys.stderr)
        return 2

    print(f"Imported X cookies to {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
