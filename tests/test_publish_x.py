from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from substack_gateway.publish_x import (
    XAuthenticationError,
    XPublishOutcomeUnknownError,
    XRateLimitError,
    load_x_cookies,
    publish_x,
    validate_x_text,
)


class FakeClient:
    def __init__(
        self,
        *,
        create_error: Exception | None = None,
        upload_error: Exception | None = None,
    ) -> None:
        self.create_error = create_error
        self.upload_error = upload_error
        self.calls: list[tuple[str, object]] = []

    def set_cookies(self, cookies: dict[str, str]) -> None:
        self.calls.append(("cookies", set(cookies)))

    async def upload_media(self, source: str) -> str:
        self.calls.append(("upload", source))
        if self.upload_error is not None:
            raise self.upload_error
        return "media-123"

    async def create_tweet(
        self, text: str, media_ids: list[str] | None = None
    ) -> SimpleNamespace:
        self.calls.append(("create", (text, media_ids)))
        if self.create_error is not None:
            raise self.create_error
        return SimpleNamespace(id="tweet-456")


def _cookie_file(tmp_path: Path) -> Path:
    path = tmp_path / "x-cookies.json"
    path.write_text(
        json.dumps({"auth_token": "secret-auth", "ct0": "secret-csrf"}),
        encoding="utf-8",
    )
    return path


def test_load_x_cookies_requires_flat_complete_untruncated_values(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cookies.json"
    for payload, message in [
        ({"auth_token": "secret", "ct0": ""}, "nonempty ct0"),
        ({"auth_token": "abc...xyz", "ct0": "csrf"}, "appears to be ellipsized"),
        ({"auth_token": {"value": "secret"}, "ct0": "csrf"}, "flat JSON"),
    ]:
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match=message) as error:
            load_x_cookies(path)
        assert "secret" not in str(error.value)
        assert "abc...xyz" not in str(error.value)


def test_validate_x_text_uses_simple_unicode_code_point_limit() -> None:
    validate_x_text("😀" * 280)

    with pytest.raises(ValueError, match="280 Unicode code points"):
        validate_x_text("😀" * 281)
    with pytest.raises(ValueError, match="must not be empty"):
        validate_x_text(" \n")


def test_publish_without_media_uses_cookies_only_client(tmp_path: Path) -> None:
    client = FakeClient()

    result = asyncio.run(publish_x("Hello", _cookie_file(tmp_path), client=client))

    assert result.tweet_id == "tweet-456"
    assert result.tweet_url == "https://x.com/i/status/tweet-456"
    assert result.media_id is None
    assert client.calls == [
        ("cookies", {"auth_token", "ct0"}),
        ("create", ("Hello", None)),
    ]


def test_media_id_is_persisted_before_create(tmp_path: Path) -> None:
    client = FakeClient()
    media = tmp_path / "image.png"
    media.write_bytes(b"not-read-by-publisher")
    events: list[str] = []

    async def persist_media(media_id: str) -> None:
        events.append(f"persist:{media_id}")
        assert all(call[0] != "create" for call in client.calls)

    result = asyncio.run(
        publish_x(
            "With media",
            _cookie_file(tmp_path),
            media_path=media,
            on_media_upload_started=lambda: events.append("started"),
            on_media_uploaded=persist_media,
            client=client,
        )
    )

    assert result.media_id == "media-123"
    assert events == ["started", "persist:media-123"]
    assert client.calls[-2:] == [
        ("upload", str(media)),
        ("create", ("With media", ["media-123"])),
    ]


def test_existing_media_id_skips_upload(tmp_path: Path) -> None:
    client = FakeClient()

    result = asyncio.run(
        publish_x(
            "Resume safely",
            _cookie_file(tmp_path),
            media_path=tmp_path / "unused.png",
            existing_media_id="saved-media",
            client=client,
        )
    )

    assert result.media_id == "saved-media"
    assert [name for name, _ in client.calls] == ["cookies", "create"]
    assert client.calls[-1] == ("create", ("Resume safely", ["saved-media"]))


def test_create_exception_is_unknown_and_never_retried(tmp_path: Path) -> None:
    client = FakeClient(create_error=TimeoutError("response lost"))

    with pytest.raises(XPublishOutcomeUnknownError, match="do not retry automatically"):
        asyncio.run(publish_x("Once", _cookie_file(tmp_path), client=client))

    assert [name for name, _ in client.calls].count("create") == 1


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RuntimeError("401 unauthorized"), XAuthenticationError),
        (RuntimeError("429 rate limit"), XRateLimitError),
    ],
)
def test_pre_create_errors_are_actionable(
    tmp_path: Path, error: Exception, expected: type[Exception]
) -> None:
    client = FakeClient(upload_error=error)

    with pytest.raises(expected):
        asyncio.run(
            publish_x(
                "Media",
                _cookie_file(tmp_path),
                media_path=tmp_path / "image.png",
                client=client,
            )
        )
