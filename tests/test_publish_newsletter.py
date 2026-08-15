from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, final

import pytest

import substack_gateway.publish_newsletter as publish_newsletter
from substack_gateway.publish_newsletter import (
    NewsletterOutcomeUnknownError,
    NewsletterPublisher,
    UploadedImage,
    parse_newsletter_markdown,
    publish_markdown_newsletter,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@final
class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


@final
class FakeIdentity:
    def __init__(self) -> None:
        self.calls = 0

    async def get_own_id(self) -> int:
        self.calls += 1
        return 42


@final
class FakePublication:
    def __init__(
        self,
        *,
        fail_image: bool = False,
        image_response: dict[str, Any] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.fail_image = fail_image
        self.image_response = image_response

    async def get(self, path: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("GET", path, kwargs.get("json")))
        if path.endswith("/prepublish"):
            return FakeResponse({"ok": True})
        return FakeResponse({"id": 101, "draft_updated_at": "2026-08-14T20:00:00Z"})

    async def post_once(self, path: str, **kwargs: Any) -> FakeResponse:
        payload = kwargs.get("json")
        self.calls.append(("POST", path, payload))
        if path == "drafts":
            return FakeResponse({"id": 101, "draft_updated_at": "2026-08-14T20:00:00Z"})
        if path == "image":
            if self.fail_image:
                raise RuntimeError("response lost")
            return FakeResponse(
                self.image_response
                or {
                    "id": 303,
                    "url": "https://cdn.example.test/generated.png",
                    "contentType": "image/png",
                    "bytes": 13,
                    "imageWidth": 440,
                    "imageHeight": 78,
                }
            )
        return FakeResponse({"id": 202, "is_published": True})

    async def put_once(self, path: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("PUT", path, kwargs.get("json")))
        return FakeResponse({"id": 101, "draft_updated_at": "2026-08-14T20:01:00Z"})


def test_parse_newsletter_extracts_title_subtitle_and_body() -> None:
    content = parse_newsletter_markdown(
        "# A useful title\n\n*A concise subtitle*\n\n## Start\n\nBody."
    )

    assert content.title == "A useful title"
    assert content.subtitle == "A concise subtitle"
    assert content.body_markdown == "## Start\n\nBody."


@pytest.mark.anyio
async def test_draft_only_uses_har_payload_and_persists_id_immediately() -> None:
    publication = FakePublication()
    persisted: list[tuple[int, str]] = []
    content = parse_newsletter_markdown(
        "# A useful title\n\n*A concise subtitle*\n\n## Start\n\nUse **care**."
    )

    result = await NewsletterPublisher(publication, FakeIdentity()).publish(
        content,
        "draft_only",
        on_draft_created=lambda draft_id, updated_at: persisted.append(
            (draft_id, updated_at)
        ),
    )

    assert result.status == "drafted"
    assert result.draft_id == 101
    assert persisted == [
        (101, "2026-08-14T20:00:00Z"),
        (101, "2026-08-14T20:01:00Z"),
    ]
    assert [call[:2] for call in publication.calls] == [
        ("POST", "drafts"),
        ("PUT", "drafts/101"),
    ]
    create = publication.calls[0][2]
    assert create is not None
    assert create["draft_bylines"] == [{"id": 42, "is_guest": False}]
    assert create["type"] == "newsletter"
    assert isinstance(create["draft_body"], str)

    update = publication.calls[1][2]
    assert update is not None
    assert update["draft_title"] == "A useful title"
    assert update["draft_subtitle"] == "A concise subtitle"
    assert update["last_updated_at"] == "2026-08-14T20:00:00Z"
    assert update["cover_image"] is None
    assert update["should_send_email"] is False
    body = json.loads(update["draft_body"])
    assert body["content"][0]["type"] == "heading"
    assert body["content"][1]["content"][1]["marks"] == [{"type": "bold"}]


@pytest.mark.anyio
async def test_explicit_writer_id_bypasses_identity_lookup() -> None:
    publication = FakePublication()
    identity = FakeIdentity()

    _ = await NewsletterPublisher(publication, identity).publish(
        parse_newsletter_markdown("# Title\n\nBody"),
        "draft_only",
        writer_id=13775033,
    )

    assert identity.calls == 0
    create = publication.calls[0][2]
    assert create is not None
    assert create["draft_bylines"] == [{"id": 13775033, "is_guest": False}]


@pytest.mark.anyio
async def test_identity_lookup_remains_the_fallback() -> None:
    publication = FakePublication()
    identity = FakeIdentity()

    _ = await NewsletterPublisher(publication, identity).publish(
        parse_newsletter_markdown("# Title\n\nBody"), "draft_only"
    )

    assert identity.calls == 1
    create = publication.calls[0][2]
    assert create is not None
    assert create["draft_bylines"] == [{"id": 42, "is_guest": False}]


@pytest.mark.anyio
async def test_configured_writer_id_does_not_open_global_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    markdown = tmp_path / "newsletter.md"
    _ = markdown.write_text("# Title\n\nBody", encoding="utf-8")
    publication = FakePublication()

    class AsyncClientContext:
        async def __aenter__(self) -> FakePublication:
            return publication

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(
        publish_newsletter,
        "make_publication_client",
        lambda credentials, publication_url: AsyncClientContext(),
    )

    def unexpected_global_client(credentials: object) -> None:
        raise AssertionError("configured writer ID must bypass the global client")

    monkeypatch.setattr(
        publish_newsletter, "make_substack_client", unexpected_global_client
    )
    token = base64.b64encode(
        json.dumps(
            {
                "publication_url": "https://publication.example.test",
                "substack_sid": "session",
            }
        ).encode()
    ).decode()

    _ = await publish_markdown_newsletter(
        markdown, token, "draft_only", writer_id=13775033
    )

    create = publication.calls[0][2]
    assert create is not None
    assert create["draft_bylines"] == [{"id": 13775033, "is_guest": False}]


@pytest.mark.anyio
async def test_publish_web_checks_prepublish_and_never_sends_email() -> None:
    publication = FakePublication()
    content = parse_newsletter_markdown("# Title\n\nBody")

    result = await NewsletterPublisher(publication, FakeIdentity()).publish(
        content, "publish_web"
    )

    assert result.post_id == 202
    assert [call[:2] for call in publication.calls] == [
        ("POST", "drafts"),
        ("PUT", "drafts/101"),
        ("GET", "drafts/101/prepublish"),
        ("POST", "drafts/101/publish"),
    ]
    assert publication.calls[-1][2] == {"send": False, "saved_segment_id": None}


@pytest.mark.anyio
async def test_email_mode_requires_exact_confirmation() -> None:
    content = parse_newsletter_markdown("# Title\n\nBody")

    with pytest.raises(ValueError, match="SEND_EMAIL_TO_ALL_SUBSCRIBERS"):
        await NewsletterPublisher(FakePublication(), FakeIdentity()).publish(
            content, "publish_and_email"
        )


@pytest.mark.anyio
async def test_existing_draft_is_reused_instead_of_created() -> None:
    publication = FakePublication()
    content = parse_newsletter_markdown("# Title\n\nBody")

    result = await NewsletterPublisher(publication, FakeIdentity()).publish(
        content,
        "draft_only",
        existing_draft_id=101,
        existing_draft_updated_at="2026-08-14T20:00:00Z",
    )

    assert result.draft_id == 101
    assert [call[:2] for call in publication.calls] == [("PUT", "drafts/101")]
    update = publication.calls[0][2]
    assert update is not None
    assert update["last_updated_at"] == "2026-08-14T20:00:00Z"


@pytest.mark.anyio
async def test_image_is_uploaded_once_and_prepended_to_draft_body(
    tmp_path: Path,
) -> None:
    publication = FakePublication()
    image_path = tmp_path / "generated.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    events: list[str] = []
    uploaded: list[UploadedImage] = []

    def persist_uploaded(image: UploadedImage) -> None:
        events.append("uploaded")
        uploaded.append(image)

    result = await NewsletterPublisher(
        publication, FakeIdentity(), "https://publication.example.test"
    ).publish(
        parse_newsletter_markdown("# Title\n\nBody"),
        "draft_only",
        image_path=image_path,
        on_image_upload_started=lambda: events.append("started"),
        on_image_uploaded=persist_uploaded,
    )

    assert result.draft_id == 101
    assert [call[:2] for call in publication.calls] == [
        ("POST", "drafts"),
        ("POST", "image"),
        ("PUT", "drafts/101"),
    ]
    assert events == ["started", "uploaded"]
    assert uploaded == [
        UploadedImage(
            id=303,
            url="https://cdn.example.test/generated.png",
            content_type="image/png",
            bytes=13,
            width=440,
            height=78,
        )
    ]
    assert publication.calls[1][2] == {
        "image": "data:image/png;base64,iVBORw0KGgppbWFnZQ==",
        "postId": 101,
    }
    update = publication.calls[2][2]
    assert update is not None
    body = json.loads(update["draft_body"])
    assert body["content"][1]["type"] == "paragraph"
    assert body["content"][0] == {
        "type": "captionedImage",
        "content": [
            {
                "type": "image2",
                "attrs": {
                    "src": "https://cdn.example.test/generated.png",
                    "srcNoWatermark": None,
                    "fullscreen": None,
                    "imageSize": None,
                    "height": 78,
                    "width": 440,
                    "resizeWidth": None,
                    "bytes": 13,
                    "alt": None,
                    "title": None,
                    "type": "image/png",
                    "href": None,
                    "belowTheFold": False,
                    "topImage": False,
                    "internalRedirect": (
                        "https://publication.example.test/i/101?img="
                        "https%3A%2F%2Fcdn.example.test%2Fgenerated.png"
                    ),
                    "isProcessing": False,
                    "align": None,
                    "offset": False,
                },
            }
        ],
    }
    assert update["cover_image"] is None


@pytest.mark.anyio
async def test_existing_uploaded_image_avoids_reupload(tmp_path: Path) -> None:
    publication = FakePublication()
    image_path = tmp_path / "generated.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    existing = UploadedImage(
        303,
        "https://cdn.example.test/generated.png",
        "image/png",
        13,
        440,
        78,
    )

    await NewsletterPublisher(
        publication, FakeIdentity(), "https://publication.example.test"
    ).publish(
        parse_newsletter_markdown("# Title\n\nBody"),
        "draft_only",
        existing_draft_id=101,
        existing_draft_updated_at="2026-08-14T20:00:00Z",
        image_path=image_path,
        existing_uploaded_image=existing,
    )

    assert [call[:2] for call in publication.calls] == [("PUT", "drafts/101")]


@pytest.mark.anyio
async def test_lost_image_upload_response_is_ambiguous(tmp_path: Path) -> None:
    publication = FakePublication(fail_image=True)
    image_path = tmp_path / "generated.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")

    with pytest.raises(NewsletterOutcomeUnknownError, match="Image upload"):
        await NewsletterPublisher(
            publication, FakeIdentity(), "https://publication.example.test"
        ).publish(
            parse_newsletter_markdown("# Title\n\nBody"),
            "draft_only",
            image_path=image_path,
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", True),
        ("url", "http://cdn.example.test/generated.png"),
        ("contentType", "image/gif"),
        ("bytes", True),
        ("imageWidth", True),
        ("imageHeight", True),
    ],
)
async def test_invalid_image_upload_response_is_ambiguous(
    tmp_path: Path, field: str, value: object
) -> None:
    response: dict[str, object] = {
        "id": 303,
        "url": "https://cdn.example.test/generated.png",
        "contentType": "image/png",
        "bytes": 13,
        "imageWidth": 440,
        "imageHeight": 78,
    }
    response[field] = value
    publication = FakePublication(image_response=response)
    image_path = tmp_path / "generated.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")

    with pytest.raises(NewsletterOutcomeUnknownError, match="Image upload"):
        await NewsletterPublisher(
            publication, FakeIdentity(), "https://publication.example.test"
        ).publish(
            parse_newsletter_markdown("# Title\n\nBody"),
            "draft_only",
            image_path=image_path,
        )
    assert [call[:2] for call in publication.calls] == [
        ("POST", "drafts"),
        ("POST", "image"),
    ]
