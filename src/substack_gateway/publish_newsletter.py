from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, final
from urllib.parse import quote, urlparse

from gateway_core.auth import (
    decode_bearer_credentials,
    make_publication_client,
    make_substack_client,
)
from substack_gateway.generation import validate_image

NewsletterMode = Literal[
    "artifact_only", "draft_only", "publish_web", "publish_and_email"
]

_EMAIL_CONFIRMATION = "SEND_EMAIL_TO_ALL_SUBSCRIBERS"
_HEADING = re.compile(r"^(#{2,6})\s+(.+)$")
_UNORDERED = re.compile(r"^[-*]\s+(.+)$")
_ORDERED = re.compile(r"^\d+\.\s+(.+)$")
_INLINE = re.compile(r"\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`|\[([^\]]+)\]\(([^)]+)\)")


class PublicationAPI(Protocol):
    async def get(self, path: str, **kwargs: Any) -> Any: ...

    async def post_once(self, path: str, **kwargs: Any) -> Any: ...

    async def put_once(self, path: str, **kwargs: Any) -> Any: ...


class IdentityAPI(Protocol):
    async def get_own_id(self) -> int: ...


@dataclass(frozen=True)
class NewsletterContent:
    title: str
    subtitle: str
    body_markdown: str


@dataclass(frozen=True)
class NewsletterResult:
    status: Literal["drafted", "published"]
    draft_id: int
    post_id: int | None = None


@dataclass(frozen=True)
class UploadedImage:
    id: int
    url: str
    content_type: str
    bytes: int
    width: int
    height: int


class NewsletterOutcomeUnknownError(RuntimeError):
    """A write may have succeeded, so retrying could duplicate publication."""


