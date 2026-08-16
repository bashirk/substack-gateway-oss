from __future__ import annotations

import asyncio
import base64

import httpx
import pytest
import respx

from substack_gateway.generation import (
    AzureOpenAIConfig,
    AzureOpenAIContentGenerator,
    AzureOpenAIImageConfig,
    AzureOpenAIImageGenerator,
    image_prompt,
    validate_image,
)

_PNG = b"\x89PNG\r\n\x1a\nimage-data"


def content_config() -> AzureOpenAIConfig:
    return AzureOpenAIConfig(
        endpoint="https://example.openai.azure.com",
        api_key="secret",
        api_version="2024-10-21",
        deployment="text model",
    )


def config() -> AzureOpenAIImageConfig:
    return AzureOpenAIImageConfig(
        endpoint="https://example.openai.azure.com",
        api_key="secret",
        api_version="2025-04-01-preview",
        deployment="image model",
    )


@respx.mock
def test_content_generator_omits_temperature() -> None:
    route = respx.post(
        "https://example.openai.azure.com/openai/deployments/text%20model/"
        "chat/completions?api-version=2024-10-21"
    ).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "Generated text"}}]}
        )
    )

    content = asyncio.run(
        AzureOpenAIContentGenerator(content_config()).generate(
            "note", "reliable systems", "Direct and practical."
        )
    )

    assert route.called
    assert "temperature" not in route.calls[0].request.content.decode()
    assert content == "Generated text"


@respx.mock
def test_content_generator_includes_azure_error_detail() -> None:
    respx.post(
        "https://example.openai.azure.com/openai/deployments/text%20model/"
        "chat/completions?api-version=2024-10-21"
    ).mock(
        return_value=httpx.Response(
            400,
            json={"error": {"message": "Unsupported parameter: temperature"}},
        )
    )

    with pytest.raises(RuntimeError, match="Unsupported parameter: temperature"):
        asyncio.run(
            AzureOpenAIContentGenerator(content_config()).generate(
                "note", "reliable systems", "Direct and practical."
            )
        )


@respx.mock
def test_x_content_generator_uses_platform_specific_prompt() -> None:
    route = respx.post(
        "https://example.openai.azure.com/openai/deployments/text%20model/"
        "chat/completions?api-version=2024-10-21"
    ).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "Concise X post"}}]}
        )
    )

    content = asyncio.run(
        AzureOpenAIContentGenerator(content_config()).generate(
            "x_post", "reliable systems", "Direct and practical."
        )
    )

    request = route.calls[0].request.content.decode()
    assert "expert X writer" in request
    assert "under 260 Unicode characters" in request
    assert content == "Concise X post"


def test_image_prompt_is_deterministic_and_excludes_text() -> None:
    first = image_prompt("newsletter", "reliable systems", "Direct and practical.")

    assert first == image_prompt(
        "newsletter", "reliable systems", "Direct and practical."
    )
    assert "reliable systems" in first
    assert "Do not include words" in first
    assert "an X post" in image_prompt("x_post", "reliable systems", "Direct")


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
