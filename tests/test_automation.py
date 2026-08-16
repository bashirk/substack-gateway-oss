from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, time
from pathlib import Path
from typing import final
from zoneinfo import ZoneInfo

import pytest

from substack_gateway.automation import (
    AutomationConfig,
    Autopilot,
    ScheduleSlot,
    StateStore,
    XMode,
    config_from_env,
    due_slots,
    topic_for_slot,
)
from substack_gateway.generation import GeneratedImage
from substack_gateway.publish_newsletter import (
    NewsletterMode,
    NewsletterOutcomeUnknownError,
    NewsletterResult,
    UploadedImage,
)
from substack_gateway.publish_x import XPublishOutcomeUnknownError, XPublishResult


@final
class FakeImageGenerator:
    def __init__(self, failures: int = 0) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.failures = failures

    async def generate(self, kind: str, topic: str, context: str) -> GeneratedImage:
        self.calls.append((kind, topic, context))
        if len(self.calls) <= self.failures:
            raise RuntimeError("temporary image generation failure")
        return GeneratedImage(b"\x89PNG\r\n\x1a\nimage", "image/png", ".png")


@final
class FakeGenerator:
    def __init__(self, failures: int = 0) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.failures: int = failures

    async def generate(self, kind: str, topic: str, context: str) -> str:
        self.calls.append((kind, topic, context))
        if len(self.calls) <= self.failures:
            raise RuntimeError("temporary generation failure")
        return f"# Generated {kind}\n\nTopic: {topic}"


def config(
    tmp_path: Path,
    *,
    timezone_value: ZoneInfo | None = None,
    notes_enabled: bool = True,
    newsletters_enabled: bool = True,
    dry_run: bool = False,
    newsletter_mode: NewsletterMode = "draft_only",
    images_enabled: bool = False,
    writer_id: int | None = None,
    x_enabled: bool = False,
    x_mode: XMode = "artifact_only",
    x_images_enabled: bool = False,
) -> AutomationConfig:
    return AutomationConfig(
        token="token",
        state_path=tmp_path / "state.sqlite3",
        artifact_dir=tmp_path / "artifacts",
        timezone=timezone_value or ZoneInfo("UTC"),
        topics=("engineering", "writing", "AI"),
        context="Practical and direct.",
        note_interval_hours=3,
        newsletter_time=time(8),
        notes_enabled=notes_enabled,
        newsletters_enabled=newsletters_enabled,
        dry_run=dry_run,
        poll_seconds=60,
        retry_seconds=10,
        newsletter_mode=newsletter_mode,
        writer_id=writer_id,
        images_enabled=images_enabled,
        x_enabled=x_enabled,
        x_mode=x_mode,
        x_cookies_path=tmp_path / "x-cookies.json",
        x_images_enabled=x_images_enabled,
    )


def rows(path: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT * FROM schedule_slots ORDER BY kind"
        ).fetchall()
    finally:
        connection.close()


def test_due_slots_use_interval_and_daily_time(tmp_path: Path) -> None:
    settings = config(tmp_path, timezone_value=ZoneInfo("America/New_York"))
    now = datetime(2026, 8, 14, 17, 47, tzinfo=UTC)

    slots = due_slots(now, settings)

    assert [(slot.kind, slot.scheduled_at.isoformat()) for slot in slots] == [
        ("note", "2026-08-14T12:00:00-04:00"),
        ("newsletter", "2026-08-14T08:00:00-04:00"),
    ]


def test_x_due_slot_uses_independent_interval(tmp_path: Path) -> None:
    settings = config(
        tmp_path,
        notes_enabled=False,
        newsletters_enabled=False,
        x_enabled=True,
    )
    now = datetime(2026, 8, 14, 17, 47, tzinfo=UTC)

    slots = due_slots(now, settings)

    assert [(slot.kind, slot.scheduled_at.isoformat()) for slot in slots] == [
        ("x_post", "2026-08-14T15:00:00+00:00")
    ]


def test_topic_rotation_is_deterministic() -> None:
    slot = ScheduleSlot("note", datetime(2026, 8, 14, 12, tzinfo=UTC))
    topics = ("one", "two", "three")

    assert topic_for_slot(slot, topics) == topic_for_slot(slot, topics)
    assert topic_for_slot(slot, topics) in topics


