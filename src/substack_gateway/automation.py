from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import signal
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Literal, Protocol, cast, final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from substack_gateway.generation import (
    AzureOpenAIConfig,
    AzureOpenAIContentGenerator,
    AzureOpenAIImageConfig,
    AzureOpenAIImageGenerator,
    ContentGenerator,
    GeneratedImage,
    ImageGenerator,
)
from substack_gateway.publish_newsletter import (
    NewsletterMode,
    NewsletterOutcomeUnknownError,
    NewsletterResult,
    UploadedImage,
    publish_markdown_newsletter,
)
from substack_gateway.publish_note import publish_markdown_note
from substack_gateway.publish_x import (
    XPublishOutcomeUnknownError,
    XPublishResult,
    publish_x,
)

_log = logging.getLogger(__name__)

XMode = Literal["artifact_only", "publish"]


@dataclass(frozen=True)
class AutomationConfig:
    token: str
    state_path: Path
    artifact_dir: Path
    timezone: ZoneInfo
    topics: tuple[str, ...]
    context: str
    note_interval_hours: int = 3
    newsletter_time: time = time(8)
    notes_enabled: bool = True
    newsletters_enabled: bool = True
    dry_run: bool = False
    poll_seconds: int = 60
    retry_seconds: int = 300
    newsletter_mode: NewsletterMode = "draft_only"
    email_confirmation: str | None = None
    writer_id: int | None = None
    images_enabled: bool = False
    image_maximum_bytes: int = 20 * 1024 * 1024
    x_enabled: bool = False
    x_interval_hours: int = 3
    x_mode: XMode = "artifact_only"
    x_publish_confirmation: str | None = None
    x_cookies_path: Path | None = None
    x_images_enabled: bool = False


class NewsletterPublish(Protocol):
    async def __call__(
        self,
        path: Path,
        token: str,
        mode: NewsletterMode,
        *,
        existing_draft_id: int | None = None,
        existing_draft_updated_at: str | None = None,
        on_draft_created: Callable[[int, str], None] | None = None,
        image_path: Path | None = None,
        existing_uploaded_image: UploadedImage | None = None,
        on_image_upload_started: Callable[[], None] | None = None,
        on_image_uploaded: Callable[[UploadedImage], None] | None = None,
        image_maximum_bytes: int = 20 * 1024 * 1024,
        email_confirmation: str | None = None,
        writer_id: int | None = None,
    ) -> NewsletterResult: ...


class XPublish(Protocol):
    async def __call__(
        self,
        text: str,
        cookie_file: Path,
        *,
        media_path: Path | None = None,
        existing_media_id: str | None = None,
        on_media_upload_started: Callable[[], object] | None = None,
        on_media_uploaded: Callable[[str], object] | None = None,
    ) -> XPublishResult: ...


@dataclass(frozen=True)
class ScheduleSlot:
    kind: str
    scheduled_at: datetime

    @property
    def id(self) -> str:
        utc = self.scheduled_at.astimezone(UTC)
        return f"{self.kind}:{utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"


