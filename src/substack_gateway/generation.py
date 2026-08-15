from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any, Protocol, final
from urllib.parse import quote

import httpx


class ContentGenerator(Protocol):
    async def generate(self, kind: str, topic: str, context: str) -> str: ...


class ImageGenerator(Protocol):
    async def generate(self, kind: str, topic: str, context: str) -> GeneratedImage: ...


@dataclass(frozen=True)
class GeneratedImage:
    content: bytes
    media_type: str
    extension: str


@dataclass(frozen=True)
class AzureOpenAIConfig:
    endpoint: str
    api_key: str
    api_version: str
    deployment: str


@dataclass(frozen=True)
class AzureOpenAIImageConfig:
    endpoint: str
    api_key: str
    api_version: str
    deployment: str
    size: str = "1536x1024"
    quality: str = "medium"
    maximum_bytes: int = 20 * 1024 * 1024


@final
class AzureOpenAIContentGenerator:
    def __init__(self, config: AzureOpenAIConfig) -> None:
        self._config: AzureOpenAIConfig = config

    async def generate(self, kind: str, topic: str, context: str) -> str:
        prompt = _prompt(kind, topic, context)
        deployment = quote(self._config.deployment, safe="")
        url = (
            f"{self._config.endpoint.rstrip('/')}/openai/deployments/{deployment}"
            f"/chat/completions?api-version={quote(self._config.api_version, safe='')}"
        )
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                url,
                headers={"api-key": self._config.api_key},
                json={
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are an expert Substack writer. Return only polished "
                                + "Markdown with no preamble or fenced code block. Never invent "
                                + "facts, quotes, sources, or personal experiences."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.8,
                },
            )
            _ = response.raise_for_status()
            payload: Any = response.json()

        try:
            raw_content = payload["choices"][0]["message"]["content"]
            if not isinstance(raw_content, str):
                raise TypeError
            content = raw_content.strip()
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ValueError("Azure OpenAI returned no text content") from exc
        if not content:
            raise ValueError("Azure OpenAI returned empty text content")
        return content


@final
class AzureOpenAIImageGenerator:
    def __init__(self, config: AzureOpenAIImageConfig) -> None:
        self._config = config

    async def generate(self, kind: str, topic: str, context: str) -> GeneratedImage:
        deployment = quote(self._config.deployment, safe="")
        url = (
            f"{self._config.endpoint.rstrip('/')}/openai/deployments/{deployment}"
            f"/images/generations?api-version={quote(self._config.api_version, safe='')}"
        )
        async with httpx.AsyncClient(timeout=180) as client:
            request = {
                "prompt": image_prompt(kind, topic, context),
                "n": 1,
                "size": self._config.size,
            }
            if self._config.quality:
                request["quality"] = self._config.quality
            response = await client.post(
                url,
                headers={"api-key": self._config.api_key},
                json=request,
            )
            _ = response.raise_for_status()
            payload: Any = response.json()
            item = _first_image(payload)
            encoded = item.get("b64_json")
            if isinstance(encoded, str) and encoded:
                try:
                    content = base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ValueError(
                        "Azure OpenAI returned invalid base64 image data"
                    ) from exc
            else:
                image_url = item.get("url")
                if not isinstance(image_url, str) or not image_url.startswith(
                    "https://"
                ):
                    raise ValueError("Azure OpenAI returned no image data")
                download = await client.get(image_url)
                _ = download.raise_for_status()
                content = download.content
        return validate_image(content, self._config.maximum_bytes)


def image_prompt(kind: str, topic: str, context: str) -> str:
    if kind not in {"note", "newsletter"}:
        raise ValueError(f"Unsupported content kind: {kind}")
    format_hint = (
        "editorial hero illustration in a wide landscape composition"
        if kind == "newsletter"
        else "editorial social illustration with a clear central subject"
    )
    return (
        f"Create a polished {format_hint} for a Substack {kind} about {topic}. "
        "Use an original, modern visual metaphor, strong composition, restrained colors, "
        "and generous negative space. Do not include words, letters, logos, watermarks, "
        "screenshots, recognizable public figures, or copyrighted characters. "
        f"Publication direction: {context or 'practical, thoughtful, and credible'}."
    )


def validate_image(content: bytes, maximum_bytes: int) -> GeneratedImage:
    if not content:
        raise ValueError("Image generator returned empty content")
    if len(content) > maximum_bytes:
        raise ValueError(f"Generated image exceeds {maximum_bytes} bytes")
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return GeneratedImage(content, "image/png", ".png")
    if content.startswith(b"\xff\xd8\xff"):
        return GeneratedImage(content, "image/jpeg", ".jpg")
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return GeneratedImage(content, "image/webp", ".webp")
    raise ValueError("Image generator returned an unsupported image format")


def _first_image(payload: Any) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise ValueError("Azure OpenAI returned no image data")
    return data[0]


def _prompt(kind: str, topic: str, context: str) -> str:
    shared = (
        f"Topic: {topic}\nPublication context and voice: {context or 'None provided.'}"
    )
    if kind == "note":
        return (
            "Write one engaging Substack Note in Markdown. Keep it between 60 and 180 "
            + "words. Lead with a strong standalone observation, provide one useful insight, "
            + "and end with a natural question or memorable conclusion. Do not use a title, "
            + "generic motivational filler, or more than one hashtag.\n\n"
            + shared
        )
    if kind == "newsletter":
        return (
            "Write a substantial daily Substack newsletter in Markdown, roughly 700 to "
            + "1,200 words. Start with an H1 title, then a one-sentence subtitle in italics. "
            + "Use a compelling opening, clear H2 sections, concrete useful takeaways, and a "
            + "concise closing. Do not claim current events or statistics unless they are "
            + "included in the supplied context.\n\n"
            + shared
        )
    raise ValueError(f"Unsupported content kind: {kind}")
