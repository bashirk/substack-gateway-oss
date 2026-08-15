from __future__ import annotations

import asyncio
import base64
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from substack_gateway import publish_note


def _token() -> str:
    credentials = {
        "publication_url": "https://example.substack.com",
        "substack_sid": "sid",
        "connect_sid": "connect",
    }
    return base64.b64encode(json.dumps(credentials).encode()).decode()


def test_resolve_markdown_path_replaces_date() -> None:
    path = publish_note.resolve_markdown_path("notes/{date}.md", date(2026, 8, 14))

    assert path == Path("notes/2026-08-14.md")


def test_publish_markdown_note_uses_notes_service(tmp_path, monkeypatch) -> None:
    markdown = tmp_path / "note.md"
    markdown.write_text("**Good morning!**", encoding="utf-8")
    calls: dict[str, object] = {}

    class AsyncClientContext:
        def __init__(self, client: object) -> None:
            self.client = client

        async def __aenter__(self) -> object:
            return self.client

        async def __aexit__(self, *args: object) -> None:
            return None

    publication_client = object()
    substack_client = object()

    class FakeNotesService:
        def __init__(self, publication: object, substack: object) -> None:
            assert publication is publication_client
            assert substack is substack_client

        async def create_note(
            self, content: str, attachment: str | None = None
        ) -> SimpleNamespace:
            calls["content"] = content
            calls["attachment"] = attachment
            return SimpleNamespace(id=42)

    monkeypatch.setattr(
        publish_note,
        "make_publication_client",
        lambda credentials, publication_url: AsyncClientContext(publication_client),
    )
    monkeypatch.setattr(
        publish_note,
        "make_substack_client",
        lambda credentials: AsyncClientContext(substack_client),
    )
    monkeypatch.setattr(publish_note, "NotesService", FakeNotesService)

    note_id = asyncio.run(
        publish_note.publish_markdown_note(
            markdown, _token(), attachment="https://example.com"
        )
    )

    assert note_id == 42
    assert calls == {
        "content": "**Good morning!**",
        "attachment": "https://example.com",
    }


def test_publish_markdown_note_rejects_empty_file(tmp_path) -> None:
    markdown = tmp_path / "empty.md"
    markdown.write_text("  \n", encoding="utf-8")

    with pytest.raises(ValueError, match="Markdown file is empty"):
        asyncio.run(publish_note.publish_markdown_note(markdown, _token()))