@final
class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection: sqlite3.Connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        _ = self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schedule_slots (
                slot_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                topic TEXT NOT NULL,
                status TEXT NOT NULL,
                artifact_path TEXT,
                image_artifact_path TEXT,
                image_status TEXT,
                image_error TEXT,
                image_upload_status TEXT,
                image_upload_id INTEGER,
                image_upload_url TEXT,
                image_upload_content_type TEXT,
                image_upload_bytes INTEGER,
                image_upload_width INTEGER,
                image_upload_height INTEGER,
                image_upload_error TEXT,
                x_media_id TEXT,
                external_id TEXT,
                post_id TEXT,
                draft_updated_at TEXT,
                error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                retry_after TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(schedule_slots)")
        }
        text_columns = (
            "image_artifact_path",
            "image_status",
            "image_error",
            "image_upload_status",
            "image_upload_url",
            "image_upload_content_type",
            "image_upload_error",
            "x_media_id",
        )
        for name in text_columns:
            if name not in columns:
                _ = self._connection.execute(
                    f"ALTER TABLE schedule_slots ADD COLUMN {name} TEXT"
                )
        integer_columns = (
            "image_upload_id",
            "image_upload_bytes",
            "image_upload_width",
            "image_upload_height",
        )
        for name in integer_columns:
            if name not in columns:
                _ = self._connection.execute(
                    f"ALTER TABLE schedule_slots ADD COLUMN {name} INTEGER"
                )
        if "post_id" not in columns:
            _ = self._connection.execute(
                "ALTER TABLE schedule_slots ADD COLUMN post_id TEXT"
            )
        if "draft_updated_at" not in columns:
            _ = self._connection.execute(
                "ALTER TABLE schedule_slots ADD COLUMN draft_updated_at TEXT"
            )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def ensure(self, slot: ScheduleSlot, topic: str, now: datetime) -> sqlite3.Row:
        _ = self._connection.execute(
            """
            INSERT OR IGNORE INTO schedule_slots
                (slot_id, kind, scheduled_at, topic, status, updated_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (slot.id, slot.kind, slot.scheduled_at.isoformat(), topic, now.isoformat()),
        )
        self._connection.commit()
        row = self._connection.execute(
            "SELECT * FROM schedule_slots WHERE slot_id = ?", (slot.id,)
        ).fetchone()
        assert row is not None
        return row

    def update(self, slot_id: str, now: datetime, **values: object) -> None:
        allowed = {
            "status",
            "artifact_path",
            "image_artifact_path",
            "image_status",
            "image_error",
            "image_upload_status",
            "image_upload_id",
            "image_upload_url",
            "image_upload_content_type",
            "image_upload_bytes",
            "image_upload_width",
            "image_upload_height",
            "image_upload_error",
            "x_media_id",
            "external_id",
            "post_id",
            "draft_updated_at",
            "error",
            "attempts",
            "retry_after",
        }
        if not values.keys() <= allowed:
            raise ValueError("Unsupported state field")
        assignments = ", ".join(f"{name} = ?" for name in values)
        parameters = [*values.values(), now.isoformat(), slot_id]
        _ = self._connection.execute(
            f"UPDATE schedule_slots SET {assignments}, updated_at = ? WHERE slot_id = ?",
            parameters,
        )
        self._connection.commit()


def due_slots(now: datetime, config: AutomationConfig) -> list[ScheduleSlot]:
    local_now = now.astimezone(config.timezone)
    slots: list[ScheduleSlot] = []
    if config.notes_enabled:
        hour = (
            local_now.hour // config.note_interval_hours
        ) * config.note_interval_hours
        slots.append(
            ScheduleSlot(
                "note", local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
            )
        )
    if config.newsletters_enabled:
        scheduled = datetime.combine(
            local_now.date(), config.newsletter_time, tzinfo=config.timezone
        )
        if scheduled <= local_now:
            slots.append(ScheduleSlot("newsletter", scheduled))
    if config.x_enabled:
        hour = (local_now.hour // config.x_interval_hours) * config.x_interval_hours
        slots.append(
            ScheduleSlot(
                "x_post",
                local_now.replace(hour=hour, minute=0, second=0, microsecond=0),
            )
        )
    return slots


def topic_for_slot(slot: ScheduleSlot, topics: tuple[str, ...]) -> str:
    digest = hashlib.sha256(slot.id.encode()).digest()
    return topics[int.from_bytes(digest[:8], "big") % len(topics)]


@final
class Autopilot:
    def __init__(
        self,
        config: AutomationConfig,
        generator: ContentGenerator,
        store: StateStore,
        publisher: Callable[[Path, str, str | None], Awaitable[int]] = (
            publish_markdown_note
        ),
        newsletter_publisher: NewsletterPublish = publish_markdown_newsletter,
        image_generator: ImageGenerator | None = None,
        x_publisher: XPublish = publish_x,
    ) -> None:
        self._config: AutomationConfig = config
        self._generator: ContentGenerator = generator
        self._store: StateStore = store
        self._publisher: Callable[[Path, str, str | None], Awaitable[int]] = publisher
        self._newsletter_publisher: NewsletterPublish = newsletter_publisher
        self._image_generator: ImageGenerator | None = image_generator
        self._x_publisher: XPublish = x_publisher

    async def run_due(self, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        for slot in due_slots(current, self._config):
            await self._run_slot(slot, current)

    async def _run_slot(self, slot: ScheduleSlot, now: datetime) -> None:
        topic = topic_for_slot(slot, self._config.topics)
        row = self._store.ensure(slot, topic, now)
        if row["status"] in {
            "published",
            "artifact_only",
            "drafted",
            "dry_run",
            "publishing",
            "publishing_unknown",
        }:
            return
        retry_after = row["retry_after"]
        if retry_after and datetime.fromisoformat(retry_after) > now:
            return

        publishing_started = False
        image_generation_started = False
        try:
            artifact = Path(row["artifact_path"]) if row["artifact_path"] else None
            image_artifact = (
                Path(row["image_artifact_path"]) if row["image_artifact_path"] else None
            )
            if artifact is None:
                content = await self._generator.generate(
                    slot.kind, topic, self._config.context
                )
                artifact = self._write_artifact(slot, content)
                self._store.update(
                    slot.id,
                    now,
                    status="generated",
                    artifact_path=str(artifact),
                    error=None,
                    retry_after=None,
                )

            generate_image = (
                slot.kind == "newsletter" and self._config.images_enabled
            ) or (slot.kind == "x_post" and self._config.x_images_enabled)
            if generate_image and image_artifact is None:
                if self._image_generator is None:
                    raise ValueError(
                        "Image generation is enabled without an image generator"
                    )
                self._store.update(
                    slot.id,
                    now,
                    image_status="generating",
                    image_error=None,
                )
                image_generation_started = True
                image = await self._image_generator.generate(
                    slot.kind, topic, self._config.context
                )
                image_artifact = self._write_image_artifact(slot, image)
                self._store.update(
                    slot.id,
                    now,
                    image_artifact_path=str(image_artifact),
                    image_status="generated",
                    image_error=None,
                )
                image_generation_started = False

            if slot.kind == "newsletter":
                await self._run_newsletter(slot, row, artifact, image_artifact, now)
                return
            if slot.kind == "x_post":
                await self._run_x_post(slot, row, artifact, image_artifact, now)
                return
            if self._config.dry_run:
                self._store.update(slot.id, now, status="dry_run")
                _log.info("Dry run generated note artifact %s", artifact)
                return

            self._store.update(slot.id, now, status="publishing")
            publishing_started = True
            note_id = await self._publisher(artifact, self._config.token, None)
            self._store.update(
                slot.id, now, status="published", external_id=str(note_id), error=None
            )
            _log.info("Published note %s from %s", note_id, artifact)
        except Exception as exc:
            attempts = int(row["attempts"]) + 1
            retry_at = now + timedelta(seconds=self._config.retry_seconds)
            status = "publishing_unknown" if publishing_started else "failed"
            error = f"{type(exc).__name__}: {exc}"
            values: dict[str, object] = {
                "status": status,
                "attempts": attempts,
                "error": error,
                "retry_after": None if publishing_started else retry_at.isoformat(),
            }
            if image_generation_started:
                values.update(image_status="failed", image_error=error)
            self._store.update(slot.id, now, **values)
            _log.exception("Autopilot slot %s failed with status %s", slot.id, status)

    async def _run_x_post(
        self,
        slot: ScheduleSlot,
        row: sqlite3.Row,
        artifact: Path,
        image_artifact: Path | None,
        now: datetime,
    ) -> None:
        if self._config.dry_run or self._config.x_mode == "artifact_only":
            self._store.update(slot.id, now, status="artifact_only")
            _log.info("Generated X post artifact %s", artifact)
            return
        if self._config.x_cookies_path is None:
            raise ValueError("X cookie path is required when X publishing is enabled")

        existing_media_id = str(row["x_media_id"]) if row["x_media_id"] else None
        upload_started = False
        upload_completed = existing_media_id is not None

        def persist_upload_started() -> None:
            nonlocal upload_started
            upload_started = True
            self._store.update(
                slot.id,
                datetime.now(UTC),
                image_upload_status="uploading",
                image_upload_error=None,
            )

        def persist_uploaded(media_id: str) -> None:
            nonlocal upload_completed
            upload_completed = True
            self._store.update(
                slot.id,
                datetime.now(UTC),
                image_upload_status="uploaded",
                x_media_id=media_id,
                image_upload_error=None,
            )

        self._store.update(slot.id, now, status="publishing")
        text = artifact.read_text(encoding="utf-8").strip()
        try:
            result = await self._x_publisher(
                text,
                self._config.x_cookies_path,
                media_path=image_artifact,
                existing_media_id=existing_media_id,
                on_media_upload_started=persist_upload_started,
                on_media_uploaded=persist_uploaded,
            )
        except XPublishOutcomeUnknownError as exc:
            self._store.update(
                slot.id,
                now,
                status="publishing_unknown",
                attempts=int(row["attempts"]) + 1,
                error=f"{type(exc).__name__}: {exc}",
                retry_after=None,
            )
            _log.exception("X slot %s has an ambiguous outcome", slot.id)
            return
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._store.update(
                slot.id,
                now,
                status="failed",
                attempts=int(row["attempts"]) + 1,
                error=error,
                image_upload_error=(
                    error if upload_started and not upload_completed else None
                ),
                retry_after=(
                    now + timedelta(seconds=self._config.retry_seconds)
                ).isoformat(),
            )
            _log.exception("X slot %s failed before tweet creation", slot.id)
            return

        self._store.update(
            slot.id,
            now,
            status="published",
            external_id=result.tweet_id,
            post_id=result.tweet_id,
            x_media_id=result.media_id,
            image_upload_status="uploaded" if result.media_id else None,
            error=None,
            retry_after=None,
        )
        _log.info("Published X post %s at %s", result.tweet_id, result.tweet_url)

    async def _run_newsletter(
        self,
        slot: ScheduleSlot,
        row: sqlite3.Row,
        artifact: Path,
        image_artifact: Path | None,
        now: datetime,
    ) -> None:
        mode = self._config.newsletter_mode
        if self._config.dry_run or mode == "artifact_only":
            self._store.update(slot.id, now, status="artifact_only")
            _log.info("Generated newsletter artifact %s", artifact)
            return

        existing_id = int(row["external_id"]) if row["external_id"] else None
        existing_updated_at = row["draft_updated_at"]
        uploaded_image = _uploaded_image_from_row(row)
        if row["image_upload_status"] == "uploading" and uploaded_image is None:
            attempts = int(row["attempts"]) + 1
            error = (
                "NewsletterOutcomeUnknownError: A previous image upload may have "
                "succeeded; inspect Substack before retrying"
            )
            self._store.update(
                slot.id,
                now,
                status="publishing_unknown",
                attempts=attempts,
                error=error,
                image_upload_error=error,
                retry_after=None,
            )
            return

        def persist_draft(draft_id: int, updated_at: str) -> None:
            self._store.update(
                slot.id,
                datetime.now(UTC),
                status="draft_created",
                external_id=str(draft_id),
                draft_updated_at=updated_at,
                error=None,
            )

        upload_started = False
        upload_completed = uploaded_image is not None

        def persist_upload_started() -> None:
            nonlocal upload_started
            upload_started = True
            self._store.update(
                slot.id,
                datetime.now(UTC),
                image_upload_status="uploading",
                image_upload_error=None,
            )

        def persist_uploaded(image: UploadedImage) -> None:
            nonlocal upload_completed
            upload_completed = True
            self._store.update(
                slot.id,
                datetime.now(UTC),
                image_upload_status="uploaded",
                image_upload_id=image.id,
                image_upload_url=image.url,
                image_upload_content_type=image.content_type,
                image_upload_bytes=image.bytes,
                image_upload_width=image.width,
                image_upload_height=image.height,
                image_upload_error=None,
            )

        self._store.update(slot.id, now, status="publishing")
        try:
            result = await self._newsletter_publisher(
                artifact,
                self._config.token,
                mode,
                existing_draft_id=existing_id,
                existing_draft_updated_at=existing_updated_at,
                on_draft_created=persist_draft,
                image_path=image_artifact,
                existing_uploaded_image=uploaded_image,
                on_image_upload_started=persist_upload_started,
                on_image_uploaded=persist_uploaded,
                image_maximum_bytes=self._config.image_maximum_bytes,
                email_confirmation=self._config.email_confirmation,
                writer_id=self._config.writer_id,
            )
        except NewsletterOutcomeUnknownError as exc:
            attempts = int(row["attempts"]) + 1
            error = f"{type(exc).__name__}: {exc}"
            values: dict[str, object] = {
                "status": "publishing_unknown",
                "attempts": attempts,
                "error": error,
                "retry_after": None,
            }
            if upload_started and not upload_completed:
                values["image_upload_error"] = error
            self._store.update(slot.id, now, **values)
            _log.exception("Newsletter slot %s has an ambiguous outcome", slot.id)
            return

        self._store.update(
            slot.id,
            now,
            status="drafted" if result.status == "drafted" else "published",
            external_id=str(result.draft_id),
            post_id=str(result.post_id) if result.post_id is not None else None,
            error=None,
        )
        if result.status == "drafted":
            _log.info("Newsletter saved as draft %s", result.draft_id)
        else:
            _log.info(
                "Newsletter published: draft_id=%s post_id=%s send_email=%s",
                result.draft_id,
                result.post_id,
                mode == "publish_and_email",
            )

    def _write_image_artifact(self, slot: ScheduleSlot, image: GeneratedImage) -> Path:
        timestamp = slot.scheduled_at.strftime("%Y-%m-%dT%H-%M-%S%z")
        directory = self._config.artifact_dir / slot.kind
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{timestamp}{image.extension}"
        temporary = path.with_suffix(f"{image.extension}.tmp")
        _ = temporary.write_bytes(image.content)
        _ = temporary.replace(path)
        return path

    def _write_artifact(self, slot: ScheduleSlot, content: str) -> Path:
        if not content.strip():
            raise ValueError("Generator returned empty content")
        timestamp = slot.scheduled_at.strftime("%Y-%m-%dT%H-%M-%S%z")
        directory = self._config.artifact_dir / slot.kind
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{timestamp}.md"
        temporary = path.with_suffix(".md.tmp")
        _ = temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
        _ = temporary.replace(path)
        return path


async def run_forever(autopilot: Autopilot, poll_seconds: int) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    while not stop.is_set():
        await autopilot.run_due()
        try:
            _ = await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
        except TimeoutError:
            pass


def _uploaded_image_from_row(row: sqlite3.Row) -> UploadedImage | None:
    fields = (
        "image_upload_id",
        "image_upload_url",
        "image_upload_content_type",
        "image_upload_bytes",
        "image_upload_width",
        "image_upload_height",
    )
    values = [row[field] for field in fields]
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError("Persisted image upload metadata is incomplete")
    return UploadedImage(
        id=int(row["image_upload_id"]),
        url=str(row["image_upload_url"]),
        content_type=str(row["image_upload_content_type"]),
        bytes=int(row["image_upload_bytes"]),
        width=int(row["image_upload_width"]),
        height=int(row["image_upload_height"]),
    )


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def config_from_env(
    dry_run: bool = False,
) -> tuple[AutomationConfig, AzureOpenAIConfig, AzureOpenAIImageConfig | None]:
    timezone_name = os.environ.get("SUBSTACK_AUTOPILOT_TIMEZONE", "UTC")
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {timezone_name}") from exc
    topics = tuple(
        topic.strip()
        for topic in _required("SUBSTACK_AUTOPILOT_TOPICS").split("|")
        if topic.strip()
    )
    if not topics:
        raise ValueError("SUBSTACK_AUTOPILOT_TOPICS must contain at least one topic")
    daily_time = time.fromisoformat(
        os.environ.get("SUBSTACK_AUTOPILOT_NEWSLETTER_TIME", "08:00")
    )
    notes_enabled = _boolean("SUBSTACK_AUTOPILOT_NOTES_ENABLED", True)
    newsletters_enabled = _boolean("SUBSTACK_AUTOPILOT_NEWSLETTERS_ENABLED", True)
    images_enabled = _boolean("SUBSTACK_AUTOPILOT_IMAGES_ENABLED", False)
    x_enabled = _boolean("X_AUTOPILOT_ENABLED", False)
    x_images_enabled = _boolean("X_AUTOPILOT_IMAGES_ENABLED", False)
    x_mode_value = os.environ.get("X_AUTOPILOT_MODE", "artifact_only")
    if x_mode_value not in {"artifact_only", "publish"}:
        raise ValueError("X_AUTOPILOT_MODE must be one of: artifact_only, publish")
    x_mode = x_mode_value
    x_publish_confirmation = os.environ.get("X_AUTOPILOT_PUBLISH_CONFIRMATION")
    if x_enabled and x_mode == "publish" and x_publish_confirmation != "PUBLISH_TO_X":
        raise ValueError(
            "X publish mode requires X_AUTOPILOT_PUBLISH_CONFIRMATION=PUBLISH_TO_X"
        )
    x_cookies_value = os.environ.get("X_AUTOPILOT_COOKIES_PATH", "").strip()
    if x_enabled and x_mode == "publish" and not x_cookies_value:
        raise ValueError("X_AUTOPILOT_COOKIES_PATH is required in X publish mode")
    newsletter_mode_value = os.environ.get(
        "SUBSTACK_AUTOPILOT_NEWSLETTER_MODE", "draft_only"
    )
    valid_modes = {"artifact_only", "draft_only", "publish_web", "publish_and_email"}
    if newsletter_mode_value not in valid_modes:
        raise ValueError(
            "SUBSTACK_AUTOPILOT_NEWSLETTER_MODE must be one of: "
            + ", ".join(sorted(valid_modes))
        )
    newsletter_mode = cast(NewsletterMode, newsletter_mode_value)
    email_confirmation = os.environ.get("SUBSTACK_AUTOPILOT_EMAIL_CONFIRMATION")
    writer_id = _optional_positive_int("SUBSTACK_AUTOPILOT_WRITER_ID")
    if newsletter_mode == "publish_and_email" and email_confirmation != (
        "SEND_EMAIL_TO_ALL_SUBSCRIBERS"
    ):
        raise ValueError(
            "publish_and_email requires SUBSTACK_AUTOPILOT_EMAIL_CONFIRMATION="
            "SEND_EMAIL_TO_ALL_SUBSCRIBERS"
        )
    token = os.environ.get("SUBSTACK_GATEWAY_TOKEN", "").strip()
    publishing_enabled = notes_enabled or (
        newsletters_enabled and newsletter_mode != "artifact_only"
    )
    if publishing_enabled and not dry_run and not token:
        raise ValueError(
            "SUBSTACK_GATEWAY_TOKEN is required when publishing is enabled"
        )
    config = AutomationConfig(
        token=token,
        state_path=Path(
            os.environ.get("SUBSTACK_AUTOPILOT_STATE_PATH", "data/autopilot.sqlite3")
        ).expanduser(),
        artifact_dir=Path(
            os.environ.get("SUBSTACK_AUTOPILOT_ARTIFACT_DIR", "data/artifacts")
        ).expanduser(),
        timezone=zone,
        topics=topics,
        context=os.environ.get("SUBSTACK_AUTOPILOT_CONTEXT", ""),
        note_interval_hours=int(
            os.environ.get("SUBSTACK_AUTOPILOT_NOTE_INTERVAL_HOURS", "3")
        ),
        newsletter_time=daily_time,
        notes_enabled=notes_enabled,
        newsletters_enabled=newsletters_enabled,
        dry_run=dry_run,
        poll_seconds=int(os.environ.get("SUBSTACK_AUTOPILOT_POLL_SECONDS", "60")),
        retry_seconds=int(os.environ.get("SUBSTACK_AUTOPILOT_RETRY_SECONDS", "300")),
        newsletter_mode=newsletter_mode,
        email_confirmation=email_confirmation,
        writer_id=writer_id,
        images_enabled=images_enabled,
        image_maximum_bytes=int(
            os.environ.get("SUBSTACK_AUTOPILOT_IMAGE_MAX_BYTES", "20971520")
        ),
        x_enabled=x_enabled,
        x_interval_hours=int(os.environ.get("X_AUTOPILOT_INTERVAL_HOURS", "3")),
        x_mode=x_mode,
        x_publish_confirmation=x_publish_confirmation,
        x_cookies_path=Path(x_cookies_value).expanduser() if x_cookies_value else None,
        x_images_enabled=x_images_enabled,
    )
    if config.note_interval_hours < 1 or config.note_interval_hours > 24:
        raise ValueError(
            "SUBSTACK_AUTOPILOT_NOTE_INTERVAL_HOURS must be between 1 and 24"
        )
    if config.x_interval_hours < 1 or config.x_interval_hours > 24:
        raise ValueError("X_AUTOPILOT_INTERVAL_HOURS must be between 1 and 24")
    endpoint = _required("AZURE_OPENAI_ENDPOINT")
    api_key = _required("AZURE_OPENAI_KEY")
    api_version = _required("OPENAI_API_VERSION")
    provider = AzureOpenAIConfig(
        endpoint=endpoint,
        api_key=api_key,
        api_version=api_version,
        deployment=_required("AZURE_OPENAI_DEPLOYMENT_NAME"),
    )
    image_provider = None
    if images_enabled or (x_enabled and x_images_enabled):
        image_provider = AzureOpenAIImageConfig(
            endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
            deployment=_required("AZURE_OPENAI_IMAGE_DEPLOYMENT_NAME"),
            size=os.environ.get("AZURE_OPENAI_IMAGE_SIZE", "1536x1024"),
            quality=os.environ.get("AZURE_OPENAI_IMAGE_QUALITY", "medium"),
            maximum_bytes=int(
                os.environ.get("SUBSTACK_AUTOPILOT_IMAGE_MAX_BYTES", "20971520")
            ),
        )
    return config, provider, image_provider


def _optional_positive_int(name: str) -> int | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _boolean(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate and schedule Substack and X content."
    )
    _ = parser.add_argument(
        "--once", action="store_true", help="Process due slots and exit"
    )
    _ = parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate artifacts without publishing",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    try:
        config, provider, image_provider = config_from_env(dry_run=args.dry_run)
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from exc
    store = StateStore(config.state_path)
    image_generator = (
        AzureOpenAIImageGenerator(image_provider)
        if image_provider is not None
        else None
    )
    autopilot = Autopilot(
        config,
        AzureOpenAIContentGenerator(provider),
        store,
        image_generator=image_generator,
    )
    _log.info(
        "Autopilot started: mode=%s timezone=%s poll_seconds=%d notes=%s "
        "newsletters=%s images=%s x=%s x_mode=%s x_images=%s state=%s artifacts=%s",
        config.newsletter_mode,
        config.timezone.key,
        config.poll_seconds,
        config.notes_enabled,
        config.newsletters_enabled,
        config.images_enabled,
        config.x_enabled,
        config.x_mode,
        config.x_images_enabled,
        config.state_path,
        config.artifact_dir,
    )
    try:
        if args.once:
            asyncio.run(autopilot.run_due())
        else:
            asyncio.run(run_forever(autopilot, config.poll_seconds))
    finally:
        store.close()
        _log.info("Autopilot stopped")


if __name__ == "__main__":
    main()