def test_restart_does_not_publish_slot_twice(tmp_path: Path) -> None:
    settings = config(tmp_path, newsletters_enabled=False)
    generator = FakeGenerator()
    published: list[Path] = []

    async def publish(path: Path, token: str, attachment: str | None) -> int:
        published.append(path)
        return 123

    now = datetime(2026, 8, 14, 12, 5, tzinfo=UTC)
    first_store = StateStore(settings.state_path)
    asyncio.run(Autopilot(settings, generator, first_store, publish).run_due(now))
    first_store.close()
    second_store = StateStore(settings.state_path)
    asyncio.run(Autopilot(settings, generator, second_store, publish).run_due(now))
    second_store.close()

    assert len(generator.calls) == 1
    assert len(published) == 1
    assert rows(settings.state_path)[0]["status"] == "published"


def test_generation_failure_retries_after_delay(tmp_path: Path) -> None:
    settings = config(tmp_path, newsletters_enabled=False)
    generator = FakeGenerator(failures=1)

    async def publish(path: Path, token: str, attachment: str | None) -> int:
        return 10

    store = StateStore(settings.state_path)
    autopilot = Autopilot(settings, generator, store, publish)
    now = datetime(2026, 8, 14, 12, 5, tzinfo=UTC)
    asyncio.run(autopilot.run_due(now))
    asyncio.run(autopilot.run_due(now.replace(second=9)))
    asyncio.run(autopilot.run_due(now.replace(second=11)))
    store.close()

    assert len(generator.calls) == 2
    assert rows(settings.state_path)[0]["status"] == "published"


def test_publish_failure_is_not_automatically_retried(tmp_path: Path) -> None:
    settings = config(tmp_path, newsletters_enabled=False)
    generator = FakeGenerator()
    publish_calls = 0

    async def publish(path: Path, token: str, attachment: str | None) -> int:
        nonlocal publish_calls
        publish_calls += 1
        raise RuntimeError("connection lost after request")

    store = StateStore(settings.state_path)
    autopilot = Autopilot(settings, generator, store, publish)
    now = datetime(2026, 8, 14, 12, 5, tzinfo=UTC)
    asyncio.run(autopilot.run_due(now))
    asyncio.run(autopilot.run_due(now.replace(minute=10)))
    store.close()

    assert publish_calls == 1
    assert rows(settings.state_path)[0]["status"] == "publishing_unknown"


def test_newsletter_draft_id_and_timestamp_are_persisted(tmp_path: Path) -> None:
    settings = config(tmp_path, notes_enabled=False, writer_id=13775033)
    generator = FakeGenerator()
    calls = 0

    async def publish_newsletter(
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
    ) -> NewsletterResult:
        nonlocal calls
        calls += 1
        assert mode == "draft_only"
        assert writer_id == 13775033
        assert existing_draft_id is None
        assert existing_draft_updated_at is None
        assert on_draft_created is not None
        on_draft_created(101, "created-at")
        on_draft_created(101, "updated-at")
        return NewsletterResult("drafted", 101)

    store = StateStore(settings.state_path)
    now = datetime(2026, 8, 14, 8, 5, tzinfo=UTC)
    asyncio.run(
        Autopilot(
            settings,
            generator,
            store,
            newsletter_publisher=publish_newsletter,
        ).run_due(now)
    )
    store.close()

    row = rows(settings.state_path)[0]
    assert calls == 1
    assert row["status"] == "drafted"
    assert row["external_id"] == "101"
    assert row["draft_updated_at"] == "updated-at"


def test_newsletter_restart_reuses_persisted_draft(tmp_path: Path) -> None:
    settings = config(tmp_path, notes_enabled=False, newsletter_mode="publish_web")
    generator = FakeGenerator()
    seen_existing: list[tuple[int | None, str | None]] = []

    async def publish_newsletter(
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
    ) -> NewsletterResult:
        seen_existing.append((existing_draft_id, existing_draft_updated_at))
        assert on_draft_created is not None
        if len(seen_existing) == 1:
            on_draft_created(101, "updated-at")
            raise ValueError("prepublish validation failed")
        return NewsletterResult("published", 101, 202)

    now = datetime(2026, 8, 14, 8, 5, tzinfo=UTC)
    first_store = StateStore(settings.state_path)
    asyncio.run(
        Autopilot(
            settings,
            generator,
            first_store,
            newsletter_publisher=publish_newsletter,
        ).run_due(now)
    )
    first_store.close()
    second_store = StateStore(settings.state_path)
    asyncio.run(
        Autopilot(
            settings,
            generator,
            second_store,
            newsletter_publisher=publish_newsletter,
        ).run_due(now.replace(second=11))
    )
    second_store.close()

    row = rows(settings.state_path)[0]
    assert seen_existing == [(None, None), (101, "updated-at")]
    assert len(generator.calls) == 1
    assert row["status"] == "published"
    assert row["post_id"] == "202"