@final
class NewsletterPublisher:
    def __init__(
        self,
        publication: PublicationAPI,
        identity: IdentityAPI | None,
        publication_url: str | None = None,
    ) -> None:
        self._publication = publication
        self._identity = identity
        self._publication_url = publication_url

    async def publish(
        self,
        content: NewsletterContent,
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
        if mode == "artifact_only":
            raise ValueError("artifact_only mode does not create a Substack draft")
        if mode == "publish_and_email" and email_confirmation != _EMAIL_CONFIRMATION:
            raise ValueError(
                "Email delivery requires SUBSTACK_AUTOPILOT_EMAIL_CONFIRMATION="
                + _EMAIL_CONFIRMATION
            )

        if writer_id is None:
            if self._identity is None:
                raise ValueError("Newsletter publishing requires a writer ID")
            writer_id = await self._identity.get_own_id()
        bylines: list[dict[str, object]] = [{"id": writer_id, "is_guest": False}]
        if existing_draft_id is None:
            try:
                response = await self._publication.post_once(
                    "drafts", json=_create_payload(bylines)
                )
                created = response.json()
                draft_id = _required_int(created, "id")
                updated_at = _required_str(created, "draft_updated_at")
                if on_draft_created is not None:
                    on_draft_created(draft_id, updated_at)
            except Exception as exc:
                raise NewsletterOutcomeUnknownError(
                    "Draft creation may have succeeded; inspect Substack before retrying"
                ) from exc
        else:
            if not existing_draft_updated_at:
                raise ValueError(
                    "Reusing a draft requires its persisted draft_updated_at value"
                )
            draft_id = existing_draft_id
            updated_at = existing_draft_updated_at

        uploaded_image = existing_uploaded_image
        if image_path is not None and uploaded_image is None:
            generated = validate_image(image_path.read_bytes(), image_maximum_bytes)
            data_url = (
                f"data:{generated.media_type};base64,"
                f"{base64.b64encode(generated.content).decode('ascii')}"
            )
            if on_image_upload_started is not None:
                on_image_upload_started()
            try:
                response = await self._publication.post_once(
                    "image", json={"image": data_url, "postId": draft_id}
                )
                uploaded_image = _parse_uploaded_image(response.json())
                if on_image_uploaded is not None:
                    on_image_uploaded(uploaded_image)
            except Exception as exc:
                raise NewsletterOutcomeUnknownError(
                    f"Image upload for draft {draft_id} has an unknown outcome; "
                    "inspect Substack before retrying"
                ) from exc

        send_email = mode == "publish_and_email"
        try:
            response = await self._publication.put_once(
                f"drafts/{draft_id}",
                json=_update_payload(
                    content,
                    bylines,
                    updated_at,
                    send_email,
                    uploaded_image=uploaded_image,
                    publication_url=self._publication_url,
                    draft_id=draft_id,
                ),
            )
            updated_at = _required_str(response.json(), "draft_updated_at")
            if on_draft_created is not None:
                on_draft_created(draft_id, updated_at)
        except Exception as exc:
            raise NewsletterOutcomeUnknownError(
                f"Draft {draft_id} update has an unknown outcome"
            ) from exc
        if mode == "draft_only":
            return NewsletterResult("drafted", draft_id)

        await self._publication.get(f"drafts/{draft_id}/prepublish")
        try:
            response = await self._publication.post_once(
                f"drafts/{draft_id}/publish",
                json={"send": send_email, "saved_segment_id": None},
            )
            published = response.json()
            return NewsletterResult(
                "published", draft_id, _optional_int(published, "id")
            )
        except Exception as exc:
            raise NewsletterOutcomeUnknownError(
                f"Draft {draft_id} publish has an unknown outcome"
            ) from exc


def parse_newsletter_markdown(markdown: str) -> NewsletterContent:
    lines = markdown.replace("\\n", "\n").splitlines()
    first = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first is None or not lines[first].startswith("# "):
        raise ValueError("Newsletter Markdown must start with an H1 title")
    title = lines[first][2:].strip()
    if not title:
        raise ValueError("Newsletter title cannot be empty")

    index = first + 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    subtitle = ""
    if index < len(lines):
        candidate = lines[index].strip()
        if (
            len(candidate) >= 2
            and candidate.startswith("*")
            and candidate.endswith("*")
        ):
            subtitle = candidate[1:-1].strip()
            index += 1
    body = "\n".join(lines[index:]).strip()
    if not body:
        raise ValueError("Newsletter body cannot be empty")
    return NewsletterContent(title, subtitle, body)


def markdown_to_draft_doc(markdown: str) -> dict[str, Any]:
    if not markdown.strip():
        raise ValueError("Newsletter body cannot be empty")
    content: list[dict[str, Any]] = []
    paragraphs: list[str] = []

    def flush_paragraph() -> None:
        if paragraphs:
            text = "\n".join(paragraphs).strip()
            if text:
                content.append(_text_block("paragraph", text))
            paragraphs.clear()

    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
        elif match := _HEADING.match(stripped):
            flush_paragraph()
            content.append(
                _text_block("heading", match.group(2), level=len(match.group(1)))
            )
        elif match := _UNORDERED.match(stripped):
            flush_paragraph()
            content.append(_list("bulletList", match.group(1)))
        elif match := _ORDERED.match(stripped):
            flush_paragraph()
            content.append(_list("orderedList", match.group(1)))
        else:
            paragraphs.append(line)
    flush_paragraph()
    if not content:
        raise ValueError("Newsletter body cannot be empty")
    return {"type": "doc", "content": content}


async def publish_markdown_newsletter(
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
    content = parse_newsletter_markdown(path.read_text(encoding="utf-8"))
    credentials = decode_bearer_credentials(token)
    assert credentials.publication_url is not None
    async with make_publication_client(
        credentials, credentials.publication_url
    ) as publication:
        async def publish_with(identity: IdentityAPI | None) -> NewsletterResult:
            return await NewsletterPublisher(
                publication, identity, credentials.publication_url
            ).publish(
                content,
                mode,
                existing_draft_id=existing_draft_id,
                existing_draft_updated_at=existing_draft_updated_at,
                on_draft_created=on_draft_created,
                image_path=image_path,
                existing_uploaded_image=existing_uploaded_image,
                on_image_upload_started=on_image_upload_started,
                on_image_uploaded=on_image_uploaded,
                image_maximum_bytes=image_maximum_bytes,
                email_confirmation=email_confirmation,
                writer_id=writer_id,
            )

        if writer_id is not None:
            return await publish_with(None)
        async with make_substack_client(credentials) as identity:
            return await publish_with(identity)


def _create_payload(bylines: list[dict[str, object]]) -> dict[str, Any]:
    empty_doc = {
        "type": "doc",
        "content": [{"type": "paragraph", "attrs": {"textAlign": None}}],
    }
    return {
        "draft_title": "",
        "draft_subtitle": "",
        "draft_podcast_url": None,
        "draft_podcast_duration": None,
        "draft_body": json.dumps(empty_doc, separators=(",", ":")),
        "section_chosen": False,
        "draft_section_id": None,
        "translations": [],
        "draft_bylines": bylines,
        "audience": "everyone",
        "meter_type": "none",
        "account_based_meter_type": "metered",
        "type": "newsletter",
    }


def _update_payload(
    content: NewsletterContent,
    bylines: list[dict[str, object]],
    updated_at: str,
    send_email: bool,
    *,
    uploaded_image: UploadedImage | None = None,
    publication_url: str | None = None,
    draft_id: int,
) -> dict[str, Any]:
    document = markdown_to_draft_doc(content.body_markdown)
    if uploaded_image is not None:
        if publication_url is None:
            raise ValueError("Publication URL is required for newsletter images")
        document["content"].insert(
            0, _image_node(uploaded_image, publication_url, draft_id)
        )
    return {
        "draft_title": content.title,
        "draft_subtitle": content.subtitle,
        "draft_podcast_url": None,
        "draft_podcast_duration": None,
        "draft_body": json.dumps(document, separators=(",", ":")),
        "section_chosen": False,
        "draft_section_id": None,
        "translations": [],
        "draft_bylines": bylines,
        "last_updated_at": updated_at,
        "audience": "everyone",
        "audience_before_archived": None,
        "syndicate_voiceover_to_rss": False,
        "syndicate_to_section_id": None,
        "should_syndicate_to_other_feed": None,
        "write_comment_permissions": "everyone",
        "default_comment_sort": None,
        "should_send_email": send_email,
        "meter_type": "none",
        "account_based_meter_type": "metered",
        "cover_image": None,
        "search_engine_title": None,
        "search_engine_description": None,
    }


def _image_node(
    image: UploadedImage, publication_url: str, draft_id: int
) -> dict[str, Any]:
    internal_redirect = (
        f"{publication_url.rstrip('/')}/i/{draft_id}?img={quote(image.url, safe='')}"
    )
    return {
        "type": "captionedImage",
        "content": [
            {
                "type": "image2",
                "attrs": {
                    "src": image.url,
                    "srcNoWatermark": None,
                    "fullscreen": None,
                    "imageSize": None,
                    "height": image.height,
                    "width": image.width,
                    "resizeWidth": None,
                    "bytes": image.bytes,
                    "alt": None,
                    "title": None,
                    "type": image.content_type,
                    "href": None,
                    "belowTheFold": False,
                    "topImage": False,
                    "internalRedirect": internal_redirect,
                    "isProcessing": False,
                    "align": None,
                    "offset": False,
                },
            }
        ],
    }


def _text_block(node_type: str, text: str, **attrs: object) -> dict[str, Any]:
    node: dict[str, Any] = {
        "type": node_type,
        "attrs": {"textAlign": None, **attrs},
        "content": _inline_nodes(text),
    }
    return node


def _list(node_type: str, text: str) -> dict[str, Any]:
    attrs = {"start": 1} if node_type == "orderedList" else {}
    return {
        "type": node_type,
        "attrs": attrs,
        "content": [
            {
                "type": "listItem",
                "content": [_text_block("paragraph", text)],
            }
        ],
    }


def _inline_nodes(text: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    position = 0
    for match in _INLINE.finditer(text):
        if match.start() > position:
            nodes.append({"type": "text", "text": text[position : match.start()]})
        if match.group(1) is not None:
            nodes.append(_marked_text(match.group(1), "bold"))
        elif match.group(2) is not None:
            nodes.append(_marked_text(match.group(2), "italic"))
        elif match.group(3) is not None:
            nodes.append(_marked_text(match.group(3), "code"))
        else:
            nodes.append(
                {
                    "type": "text",
                    "text": match.group(4),
                    "marks": [
                        {
                            "type": "link",
                            "attrs": {
                                "href": match.group(5),
                                "target": "_blank",
                                "rel": "noopener noreferrer nofollow",
                                "class": None,
                            },
                        }
                    ],
                }
            )
        position = match.end()
    if position < len(text):
        nodes.append({"type": "text", "text": text[position:]})
    return nodes


def _marked_text(text: str, mark: str) -> dict[str, Any]:
    return {"type": "text", "text": text, "marks": [{"type": mark}]}


def _parse_uploaded_image(payload: Any) -> UploadedImage:
    content_type = _required_str(payload, "contentType")
    if content_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise ValueError("Substack returned an unsupported image contentType")
    url = _required_str(payload, "url")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Substack returned a non-HTTPS image URL")
    return UploadedImage(
        id=_required_int(payload, "id"),
        url=url,
        content_type=content_type,
        bytes=_required_int(payload, "bytes"),
        width=_required_int(payload, "imageWidth"),
        height=_required_int(payload, "imageHeight"),
    )


def _required_int(payload: Any, field: str) -> int:
    value = payload.get(field) if isinstance(payload, dict) else None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Substack response is missing integer {field}")
    return value


def _optional_int(payload: Any, field: str) -> int | None:
    value = payload.get(field) if isinstance(payload, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _required_str(payload: Any, field: str) -> str:
    value = payload.get(field) if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value:
        raise ValueError(f"Substack response is missing string {field}")
    return value
