from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from gateway_core.auth import (
    decode_bearer_credentials,
    make_publication_client,
    make_substack_client,
)
from gateway_notes.service import NotesService

_TOKEN_ENV = "SUBSTACK_GATEWAY_TOKEN"


def resolve_markdown_path(template: str, publish_date: date | None = None) -> Path:
    """Resolve a Markdown path, replacing ``{date}`` with an ISO date."""
    resolved_date = publish_date or date.today()
    return Path(template.replace("{date}", resolved_date.isoformat())).expanduser()


async def publish_markdown_note(
    path: Path, token: str, attachment: str | None = None
) -> int:
    """Read a Markdown file and publish it as a Substack note."""
    content = path.read_text(encoding="utf-8")
    if not content.strip():
        raise ValueError(f"Markdown file is empty: {path}")

    credentials = decode_bearer_credentials(token)
    assert credentials.publication_url is not None
    async with make_publication_client(
        credentials, credentials.publication_url
    ) as publication_client:
        async with make_substack_client(credentials) as substack_client:
            note = await NotesService(publication_client, substack_client).create_note(
                content, attachment=attachment
            )
    return note.id


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish a Markdown file as a Substack note."
    )
    parser.add_argument(
        "markdown_file",
        help="Markdown path; {date} is replaced with today's YYYY-MM-DD date",
    )
    parser.add_argument(
        "--attachment",
        help="Optional URL to attach to the note",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    token = os.environ.get(_TOKEN_ENV)
    if not token:
        parser.error(f"{_TOKEN_ENV} must contain base64-encoded Substack credentials")

    path = resolve_markdown_path(args.markdown_file)
    try:
        note_id = asyncio.run(publish_markdown_note(path, token, args.attachment))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    print(json.dumps({"id": note_id, "source": str(path)}))


if __name__ == "__main__":
    main()