def test_ambiguous_newsletter_outcome_is_not_retried(tmp_path: Path) -> None:
    settings = config(tmp_path, notes_enabled=False)
    generator = FakeGenerator()
    calls = 0

    async def publish_newsletter(
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
    ) -> NewsletterResult:
        nonlocal calls
        calls += 1
        raise NewsletterOutcomeUnknownError("publish response was lost")

    store = StateStore(settings.state_path)
    autopilot = Autopilot(
        settings,
        generator,
        store,
        newsletter_publisher=publish_newsletter,
    )
    now = datetime(2026, 8, 14, 8, 5, tzinfo=UTC)
    asyncio.run(autopilot.run_due(now))
    asyncio.run(autopilot.run_due(now.replace(minute=20)))
    store.close()

    assert calls == 1
    assert rows(settings.state_path)[0]["status"] == "publishing_unknown"


def test_state_store_migrates_image_columns(tmp_path: Path) -> None:
    state_path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(state_path)
    connection.execute(
        """
        CREATE TABLE schedule_slots (
            slot_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            topic TEXT NOT NULL,
            status TEXT NOT NULL,
            artifact_path TEXT,
            external_id TEXT,
            error TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            retry_after TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.close()

    store = StateStore(state_path)
    store.close()
    migrated = sqlite3.connect(state_path)
    try:
        columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(schedule_slots)")
        }
    finally:
        migrated.close()

    assert {
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
    } <= columns


def test_restart_reuses_generated_image_artifact(tmp_path: Path) -> None:
    settings = config(
        tmp_path,
        notes_enabled=False,
        images_enabled=True,
        newsletter_mode="publish_web",
    )
    content_generator = FakeGenerator()
    image_generator = FakeImageGenerator()
    publish_calls = 0

    async def publish_newsletter(
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
    ) -> NewsletterResult:
        nonlocal publish_calls
        publish_calls += 1
        assert image_path is not None
        if publish_calls == 1:
            assert existing_uploaded_image is None
            assert on_draft_created is not None
            assert on_image_upload_started is not None
            assert on_image_uploaded is not None
            on_draft_created(101, "created-at")
            on_image_upload_started()
            on_image_uploaded(
                UploadedImage(
                    303,
                    "https://cdn.example.test/generated.png",
                    "image/png",
                    13,
                    440,
                    78,
                )
            )
            raise ValueError("prepublish validation failed")
        assert existing_uploaded_image == UploadedImage(
            303,
            "https://cdn.example.test/generated.png",
            "image/png",
            13,
            440,
            78,
        )
        return NewsletterResult("published", 101, 202)

    now = datetime(2026, 8, 14, 8, 5, tzinfo=UTC)
    first_store = StateStore(settings.state_path)
    asyncio.run(
        Autopilot(
            settings,
            content_generator,
            first_store,
            newsletter_publisher=publish_newsletter,
            image_generator=image_generator,
        ).run_due(now)
    )
    first_store.close()
    second_store = StateStore(settings.state_path)
    asyncio.run(
        Autopilot(
            settings,
            content_generator,
            second_store,
            newsletter_publisher=publish_newsletter,
            image_generator=image_generator,
        ).run_due(now.replace(second=11))
    )
    second_store.close()

    row = rows(settings.state_path)[0]
    image_path = Path(row["image_artifact_path"])
    assert len(content_generator.calls) == 1
    assert len(image_generator.calls) == 1
    assert image_path.read_bytes().startswith(b"\x89PNG")
    assert row["image_status"] == "generated"
    assert row["image_upload_status"] == "uploaded"
    assert row["image_upload_id"] == 303
    assert row["image_upload_url"] == "https://cdn.example.test/generated.png"
    assert row["image_upload_content_type"] == "image/png"
    assert row["image_upload_bytes"] == 13
    assert row["image_upload_width"] == 440
    assert row["image_upload_height"] == 78
    assert row["image_upload_error"] is None
    assert row["status"] == "published"


def test_image_failure_is_persisted_and_retried(tmp_path: Path) -> None:
    settings = config(
        tmp_path,
        notes_enabled=False,
        images_enabled=True,
        newsletter_mode="publish_web",
    )
    image_generator = FakeImageGenerator(failures=1)

    async def publish_newsletter(
        path: Path,
        token: str,
        mode: NewsletterMode,
        **kwargs: object,
    ) -> NewsletterResult:
        return NewsletterResult("published", 123, 456)

    store = StateStore(settings.state_path)
    autopilot = Autopilot(
        settings,
        FakeGenerator(),
        store,
        newsletter_publisher=publish_newsletter,
        image_generator=image_generator,
    )
    now = datetime(2026, 8, 14, 8, 5, tzinfo=UTC)
    asyncio.run(autopilot.run_due(now))
    failed = rows(settings.state_path)[0]
    assert failed["status"] == "failed"
    assert failed["image_status"] == "failed"
    assert "temporary image generation failure" in failed["image_error"]

    asyncio.run(autopilot.run_due(now.replace(second=11)))
    store.close()

    recovered = rows(settings.state_path)[0]
    assert len(image_generator.calls) == 2
    assert recovered["status"] == "published"
    assert recovered["image_status"] == "generated"
    assert recovered["image_error"] is None


def test_disabled_images_preserve_existing_behavior(tmp_path: Path) -> None:
    settings = config(tmp_path, newsletters_enabled=False)
    image_generator = FakeImageGenerator()

    async def publish(path: Path, token: str, attachment: str | None) -> int:
        return 123

    store = StateStore(settings.state_path)
    asyncio.run(
        Autopilot(
            settings,
            FakeGenerator(),
            store,
            publish,
            image_generator=image_generator,
        ).run_due(datetime(2026, 8, 14, 12, 5, tzinfo=UTC))
    )
    store.close()

    assert image_generator.calls == []
    assert rows(settings.state_path)[0]["image_artifact_path"] is None


def test_x_publish_persists_media_and_post_ids(tmp_path: Path) -> None:
    settings = config(
        tmp_path,
        notes_enabled=False,
        newsletters_enabled=False,
        x_enabled=True,
        x_mode="publish",
        x_images_enabled=True,
    )
    image_generator = FakeImageGenerator()
    calls: list[tuple[str | None, Path | None]] = []

    async def publish_x_post(
        text: str,
        cookie_file: Path,
        *,
        media_path: Path | None = None,
        existing_media_id: str | None = None,
        on_media_upload_started: Callable[[], object] | None = None,
        on_media_uploaded: Callable[[str], object] | None = None,
    ) -> XPublishResult:
        calls.append((existing_media_id, media_path))
        assert on_media_upload_started is not None
        assert on_media_uploaded is not None
        on_media_upload_started()
        on_media_uploaded("123456")
        return XPublishResult("789", "https://x.com/i/status/789", "123456")

    store = StateStore(settings.state_path)
    asyncio.run(
        Autopilot(
            settings,
            FakeGenerator(),
            store,
            image_generator=image_generator,
            x_publisher=publish_x_post,
        ).run_due(datetime(2026, 8, 14, 15, 5, tzinfo=UTC))
    )
    store.close()

    row = rows(settings.state_path)[0]
    assert calls[0][0] is None
    assert calls[0][1] is not None
    assert row["status"] == "published"
    assert row["external_id"] == "789"
    assert row["post_id"] == "789"
    assert row["x_media_id"] == "123456"


def test_x_publish_reuses_persisted_media_after_pre_create_failure(
    tmp_path: Path,
) -> None:
    settings = config(
        tmp_path,
        notes_enabled=False,
        newsletters_enabled=False,
        x_enabled=True,
        x_mode="publish",
        x_images_enabled=True,
    )
    calls: list[str | None] = []

    async def publish_x_post(
        text: str,
        cookie_file: Path,
        *,
        media_path: Path | None = None,
        existing_media_id: str | None = None,
        on_media_upload_started: Callable[[], object] | None = None,
        on_media_uploaded: Callable[[str], object] | None = None,
    ) -> XPublishResult:
        calls.append(existing_media_id)
        if existing_media_id is None:
            assert media_path is not None
            assert on_media_upload_started is not None
            assert on_media_uploaded is not None
            _ = on_media_upload_started()
            _ = on_media_uploaded("123456")
            raise RuntimeError("failed before tweet creation")
        return XPublishResult("789", "https://x.com/i/status/789", existing_media_id)

    first_run = datetime(2026, 8, 14, 15, 5, tzinfo=UTC)
    store = StateStore(settings.state_path)
    autopilot = Autopilot(
        settings,
        FakeGenerator(),
        store,
        image_generator=FakeImageGenerator(),
        x_publisher=publish_x_post,
    )
    asyncio.run(autopilot.run_due(first_run))
    asyncio.run(autopilot.run_due(first_run.replace(minute=20)))
    store.close()

    row = rows(settings.state_path)[0]
    assert calls == [None, "123456"]
    assert row["status"] == "published"
    assert row["x_media_id"] == "123456"


def test_ambiguous_x_publish_is_never_retried(tmp_path: Path) -> None:
    settings = config(
        tmp_path,
        notes_enabled=False,
        newsletters_enabled=False,
        x_enabled=True,
        x_mode="publish",
    )
    calls = 0

    async def publish_x_post(
        text: str,
        cookie_file: Path,
        **kwargs: object,
    ) -> XPublishResult:
        nonlocal calls
        calls += 1
        raise XPublishOutcomeUnknownError("response lost")

    now = datetime(2026, 8, 14, 15, 5, tzinfo=UTC)
    store = StateStore(settings.state_path)
    autopilot = Autopilot(settings, FakeGenerator(), store, x_publisher=publish_x_post)
    asyncio.run(autopilot.run_due(now))
    asyncio.run(autopilot.run_due(now.replace(minute=20)))
    store.close()

    assert calls == 1
    assert rows(settings.state_path)[0]["status"] == "publishing_unknown"


def _set_required_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUBSTACK_AUTOPILOT_TOPICS", "engineering")
    monkeypatch.setenv("SUBSTACK_AUTOPILOT_TIMEZONE", "UTC")
    monkeypatch.setenv("SUBSTACK_AUTOPILOT_NOTES_ENABLED", "false")
    monkeypatch.setenv("SUBSTACK_AUTOPILOT_NEWSLETTERS_ENABLED", "false")
    monkeypatch.setenv("SUBSTACK_AUTOPILOT_IMAGES_ENABLED", "false")
    monkeypatch.setenv("X_AUTOPILOT_ENABLED", "false")
    monkeypatch.setenv("SUBSTACK_AUTOPILOT_NEWSLETTER_MODE", "draft_only")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://azure.example.test")
    monkeypatch.setenv("AZURE_OPENAI_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_VERSION", "2024-10-21")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_NAME", "test-deployment")


def test_x_publish_config_requires_explicit_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("X_AUTOPILOT_ENABLED", "true")
    monkeypatch.setenv("X_AUTOPILOT_MODE", "publish")
    monkeypatch.setenv("X_AUTOPILOT_COOKIES_PATH", "data/x-cookies.json")

    with pytest.raises(ValueError, match="PUBLISH_TO_X"):
        _ = config_from_env(dry_run=False)

    monkeypatch.setenv("X_AUTOPILOT_PUBLISH_CONFIRMATION", "PUBLISH_TO_X")
    settings, _, _ = config_from_env(dry_run=False)
    assert settings.x_mode == "publish"
    assert settings.x_cookies_path == Path("data/x-cookies.json")


def test_config_accepts_positive_writer_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("SUBSTACK_AUTOPILOT_WRITER_ID", "13775033")

    settings, _, _ = config_from_env(dry_run=True)

    assert settings.writer_id == 13775033


@pytest.mark.parametrize("value", ["not-a-number", "0", "-1"])
def test_config_rejects_invalid_writer_id(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("SUBSTACK_AUTOPILOT_WRITER_ID", value)

    with pytest.raises(
        ValueError, match="SUBSTACK_AUTOPILOT_WRITER_ID must be a positive integer"
    ):
        _ = config_from_env(dry_run=True)


def test_dry_run_and_newsletter_persist_artifacts(tmp_path: Path) -> None:
    settings = config(tmp_path, dry_run=True)
    generator = FakeGenerator()

    async def unexpected_publish(path: Path, token: str, attachment: str | None) -> int:
        raise AssertionError("dry run must not publish")

    store = StateStore(settings.state_path)
    now = datetime(2026, 8, 14, 12, 5, tzinfo=UTC)
    asyncio.run(Autopilot(settings, generator, store, unexpected_publish).run_due(now))
    store.close()

    stored = rows(settings.state_path)
    assert {row["status"] for row in stored} == {"artifact_only", "dry_run"}
    assert all(Path(row["artifact_path"]).read_text().strip() for row in stored)
