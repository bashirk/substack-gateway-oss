from __future__ import annotations

import importlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

X_MAX_TEXT_CODE_POINTS = 280
_REQUIRED_COOKIES = ("auth_token", "ct0")


class XClient(Protocol):
    def set_cookies(self, cookies: dict[str, str]) -> object: ...

    def upload_media(self, source: str) -> Awaitable[object]: ...

    def create_tweet(
        self, text: str, media_ids: list[str] | None = None
    ) -> Awaitable[object]: ...


class XClientFactory(Protocol):
    def __call__(self) -> XClient: ...


@dataclass(frozen=True)
class XPublishResult:
    tweet_id: str
    tweet_url: str
    media_id: str | None = None


class XPublishError(RuntimeError):
    """Base class for actionable X publishing failures."""


class XAuthenticationError(XPublishError):
    """The X cookies appear to be invalid or expired."""


class XRateLimitError(XPublishError):
    """X rejected an operation because of a rate limit."""


class XPublishOutcomeUnknownError(XPublishError):
    """Tweet creation was attempted, but its outcome could not be determined."""


def load_x_cookies(path: Path) -> dict[str, str]:
    """Load and validate a flat Twikit-compatible JSON cookie object."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Could not read X cookie file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("X cookie file must contain valid JSON") from exc

    if not isinstance(payload, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in payload.items()
    ):
        raise ValueError("X cookie file must be a flat JSON object of string values")

    cookies = cast(dict[str, str], payload)
    validate_x_cookies(cookies)
    return cookies


def validate_x_cookies(cookies: Mapping[str, str]) -> None:
    """Validate required cookies without including their values in errors."""
    for name in _REQUIRED_COOKIES:
        value = cookies.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"X cookie file is missing a nonempty {name} cookie")
        if "..." in value or "…" in value:
            raise ValueError(f"X cookie {name} appears to be ellipsized")


def validate_x_text(text: str) -> None:
    """Conservatively enforce X's 280 Unicode-code-point text limit.

    X applies additional URL and weighted-character rules. This intentionally simple
    validation only rejects empty text and strings over 280 Python Unicode code points.
    """
    if not text.strip():
        raise ValueError("X post text must not be empty")
    if len(text) > X_MAX_TEXT_CODE_POINTS:
        raise ValueError(
            f"X post text exceeds {X_MAX_TEXT_CODE_POINTS} Unicode code points"
        )


async def publish_x(
    text: str,
    cookie_file: Path,
    *,
    media_path: Path | None = None,
    existing_media_id: str | None = None,
    on_media_upload_started: Callable[[], object] | None = None,
    on_media_uploaded: Callable[[str], object] | None = None,
    client: XClient | None = None,
    client_factory: XClientFactory | None = None,
) -> XPublishResult:
    """Publish one X post using cookies, optionally uploading media first.

    ``on_media_uploaded`` runs after upload and before tweet creation so a scheduler
    can persist the media ID. No operation is retried internally.
    """
    validate_x_text(text)
    cookies = load_x_cookies(cookie_file)
    if client is not None and client_factory is not None:
        raise ValueError("Provide either client or client_factory, not both")
    x_client = client or (client_factory or _default_client_factory)()

    try:
        await _maybe_await(x_client.set_cookies(cookies))
    except Exception as exc:
        raise _actionable_error("loading X cookies", exc) from exc

    media_id = existing_media_id
    if media_id is not None and not media_id.strip():
        raise ValueError("existing_media_id must be nonempty")
    if media_path is not None and media_id is None:
        if on_media_upload_started is not None:
            await _maybe_await(on_media_upload_started())
        try:
            uploaded = await x_client.upload_media(str(media_path))
            media_id = _required_id(uploaded, "media upload")
        except Exception as exc:
            if isinstance(exc, XPublishError):
                raise
            raise _actionable_error("uploading X media", exc) from exc
        if on_media_uploaded is not None:
            await _maybe_await(on_media_uploaded(media_id))

    media_ids = [media_id] if media_id is not None else None
    try:
        tweet = await x_client.create_tweet(text, media_ids=media_ids)
        tweet_id = _required_id(tweet, "tweet creation")
    except Exception as exc:
        detail = _actionable_detail(exc)
        raise XPublishOutcomeUnknownError(
            "X tweet creation was attempted, but its outcome is unknown; do not "
            f"retry automatically. {detail}"
        ) from exc

    return XPublishResult(
        tweet_id=tweet_id,
        tweet_url=f"https://x.com/i/status/{tweet_id}",
        media_id=media_id,
    )


def _default_client_factory() -> XClient:
    try:
        twikit = importlib.import_module("twikit")
    except ImportError as exc:
        raise XPublishError(
            "Twikit is required to publish to X; install the 'twikit' package"
        ) from exc
    client_type = cast(Callable[[str], XClient], getattr(twikit, "Client"))
    return client_type("en-US")


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await cast(Awaitable[object], value)
    return value


def _required_id(result: object, operation: str) -> str:
    value: object
    if isinstance(result, Mapping):
        value = result.get("id")
    else:
        value = getattr(
            result, "id", result if isinstance(result, (str, int)) else None
        )
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"X {operation} returned no usable ID")
    identifier = str(value).strip()
    if not identifier:
        raise ValueError(f"X {operation} returned no usable ID")
    return identifier


def _actionable_error(operation: str, exc: Exception) -> XPublishError:
    detail = _actionable_detail(exc)
    error_type: type[XPublishError]
    if _looks_like_rate_limit(exc):
        error_type = XRateLimitError
    elif _looks_like_auth_error(exc):
        error_type = XAuthenticationError
    else:
        error_type = XPublishError
    return error_type(f"Failed while {operation}. {detail}")


def _actionable_detail(exc: Exception) -> str:
    if _looks_like_rate_limit(exc):
        return "X may be rate-limiting this account; wait before trying manually."
    if _looks_like_auth_error(exc):
        return "The X cookies may be invalid or expired; import fresh cookies."
    return "Inspect the original exception before deciding whether to retry."


def _looks_like_rate_limit(exc: Exception) -> bool:
    status = getattr(exc, "status_code", getattr(exc, "status", None))
    text = f"{type(exc).__name__} {exc}".lower()
    return status == 429 or "rate limit" in text or "too many requests" in text


def _looks_like_auth_error(exc: Exception) -> bool:
    status = getattr(exc, "status_code", getattr(exc, "status", None))
    text = f"{type(exc).__name__} {exc}".lower()
    return status in {401, 403} or any(
        marker in text
        for marker in ("unauthorized", "forbidden", "authentication", "login required")
    )
