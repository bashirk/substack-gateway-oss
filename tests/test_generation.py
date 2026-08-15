from __future__ import annotations

import asyncio
import base64

import httpx
import pytest
import respx

from substack_gateway.generation import (
    AzureOpenAIImageConfig,
    AzureOpenAIImageGenerator,
    image_prompt,
    validate_image,
)

_PNG = b"\x89PNG\r\n\x1a\nimage-data"


def config() -> AzureOpenAIImageConfig:
    return AzureOpenAIImageConfig(
        endpoint="https://example.openai.azure.com",
        api_key="secret",
        api_version="2025-04-01-preview",
        deployment="image model",
    )


def test_image_prompt_is_deterministic_and_excludes_text() -> None:
    first = image_prompt("newsletter", "reliable systems", "Direct and practical.")

    assert first == image_prompt(
        "newsletter", "reliable systems", "Direct and practical."
    )
    assert "reliable systems" in first
    assert "Do not include words" in first


@respx.mock
def test_image_generator_decodes_base64_response() -> None:
    route = respx.post(
        "https://example.openai.azure.com/openai/deployments/image%20model/"
        "images/generations?api-version=2025-04-01-preview"
    ).mock(
        return_value=httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(_PNG).decode()}]}
        )
    )

    image = asyncio.run(
        AzureOpenAIImageGenerator(config()).generate("note", "AI", "Practical")
    )

    assert route.called
    assert image.content == _PNG
    assert image.media_type == "image/png"
    assert image.extension == ".png"


@respx.mock
def test_image_generator_downloads_temporary_https_url() -> None:
    respx.post(
        "https://example.openai.azure.com/openai/deployments/image%20model/"
        "images/generations?api-version=2025-04-01-preview"
    ).mock(
        return_value=httpx.Response(
            200, json={"data": [{"url": "https://images.example/result.png"}]}
        )
    )
    download = respx.get("https://images.example/result.png").mock(
        return_value=httpx.Response(200, content=_PNG)
    )

    image = asyncio.run(
        AzureOpenAIImageGenerator(config()).generate("newsletter", "AI", "Practical")
    )

    assert download.called
    assert image.content == _PNG


def test_validate_image_rejects_unknown_or_oversized_data() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        validate_image(b"not an image", 100)
    with pytest.raises(ValueError, match="exceeds"):
        validate_image(_PNG, 5)
